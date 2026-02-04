import os
import requests


def send_password_reset_email(to_email: str, reset_token: str) -> None:
    api_key = (os.getenv("BREVO_API_KEY") or "").strip()
    from_email = (os.getenv("FROM_EMAIL") or "").strip()
    app_url = (os.getenv("APP_URL") or "").strip().rstrip("/")

    if not api_key:
        raise RuntimeError("Missing BREVO_API_KEY")
    if not from_email:
        raise RuntimeError("Missing FROM_EMAIL")

    reset_link = f"{app_url}/?reset_token={reset_token}" if app_url else None

    html = f"""
    <p>Hi,</p>
    <p>We received a request to reset your password.</p>
    <p><a href="{reset_link}">{reset_link}</a></p>
    <p><b>Token:</b> {reset_token}</p>
    <p>Mulyba Digital Tools</p>
    """

    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": api_key,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={
            "sender": {"email": from_email, "name": "Mulyba Digital Tools"},
            "to": [{"email": to_email}],
            "subject": "Password reset",
            "htmlContent": html,
        },
        timeout=20,
    )

    if r.status_code >= 300:
        raise RuntimeError(f"Brevo failed: {r.status_code} {r.text}")
