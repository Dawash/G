"""
Email sending module for G.

Sends emails via SMTP (Gmail, Outlook, custom).
Credentials are saved locally on first use.
The Brain can call this to send emails on voice command.
"""

import json
import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_creds.json")

# Common SMTP servers
SMTP_SERVERS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587},
    "hotmail": {"host": "smtp-mail.outlook.com", "port": 587},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 587},
}


def _load_creds():
    """Load saved email credentials (uses encrypted storage from config.py)."""
    try:
        from config import load_email_creds
        creds = load_email_creds()
        if creds:
            return creds
    except ImportError:
        pass
    # Fallback: read directly (legacy)
    if not os.path.exists(CREDS_FILE):
        return None
    try:
        with open(CREDS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_creds(creds):
    """Save email credentials (uses encrypted storage from config.py)."""
    try:
        from config import save_email_creds
        save_email_creds(creds)
    except ImportError:
        # Fallback: plaintext (legacy)
        with open(CREDS_FILE, "w") as f:
            json.dump(creds, f, indent=2)


def _detect_provider(email):
    """Detect SMTP server from email address."""
    domain = email.split("@")[-1].lower()
    for provider, server in SMTP_SERVERS.items():
        if provider in domain:
            return server
    # Default to Gmail-style TLS
    return {"host": f"smtp.{domain}", "port": 587}


def setup_email():
    """Interactive setup for email credentials. Returns creds dict or None."""
    print("\n=== Email Setup ===")
    print("To send emails, I need your email credentials.")
    print("For Gmail: use an App Password (not your regular password).")
    print("  Go to: Google Account > Security > App Passwords\n")

    email = input("Your email address: ").strip()
    if not email or "@" not in email:
        return None

    password = input("App password (or SMTP password): ").strip()
    if not password:
        return None

    server = _detect_provider(email)

    creds = {
        "email": email,
        "password": password,
        "smtp_host": server["host"],
        "smtp_port": server["port"],
    }

    # Test the connection
    print("Testing connection...")
    try:
        with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"], timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(creds["email"], creds["password"])
        print("[OK] Email connection works!")
        _save_creds(creds)
        return creds
    except smtplib.SMTPAuthenticationError:
        print("[ERROR] Authentication failed. Check your email and app password.")
        return None
    except Exception as e:
        print(f"[ERROR] Connection failed: {e}")
        return None


def send_email(to, subject, body):
    """
    Send an email. Returns a status message.

    If credentials aren't set up yet, returns immediately with instructions.
    """
    creds = _load_creds()

    if not creds:
        # Don't block the voice loop with interactive input() — return immediately
        return ("Email isn't set up yet. Please run 'python -c \"from email_sender import setup_email; setup_email()\"' "
                "in a terminal to configure your email credentials.")

    if not to or "@" not in to:
        return f"I need a valid email address to send to. '{to}' doesn't look right."

    if not subject:
        subject = "(No subject)"

    try:
        msg = MIMEMultipart()
        msg["From"] = creds["email"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body or "", "plain"))

        with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"], timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(creds["email"], creds["password"])
            smtp.send_message(msg)

        logger.info(f"Email sent to {to}: {subject}")
        return f"Done! I've sent the email to {to} with subject '{subject}'."

    except smtplib.SMTPAuthenticationError:
        logger.error("Email auth failed")
        return "Email authentication failed. Your credentials might have changed. Delete email_creds.json and set up again."

    except smtplib.SMTPRecipientsRefused:
        return f"The email address {to} was rejected. Please check it."

    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return f"I couldn't send the email: {e}"


def is_configured():
    """Check if email credentials are saved."""
    return _load_creds() is not None
