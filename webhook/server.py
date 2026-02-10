import os
import stripe
from flask import Flask, request, jsonify

app = Flask(__name__)

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]


def grant_pack_credits(email: str, pack: str) -> None:
    pack = (pack or "").strip()

    PACKS = {
        "monthly": {"cv": 20, "ai": 30},
        "pro": {"cv": 50, "ai": 90},
    }

    add = PACKS.get(pack)
    if not add:
        raise ValueError(f"Unknown pack: {pack}")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET
                cv_credits = COALESCE(cv_credits, 0) + %s,
                ai_credits = COALESCE(ai_credits, 0) + %s,
                paid_plan = %s,
                last_purchase_at = NOW()
            WHERE email = %s
            """,
            (add["cv"], add["ai"], pack, email),
        )
        conn.commit()


def mark_event_processed(event_id: str) -> bool:
    """
    Returns True if inserted (new event).
    Returns False if already processed (duplicate retry).
    """
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO stripe_events (event_id) VALUES (%s)",
                (event_id,),
            )
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

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        email = (
            (session.get("customer_details") or {}).get("email")
            or session.get("customer_email")
            or ""
        )
        pack = (session.get("metadata") or {}).get("pack") or ""

        if email and pack:
            grant_pack_credits(email=email, pack=pack)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
