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
