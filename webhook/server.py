import os
import logging
from typing import Optional, Tuple

import stripe
import psycopg2
from flask import Flask, request, jsonify

# -----------------------------
# App + logging
# -----------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------------
# Env
# -----------------------------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "").strip()
PRICE_PRO = os.getenv("STRIPE_PRICE_PRO", "").strip()

ALLOWED_PLANS = {"monthly", "pro"}


# -----------------------------
# DB helpers
# -----------------------------
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
    Returns True if this is the first time we see event_id.
    Returns False if it was already processed (Stripe retry).
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


def plan_from_price(price_id: str) -> Optional[str]:
    if price_id == PRICE_MONTHLY:
        return "monthly"
    if price_id == PRICE_PRO:
        return "pro"
    return None


def credits_for_plan(plan: str) -> Tuple[int, int]:
    plan = (plan or "").strip().lower()
    if plan == "monthly":
        return (20, 30)  # cv, ai
    if plan == "pro":
        return (50, 90)  # cv, ai
    return (0, 0)


def set_user_plan(user_id: int, plan: str) -> None:
    plan = (plan or "free").strip().lower()
    if plan not in {"free", "monthly", "pro"}:
        plan = "free"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET plan=%s WHERE id=%s", (plan, user_id))
        conn.commit()


def upsert_subscription(
    user_id: int,
    customer_id: Optional[str],
    subscription_id: Optional[str],
    plan: str,
    status: str,
    period_end_unix: Optional[int],
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
              updated_at = clock_timestamp()
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


def find_user_id_by_stripe_subscription(subscription_id: str) -> Optional[int]:
    if not subscription_id:
        return None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_subscription_id=%s LIMIT 1",
            (subscription_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def find_user_id_by_stripe_customer(customer_id: str) -> Optional[int]:
    if not customer_id:
        return None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_customer_id=%s ORDER BY id DESC LIMIT 1",
            (customer_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def find_user_id_by_email(email: str) -> Optional[int]:
    email = (email or "").strip().lower()
    if not email:
        return None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
        row = cur.fetchone()
        return int(row[0]) if row else None


def extract_price_id_from_invoice(invoice: dict) -> str:
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


def insert_credit_grant(
    user_id: int,
    source: str,
    cv_amount: int,
    ai_amount: int,
    expires_at_unix: Optional[int],
    stripe_invoice_id: Optional[str],
) -> bool:
    """
    Inserts a grant into credit_grants.
    Idempotent on stripe_invoice_id (recommended to add a full UNIQUE constraint on stripe_invoice_id).
    If you didn't add a full UNIQUE constraint yet, we still safely avoid duplicates by checking first.
    """
    with get_conn() as conn:
        cur = conn.cursor()

        # If invoice id exists, avoid duplicates even if DB only has a partial unique index.
        if stripe_invoice_id:
            cur.execute(
                "SELECT 1 FROM credit_grants WHERE stripe_invoice_id=%s LIMIT 1",
                (stripe_invoice_id,),
            )
            if cur.fetchone():
                conn.commit()
                return False

        cur.execute(
            """
            INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at, stripe_invoice_id)
            VALUES (
              %s, %s, %s, %s,
              CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s) END,
              %s
            )
            RETURNING id
            """,
            (
                user_id,
                source,
                cv_amount,
                ai_amount,
                expires_at_unix,
                expires_at_unix,
                stripe_invoice_id,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return bool(row)


def resolve_user_id_from_checkout_session(session: dict) -> Optional[int]:
    md = session.get("metadata") or {}
    uid = (md.get("user_id") or "").strip()
    if uid.isdigit():
        return int(uid)

    cr = (session.get("client_reference_id") or "").strip()
    if cr.isdigit():
        return int(cr)

    return None


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True})


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
        logging.exception("Webhook signature verify failed")
        return jsonify({"error": str(e)}), 400

    event_id = event.get("id")
    typ = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    logging.info("stripe_webhook event=%s type=%s", event_id, typ)

    # Idempotency: ignore retries
    if event_id and not mark_event_processed(event_id):
        return jsonify({"status": "duplicate_ignored"}), 200

    try:
        # ------------------------------------------------------------
        # A) checkout.session.completed
        #   - Record/attach subscription row early (best effort)
        # ------------------------------------------------------------
        if typ == "checkout.session.completed":
            session = obj
            metadata = session.get("metadata") or {}
            pack = (metadata.get("pack") or "").strip().lower()

            # Not all sessions are subscriptions; ignore unknown packs
            if pack and pack not in ALLOWED_PLANS:
                return jsonify({"status": "ignored", "reason": "invalid_pack"}), 200

            user_id = resolve_user_id_from_checkout_session(session)

            # Fallback: email lookup (NOT preferred; used only if metadata missing)
            if not user_id:
                email = (
                    (metadata.get("app_user_email") or "").strip().lower()
                    or ((session.get("customer_details") or {}).get("email") or "").strip().lower()
                    or (session.get("customer_email") or "").strip().lower()
                )
                user_id = find_user_id_by_email(email) if email else None

            if not user_id:
                return jsonify({"status": "ignored", "reason": "no_user_mapping"}), 200

            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            # If subscription exists, upsert a minimal row to link customer/subscription -> user_id.
            if subscription_id:
                # best effort retrieve for status/period_end
                sub = stripe.Subscription.retrieve(subscription_id)
                plan = pack if pack in ALLOWED_PLANS else (metadata.get("pack") or "free")
                upsert_subscription(
                    user_id=user_id,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    plan=plan if plan in {"monthly", "pro"} else "free",
                    status=sub.get("status") or "unknown",
                    period_end_unix=sub.get("current_period_end"),
                    cancel_at_period_end=bool(sub.get("cancel_at_period_end")),
                )

            return jsonify({"status": "ok"}), 200

        # ------------------------------------------------------------
        # B) invoice.paid / invoice.payment_succeeded
        #   - Grant credits HERE
        # ------------------------------------------------------------
        if typ in ("invoice.paid", "invoice.payment_succeeded"):
            invoice = obj
            invoice_id = (invoice.get("id") or "").strip() or None
            customer_id = (invoice.get("customer") or "").strip() or None
            subscription_id = (invoice.get("subscription") or "").strip() or None

            # Resolve plan by price id from invoice lines
            price_id = extract_price_id_from_invoice(invoice)
            plan = plan_from_price(price_id)

            if plan not in ALLOWED_PLANS:
                return jsonify({"status": "ignored", "reason": "unknown_price"}), 200

            # Resolve user deterministically:
            # 1) subscription_id mapping (best)
            # 2) customer_id mapping
            # 3) email fallback (last resort)
            user_id = None
            if subscription_id:
                user_id = find_user_id_by_stripe_subscription(subscription_id)
            if not user_id and customer_id:
                user_id = find_user_id_by_stripe_customer(customer_id)

            if not user_id:
                email = (invoice.get("customer_email") or "").strip().lower()
                if not email and customer_id:
                    cust = stripe.Customer.retrieve(customer_id)
                    email = (cust.get("email") or "").strip().lower()
                user_id = find_user_id_by_email(email) if email else None

            if not user_id:
                return jsonify({"status": "ignored", "reason": "no_user_mapping"}), 200

            # Retrieve subscription for period end / status
            period_end = None
            sub_status = "unknown"
            cancel_at_period_end = False
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                period_end = sub.get("current_period_end")
                sub_status = sub.get("status") or "unknown"
                cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

            # Update subscription row + user plan
            upsert_subscription(
                user_id=user_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                plan=plan,
                status=sub_status,
                period_end_unix=period_end,
                cancel_at_period_end=cancel_at_period_end,
            )
            set_user_plan(user_id, plan)

            cv_amt, ai_amt = credits_for_plan(plan)

            inserted = insert_credit_grant(
                user_id=user_id,
                source=f"stripe_invoice:{invoice_id or 'unknown'}",
                cv_amount=cv_amt,
                ai_amount=ai_amt,
                expires_at_unix=period_end,
                stripe_invoice_id=invoice_id,
            )

            return jsonify({"status": "ok", "credits_granted": bool(inserted)}), 200

        # ------------------------------------------------------------
        # C) Subscription updates (no credits)
        # ------------------------------------------------------------
        if typ in ("customer.subscription.updated", "customer.subscription.deleted"):
            sub = obj
            subscription_id = (sub.get("id") or "").strip() or None
            customer_id = (sub.get("customer") or "").strip() or None
            status = (sub.get("status") or "unknown")
            period_end = sub.get("current_period_end")
            cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

            # Resolve plan from subscription items
            plan = None
            for it in ((sub.get("items") or {}).get("data") or []):
                pid = ((it.get("price") or {}).get("id") or "").strip()
                plan = plan_from_price(pid)
                if plan:
                    break

            # Resolve user via subscription/customer mapping first
            user_id = None
            if subscription_id:
                user_id = find_user_id_by_stripe_subscription(subscription_id)
            if not user_id and customer_id:
                user_id = find_user_id_by_stripe_customer(customer_id)

            if not user_id and customer_id:
                # final fallback
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").strip().lower()
                user_id = find_user_id_by_email(email) if email else None

            if not user_id:
                return jsonify({"status": "ignored", "reason": "no_user_mapping"}), 200

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

        return jsonify({"status": "ignored"}), 200

    except Exception as e:
        logging.exception("Webhook handler failed")
        # Returning 500 forces Stripe to retry (good during failures)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Railway: bind to PORT if set
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)