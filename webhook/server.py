import os
import json
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
DEBUG_WEBHOOK = os.getenv("DEBUG_WEBHOOK", "true").lower() == "true"


def log(*args):
    print("[WEBHOOK]", *args, flush=True)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg2.connect(DATABASE_URL)


def ensure_stripe_events_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()


def mark_event_processed(event_id: str) -> bool:
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


def plan_from_price(price_id: str) -> str | None:
    if price_id == PRICE_MONTHLY:
        return "monthly"
    if price_id == PRICE_PRO:
        return "pro"
    return None


def credits_for_plan(plan: str) -> tuple[int, int]:
    if plan == "monthly":
        return (20, 30)
    if plan == "pro":
        return (50, 90)
    return (0, 0)


def get_or_create_user_id(email: str) -> int:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Missing email")
    with get_conn() as conn:
        cur = conn.cursor()
        # requires users.email unique (you have users_email_key)
        cur.execute("""
            INSERT INTO users (email)
            VALUES (%s)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
        """, (email,))
        uid = cur.fetchone()[0]
        conn.commit()
        return int(uid)


def upsert_subscription(user_id: int, customer_id: str, subscription_id: str,
                        plan: str, status: str, period_end_unix: int | None,
                        cancel_at_period_end: bool):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
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
        """, (user_id, customer_id, subscription_id, plan, status,
              period_end_unix, period_end_unix, cancel_at_period_end))
        conn.commit()


def insert_credit_grant(user_id: int, source: str, cv_amount: int, ai_amount: int, expires_at_unix: int | None) -> bool:
    """Returns True if inserted, False if conflict/ignored."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO credit_grants (user_id, source, cv_amount, ai_amount, expires_at)
            VALUES (
              %s, %s, %s, %s,
              CASE WHEN %s IS NULL THEN NULL ELSE to_timestamp(%s) END
            )
            ON CONFLICT (source) DO NOTHING
            RETURNING id
        """, (user_id, source, cv_amount, ai_amount, expires_at_unix, expires_at_unix))
        row = cur.fetchone()
        conn.commit()
        return bool(row)


def extract_price_id(invoice: dict) -> tuple[str, list[str]]:
    """
    Your payload shows: lines.data[0].pricing.price_details.price
    We'll scan all lines and collect any price IDs we see.
    """
    found = []
    lines = (invoice.get("lines") or {}).get("data") or []
    for ln in lines:
        pricing = ln.get("pricing") or {}
        pd = pricing.get("price_details") or {}
        pid = (pd.get("price") or "").strip()
        if pid:
            found.append(pid)
            continue

        # fallback legacy
        p = ln.get("price") or {}
        pid2 = (p.get("id") or "").strip()
        if pid2:
            found.append(pid2)

    return (found[0] if found else ""), found


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
        log("signature/parse error:", e)
        return jsonify({"error": str(e)}), 400

    event_id = event.get("id", "")
    typ = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    debug = {
        "event_id": event_id,
        "type": typ,
        "env_price_monthly": PRICE_MONTHLY,
        "env_price_pro": PRICE_PRO,
    }

    if event_id and not mark_event_processed(event_id):
        debug["dedupe"] = "duplicate_ignored"
        log("duplicate ignored", event_id, typ)
        return jsonify({"status": "duplicate_ignored", "debug": debug}), 200

    try:
        # Handle invoice-paid variants (covers renewals)
        if typ in ("invoice.paid", "invoice.payment_succeeded", "invoice.payment.paid"):
            invoice = obj
            invoice_id = (invoice.get("id") or "").strip()
            customer_id = (invoice.get("customer") or "").strip()
            subscription_id = (invoice.get("subscription") or "").strip() or None

            price_id, all_prices = extract_price_id(invoice)
            debug.update({
                "invoice_id": invoice_id,
                "customer_id": customer_id,
                "subscription_id": subscription_id,
                "price_id": price_id,
                "all_prices": all_prices,
            })

            plan = plan_from_price(price_id)
            debug["plan"] = plan

            if not plan:
                log("IGNORED unknown_price", json.dumps(debug))
                return jsonify({"status": "ignored", "reason": "unknown_price", "debug": debug}), 200

            # Prefer invoice.customer_email if present, else stripe customer email
            email = (invoice.get("customer_email") or "").strip().lower()
            if not email and customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = (cust.get("email") or "").strip().lower()

            debug["email"] = email

            if not email:
                log("IGNORED no_customer_email", json.dumps(debug))
                return jsonify({"status": "ignored", "reason": "no_customer_email", "debug": debug}), 200

            user_id = get_or_create_user_id(email)
            debug["user_id"] = user_id

            # Sub details
            status = "unknown"
            period_end = None
            cancel_at_period_end = False

            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                status = (sub.get("status") or "unknown")
                period_end = sub.get("current_period_end")
                cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

            debug.update({
                "sub_status": status,
                "period_end": period_end,
                "cancel_at_period_end": cancel_at_period_end,
            })

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
                debug["subscription_upsert"] = True
            else:
                debug["subscription_upsert"] = False

            cv_amt, ai_amt = credits_for_plan(plan)
            expires_at = None if CREDIT_STACKING else period_end

            inserted = insert_credit_grant(
                user_id=user_id,
                source=f"stripe_invoice:{invoice_id}",
                cv_amount=cv_amt,
                ai_amount=ai_amt,
                expires_at_unix=expires_at,
            )
            debug["credit_grant_inserted"] = inserted
            debug["cv_amt"] = cv_amt
            debug["ai_amt"] = ai_amt
            debug["expires_at_unix"] = expires_at

            log("OK granted", json.dumps(debug))
            return jsonify({"status": "ok", "granted": True, "debug": debug}), 200

        # Default: ignore other events (still 200)
        debug["note"] = "event_not_handled"
        return jsonify({"status": "ok", "debug": debug}), 200

    except Exception as e:
        # Return 200 so Stripe won't spam retries forever; but log the exception
        debug["exception"] = str(e)
        log("EXCEPTION", json.dumps(debug))
        return jsonify({"status": "error", "debug": debug}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
