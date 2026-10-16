"""1.0.3 email two-factor: settings switch, sign-in codes, limits; the authenticator flow is unchanged."""
from __future__ import annotations

import re
import time
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.emailer import MemorySender
from app.models import LoginChallenge, User
from app.security import TOTP_STEP, totp_now, utcnow

from .test_auth_flow import PASSWORD, _register_verified


def _last_code(outbox: MemorySender, email: str, word: str) -> str:
    mails = [m for m in outbox.outbox if m[0] == email and word in m[1].lower()]
    assert mails, f"no '{word}' email"
    return re.search(r"Your code: (\d{6})", mails[-1][2]).group(1)


def _login(client: TestClient, email: str):
    return client.post("/auth/login", json={"email": email, "password": PASSWORD})


def _auth(client: TestClient, email: str) -> dict:
    tok = _login(client, email).json()
    assert tok["status"] == "ok", tok
    return {"Authorization": f"Bearer {tok['access_token']}"}


def _enable_email_2fa(client: TestClient, outbox: MemorySender, email: str, auth: dict, **extra) -> None:
    assert client.post("/auth/2fa/email/send", headers=auth, json={"purpose": "enable"}).status_code == 202
    code = _last_code(outbox, email, "security code")
    r = client.post("/auth/2fa/email/enable", headers=auth, json={"code": code, **extra})
    assert r.status_code == 200, r.text


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_enable_and_sign_in_with_email_code(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    auth = _auth(client, email)
    assert client.get("/auth/me", headers=auth).json()["twofa_method"] == "none"
    _enable_email_2fa(client, outbox, email, auth)
    assert client.get("/auth/me", headers=auth).status_code == 401  # sessions ended
    r = _login(client, email)
    body = r.json()
    assert body["status"] == "totp_required" and body["method"] == "email"
    code = _last_code(outbox, email, "sign-in code")
    assert client.post("/auth/login/totp", json={"challenge_token": body["challenge_token"],
                                                 "code": _wrong(code)}).status_code == 401
    r = client.post("/auth/login/totp", json={"challenge_token": body["challenge_token"], "code": code})
    assert r.status_code == 200, r.text
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["email_2fa_enabled"] is True and me["totp_enabled"] is False and me["twofa_method"] == "email"
    # the challenge is single-use
    assert client.post("/auth/login/totp", json={"challenge_token": body["challenge_token"],
                                                 "code": code}).status_code == 401


def test_email_code_attempts_expiry_and_resend(client: TestClient, outbox: MemorySender) -> None:
    s = get_settings()
    email = _register_verified(client, outbox)
    _enable_email_2fa(client, outbox, email, _auth(client, email))
    ch = _login(client, email).json()["challenge_token"]
    code = _last_code(outbox, email, "sign-in code")
    for _ in range(s.email_2fa_max_attempts):
        client.post("/auth/login/totp", json={"challenge_token": ch, "code": _wrong(code)})
    assert client.post("/auth/login/totp", json={"challenge_token": ch, "code": code}).status_code == 401
    with SessionLocal() as db:  # undo the lockout the wrong guesses caused
        u = db.scalar(select(User).where(User.email == email))
        u.locked_until, u.failed_login_count = None, 0
        db.commit()
    ch = _login(client, email).json()["challenge_token"]
    first = _last_code(outbox, email, "sign-in code")
    r = client.post("/auth/login/email-code", json={"challenge_token": ch})
    assert r.status_code == 200
    second = _last_code(outbox, email, "sign-in code")
    if first != second:
        assert client.post("/auth/login/totp", json={"challenge_token": ch, "code": first}).status_code == 401
    with SessionLocal() as db:
        row = db.scalar(select(LoginChallenge).where(LoginChallenge.used.is_(False), LoginChallenge.method == "email")
                        .order_by(LoginChallenge.id.desc()))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.post("/auth/login/totp", json={"challenge_token": ch, "code": second}).status_code == 401
    assert client.post("/auth/login/email-code", json={"challenge_token": ch}).status_code == 401


def test_email_quota(client: TestClient, outbox: MemorySender) -> None:
    s = get_settings()
    email = _register_verified(client, outbox)
    auth = _auth(client, email)
    codes = [client.post("/auth/2fa/email/send", headers=auth, json={"purpose": "enable"}).status_code
             for _ in range(s.email_2fa_emails_per_hour + 1)]
    assert codes[-1] == 429 and codes[:-1] == [202] * s.email_2fa_emails_per_hour


def test_switch_from_authenticator_needs_app_code(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    auth = _auth(client, email)
    secret = client.post("/auth/2fa/setup", headers=auth, json={}).json()["secret"]
    assert client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(secret)}).status_code == 200
    ch = _login(client, email).json()
    assert ch["method"] == "totp"
    now = time.time()
    tok = client.post("/auth/login/totp", json={"challenge_token": ch["challenge_token"],
                                                "code": totp_now(secret, now + TOTP_STEP)}).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    with SessionLocal() as db:  # forget the used step so a code from the current window counts again
        u = db.scalar(select(User).where(User.email == email))
        u.totp_last_step = None
        db.commit()
    assert client.post("/auth/2fa/email/send", headers=auth, json={"purpose": "enable"}).status_code == 202
    code = _last_code(outbox, email, "security code")
    r = client.post("/auth/2fa/email/enable", headers=auth, json={"code": code})
    assert r.status_code == 401 and r.headers.get("x-error-code") == "totp_required"
    r = client.post("/auth/2fa/email/enable", headers=auth,
                    json={"code": code, "totp_code": totp_now(secret, now)})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == email))
        assert u.email_2fa_enabled and not u.totp_enabled and u.totp_secret_enc is None


def test_disable_and_switch_back_to_authenticator(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    _enable_email_2fa(client, outbox, email, _auth(client, email))

    def signed_in() -> dict:
        ch = _login(client, email).json()["challenge_token"]
        tok = client.post("/auth/login/totp", json={"challenge_token": ch,
                                                    "code": _last_code(outbox, email, "sign-in code")}).json()
        return {"Authorization": f"Bearer {tok['access_token']}"}

    auth = signed_in()
    # an enable-code can't be used to disable
    assert client.post("/auth/2fa/email/send", headers=auth, json={"purpose": "disable"}).status_code == 202
    code = _last_code(outbox, email, "security code")
    assert client.post("/auth/2fa/email/disable", headers=auth, json={"code": _wrong(code)}).status_code == 401
    assert client.post("/auth/2fa/email/disable", headers=auth, json={"code": code}).status_code == 200
    assert _login(client, email).json()["status"] == "ok"
    # back on email, then switch to the authenticator: email codes stop
    _enable_email_2fa(client, outbox, email, _auth(client, email))
    auth = signed_in()
    secret = client.post("/auth/2fa/setup", headers=auth, json={}).json()["secret"]
    assert client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(secret)}).status_code == 200
    assert _login(client, email).json()["method"] == "totp"
