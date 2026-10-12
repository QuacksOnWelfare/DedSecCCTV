from __future__ import annotations

from datetime import datetime
from typing import Literal

from datetime import timezone

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class RegisterIn(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    email_updates: bool = False  # sign-up opt-in for product-update emails (unticked by default)
    terms_accepted: bool = False  # new clients require the tick; optional so older clients keep working


class VerifyEmailIn(_Strict):
    # 6-digit code (new) or a legacy long token from emails sent before the switch.
    token: str = Field(min_length=6, max_length=200)
    email: EmailStr | None = None  # recommended with 6-digit codes (per-account lookup + attempt limit)


# Client device id: SHA-256 hex of machine identifiers, truncated (the raw ids never leave the PC).
HWID_PATTERN = r"^[0-9a-fA-F]{32,64}$"


class LoginIn(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    hwid: str | None = Field(default=None, pattern=HWID_PATTERN)  # or header X-Device-Id


class TotpLoginIn(_Strict):
    challenge_token: str = Field(min_length=10, max_length=200)
    code: str = Field(min_length=6, max_length=10)
    hwid: str | None = Field(default=None, pattern=HWID_PATTERN)  # or header X-Device-Id


class DeviceResetRequestIn(_Strict):
    # email+password, or omit both and send a Bearer token
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=1, max_length=256)
    new_hwid: str | None = Field(default=None, pattern=HWID_PATTERN)  # or header X-Device-Id


class DeviceResetConfirmIn(_Strict):
    email: EmailStr
    code: str = Field(min_length=6, max_length=10)
    new_hwid: str | None = Field(default=None, pattern=HWID_PATTERN)  # or header X-Device-Id


class RefreshIn(_Strict):
    refresh_token: str = Field(min_length=10, max_length=200)


class LogoutIn(_Strict):
    refresh_token: str | None = Field(default=None, max_length=200)
    all_sessions: bool = False


class TwoFASetupIn(_Strict):
    # Optional migration path: import the base32 secret already stored in the app's
    # lock.json so the user's existing authenticator entry keeps working.
    existing_secret: str | None = Field(default=None, min_length=16, max_length=64)


class TwoFAEnableIn(_Strict):
    code: str = Field(min_length=6, max_length=10)


class MessageOut(BaseModel):
    message: str


class TokenOut(BaseModel):
    status: Literal["ok"] = "ok"
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str
    device_bound: bool = False


class ChallengeOut(BaseModel):
    status: Literal["totp_required"] = "totp_required"
    challenge_token: str
    expires_in: int


class TwoFASetupOut(BaseModel):
    secret: str
    otpauth_uri: str
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: EmailStr
    email_verified: bool
    totp_enabled: bool
    device_bound: bool = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
