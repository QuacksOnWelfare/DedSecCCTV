"""Cryptographic primitives: password hashing, JWTs, opaque tokens, TOTP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive datetimes from the DB as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# passwords (Argon2id, OWASP-recommended parameters)
# ---------------------------------------------------------------------------

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16)
# Hash of a random password, used to equalise timing when the account does not exist.
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: always runs one Argon2 verification even for unknown users."""
    try:
        return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


_COMMON = {
    "password", "password1", "password123", "123456789012", "qwertyuiop", "letmein", "iloveyou",
    "admin", "welcome", "dragon", "monkey", "football", "baseball", "master", "sunshine",
    "princess", "trustno1", "dedsec", "dedseccctv", "cctvcamera", "changeme", "qwerty123",
}


def password_problems(password: str, email: str | None = None) -> list[str]:
    """Return a list of human-readable reasons the password is too weak (empty = OK)."""
    problems: list[str] = []
    if len(password) < 12:
        problems.append("must be at least 12 characters")
    if len(password) > 128:
        problems.append("must be at most 128 characters")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3 and len(password) < 20:
        problems.append("must mix at least 3 of: lowercase, uppercase, digits, symbols (or be 20+ chars)")
    if len(set(password)) < 6:
        problems.append("has too few distinct characters")
    lowered = password.lower()
    if lowered in _COMMON or any(lowered.startswith(w) and len(lowered) - len(w) <= 4 for w in _COMMON):
        problems.append("is too common")
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and local in lowered:
            problems.append("must not contain your email name")
    return problems


# ---------------------------------------------------------------------------
# opaque tokens (refresh, email verification, login challenge)
# ---------------------------------------------------------------------------

def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


def hash_token(token: str) -> str:
    """SHA-256 is fine for high-entropy random tokens (no need for a slow KDF)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_hwid(hwid: str) -> str:
    """Stored form of a client device id (itself already a truncated SHA-256 on the client)."""
    return hashlib.sha256(b"dedsec-hwid-v1:" + hwid.strip().lower().encode("ascii")).hexdigest()


def new_reset_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_reset_code(user_id: str, code: str) -> str:
    """Keyed hash: a 6-digit code is trivially brute-forced from a plain SHA-256 if the DB leaks."""
    key = get_settings().jwt_secret.encode()
    return hmac.new(key, f"device-reset:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# JWT access tokens
# ---------------------------------------------------------------------------

def create_access_token(user_id: str, token_version: int) -> tuple[str, int]:
    s = get_settings()
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": user_id,
        "ver": token_version,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=s.access_token_ttl_seconds)).timestamp()),
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "jti": str(uuid.uuid4()),
        "typ": "access",
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256"), s.access_token_ttl_seconds


def decode_access_token(token: str) -> dict[str, Any] | None:
    s = get_settings()
    try:
        claims = jwt.decode(
            token,
            s.jwt_secret,
            algorithms=["HS256"],  # pinned: never accept "none" or asymmetric confusion
            audience=s.jwt_audience,
            issuer=s.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]},
            leeway=5,
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "access":
        return None
    return claims


# ---------------------------------------------------------------------------
# TOTP — identical scheme to the desktop app: RFC 6238, HMAC-SHA1, 30 s, 6 digits, ±1 step
# ---------------------------------------------------------------------------

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_DRIFT = 1


def generate_totp_secret() -> str:
    """Same as the app: os.urandom(20) -> base32 (no padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def normalize_b32(secret: str) -> str:
    s = secret.replace(" ", "").replace("-", "").upper().rstrip("=")
    base64.b32decode(s + "=" * (-len(s) % 8))  # validate, raises binascii.Error
    return s


def _hotp(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    key = base64.b32decode(normalize_b32(secret_b32) + "=" * (-len(normalize_b32(secret_b32)) % 8))
    return _hotp(key, int((at if at is not None else time.time()) // TOTP_STEP))


def verify_totp(secret_b32: str, code: str, last_step: int | None, at: float | None = None) -> int | None:
    """Return the matched time-step (for replay protection) or None.

    Every candidate window is compared with hmac.compare_digest and the loop never
    short-circuits, so timing does not leak which window matched.
    """
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    s = normalize_b32(secret_b32)
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    now_step = int((at if at is not None else time.time()) // TOTP_STEP)
    matched: int | None = None
    for step in range(now_step - TOTP_DRIFT, now_step + TOTP_DRIFT + 1):
        if hmac.compare_digest(_hotp(key, step), code) and matched is None:
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None  # wrong, or a replay of an already-used code
    return matched


def provisioning_uri(secret_b32: str, account: str) -> str:
    s = get_settings()
    label = quote(f"{s.totp_issuer}:{account}", safe=":@")
    params = urlencode({"secret": secret_b32, "issuer": s.totp_issuer, "algorithm": "SHA1",
                        "digits": TOTP_DIGITS, "period": TOTP_STEP})
    return f"otpauth://totp/{label}?{params}"


def _fernet() -> Fernet:
    return Fernet(get_settings().totp_encryption_key.encode())


def encrypt_secret(secret_b32: str) -> bytes:
    return _fernet().encrypt(secret_b32.encode())


def decrypt_secret(blob: bytes) -> str:
    try:
        return _fernet().decrypt(blob).decode()
    except InvalidToken as exc:  # wrong key / tampered row
        raise RuntimeError("TOTP secret could not be decrypted (check TOTP_ENCRYPTION_KEY)") from exc
