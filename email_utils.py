# email_utils.py
import os
import sys
import requests


def send_password_reset_email(to_email: str, reset_token: str):
    # Clean key aggressively (handles quotes/newlines)
    api_key = (os.getenv("RESEND_API_KEY") or "").strip().strip('"').strip("'")
    from_email = (os.getenv("FROM_EMAIL") or "").strip()
    app_url = (os.getenv("APP_URL") or "").strip().rstrip("/")

    # SAFE debug -> Railway logs
    print("=== RESEND RESET EMAIL ===", file=sys.stderr, flush=True)
    print(f"key_prefix={api_key[:3]} key_len={len(api_key)}", file=sys.stderr, flush=True)
    print(f"from_email_set={bool(from_email)} app_url_set={bool(app_url)}", file=sys.stderr, flush=True)

    if not api_key:
        raise RuntimeError("Missing RESEND_API_KEY env var")
    if not api_key.startswith("re_"):
        raise RuntimeError("RESEND_API_KEY does not look valid (expected prefix re_)")
    if not from_email:
        raise RuntimeError("Missing FROM_EMAIL env var")

    reset_link = f"{app_url}/?reset_token={reset_token}" if app_url else None
    subject = "Password reset for your account"

    link_block = (
        f"""
        <p>
          <b>Reset link:</b><br/>
          <a href="{reset_link}">{reset_link}</a>
        </p>
        """
        if reset_link
        else "<p><b>Reset link:</b> (not available yet — use the token below)</p>"
    )

    html = f"""
    <p>Hi,</p>
    <p>We received a request to reset the password for your account.</p>
    {link_block}
    <p><b>Token:</b> {reset_token}</p>
    <p>If you did not request this, you can safely ignore this email.</p>
    <p>Thanks,<br/>Munib's Career Support Tools</p>
    """

    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
        },
        timeout=20,
    )

    if r.status_code >= 300:
        raise RuntimeError(f"Resend failed: {r.status_code} {r.text}")
