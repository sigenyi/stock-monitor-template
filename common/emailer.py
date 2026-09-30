"""Shared email utility for both the price-alerts and filings workflows.

Sends plain-text email over SMTP. By default it uses Gmail
(smtp.gmail.com:465, implicit SSL) with a Gmail app password; any other SMTP
provider works by setting the optional SMTP_HOST / SMTP_PORT environment
variables (port 465 uses implicit SSL, any other port uses STARTTLS). Empty
values count as unset, so the workflows can always pass the (possibly
undefined) repository variables through.

Credentials come from environment variables — on GitHub Actions, injected
from repository secrets: EMAIL_ADDRESS (the sending account / SMTP login),
EMAIL_APP_PASSWORD (its app password or SMTP password) and EMAIL_TO (the
recipient). Fails fast with a clear message if a required variable is missing.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465  # implicit SSL; any other port uses STARTTLS
SMTP_TIMEOUT_S = 60

# Severity → emoji prefix for subjects.
SEVERITY_EMOJI = {
    "red": "\U0001f534",     # 🔴
    "yellow": "\U0001f7e1",  # 🟡
    "info": "ℹ️",  # ℹ️
    "clock": "⏱️", # ⏱️
}


def require_env(name: str) -> str:
    """Return the environment variable or exit(1) with an actionable message."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(
            f"FATAL: required environment variable {name!r} is missing or empty. "
            f"On GitHub Actions, add it as a repository secret "
            f"(Settings > Secrets and variables > Actions) and make sure the "
            f"workflow passes it into the job environment. When running "
            f"locally, export it in your shell first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def smtp_settings() -> tuple[str, int]:
    """Resolve (host, port) from SMTP_HOST / SMTP_PORT, treating empty as unset."""
    host = os.environ.get("SMTP_HOST", "").strip() or DEFAULT_SMTP_HOST
    port_text = os.environ.get("SMTP_PORT", "").strip()
    if not port_text:
        return host, DEFAULT_SMTP_PORT
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        print(
            f"FATAL: SMTP_PORT must be a port number such as 465 (SSL) or 587 "
            f"(STARTTLS); got {port_text!r}. Fix or remove the SMTP_PORT "
            f"repository variable.",
            file=sys.stderr,
        )
        sys.exit(1)
    return host, port


def send_email(subject: str, body: str, severity: str | None = None) -> None:
    """Send a plain-text email.

    severity: one of 'red', 'yellow', 'info', 'clock', or None. When given,
    the matching emoji is prefixed to the subject line.
    """
    sender = require_env("EMAIL_ADDRESS")
    app_password = require_env("EMAIL_APP_PASSWORD")
    recipient = require_env("EMAIL_TO")
    host, port = smtp_settings()

    if severity is not None:
        emoji = SEVERITY_EMOJI.get(severity)
        if emoji is None:
            print(f"WARNING: unknown severity {severity!r}; sending without prefix.")
        else:
            subject = f"{emoji} {subject}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    # Verify the server certificate (smtplib's default context does not).
    context = ssl.create_default_context()
    print(f"Sending email: {subject!r}")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=context) as smtp:
            smtp.login(sender, app_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.starttls(context=context)
            smtp.login(sender, app_password)
            smtp.send_message(msg)
    print("Email sent.")
