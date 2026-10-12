"""Application configuration. Every secret comes from environment variables (or a .env file)."""
from __future__ import annotations

import base64
import logging
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PLACEHOLDER_MARK = "CHANGE_ME"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core ---
    environment: Literal["development", "production", "test"] = "development"
    database_url: str = "sqlite:///./auth.db"

    # --- JWT access tokens ---
    jwt_secret: str = Field(..., min_length=32, description="HS256 signing key, >= 32 random bytes")
    jwt_issuer: str = "dedsec-auth"
    jwt_audience: str = "dedsec-cctv"
    access_token_ttl_seconds: int = 15 * 60

    # --- refresh tokens / sessions ---
    refresh_token_ttl_days: int = 30

    # --- TOTP secret encryption at rest (Fernet key, urlsafe base64 of 32 bytes) ---
    totp_encryption_key: str = Field(..., description="Fernet key used to encrypt TOTP secrets in the DB")
    totp_issuer: str = "DedSecCCTV"
    login_challenge_ttl_seconds: int = 5 * 60
    login_challenge_max_attempts: int = 5

    # --- brute-force protection ---
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    rate_limit_ip_per_minute: int = 20          # auth endpoints, per client IP
    rate_limit_account_per_15min: int = 10      # login/totp attempts per account
    trust_proxy_headers: bool = False           # honour X-Forwarded-For (only behind your own proxy!)

    # --- email verification ---
    email_verification_ttl_hours: int = 24
    email_backend: Literal["console", "smtp", "memory"] = "console"
    public_base_url: str = "http://localhost:8000"  # used to build verification links
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "DedSec CCTV <no-reply@example.com>"

    # --- product-update emails: HMAC key for unsubscribe tokens (>= 32 random bytes) ---
    email_unsub_key: str = ""
    # --- branding used in transactional emails (self-hosters: set these) ---
    email_asset_base: str = "https://auth.example.com/updates/email/"
    support_email: str = "support@example.com"
    discord_url: str = ""
    github_url: str = "https://github.com/QuacksOnWelfare/DedSecCCTV"

    # --- minimum desktop-app version (empty = no minimum; see app/minversion.py) ---
    min_app_version: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False

    # --- hardware (device) lock: one account = one PC ---
    device_lock_require_hwid: bool = True       # refuse sign-ins without a device id (pre-1.0.0 clients)
    device_reset_ttl_minutes: int = 15          # lifetime of the emailed device-reset code
    device_reset_max_attempts: int = 5          # wrong codes before the pending reset is burned
    device_reset_emails_per_hour: int = 3       # per account

    # --- HTTP hardening ---
    cors_origins: list[str] = []                # desktop clients don't need CORS; keep empty
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    force_https: bool = False                   # redirect http->https (enable if not done by proxy)
    enable_docs: bool = False                   # expose /docs only when explicitly enabled

    @field_validator("totp_encryption_key")
    @classmethod
    def _check_fernet_key(cls, v: str) -> str:
        if PLACEHOLDER_MARK.lower() in v.lower():
            raise ValueError("TOTP_ENCRYPTION_KEY is still CHANGE_ME; generate one with "
                             "`python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\"`")
        try:
            raw = base64.urlsafe_b64decode(v.encode())
        except Exception as exc:  # pragma: no cover
            raise ValueError("TOTP_ENCRYPTION_KEY must be urlsafe base64") from exc
        if len(raw) != 32:
            raise ValueError("TOTP_ENCRYPTION_KEY must decode to 32 bytes (use Fernet.generate_key())")
        return v

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_cors(cls, v: list[str]) -> list[str]:
        if "*" in v:
            raise ValueError("CORS_ORIGINS must list explicit origins; '*' is not allowed")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _check_jwt_secret(cls, v: str) -> str:
        if PLACEHOLDER_MARK.lower() in v.lower():
            return v  # rejected at startup in production by check_production_safety()
        if v.lower() in {"changeme", "secret", "change-me"} or len(set(v)) < 10:
            raise ValueError("JWT_SECRET looks weak; generate one with `python -c \"import secrets;print(secrets.token_urlsafe(64))\"`")
        return v


_log = logging.getLogger(__name__)


def check_production_safety(s: Settings) -> None:
    """Refuse to start in production while any setting still contains a CHANGE_ME placeholder."""
    bad = [name for name, val in s.model_dump().items() if PLACEHOLDER_MARK.lower() in str(val).lower()]
    if s.environment == "production":
        if s.email_backend != "smtp":
            bad.append("email_backend (must be 'smtp' in production)")
        if bad:
            raise RuntimeError("Refusing to start in production; replace CHANGE_ME/unsafe values in: "
                               + ", ".join(sorted(bad)))
    elif bad:
        _log.warning("Settings still contain CHANGE_ME placeholders: %s", ", ".join(sorted(bad)))


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    check_production_safety(s)
    return s
