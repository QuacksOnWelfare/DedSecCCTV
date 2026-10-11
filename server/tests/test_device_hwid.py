"""Hardware (device) lock: bind on first sign-in, refuse other PCs, emailed reset code flow."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.config import get_settings
from app.db import SessionLocal, add_missing_columns
from app.emailer import MemorySender
from app.models import User
from app.security import TOTP_STEP, hash_hwid, totp_now, utcnow

from .conftest import PC_A, PC_B
from .test_auth_flow import PASSWORD, _register_verified

B = {"X-Device-Id": PC_B}
MISMATCH = "This account is locked to another PC. Request a device reset."


def _login(client: TestClient, email: str, headers: dict | None = None, **extra):
    return client.post("/auth/login", json={"email": email, "password": PASSWORD, **extra}, headers=headers or {})


def _reset_code(outbox: MemorySender, email: str) -> str:
    mails = [m for m in outbox.outbox if m[0] == email and "device reset" in m[1].lower()]
    assert mails, "no device reset email"
    m = re.search(r"Your reset code: (\d{6})", mails[-1][2])
    assert m
    return m.group(1)


def _user(email: str) -> User:
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.email == email))


def test_first_login_binds_and_other_pc_is_refused(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    r = _login(client, email)
    assert r.status_code == 200 and r.json()["device_bound"] is True
    tok = r.json()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    assert me["device_bound"] is True
    u = _user(email)
    assert u.device_hwid_hash == hash_hwid(PC_A) and PC_A not in u.device_hwid_hash  # hash only
    assert u.device_bound_at is not None
    # other PC (header or body field) -> 403 with a clear message + machine-readable code
    r = _login(client, email, B)
    assert r.status_code == 403 and r.json()["detail"] == MISMATCH
    assert r.headers["x-error-code"] == "device_mismatch"
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD, "hwid": PC_B},
                    headers={"X-Device-Id": ""})
    assert r.status_code == 403
    # wrong password from the other PC: generic 401, binding not disclosed
    r = client.post("/auth/login", json={"email": email, "password": "Nope-nope-12345"}, headers=B)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."
    # same PC still fine; case-insensitive hex
    assert _login(client, email, {"X-Device-Id": PC_A.upper()}).status_code == 200
    # refresh token carried to another PC -> refused and the session family revoked
    assert client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]}, headers=B).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 401


def test_missing_and_malformed_device_id(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    r = _login(client, email, {"X-Device-Id": ""})
    assert r.status_code == 426  # old client without a device id
    for bad in ("xyz", "g" * 32, "a" * 31, "a" * 65):
        assert _login(client, email, {"X-Device-Id": bad}).status_code == 422, bad
        assert client.post("/auth/login", json={"email": email, "password": PASSWORD, "hwid": bad},
                           headers={"X-Device-Id": ""}).status_code == 422, bad
    assert _user(email).device_hwid_hash is None  # nothing bound by refused attempts
    tok = _login(client, email).json()
    assert client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]},
                       headers={"X-Device-Id": ""}).status_code == 426
    assert client.post("/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 200


def test_totp_flow_binds_after_second_factor(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    tok = _login(client, email).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    secret = client.post("/auth/2fa/setup", headers=auth, json={}).json()["secret"]
    assert client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(secret)}).status_code == 200
    # other PC is refused at the password step, before a code is spent
    assert _login(client, email, B).status_code == 403
    # a challenge started on PC A but answered from PC B is refused too
    ch = _login(client, email).json()["challenge_token"]
    code = totp_now(secret, time.time() + TOTP_STEP)
    r = client.post("/auth/login/totp", json={"challenge_token": ch, "code": code}, headers=B)
    assert r.status_code == 403
    ch = _login(client, email).json()["challenge_token"]
    # the code above was valid (then refused by the device gate) so its step is used up; allow reuse here
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == email)); u.totp_last_step = None; db.commit()
    r = client.post("/auth/login/totp", json={"challenge_token": ch, "code": code})
    assert r.status_code == 200 and r.json()["device_bound"] is True


def test_unbound_totp_user_binds_on_totp_step(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    tok = _login(client, email).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    secret = client.post("/auth/2fa/setup", headers=auth, json={}).json()["secret"]
    client.post("/auth/2fa/enable", headers=auth, json={"code": totp_now(secret)})
    with SessionLocal() as db:  # simulate an account created before the device lock existed
        u = db.scalar(select(User).where(User.email == email)); u.device_hwid_hash = None; db.commit()
    ch = _login(client, email, B).json()["challenge_token"]
    r = client.post("/auth/login/totp", json={"challenge_token": ch, "code": totp_now(secret, time.time() + TOTP_STEP)},
                    headers=B)
    assert r.status_code == 200
    assert _user(email).device_hwid_hash == hash_hwid(PC_B)


def test_device_reset_flow(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    old = _login(client, email).json()
    assert _login(client, email, B).status_code == 403
    # wrong password -> generic answer, no email
    n = len(outbox.outbox)
    r = client.post("/auth/device/reset-request", json={"email": email, "password": "Wrong-Passw0rd!!"}, headers=B)
    assert r.status_code == 202 and len(outbox.outbox) == n
    # unknown account -> same generic answer
    r2 = client.post("/auth/device/reset-request", json={"email": "nobody@example.com", "password": PASSWORD}, headers=B)
    assert r2.status_code == 202 and r2.json() == r.json()
    # correct -> code emailed (branded subject), only hashes stored
    r = client.post("/auth/device/reset-request", json={"email": email, "password": PASSWORD, "new_hwid": PC_B})
    assert r.status_code == 202
    mail = [m for m in outbox.outbox if m[0] == email][-1]
    assert mail[1] == "DedSec CCTV device reset code" and "15 minutes" in mail[2]
    code = _reset_code(outbox, email)
    u = _user(email)
    assert u.device_reset_code_hash and code not in u.device_reset_code_hash
    assert u.device_reset_new_hwid_hash == hash_hwid(PC_B)
    # confirm from a third PC (hwid differs from the requested one) -> refused
    r = client.post("/auth/device/reset-confirm", json={"email": email, "code": code, "new_hwid": "c3" * 16})
    assert r.status_code == 401
    bad = "000000" if code != "000000" else "111111"
    assert client.post("/auth/device/reset-confirm", json={"email": email, "code": bad}, headers=B).status_code == 401
    # correct
    r = client.post("/auth/device/reset-confirm", json={"email": email, "code": code}, headers=B)
    assert r.status_code == 200, r.text
    u = _user(email)
    assert u.device_hwid_hash == hash_hwid(PC_B) and u.device_reset_code_hash is None
    # code is single use
    assert client.post("/auth/device/reset-confirm", json={"email": email, "code": code}, headers=B).status_code == 401
    # old PC's sessions are gone (refresh + access token), new PC signs in, old PC is now refused
    assert client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old['access_token']}"}).status_code == 401
    assert _login(client, email, B).status_code == 200
    assert _login(client, email).status_code == 403


def test_reset_code_attempts_expiry_and_bearer(client: TestClient, outbox: MemorySender) -> None:
    s = get_settings()
    email = _register_verified(client, outbox)
    tok = _login(client, email).json()
    # authenticated variant (Bearer) works too
    r = client.post("/auth/device/reset-request", json={"new_hwid": PC_B},
                    headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code == 202
    code = _reset_code(outbox, email)
    for _ in range(s.device_reset_max_attempts):
        wrong = f"{(int(code) + 1) % 10**6:06d}"
        assert client.post("/auth/device/reset-confirm", json={"email": email, "code": wrong}, headers=B).status_code == 401
    # burned after max attempts, even with the right code
    assert client.post("/auth/device/reset-confirm", json={"email": email, "code": code}, headers=B).status_code == 401
    # expiry
    client.post("/auth/device/reset-request", json={"email": email, "password": PASSWORD}, headers=B)
    code = _reset_code(outbox, email)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == email))
        u.device_reset_expires = utcnow() - timedelta(seconds=1); db.commit()
    assert client.post("/auth/device/reset-confirm", json={"email": email, "code": code}, headers=B).status_code == 401
    # no auth at all -> 401; bogus bearer -> 401
    assert client.post("/auth/device/reset-request", json={"new_hwid": PC_B}).status_code == 401
    assert client.post("/auth/device/reset-request", json={"new_hwid": PC_B},
                       headers={"Authorization": "Bearer x.y.z"}).status_code == 401
    # per-account email cap (3/h): the 4th code request is rate limited
    r = client.post("/auth/device/reset-request", json={"email": email, "password": PASSWORD}, headers=B)
    assert r.status_code == 202
    r = client.post("/auth/device/reset-request", json={"email": email, "password": PASSWORD}, headers=B)
    assert r.status_code == 429


def test_unbound_account_gets_no_reset_mail(client: TestClient, outbox: MemorySender) -> None:
    email = _register_verified(client, outbox)
    n = len(outbox.outbox)
    r = client.post("/auth/device/reset-request", json={"email": email, "password": PASSWORD}, headers=B)
    assert r.status_code == 202 and len(outbox.outbox) == n


def test_migration_adds_columns_to_old_db(tmp_path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id VARCHAR(36) NOT NULL PRIMARY KEY, email VARCHAR(320) NOT NULL,
            password_hash VARCHAR(255) NOT NULL, email_verified BOOLEAN NOT NULL, is_active BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL, failed_login_count INTEGER NOT NULL, locked_until DATETIME,
            totp_enabled BOOLEAN NOT NULL, totp_secret_enc BLOB, totp_pending_secret_enc BLOB,
            totp_last_step INTEGER, token_version INTEGER NOT NULL);
        INSERT INTO users VALUES ('1', 'old@example.com', 'x', 1, 1, '2026-01-01', 0, NULL, 0, NULL, NULL, NULL, 0);
    """)
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{path}")
    added = add_missing_columns(eng)
    assert {"users.device_hwid_hash", "users.device_bound_at", "users.device_reset_code_hash",
            "users.device_reset_expires", "users.device_reset_new_hwid_hash",
            "users.device_reset_attempts"} <= set(added)
    assert add_missing_columns(eng) == []  # idempotent
    cols = {c["name"] for c in inspect(eng).get_columns("users")}
    assert "device_hwid_hash" in cols
    with eng.connect() as c:
        assert c.exec_driver_sql("SELECT email, device_hwid_hash, device_reset_attempts FROM users").one() == \
            ("old@example.com", None, 0)
