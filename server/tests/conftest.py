from __future__ import annotations

import base64
import os
import secrets
import tempfile
from collections.abc import Iterator

import pytest

_tmp = tempfile.mkdtemp()
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "JWT_SECRET": secrets.token_urlsafe(64),
    "TOTP_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "EMAIL_BACKEND": "memory",
    "RATE_LIMIT_IP_PER_MINUTE": "1000",
    "RATE_LIMIT_ACCOUNT_PER_15MIN": "1000",
})

from fastapi.testclient import TestClient  # noqa: E402

from app.emailer import MemorySender, get_sender  # noqa: E402
from app.main import app  # noqa: E402
from app.ratelimit import limiter  # noqa: E402


PC_A = "a1" * 16   # 32-hex device id of the default test PC
PC_B = "b2" * 16


@pytest.fixture()
def client() -> Iterator[TestClient]:
    limiter.reset()
    # every request comes from one "PC" (device lock); device tests override the header
    with TestClient(app, headers={"X-Device-Id": PC_A}) as c:
        yield c


@pytest.fixture()
def outbox() -> MemorySender:
    sender = get_sender()
    assert isinstance(sender, MemorySender)
    return sender
