import os
import stripe
import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------- ENV ----------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "").strip()
PRICE_PRO = os.getenv("STRIPE_PRICE_PRO", "").strip()

# Optional: if true, credits don't expire at period end
CREDIT_STACKING = os.getenv("CREDIT_STACKING", "false").lower() == "true"


# ---------- DB ----------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
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
    """
    Returns True if inserted (new event).
    Returns False if already processed (Stripe retry).
    """
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


# ---------- HELPERS ----------
def plan_from_price(price_id: str) -> str | None:
    if price_id and price_id == PRICE_MONTHLY:
        return "monthly"
    if price_id and price_id == PRICE_PRO:
        return "pro"
    return None


def credits_for_plan(plan: str) -> tuple[int, int]:
    if plan == "monthly":
        return (20, 30)  # CV, AI
    if plan == "pro":
        return (50, 90)
    return (0, 0)


def find_user_id_by_email(email: str) -> int | None:
    if not email:
        return None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
        row = cur.fetchone()
        return int(row[0]) if row else None


def upsert_subscription(
    user_id: int,
    customer_id: str,
    subscription_id: str,
    plan: str,
    status: str,
    period_end_unix: int | None,
    cancel_at_period_end: bool,
):
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
              cancel_at_period_end = EXCLUDED.cancel_at_period_end
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
):
    """
    Inserts a grant into credit_grants.
    Requires UNIQUE(source) so we never double-grant.
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
            """,
            (user_id, source, cv_amount, ai_amount, expires_at_unix, expires_at_unix),
        )
        conn.commit()


def extract_price_id(invoice: dict, subscription_id: str | None) -> tuple[str, dict]:
    """
    Returns (price_id, debug_dict).
    Strategy:
      1) scan invoice lines for a price.id
      2) fall back to subscription items[0].price.id
    """
    debug = {"invoice_line_price_ids": [], "subscription_item_price_id": ""}

    # 1) scan invoice lines
    lines = (invoice.get("lines") or {}).get("data") or []
    for ln in lines:
        pid = ((ln.get("price") or {}).get("id") or "").strip()
        if pid:
            debug["invoice_line_price_ids"].append(pid)

    if debug["invoice_line_price_ids"]:
        return debug["invoice_line_price_ids"][0], debug

    # 2) fall back to subscription
    if subscription_id:
        sub = stripe.Subscription.retrieve(subscription_id)
        items = (sub.get("items") or {}).get("data") or []
        if items:
            pid = ((items[0].get("price") or {}).get("id") or "").strip()
            debug["subscription_item_price_id"] = pid
            return pid, debug

    return "", debug


# ---------- WEBHOOK ----------
@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    # Basic env sanity
    if not stripe.api_key:
        return jsonify({"error": "Missing STRIPE_SECRET_KEY"}), 500
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Missing STRIPE_WEBHOOK_SECRET"}), 500
    if not PRICE_MONTHLY or not PRICE_PRO:
        # We'll still run, but we return these in debug responses too.
        pass

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        return jsonify({"error": f"signature/parse error: {e}"}), 400

    event_id = event.get("id", "")
    if event_id and not mark_event_processed(event_id):
        return jsonify({"status": "duplicate_ignored"}), 200

    typ = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    # ✅ Handle ALL common "invoice paid" events
    if typ in ("invoice.paid", "invoice.payment_succeeded", "invoice.payment.paid"):
        invoice = obj
        invoice_id = (invoice.get("id") or "").strip()
        customer_id = (invoice.get("customer") or "").strip()
        subscription_id = (invoice.get("subscription") or "").strip() or None

        price_id, price_debug = extract_price_id(invoice, subscription_id)
        plan = plan_from_price(price_id)

        if not plan:
            return jsonify({
                "status": "ignored",
                "reason": "unknown_price",
                "event_type": typ,
                "price_id_seen": price_id,
                "price_debug": price_debug,
                "env_monthly": PRICE_MONTHLY,
                "env_pro": PRICE_PRO,
            }), 200

        # Get customer email
        cust = stripe.Customer.retrieve(customer_id)
        email = (cust.get("email") or "").strip().lower()
        if not email:
            return jsonify({"status": "ignored", "reason": "no_customer_email"}), 200

        user_id = find_user_id_by_email(email)
        if not user_id:
            return jsonify({"status": "ignored", "reason": "no_matching_user", "email": email}), 200

        # Get subscription truth
        sub = stripe.Subscription.retrieve(subscription_id) if subscription_id else None
        status = (sub.get("status") if sub else "unknown") or "unknown"
        period_end = sub.get("current_period_end") if sub else None
        cancel_at_period_end = bool(sub.get("cancel_at_period_end")) if sub else False

        # Upsert subscription row
        if subscription_id:
            upsert_subscription(
                user_id=user_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan=plan,
                status=status,
                period_end_unix=period_end,
                cancel_at_period_end=cancel_at_period_end,
            )

        # Grant credits (ledger)
        cv_amt, ai_amt = credits_for_plan(plan)
        expires_at = None if CREDIT_STACKING else period_end  # reset monthly by default

        insert_credit_grant(
            user_id=user_id,
            source=f"stripe_invoice:{invoice_id}",  # idempotent
            cv_amount=cv_amt,
            ai_amount=ai_amt,
            expires_at_unix=expires_at,
        )

        return jsonify({"status": "ok", "granted": True, "plan": plan, "email": email}), 200

    # Keep subscription status synced too
    if typ in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub = obj
        customer_id = (sub.get("customer") or "").strip()
        subscription_id = (sub.get("id") or "").strip()
        status = (sub.get("status") or "unknown").strip()
        period_end = sub.get("current_period_end")
        cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

        # Determine price id from subscription items
        items = (sub.get("items") or {}).get("data") or []
        price_id = ((items[0].get("price") or {}).get("id") or "").strip() if items else ""
        plan = plan_from_price(price_id) or "unknown"

        cust = stripe.Customer.retrieve(customer_id)
        email = (cust.get("email") or "").strip().lower()
        user_id = find_user_id_by_email(email) if email else None

        if user_id and subscription_id:
            upsert_subscription(
                user_id=user_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan=plan,
                status=status,
                period_end_unix=period_end,
                cancel_at_period_end=cancel_at_period_end,
            )

        return jsonify({"status": "ok"}), 200

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
