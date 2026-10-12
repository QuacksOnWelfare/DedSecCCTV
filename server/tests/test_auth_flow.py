from __future__ import annotations

import re
import time
import uuid

from fastapi.testclient import TestClient

from app.config import get_settings
from app.emailer import MemorySender
from app.ratelimit import limiter
from app.security import TOTP_STEP, totp_now, verify_totp

PASSWORD = "Correct-Horse-Battery-9"


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:8]}@example.com"


def _token_from(outbox: MemorySender, email: str) -> str:
    body = [m for m in outbox.outbox if m[0] == email][-1][2]
    m = re.search(r"Your code: (\S+)", body)
    assert m
    return m.group(1)


def _register_verified(client: TestClient, outbox: MemorySender) -> str:
    email = _email()
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 202, r.text
    r = client.post("/auth/verify-email", json={"token": _token_from(outbox, email)})
    assert r.status_code == 200, r.text
    return email


def test_full_flow(client: TestClient, outbox: MemorySender) -> None:
    """register -> verify -> login -> me -> 2fa setup/enable -> login+totp -> refresh -> me -> logout."""
    email = _email()
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 202
    # unverified login is refused
    assert client.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 403
    r = client.post("/auth/verify-email", json={"token": _token_from(outbox, email)})
    assert r.status_code == 200

    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    tok = r.json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    me = client.get("/auth/me", headers=auth).json()
    assert me["email"] == email and me["totp_enabled"] is False

    r = client.post("/auth/2fa/setup", headers=auth, json={})
    assert r.status_code == 200
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/DedSecCCTV:")
    assert "issuer=DedSecCCTV" in r.json()["otpauth_uri"]
    assert client.post("/auth/2fa/enable", headers=auth, json={"code": "000000"}).status_code in (401,)
    r = client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(secret)})
    assert r.status_code == 200, r.text
    # enabling 2FA invalidates old access + refresh tokens
    assert client.get("/auth/me", headers=auth).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 401

    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["status"] == "totp_required"
    challenge = r.json()["challenge_token"]
    # the code used for enabling can't be replayed: use the next window's code
    code = totp_now(secret, time.time() + TOTP_STEP)
    r = client.post("/auth/login/totp", json={"challenge_token": challenge, "code": code})
    assert r.status_code == 200, r.text
    tok = r.json()
    # challenge is single-use
    assert client.post("/auth/login/totp", json={"challenge_token": challenge, "code": code}).status_code == 401

    r = client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 200
    tok2 = r.json()
    assert tok2["refresh_token"] != tok["refresh_token"]
    auth = {"Authorization": f"Bearer {tok2['access_token']}"}
    assert client.get("/auth/me", headers=auth).json()["totp_enabled"] is True

    r = client.post("/auth/logout", headers=auth, json={"refresh_token": tok2["refresh_token"]})
    assert r.status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": tok2["refresh_token"]}).status_code == 401


def test_refresh_reuse_revokes_family(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    t1 = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    t2 = client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    # attacker replays the old token -> whole family revoked, including t2
    assert client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401


def test_no_user_enumeration(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    a = client.post("/auth/login", json={"email": email, "password": "Wrong-Password-123"})
    b = client.post("/auth/login", json={"email": _email(), "password": "Wrong-Password-123"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    r1 = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    r2 = client.post("/auth/register", json={"email": _email(), "password": PASSWORD})
    assert r1.status_code == r2.status_code == 202 and r1.json() == r2.json()


def test_lockout(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    for _ in range(get_settings().max_failed_logins):
        assert client.post("/auth/login", json={"email": email, "password": "Nope-Nope-1234"}).status_code == 401
    # correct password is now refused (same generic error)
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."


def test_weak_password_rejected(client: TestClient) -> None:
    for pw in ["short", "password1234", "alllowercaseletters"]:
        assert client.post("/auth/register", json={"email": _email(), "password": pw}).status_code == 422


def test_ip_rate_limit(client: TestClient) -> None:
    limiter.reset()
    s = get_settings()
    old = s.rate_limit_ip_per_minute
    s.rate_limit_ip_per_minute = 3
    try:
        codes = [client.post("/auth/login", json={"email": _email(), "password": "x"}).status_code for _ in range(5)]
        assert codes[-1] == 429
    finally:
        s.rate_limit_ip_per_minute = old
        limiter.reset()


def test_security_headers_and_bad_tokens(client: TestClient) -> None:
    r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" in r.headers
    # alg=none token must be rejected
    import base64, json
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).rstrip(b"=").decode()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {hdr}.{body}."}).status_code == 401
    assert client.get("/auth/me", headers={"Host": "evil.example"}).status_code == 400


def test_totp_matches_app_scheme() -> None:
    # RFC 6238 SHA1 test vector (secret "12345678901234567890", T=59 -> 94287082, 6-digit = 287082)
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_now(secret, 59) == "287082"
    assert verify_totp(secret, "287082", None, at=59) is not None
    assert verify_totp(secret, "287082", 1, at=59) is None  # replay rejected


def test_import_existing_app_secret(client: TestClient, outbox: MemorySender) -> None:
    import base64, os
    legacy = base64.b32encode(os.urandom(20)).decode()  # exactly how app.py generates it
    email = _register_verified(client, outbox)
    tok = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    r = client.post("/auth/2fa/setup", headers=auth, json={"existing_secret": legacy})
    assert r.status_code == 200 and r.json()["secret"] == legacy.rstrip("=")
    assert client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(legacy)}).status_code == 200
