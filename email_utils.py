import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_password_reset_email(to_email: str, reset_token: str):
    # Railway Variables: 
    # FROM_EMAIL = your full wix/google email
    # GMAIL_APP_PASSWORD = that 16-character code
    sender_email = os.getenv("FROM_EMAIL")
    password = os.getenv("GMAIL_APP_PASSWORD") 
    app_url = (os.getenv("APP_URL") or "").strip().rstrip("/")

    if not password or not sender_email:
        raise RuntimeError("Missing Google credentials in Railway variables")

    subject = "Password reset for your account"
    reset_link = f"{app_url}/?reset_token={reset_token}" if app_url else reset_token
    
    msg = MIMEMultipart()
    msg['From'] = f"Support <{sender_email}>"
    msg['To'] = to_email
    msg['Subject'] = subject
    
    body = f"Use this link to reset your password: {reset_link}\n\nToken: {reset_token}"
    msg.attach(MIMEText(body, 'plain'))

    try:
        # Google uses Port 587 with TLS
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, password)
            server.sendmail(sender_email, to_email, msg.as_string())
    except Exception as e:
        raise RuntimeError(f"Google Mail failed: {str(e)}")
