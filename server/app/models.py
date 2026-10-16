from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # brute-force protection
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 2FA (secret encrypted with Fernet)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_pending_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_last_step: Mapped[int | None] = mapped_column(Integer, nullable=True)  # replay protection

    # bump to invalidate every outstanding access token ("log out everywhere")
    token_version: Mapped[int] = mapped_column(Integer, default=0)

    # hardware (device) lock: one account = one PC. Only hashes are stored, never the raw HWID.
    # All nullable so the startup migration can ADD COLUMN them to an existing table.
    device_hwid_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_reset_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_reset_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_reset_new_hwid_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_reset_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # product-update emails (UK PECR/GDPR): explicit opt-in, default off. The unsubscribe token is
    # HMAC(EMAIL_UNSUB_KEY, id:nonce); only its SHA-256 is stored. Rotate by changing the nonce.
    email_updates: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    email_updates_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_unsub_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email_unsub_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Terms of use: set at register when the client sent terms_accepted=true (nullable for old rows).
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Email two-factor (1.0.3): sign-in codes go to the account email instead of an authenticator app.
    # email_otp_* hold the one pending settings code (enable / disable email 2FA); only a keyed hash is stored.
    email_2fa_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    email_otp_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_otp_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_otp_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    email_otp_purpose: Mapped[str | None] = mapped_column(String(16), nullable=True)

    @property
    def device_bound(self) -> bool:
        return self.device_hwid_hash is not None

    @property
    def twofa_method(self) -> str:
        if self.totp_enabled:
            return "totp"
        return "email" if self.email_2fa_enabled else "none"

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user", cascade="all, delete-orphan")


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)  # wrong 6-digit guesses


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[str] = mapped_column(String(36), index=True)  # one family per login session
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    replaced_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class LoginChallenge(Base):
    """Pending 2FA challenge issued after a correct password when TOTP is enabled."""
    __tablename__ = "login_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    # 1.0.3: "totp" (NULL for old rows) or "email"; for email challenges the keyed hash of the emailed code
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resends: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)


class PasswordReset(Base):
    """Forgot password: 6-digit emailed code (entered in the app) -> emailed single-use reset link.

    Only keyed / SHA-256 hashes are stored. One live row per user: a new request burns older ones.
    """
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    code_hash: Mapped[str] = mapped_column(String(64))
    code_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    code_used: Mapped[bool] = mapped_column(Boolean, default=False)
    link_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)  # burned / superseded / password already set
