"""Pluggable email sender. Select with EMAIL_BACKEND=console|smtp|memory."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from .config import get_settings

log = logging.getLogger("dedsec_auth.email")


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleSender:
    """Development: print the email to the server log. NEVER use in production."""
    def send(self, to: str, subject: str, body: str) -> None:
        log.warning("[DEV EMAIL] to=%s subject=%s\n%s", to, subject, body)
        print(f"\n===== DEV EMAIL to {to} =====\nSubject: {subject}\n\n{body}\n=============================\n")


class MemorySender:
    """Tests: keep messages in a list."""
    def __init__(self) -> None:
        self.outbox: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.outbox.append((to, subject, body))


class SmtpSender:
    def send(self, to: str, subject: str, body: str) -> None:
        s = get_settings()
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if s.smtp_ssl:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=15) as smtp:
                self._login_send(smtp, msg)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ctx)
                self._login_send(smtp, msg)

    @staticmethod
    def _login_send(smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        s = get_settings()
        if s.smtp_username:
            smtp.login(s.smtp_username, s.smtp_password)
        smtp.send_message(msg)


_sender: EmailSender | None = None


def get_sender() -> EmailSender:
    global _sender
    if _sender is None:
        backend = get_settings().email_backend
        _sender = {"console": ConsoleSender, "smtp": SmtpSender, "memory": MemorySender}[backend]()
    return _sender


def send_verification_email(to: str, token: str) -> None:
    s = get_settings()
    link = f"{s.public_base_url.rstrip('/')}/auth/verify-email?token={token}"
    body = (
        "Welcome to DedSec CCTV.\n\n"
        f"Confirm your email address with this link (valid {s.email_verification_ttl_hours}h):\n{link}\n\n"
        f"Or paste this code into the app: {token}\n\n"
        "If you didn't create an account, ignore this email."
    )
    try:
        get_sender().send(to, "Verify your DedSec CCTV account", body)
    except Exception:  # never leak SMTP errors to the client (or reveal account existence)
        log.exception("failed to send verification email")


def send_device_reset_email(to: str, code: str, ttl_minutes: int) -> None:
    body = (
        "DedSec CCTV - device reset\n\n"
        "Someone (hopefully you) asked to move your DedSec CCTV account to a new PC.\n\n"
        f"Your reset code: {code}\n\n"
        f"Enter it in the app within {ttl_minutes} minutes. After the reset the account works only on\n"
        "the new PC and every other session is signed out.\n\n"
        "If this wasn't you, ignore this email and change your password. Your account stays\n"
        "locked to your current PC.\n\n"
        "- DedSec CCTV"
    )
    try:
        get_sender().send(to, "DedSec CCTV device reset code", body)
    except Exception:  # never leak SMTP errors to the client
        log.exception("failed to send device reset email")
