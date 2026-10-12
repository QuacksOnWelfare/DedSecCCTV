from __future__ import annotations

import pytest

from app.config import get_settings
from app.minversion import APP_VERSION_HEADER, is_too_old, parse_version


@pytest.fixture
def minver():
    s = get_settings()
    old = s.min_app_version

    def set_(v: str) -> None:
        s.min_app_version = v
    yield set_
    s.min_app_version = old


@pytest.mark.parametrize("raw,expected", [
    ("1.0.2", (1, 0, 2, 0)), ("1.0", (1, 0, 0, 0)), ("v2.10.0", (2, 10, 0, 0)), (" 1.0.10 ", (1, 0, 10, 0)),
    ("", None), (None, None), ("1.0.2-beta", None), ("1..2", None), ("01.0", None), ("a.b.c", None),
    ("1.2.3.4.5", None), ("99999.0", None), ("1.0.2\n", (1, 0, 2, 0)), ("１.0", None), ("-1.0", None),
])
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


def test_compare_is_numeric_not_lexical():
    assert is_too_old("1.0.9", "1.0.10")
    assert not is_too_old("1.0.10", "1.0.9")
    assert not is_too_old("1.0", "1.0.0")
    assert not is_too_old("2.0.0", "1.9.99")
    assert is_too_old(None, "1.0.2") and is_too_old("junk", "1.0.2")
    assert not is_too_old(None, "") and not is_too_old("junk", "")


def _login(client, headers=None):
    return client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 12, "hwid": "a1" * 16},
                       headers=headers or {})


def test_unset_allows_no_header(client, minver):
    minver("")
    assert _login(client).status_code == 401  # normal auth failure, not 426


@pytest.mark.parametrize("hdr", [None, "1.0.1", "junk", "1.0.2-rc1", "x" * 500])
def test_426_when_too_old(client, minver, hdr):
    minver("1.0.2")
    r = _login(client, {APP_VERSION_HEADER: hdr} if hdr is not None else None)
    assert r.status_code == 426
    assert r.json()["detail"]["code"] == "update_required"
    assert r.json()["detail"]["min_version"] == "1.0.2"
    assert r.headers["x-error-code"] == "update_required"


@pytest.mark.parametrize("path,method", [("/auth/register", "post"), ("/auth/refresh", "post"),
                                         ("/auth/verify-email", "post"), ("/auth/me", "get"),
                                         ("/auth/login/totp", "post"), ("/account/email-preferences", "get")])
def test_protected_endpoints(client, minver, path, method):
    minver("9.9.9")
    r = getattr(client, method)(path, json={}) if method == "post" else client.get(path)
    assert r.status_code == 426


def test_new_enough_passes(client, minver):
    minver("1.0.2")
    assert _login(client, {APP_VERSION_HEADER: "1.0.2"}).status_code == 401
    assert _login(client, {APP_VERSION_HEADER: "1.1"}).status_code == 401


def test_exempt_endpoints(client, minver):
    minver("9.9.9")
    assert client.get("/healthz").status_code == 200
    assert client.get("/auth/verify-email?token=bad").status_code != 426
    assert client.get("/email/unsubscribe?t=bad").status_code == 400
    assert client.get("/net/check").status_code != 426


def test_invalid_min_is_ignored(client, minver):
    minver("not-a-version")
    assert _login(client).status_code == 401
