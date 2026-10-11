from __future__ import annotations

import binascii
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..emailer import get_sender, send_device_reset_email, send_verification_email
from ..models import EmailVerificationToken, LoginChallenge, RefreshToken, User
from ..ratelimit import TOO_MANY, client_ip, limit_account, limit_ip, limiter
from ..schemas import (
    HWID_PATTERN, ChallengeOut, DeviceResetConfirmIn, DeviceResetRequestIn, LoginIn, LogoutIn, MessageOut,
    RefreshIn, RegisterIn, TokenOut, TotpLoginIn, TwoFAEnableIn, TwoFASetupIn, TwoFASetupOut, UserOut,
    VerifyEmailIn,
)
from ..security import (
    as_aware, constant_time_eq, create_access_token, decode_access_token, decrypt_secret, encrypt_secret,
    generate_totp_secret, hash_hwid, hash_password, hash_reset_code, hash_token, needs_rehash,
    new_opaque_token, new_reset_code, normalize_b32, password_problems, provisioning_uri, utcnow,
    verify_password, verify_totp,
)

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(limit_ip)])

GENERIC_LOGIN_ERROR = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
INVALID_TOKEN = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token.")
INVALID_CODE = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired code.")
REGISTER_MSG = "If this email can be registered, a verification message has been sent."
DEVICE_MISMATCH_MSG = "This account is locked to another PC. Request a device reset."
DEVICE_MISMATCH = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEVICE_MISMATCH_MSG,
                                headers={"X-Error-Code": "device_mismatch"})
DEVICE_ID_REQUIRED = HTTPException(
    status_code=status.HTTP_426_UPGRADE_REQUIRED,
    detail="This version of DedSec CCTV is out of date. Please install the latest version.",
    headers={"X-Error-Code": "device_id_required"})
INVALID_DEVICE_ID = HTTPException(status_code=422, detail="Invalid device id.")
NOT_AUTH = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated",
                         headers={"WWW-Authenticate": "Bearer"})
DEVICE_RESET_MSG = ("If the details are correct and the account is locked to a PC, "
                    "a reset code has been emailed to the account address.")
_HWID_RE = re.compile(HWID_PATTERN)
_optional_bearer = HTTPBearer(auto_error=False)


def _norm_email(email: str) -> str:
    return email.strip().lower()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _issue_verification(db: Session, user: User) -> None:
    s = get_settings()
    db.execute(update(EmailVerificationToken)
               .where(EmailVerificationToken.user_id == user.id, EmailVerificationToken.used.is_(False))
               .values(used=True))  # only the newest token is valid
    token = new_opaque_token()
    db.add(EmailVerificationToken(user_id=user.id, token_hash=hash_token(token),
                                  expires_at=utcnow() + timedelta(hours=s.email_verification_ttl_hours)))
    db.commit()
    send_verification_email(user.email, token)


def _issue_tokens(db: Session, user: User, request: Request, family_id: str | None = None) -> TokenOut:
    s = get_settings()
    access, ttl = create_access_token(user.id, user.token_version)
    refresh = new_opaque_token()
    db.add(RefreshToken(
        user_id=user.id, family_id=family_id or str(uuid.uuid4()), token_hash=hash_token(refresh),
        expires_at=utcnow() + timedelta(days=s.refresh_token_ttl_days),
        user_agent=(request.headers.get("user-agent") or "")[:255], ip=client_ip(request)[:64],
    ))
    db.commit()
    return TokenOut(access_token=access, expires_in=ttl, refresh_token=refresh, device_bound=user.device_bound)


# ---------------------------------------------------------------------------
# hardware (device) lock
# ---------------------------------------------------------------------------

def _request_hwid(request: Request, body_hwid: str | None) -> str | None:
    """Device id from the JSON body (already validated by the schema) or the X-Device-Id header."""
    if body_hwid:
        return body_hwid.lower()
    header = (request.headers.get("x-device-id") or "").strip()
    if not header:
        return None
    if not _HWID_RE.fullmatch(header):
        raise INVALID_DEVICE_ID
    return header.lower()


def _hwid_required(user: User) -> bool:
    return get_settings().device_lock_require_hwid or user.device_hwid_hash is not None


def _device_mismatch(user: User, hwid: str | None) -> bool:
    return (user.device_hwid_hash is not None and hwid is not None
            and not constant_time_eq(user.device_hwid_hash, hash_hwid(hwid)))


def _device_gate(db: Session, user: User, hwid: str | None) -> None:
    """Run after full authentication (password + 2FA): bind the first PC, refuse any other."""
    if hwid is None:
        if _hwid_required(user):
            raise DEVICE_ID_REQUIRED
        return
    if user.device_hwid_hash is None:
        # conditional UPDATE: if two PCs race on the first sign-in only one of them wins
        res = db.execute(update(User).where(User.id == user.id, User.device_hwid_hash.is_(None))
                         .values(device_hwid_hash=hash_hwid(hwid), device_bound_at=utcnow()))
        db.commit()
        db.refresh(user)
        if res.rowcount == 1:
            return
    if _device_mismatch(user, hwid):
        raise DEVICE_MISMATCH


def _clear_device_reset(user: User) -> None:
    user.device_reset_code_hash = None
    user.device_reset_expires = None
    user.device_reset_new_hwid_hash = None
    user.device_reset_attempts = 0


def _is_locked(user: User) -> bool:
    lu = as_aware(user.locked_until)
    return lu is not None and lu > utcnow()


def _register_failure(db: Session, user: User) -> None:
    s = get_settings()
    user.failed_login_count += 1
    if user.failed_login_count >= s.max_failed_logins:
        user.locked_until = utcnow() + timedelta(minutes=s.lockout_minutes)
        user.failed_login_count = 0
    db.commit()


def _verify_email_token(db: Session, token: str) -> None:
    row = db.scalar(select(EmailVerificationToken).where(EmailVerificationToken.token_hash == hash_token(token)))
    if row is None or row.used or as_aware(row.expires_at) <= utcnow():  # type: ignore[operator]
        raise INVALID_TOKEN
    row.used = True
    user = db.get(User, row.user_id)
    if user is None:
        raise INVALID_TOKEN
    user.email_verified = True
    db.commit()


# ---------------------------------------------------------------------------
# registration / verification
# ---------------------------------------------------------------------------

@router.post("/register", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def register(body: RegisterIn, db: Session = Depends(get_db)) -> MessageOut:
    email = _norm_email(body.email)
    problems = password_problems(body.password, email)
    if problems:  # safe to reveal: says nothing about whether the account exists
        raise HTTPException(status_code=422, detail={"password": problems})
    limit_account(f"register:{email}")
    pw_hash = hash_password(body.password)  # always hash -> uniform timing
    existing = db.scalar(select(User).where(User.email == email))
    if existing is None:
        user = User(email=email, password_hash=pw_hash)
        db.add(user)
        db.commit()
        _issue_verification(db, user)
    elif not existing.email_verified:
        _issue_verification(db, existing)  # resend; password is NOT changed
    else:
        try:
            get_sender().send(email, "DedSec CCTV sign-up attempt",
                              "Someone tried to create an account with your email. "
                              "If it was you, just log in. Otherwise you can ignore this message.")
        except Exception:
            pass
    return MessageOut(message=REGISTER_MSG)


@router.post("/verify-email", response_model=MessageOut)
def verify_email(body: VerifyEmailIn, db: Session = Depends(get_db)) -> MessageOut:
    _verify_email_token(db, body.token)
    return MessageOut(message="Email verified. You can now log in.")


@router.get("/verify-email", response_class=PlainTextResponse, include_in_schema=False)
def verify_email_link(token: str, db: Session = Depends(get_db)) -> str:
    """Target of the link in the verification email."""
    if not 10 <= len(token) <= 200:
        raise INVALID_TOKEN
    _verify_email_token(db, token)
    return "Email verified. You can close this page and log in from the DedSec CCTV app."


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenOut | ChallengeOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut | ChallengeOut:
    s = get_settings()
    email = _norm_email(body.email)
    limit_account(email)
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or not user.is_active:
        raise GENERIC_LOGIN_ERROR
    if _is_locked(user):
        raise GENERIC_LOGIN_ERROR  # same message; lockout is not disclosed
    if not ok:
        _register_failure(db, user)
        raise GENERIC_LOGIN_ERROR
    user.failed_login_count = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    db.commit()
    if not user.email_verified:  # only disclosed to someone who knows the password
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email address not verified.")
    hwid = _request_hwid(request, body.hwid)
    if user.totp_enabled:
        # fail early (before a TOTP code is spent); binding itself happens after the 2FA step
        if hwid is None and _hwid_required(user):
            raise DEVICE_ID_REQUIRED
        if _device_mismatch(user, hwid):
            raise DEVICE_MISMATCH
        token = new_opaque_token()
        db.add(LoginChallenge(user_id=user.id, token_hash=hash_token(token),
                              expires_at=utcnow() + timedelta(seconds=s.login_challenge_ttl_seconds)))
        db.commit()
        return ChallengeOut(challenge_token=token, expires_in=s.login_challenge_ttl_seconds)
    _device_gate(db, user, hwid)
    return _issue_tokens(db, user, request)


@router.post("/login/totp", response_model=TokenOut)
def login_totp(body: TotpLoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    s = get_settings()
    ch = db.scalar(select(LoginChallenge).where(LoginChallenge.token_hash == hash_token(body.challenge_token)))
    if ch is None or ch.used or as_aware(ch.expires_at) <= utcnow():  # type: ignore[operator]
        raise INVALID_CODE
    user = db.get(User, ch.user_id)
    if user is None or not user.is_active or not user.totp_enabled or user.totp_secret_enc is None:
        raise INVALID_CODE
    limit_account(user.email)
    if _is_locked(user):
        raise INVALID_CODE
    ch.attempts += 1
    step = verify_totp(decrypt_secret(user.totp_secret_enc), body.code, user.totp_last_step)
    if step is None:
        if ch.attempts >= s.login_challenge_max_attempts:
            ch.used = True  # burn the challenge; user must re-enter the password
        _register_failure(db, user)
        raise INVALID_CODE
    ch.used = True
    user.totp_last_step = step
    user.failed_login_count = 0
    db.commit()
    _device_gate(db, user, _request_hwid(request, body.hwid))
    return _issue_tokens(db, user, request)


# ---------------------------------------------------------------------------
# 2FA management
# ---------------------------------------------------------------------------

@router.post("/2fa/setup", response_model=TwoFASetupOut)
def twofa_setup(body: TwoFASetupIn | None = None, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> TwoFASetupOut:
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled.")
    if body and body.existing_secret:
        try:
            secret = normalize_b32(body.existing_secret)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="existing_secret is not valid base32.") from None
        if len(secret) < 26:  # < 128 bits
            raise HTTPException(status_code=422, detail="existing_secret is too short.")
    else:
        secret = generate_totp_secret()
    user.totp_pending_secret_enc = encrypt_secret(secret)
    db.commit()
    return TwoFASetupOut(secret=secret, otpauth_uri=provisioning_uri(secret, user.email))


@router.post("/2fa/enable", response_model=MessageOut)
def twofa_enable(body: TwoFAEnableIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MessageOut:
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled.")
    if user.totp_pending_secret_enc is None:
        raise HTTPException(status_code=400, detail="Call /auth/2fa/setup first.")
    limit_account(f"2fa:{user.id}")
    secret = decrypt_secret(user.totp_pending_secret_enc)
    step = verify_totp(secret, body.code, None)
    if step is None:
        raise INVALID_CODE
    user.totp_secret_enc = user.totp_pending_secret_enc
    user.totp_pending_secret_enc = None
    user.totp_enabled = True
    user.totp_last_step = step
    user.token_version += 1  # security-relevant change: kill other sessions' access tokens
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
    db.commit()
    return MessageOut(message="Two-factor authentication enabled. Please log in again.")


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    invalid = HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    if row is None:
        raise invalid
    if row.revoked:
        # Reuse of a rotated token => likely theft. Revoke the whole session family.
        db.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id).values(revoked=True))
        db.commit()
        raise invalid
    if as_aware(row.expires_at) <= utcnow():  # type: ignore[operator]
        row.revoked = True
        db.commit()
        raise invalid
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid
    hwid = _request_hwid(request, None)
    if hwid is None and _hwid_required(user):
        raise DEVICE_ID_REQUIRED
    if _device_mismatch(user, hwid):  # session token carried to another PC: kill the session
        db.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id).values(revoked=True))
        db.commit()
        raise invalid
    row.revoked = True
    out = _issue_tokens(db, user, request, family_id=row.family_id)
    new_row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(out.refresh_token)))
    row.replaced_by = new_row.id if new_row else None
    db.commit()
    return out


@router.post("/logout", response_model=MessageOut)
def logout(body: LogoutIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MessageOut:
    if body.all_sessions:
        db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
        user.token_version += 1
    elif body.refresh_token:
        row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token),
                                                   RefreshToken.user_id == user.id))
        if row is not None:
            db.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id).values(revoked=True))
    db.commit()
    return MessageOut(message="Logged out.")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user


# ---------------------------------------------------------------------------
# device reset (move the account to a new PC)
# ---------------------------------------------------------------------------

@router.post("/device/reset-request", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def device_reset_request(body: DeviceResetRequestIn, request: Request, db: Session = Depends(get_db),
                         creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer)) -> MessageOut:
    """Email a 6-digit code that moves the device binding to ``new_hwid`` (the requesting PC).

    Auth: email+password, or a valid Bearer token. Always answers with the same generic message.
    """
    s = get_settings()
    new_hwid = _request_hwid(request, body.new_hwid)
    if new_hwid is None:
        raise HTTPException(status_code=422, detail="new_hwid (or X-Device-Id) is required.")
    user: User | None = None
    if body.email is not None or body.password is not None:
        if not (body.email and body.password):
            raise HTTPException(status_code=422, detail="Send both email and password.")
        email = _norm_email(body.email)
        limit_account(f"devreset:{email}")
        cand = db.scalar(select(User).where(User.email == email))
        ok = verify_password(cand.password_hash if cand else None, body.password)  # uniform timing
        if cand is not None and cand.is_active and not _is_locked(cand):
            if ok:
                user = cand
            else:
                _register_failure(db, cand)  # counts towards the normal login lockout
    elif creds is not None and creds.scheme.lower() == "bearer":
        claims = decode_access_token(creds.credentials)
        cand = db.get(User, claims["sub"]) if claims else None
        if cand is None or not cand.is_active or cand.token_version != (claims or {}).get("ver"):
            raise NOT_AUTH
        limit_account(f"devreset:{cand.email}")
        user = cand
    else:
        raise NOT_AUTH

    if user is None or not user.email_verified or user.device_hwid_hash is None:
        return MessageOut(message=DEVICE_RESET_MSG)
    if not limiter.hit(f"devreset-mail:{user.id}", s.device_reset_emails_per_hour, 3600):
        raise TOO_MANY
    code = new_reset_code()
    user.device_reset_code_hash = hash_reset_code(user.id, code)
    user.device_reset_expires = utcnow() + timedelta(minutes=s.device_reset_ttl_minutes)
    user.device_reset_new_hwid_hash = hash_hwid(new_hwid)
    user.device_reset_attempts = 0
    db.commit()
    send_device_reset_email(user.email, code, s.device_reset_ttl_minutes)
    return MessageOut(message=DEVICE_RESET_MSG)


@router.post("/device/reset-confirm", response_model=MessageOut)
def device_reset_confirm(body: DeviceResetConfirmIn, request: Request, db: Session = Depends(get_db)) -> MessageOut:
    """Check the emailed code; bind ``new_hwid`` and end every existing session (log out everywhere)."""
    s = get_settings()
    email = _norm_email(body.email)
    limit_account(f"devreset:{email}")
    new_hwid = _request_hwid(request, body.new_hwid)
    code = body.code.strip().replace(" ", "")
    user = db.scalar(select(User).where(User.email == email))
    if (user is None or not user.is_active or new_hwid is None
            or user.device_reset_code_hash is None or user.device_reset_expires is None):
        raise INVALID_CODE
    if (as_aware(user.device_reset_expires) <= utcnow()  # type: ignore[operator]
            or (user.device_reset_attempts or 0) >= s.device_reset_max_attempts):
        _clear_device_reset(user)
        db.commit()
        raise INVALID_CODE
    code_ok = constant_time_eq(user.device_reset_code_hash, hash_reset_code(user.id, code))
    hwid_ok = constant_time_eq(user.device_reset_new_hwid_hash or "", hash_hwid(new_hwid))
    if not (code_ok and hwid_ok):
        user.device_reset_attempts = (user.device_reset_attempts or 0) + 1
        if user.device_reset_attempts >= s.device_reset_max_attempts:
            _clear_device_reset(user)  # burned: the user has to request a new code
        db.commit()
        raise INVALID_CODE
    user.device_hwid_hash = user.device_reset_new_hwid_hash
    user.device_bound_at = utcnow()
    _clear_device_reset(user)
    user.token_version += 1  # same as "log out everywhere": the old PC loses every session
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
    db.commit()
    return MessageOut(message="Device reset. This PC is now linked to your account. Sign in to continue.")
