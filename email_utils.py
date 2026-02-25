import os
import requests


def _brevo_from_email() -> str:
    # Prefer your existing FROM_EMAIL, fallback to BREVO_FROM_EMAIL if you used that
    return (os.getenv("FROM_EMAIL") or os.getenv("BREVO_FROM_EMAIL") or "").strip()


def send_email_brevo(to_email: str, subject: str, html: str) -> None:
    api_key = (os.getenv("BREVO_API_KEY") or "").strip()
    from_email = _brevo_from_email()

    if not api_key:
        raise RuntimeError("Missing BREVO_API_KEY")
    if not from_email:
        raise RuntimeError("Missing FROM_EMAIL (or BREVO_FROM_EMAIL)")

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
            "subject": subject,
            "htmlContent": html,
        },
        timeout=20,
    )

    if r.status_code >= 300:
        raise RuntimeError(f"Brevo failed: {r.status_code} {r.text}")


def send_password_reset_email(to_email: str, reset_token: str) -> None:
    app_url = (os.getenv("APP_URL") or "").strip().rstrip("/")
    reset_link = f"{app_url}/?reset_token={reset_token}" if app_url else ""

    html = f"""
    <p>Hi,</p>
    <p>We received a request to reset your password.</p>
    <p><a href="{reset_link}">{reset_link}</a></p>
    <p><b>Token:</b> {reset_token}</p>
    <p>Mulyba Digital Tools</p>
    """

    send_email_brevo(to_email=to_email, subject="Password reset", html=html)


# ✅ COMPAT: old code might call this for verification emails
def send_resend_email(to_email: str, subject: str, html: str) -> None:
    # Keep the name so you don't have to refactor everything right now
    send_email_brevo(to_email=to_email, subject=subject, html=html)