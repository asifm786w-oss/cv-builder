import os
import stripe
import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "").strip()
PRICE_PRO = os.getenv("STRIPE_PRICE_PRO", "").strip()

CREDIT_STACKING = os.getenv("CREDIT_STACKING", "false").lower() == "true"

ALLOWED_PLANS = {"monthly", "pro"}


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    # Railway sometimes needs sslmode=require; if yours already works without it, keep it simple.
    return psycopg2.connect(DATABASE_URL)


def ensure_stripe_events_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.commit()


def mark_event_processed(event_id: str) -> bool:
    """True if inserted (new), False if duplicate retry."""
    ensure_stripe_events_table()
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO stripe_events (event_id) VALUES (%s)", (event_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False


def credits_for_plan(plan: str) -> tuple[int, int]:
    plan = (plan or "").strip().lower()
    if plan == "monthly":
        return (20, 30)
    if plan == "pro":
        return (50, 90)
    return (0, 0)


def plan_from_price(price_id: str) -> str | None:
    if price_id == PRICE_MONTHLY:
        return "monthly"
    if price_id == PRICE_PRO:
        return "pro"
    return None


def find_user_id_by_email(email: str) -> int | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
        row = cur.fetchone()
        return int(row[0]) if row else None


def set_user_plan(user_id: int, plan: str) -> None:
    """
    Keep users.plan in sync.
    Only allow known values: free/monthly/pro.
    """
    plan = (plan or "free").strip().lower()
    if plan not in {"free", "monthly", "pro"}:
        plan = "free"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET plan=%s WHERE id=%s", (plan, user_id))
        conn.commit()


def upsert_subscription(
    user_id: int,
    customer_id: str | None,
    subscription_id: str | None,
    plan: str,
    status: str,
    period_end_unix: int | None,
    cancel_at_period_end: bool,
):
    if not subscription_id:
        return

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO subscriptions
              (user_id, stripe_customer_id, stripe_subscription_id, plan, status, current_period_end, cancel_at_period_end)
            VALUES
              (%s, %s, %s, %s, %s,
               CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s) END,
               %s)
            ON CONFLICT (stripe_subscription_id) DO UPDATE SET
              user_id = EXCLUDED.user_id,
              stripe_customer_id = EXCLUDED.stripe_customer_id,
              plan = EXCLUDED.plan,
              status = EXCLUDED.status,
              current_period_end = EXCLUDED.current_period_end,
              cancel_at_period_end = EXCLUDED.cancel_at_period_end,
              updated_at = now()
            """,
            (
                user_id,
                customer_id,
                subscription_id,
                plan,
                status,
                period_end_unix,
                period_end_unix,
                cancel_at_period_end,
            ),
        )
        conn.commit()


def insert_credit_grant(
    user_id: int,
    source: str,
    cv_amount: int,
    ai_amount: int,
    expires_at_unix: int | None,
) -> bool:
    """
    Inserts a grant into credit_grants.
    Requires UNIQUE(source) on credit_grants.
    Returns True if inserted, False if already existed.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
            VALUES (
              %s, %s, %s, %s,
              CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s) END
            )
            ON CONFLICT (source) DO NOTHING
            RETURNING id
            """,
            (user_id, source, cv_amount, ai_amount, expires_at_unix, expires_at_unix),
        )
        row = cur.fetchone()
        conn.commit()
        return bool(row)


def extract_price_id_from_invoice(invoice: dict) -> str:
    """
    Supports payload shape:
      lines.data[*].pricing.price_details.price
    """
    lines = (invoice.get("lines") or {}).get("data") or []
    for ln in lines:
        pricing = ln.get("pricing") or {}
        pd = pricing.get("price_details") or {}
        pid = (pd.get("price") or "").strip()
        if pid:
            return pid

        # fallback legacy
        p = ln.get("price") or {}
        pid2 = (p.get("id") or "").strip()
        if pid2:
            return pid2

    return ""


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    if not stripe.api_key:
        return jsonify({"error": "Missing STRIPE_SECRET_KEY"}), 500
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Missing STRIPE_WEBHOOK_SECRET"}), 500

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    event_id = event.get("id", "")
    if event_id and not mark_event_processed(event_id):
        return jsonify({"status": "duplicate_ignored"}), 200

    typ = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

# ------------------------------------------------------------
# A) CHECKOUT COMPLETE (NO CREDITS HERE — SYNC ONLY)
# ------------------------------------------------------------
if typ == "checkout.session.completed":
    session = obj
    metadata = session.get("metadata") or {}
    pack = (metadata.get("pack") or "").strip().lower()

    if pack not in ALLOWED_PLANS:
        return jsonify({"status": "ignored", "reason": "invalid_pack"}), 200

    email = (
        (metadata.get("app_user_email") or "").strip().lower()
        or ((session.get("customer_details") or {}).get("email") or "").strip().lower()
        or (session.get("customer_email") or "").strip().lower()
    )

    customer_id = (session.get("customer") or "").strip() or None
    subscription_id = (session.get("subscription") or "").strip() or None

    if not email:
        return jsonify({"status": "ignored", "reason": "missing_email"}), 200

    user_id = find_user_id_by_email(email)
    if not user_id:
        return jsonify({"status": "ignored", "reason": "no_matching_user"}), 200

    if subscription_id:
        sub = stripe.Subscription.retrieve(subscription_id)
        upsert_subscription(
            user_id=user_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            plan=pack,
            status=sub.get("status"),
            period_end_unix=sub.get("current_period_end"),
            cancel_at_period_end=bool(sub.get("cancel_at_period_end")),
        )

        if sub.get("status") in ("active", "trialing"):
            set_user_plan(user_id, pack)

    # 🚫 NO CREDIT GRANT HERE
    return jsonify({"status": "ok", "credits": "not_granted_here"}), 200


# ------------------------------------------------------------
# B) PAYMENTS / RENEWALS (ONLY PLACE CREDITS ARE GRANTED)
# ------------------------------------------------------------
if typ == "invoice.payment_succeeded":
    invoice = obj

    billing_reason = (invoice.get("billing_reason") or "").strip()
    if billing_reason not in ("subscription_create", "subscription_cycle"):
        return jsonify({
            "status": "ignored",
            "reason": "not_subscription_charge",
            "billing_reason": billing_reason
        }), 200

    invoice_id = (invoice.get("id") or "").strip()
    customer_id = (invoice.get("customer") or "").strip() or None
    subscription_id = (invoice.get("subscription") or "").strip() or None

    price_id = extract_price_id_from_invoice(invoice)
    plan = plan_from_price(price_id)

    if plan not in ALLOWED_PLANS:
        return jsonify({"status": "ignored", "reason": "unknown_price"}), 200

    email = (invoice.get("customer_email") or "").strip().lower()
    if not email and customer_id:
        cust = stripe.Customer.retrieve(customer_id)
        email = (cust.get("email") or "").strip().lower()

    if not email:
        return jsonify({"status": "ignored", "reason": "missing_email"}), 200

    user_id = find_user_id_by_email(email)
    if not user_id:
        return jsonify({"status": "ignored", "reason": "no_matching_user"}), 200

    status = "unknown"
    period_end = None
    cancel_at_period_end = False

    if subscription_id:
        sub = stripe.Subscription.retrieve(subscription_id)
        status = sub.get("status")
        period_end = sub.get("current_period_end")
        cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

        upsert_subscription(
            user_id=user_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            plan=plan,
            status=status,
            period_end_unix=period_end,
            cancel_at_period_end=cancel_at_period_end,
        )

    if status in ("active", "trialing"):
        set_user_plan(user_id, plan)

    cv_amt, ai_amt = credits_for_plan(plan)
    expires_at = None if CREDIT_STACKING else period_end

    inserted = insert_credit_grant(
        user_id=user_id,
        source=f"stripe_invoice:{invoice_id}",
        cv_amount=cv_amt,
        ai_amount=ai_amt,
        expires_at_unix=expires_at,
        stripe_invoice_id=invoice_id,  # 🔒 prevents duplicates forever
    )

    return jsonify({
        "status": "ok",
        "granted": inserted,
        "invoice": invoice_id
    }), 200


# ------------------------------------------------------------
# C) SUBSCRIPTION STATE CHANGES (NO CREDITS)
# ------------------------------------------------------------
if typ in ("customer.subscription.updated", "customer.subscription.deleted"):
    sub = obj

    subscription_id = (sub.get("id") or "").strip()
    customer_id = (sub.get("customer") or "").strip() or None
    status = (sub.get("status") or "unknown")
    period_end = sub.get("current_period_end")
    cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

    plan = None
    for it in ((sub.get("items") or {}).get("data") or []):
        pid = ((it.get("price") or {}).get("id") or "").strip()
        plan = plan_from_price(pid)
        if plan:
            break

    email = None
    if customer_id:
        cust = stripe.Customer.retrieve(customer_id)
        email = (cust.get("email") or "").strip().lower()

    if not email:
        return jsonify({"status": "ignored", "reason": "missing_email"}), 200

    user_id = find_user_id_by_email(email)
    if not user_id:
        return jsonify({"status": "ignored", "reason": "no_matching_user"}), 200

    upsert_subscription(
        user_id=user_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        plan=plan or "free",
        status=status,
        period_end_unix=period_end,
        cancel_at_period_end=cancel_at_period_end,
    )

    if status in ("active", "trialing") and plan:
        set_user_plan(user_id, plan)
    else:
        set_user_plan(user_id, "free")

    return jsonify({"status": "ok"}), 200
