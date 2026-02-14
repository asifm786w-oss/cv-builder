import os
import stripe
import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    # Railway provides DATABASE_URL for Postgres
    return psycopg2.connect(DATABASE_URL)


def ensure_tables():
    # Creates stripe_events table if missing (dedupe protection)
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


def grant_pack_credits(email: str, pack: str) -> None:
    pack = (pack or "").strip().lower()

    PACKS = {
        "monthly": {"cv": 20, "ai": 30},
        "pro": {"cv": 50, "ai": 90},
    }

    add = PACKS.get(pack)
    if not add:
        raise ValueError(f"Unknown pack: {pack}")

    with get_conn() as conn:
        cur = conn.cursor()

        # Only update columns that we KNOW exist (cv_credits, ai_credits).
        # If you later add paid_plan / last_purchase_at, we can re-enable those.
        cur.execute(
            """
            UPDATE users
            SET
                cv_credits = COALESCE(cv_credits, 0) + %s,
                ai_credits = COALESCE(ai_credits, 0) + %s
            WHERE email = %s
            """,
            (add["cv"], add["ai"], email),
        )

        conn.commit()


def mark_event_processed(event_id: str) -> bool:
    """
    Returns True if inserted (new event).
    Returns False if already processed (duplicate retry).
    """
    ensure_tables()
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO stripe_events (event_id) VALUES (%s)", (event_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

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

    if event.get("type") == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}

        # ✅ Prefer app email from metadata (prevents mismatches),
        # then fall back to Stripe customer email fields.
        metadata = session.get("metadata") or {}

        pack = (metadata.get("pack") or "").strip().lower()

        email = (
            (metadata.get("user_email") or "").strip().lower()
            or ((session.get("customer_details") or {}).get("email") or "").strip().lower()
            or (session.get("customer_email") or "").strip().lower()
        )

        if email and pack:
            try:
                grant_pack_credits(email=email, pack=pack)
            except Exception as e:
                # Don’t fail Stripe retry logic forever; log in response for now
                return jsonify({"status": "error", "detail": str(e)}), 200

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

# =========================
# 3) STRIPE WEBHOOK HANDLER (idempotent)
# Put this into your stripe webhook route (where you already handle stripe_events)
# This block assumes you already verified signature and parsed event JSON.
# =========================

def upsert_subscription_from_stripe(user_id: int, customer_id: str | None, subscription_id: str | None,
                                   plan: str, status: str, period_end_ts: int | None, cancel_at_period_end: bool):
    # period_end_ts is Stripe unix seconds -> timestamptz
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, plan, status, current_period_end, cancel_at_period_end)
            VALUES (
              %s, %s, %s, %s, %s,
              CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s) END,
              %s
            )
            ON CONFLICT (stripe_subscription_id)
            DO UPDATE SET
              user_id=EXCLUDED.user_id,
              stripe_customer_id=EXCLUDED.stripe_customer_id,
              plan=EXCLUDED.plan,
              status=EXCLUDED.status,
              current_period_end=EXCLUDED.current_period_end,
              cancel_at_period_end=EXCLUDED.cancel_at_period_end,
              updated_at=now()
            """,
            (user_id, customer_id, subscription_id, plan, status, period_end_ts, period_end_ts, cancel_at_period_end),
        )

def handle_stripe_event(event: dict):
    stripe_event_id = event["id"]
    typ = event["type"]

    # idempotency
    if has_stripe_event(stripe_event_id):
        return
    record_stripe_event(stripe_event_id, typ)

    obj = event["data"]["object"]

    # Example: when payment succeeds for subscription invoice:
    if typ in ("invoice.paid", "invoice.payment_succeeded"):
        # You must map stripe customer/email -> user_id in YOUR system:
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        # Example mapping strategy:
        # - store stripe_customer_id on users, or
        # - read customer email via stripe API (you likely already do this)
        user_id = find_user_id_from_stripe_customer(customer_id)  # <-- implement in your app

        # Decide plan based on price/product id:
        plan = plan_from_invoice(obj)  # <-- implement (monthly/pro/yearly)
        status = "active"

        period_end = None
        lines = (obj.get("lines") or {}).get("data") or []
        if lines and lines[0].get("period") and lines[0]["period"].get("end"):
            period_end = int(lines[0]["period"]["end"])

        cancel_at_period_end = False

        upsert_subscription_from_stripe(
            user_id=user_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            plan=plan,
            status=status,
            period_end_ts=period_end,
            cancel_at_period_end=cancel_at_period_end,
        )

        # ✅ Professional credit model:
        # Every paid invoice grants a "monthly bucket" that expires in 60 days
        # (so user has 2 months to use it, as you asked)
        cv_amt, ai_amt = credits_for_plan(plan)  # <-- implement using your PLAN_LIMITS or a new mapping
        create_credit_grant(
            user_id=user_id,
            cv_amount=cv_amt,
            ai_amount=ai_amt,
            source=f"stripe:{plan}",
            expires_in_days=60,
            stripe_event_id=stripe_event_id,
        )

        return

    # Optional: subscription canceled
    if typ in ("customer.subscription.deleted",):
        subscription_id = obj.get("id")
        customer_id = obj.get("customer")
        user_id = find_user_id_from_stripe_customer(customer_id)

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE subscriptions
                SET status='canceled', updated_at=now()
                WHERE stripe_subscription_id=%s
                """,
                (subscription_id,),
            )
