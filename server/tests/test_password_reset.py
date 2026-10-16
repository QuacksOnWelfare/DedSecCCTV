"""1.0.3 forgot password: emailed code -> emailed single-use link -> browser form."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.emailer import MemorySender
from app.minversion import APP_VERSION_HEADER
from app.models import PasswordReset, User
from app.ratelimit import limiter
from app.security import totp_now, utcnow

from .test_auth_flow import PASSWORD, _email, _register_verified

NEW_PW = "Brand-New-Passw0rd!x"
ORIGIN = "http://localhost:8000"  # PUBLIC_BASE_URL default in tests


def _mails(outbox: MemorySender, email: str, word: str) -> list:
    return [m for m in outbox.outbox if m[0] == email and word in m[1].lower()]


def _code(outbox: MemorySender, email: str) -> str:
    mails = _mails(outbox, email, "password reset code")
    assert mails, "no reset code email"
    return re.search(r"Your code: (\d{6})", mails[-1][2]).group(1)


def _link_token(outbox: MemorySender, email: str) -> str:
    mails = _mails(outbox, email, "reset your")
    assert mails, "no reset link email"
    m = re.search(r"/account/reset-password\?t=([A-Za-z0-9_-]+)", mails[-1][2])
    assert m
    return m.group(1)


def _forgot(client: TestClient, email: str):
    return client.post("/auth/password/forgot", json={"email": email})


def _verify(client: TestClient, email: str, code: str):
    return client.post("/auth/password/verify-code", json={"email": email, "code": code})


def _open_form(client: TestClient, token: str):
    r = client.get(f"/account/reset-password?t={token}")
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text)
    return r, (csrf.group(1) if csrf else "")


def _submit(client: TestClient, token: str, csrf: str, pw: str = NEW_PW, pw2: str | None = None,
            origin: str | None = ORIGIN):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = origin
    data = {"t": token, "csrf": csrf, "password": pw, "confirm": pw if pw2 is None else pw2}
    return client.post("/account/reset-password", content="&".join(
        f"{k}={__import__('urllib.parse').parse.quote(v)}" for k, v in data.items()), headers=headers)


def test_full_reset_flow_revokes_sessions(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    old = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    r = _forgot(client, email)
    assert r.status_code == 202
    code = _code(outbox, email)
    assert not _mails(outbox, email, "reset your")  # no link before the code is confirmed
    r = _verify(client, email, code)
    assert r.status_code == 200, r.text
    token = _link_token(outbox, email)
    assert _verify(client, email, code).status_code == 401  # code is single-use

    r, csrf = _open_form(client, token)
    assert r.status_code == 200 and "Choose a new password" in r.text and csrf
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self'" in csp and "unsafe-inline" not in csp
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/account/reset-password" in cookie
    assert _open_form(client, token)[0].status_code == 200  # GET is side-effect free (link scanners)

    r = _submit(client, token, csrf)
    assert r.status_code == 200 and "Password changed" in r.text, r.text
    # old password and old sessions are dead, the new password works
    assert client.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old['access_token']}"}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": NEW_PW}).status_code == 200
    assert _mails(outbox, email, "password was changed")
    # the link is single-use
    _r, csrf2 = _open_form(client, token)
    assert _r.status_code == 400 and "expired" in _r.text
    assert _submit(client, token, csrf or "x").status_code in (400, 403)  # cookie is gone too


def test_no_account_enumeration(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    ghost = _email()
    a, b = _forgot(client, email), _forgot(client, ghost)
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert not [m for m in outbox.outbox if m[0] == ghost]
    # wrong code for a real and a non-existent account look identical
    a, b = _verify(client, email, "000000"), _verify(client, ghost, "000000")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_attempt_limit_burns_code(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    _forgot(client, email)
    code = _code(outbox, email)
    wrong = "111111" if code != "111111" else "222222"
    for _ in range(get_settings().password_reset_max_attempts):
        assert _verify(client, email, wrong).status_code == 401
    assert _verify(client, email, code).status_code == 401  # burned even with the right code
    assert not _mails(outbox, email, "reset your")


def test_code_expires_and_new_request_supersedes(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    _forgot(client, email)
    first = _code(outbox, email)
    _forgot(client, email)
    second = _code(outbox, email)
    if first != second:
        assert _verify(client, email, first).status_code == 401  # only the newest code is valid
    with SessionLocal() as db:
        row = db.scalar(select(PasswordReset).join(User).where(User.email == email, PasswordReset.used.is_(False)))
        row.code_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert _verify(client, email, second).status_code == 401


def test_link_expires(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    _forgot(client, email)
    assert _verify(client, email, _code(outbox, email)).status_code == 200
    token = _link_token(outbox, email)
    _r, csrf = _open_form(client, token)
    with SessionLocal() as db:
        row = db.scalar(select(PasswordReset).join(User).where(User.email == email, PasswordReset.used.is_(False)))
        row.link_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert _submit(client, token, csrf).status_code == 400
    assert client.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200


def test_csrf_and_password_rules(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    _forgot(client, email)
    _verify(client, email, _code(outbox, email))
    token = _link_token(outbox, email)
    _r, csrf = _open_form(client, token)
    assert _submit(client, token, csrf, origin="https://evil.example").status_code == 403
    assert _submit(client, token, "forged-csrf-value").status_code == 403
    jar = client.cookies
    saved = jar.get("dsr_csrf", path="/account/reset-password")
    jar.clear()
    assert _submit(client, token, csrf).status_code == 403  # no cookie (cross-site POST)
    jar.set("dsr_csrf", saved, path="/account/reset-password")
    r = _submit(client, token, csrf, pw="short")
    assert r.status_code == 422 and "at least 12 characters" in r.text
    r = _submit(client, token, csrf, pw=NEW_PW, pw2=NEW_PW + "x")
    assert r.status_code == 422 and "don&#x27;t match" in r.text
    local = email.split("@")[0]
    r = _submit(client, token, csrf, pw=f"Xx9!{local}Yy")
    assert r.status_code == 422 and "email name" in r.text
    # weak attempts did not burn the link
    assert _submit(client, token, csrf).status_code == 200


def test_2fa_codes_are_not_accepted(client: TestClient, outbox: MemorySender) -> None:
    """An authenticator code is never proof for a reset; only the emailed code is."""
    email = _register_verified(client, outbox)
    tok = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    secret = client.post("/auth/2fa/setup", headers=auth, json={}).json()["secret"]
    assert client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(secret)}).status_code == 200
    _forgot(client, email)
    code = _code(outbox, email)
    totp = totp_now(secret)
    if totp != code:
        assert _verify(client, email, totp).status_code == 401
    assert _verify(client, email, code).status_code == 200
    with pytest.raises(Exception):
        client.post("/auth/password/verify-code", json={"email": email, "code": code, "totp": totp}).raise_for_status()


def test_forgot_rate_limited_uniformly(client: TestClient, outbox: MemorySender) -> None:
    s = get_settings()
    email, ghost = _register_verified(client, outbox), _email()
    for addr in (email, ghost):
        codes = [_forgot(client, addr).status_code for _ in range(s.password_reset_requests_per_hour + 1)]
        assert codes[:-1] == [202] * s.password_reset_requests_per_hour and codes[-1] == 429
        if addr == email:  # at most PASSWORD_RESET_EMAILS_PER_HOUR code emails reached the real account
            assert len(_mails(outbox, email, "password reset code")) == s.password_reset_emails_per_hour
        limiter.reset()  # per-IP cap (10/h) is shared by both loops
    for i in range(11):  # per-IP cap across different addresses
        r = _forgot(client, _email())
    assert r.status_code == 429
    limiter.reset()


def test_unverified_account_can_reset_and_becomes_verified(client: TestClient, outbox: MemorySender) -> None:
    email = _email()
    client.post("/auth/register", json={"email": email, "password": PASSWORD})
    _forgot(client, email)
    assert _verify(client, email, _code(outbox, email)).status_code == 200
    token = _link_token(outbox, email)
    _r, csrf = _open_form(client, token)
    assert _submit(client, token, csrf).status_code == 200
    assert client.post("/auth/login", json={"email": email, "password": NEW_PW}).status_code == 200


def test_reset_page_has_no_version_gate(client: TestClient, outbox: MemorySender) -> None:
    s = get_settings()
    old, s.min_app_version = s.min_app_version, "9.9.9"
    try:
        with TestClient(client.app) as browser:  # no app headers at all
            assert browser.get("/account/reset-password?t=" + "x" * 43).status_code == 400
            assert browser.post("/auth/password/forgot", json={"email": _email()}).status_code == 426
        assert client.post("/auth/password/forgot", json={"email": _email()},
                           headers={APP_VERSION_HEADER: "9.9.9"}).status_code == 202
    finally:
        s.min_app_version = old
