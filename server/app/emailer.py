"""Pluggable email sender. Select with EMAIL_BACKEND=console|smtp|memory."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from .config import get_settings

log = logging.getLogger("dedsec_auth.email")


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str, html_body: str | None = None) -> str | None: ...


class ConsoleSender:
    """Development: print the email to the server log. NEVER use in production."""
    def send(self, to: str, subject: str, body: str, html_body: str | None = None) -> None:
        log.warning("[DEV EMAIL] to=%s subject=%s\n%s", to, subject, body)
        print(f"\n===== DEV EMAIL to {to} =====\nSubject: {subject}\n\n{body}\n=============================\n")


class MemorySender:
    """Tests: keep messages in a list."""
    def __init__(self) -> None:
        self.outbox: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str, html_body: str | None = None) -> None:
        self.outbox.append((to, subject, body))


class SmtpSender:
    def send(self, to: str, subject: str, body: str, html_body: str | None = None) -> str:
        """Send; returns the server's reply to DATA (Brevo: 'OK: queued as <id>')."""
        s = get_settings()
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
        msg["Date"] = formatdate(usegmt=True)
        domain = (s.smtp_from.rsplit("@", 1)[-1].strip(" >") or "localhost")
        msg["Message-ID"] = make_msgid(idstring="acct", domain=domain)
        if s.support_email:
            msg["Reply-To"] = s.support_email
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        ctx = ssl.create_default_context()
        if s.smtp_ssl:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=15) as smtp:
                return self._login_send(smtp, msg)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls(context=ctx)
            return self._login_send(smtp, msg)

    @staticmethod
    def _login_send(smtp: smtplib.SMTP, msg: EmailMessage) -> str:
        s = get_settings()
        if s.smtp_username:
            smtp.login(s.smtp_username, s.smtp_password)
        frm, to = parseaddr(msg["From"])[1], parseaddr(msg["To"])[1]
        code, _ = smtp.mail(frm)
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, b"", frm)
        code, _ = smtp.rcpt(to)
        if code not in (250, 251):
            raise smtplib.SMTPRecipientsRefused({to: (code, b"")})
        code, resp = smtp.data(msg.as_bytes())
        if code != 250:
            raise smtplib.SMTPDataError(code, resp)
        return resp.decode(errors="replace")


_sender: EmailSender | None = None


def get_sender() -> EmailSender:
    global _sender
    if _sender is None:
        backend = get_settings().email_backend
        _sender = {"console": ConsoleSender, "smtp": SmtpSender, "memory": MemorySender}[backend]()
    return _sender


def send_verification_email(to: str, code: str, ttl_minutes: int | None = None) -> None:
    from .email_templates import render
    ttl = ttl_minutes or get_settings().email_verification_ttl_hours * 60
    subject, _pre, html_body, text = render("verify", code, ttl)
    try:
        get_sender().send(to, subject, text, html_body=html_body)
    except Exception:  # never leak SMTP errors to the client (or reveal account existence)
        log.exception("failed to send verification email")


def send_device_reset_email(to: str, code: str, ttl_minutes: int) -> None:
    from .email_templates import render
    subject, _pre, html_body, text = render("device_reset", code, ttl_minutes)
    try:
        get_sender().send(to, subject, text, html_body=html_body)
    except Exception:  # never leak SMTP errors to the client
        log.exception("failed to send device reset email")


def send_code_email(kind: str, to: str, code: str, ttl_minutes: int) -> None:
    """Password-reset / email-2FA codes (kinds in email_templates.KINDS). Never raises."""
    from .email_templates import render
    try:
        subject, _pre, html_body, text = render(kind, code, ttl_minutes)
        get_sender().send(to, subject, text, html_body=html_body)
    except Exception:  # never leak SMTP errors to the client (or reveal account existence)
        log.exception("failed to send %s email", kind)


def send_reset_link_email(to: str, url: str, ttl_minutes: int) -> None:
    from .email_templates import render_reset_link
    try:
        subject, _pre, html_body, text = render_reset_link(url, ttl_minutes)
        get_sender().send(to, subject, text, html_body=html_body)
    except Exception:
        log.exception("failed to send password reset link email")


def send_password_changed_email(to: str) -> None:
    from .email_templates import render_password_changed
    try:
        subject, _pre, html_body, text = render_password_changed()
        get_sender().send(to, subject, text, html_body=html_body)
    except Exception:
        log.exception("failed to send password changed email")
