"""Unsubscribe tokens for product-update emails.

token = base64url(HMAC-SHA256(EMAIL_UNSUB_KEY, "<user id>:<nonce>")). Only sha256(token) is stored, so a DB leak
does not reveal working links; a mailer recomputes the token from the same key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from sqlalchemy import select

from .config import get_settings
from .models import User


def _key() -> bytes:
    k = get_settings().email_unsub_key
    if len(k) < 32:
        raise RuntimeError("EMAIL_UNSUB_KEY is missing or too short")
    return k.encode()


def make_token(user_id: str, nonce: str) -> str:
    mac = hmac.new(_key(), f"{user_id}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_token(user: User) -> None:
    if not user.email_unsub_nonce or not user.email_unsub_token_hash:
        user.email_unsub_nonce = secrets.token_hex(16)
        user.email_unsub_token_hash = token_hash(make_token(user.id, user.email_unsub_nonce))


def ensure_all_tokens() -> None:
    from .db import SessionLocal
    if not get_settings().email_unsub_key:
        return
    with SessionLocal() as db:
        for u in db.scalars(select(User).where((User.email_unsub_token_hash.is_(None)) | (User.email_unsub_nonce.is_(None)))):
            ensure_token(u)
        db.commit()
