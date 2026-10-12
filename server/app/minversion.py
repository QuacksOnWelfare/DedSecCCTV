"""Server-enforced minimum desktop-app version.

Clients send ``X-DedSec-App-Version: x.y.z``. When MIN_APP_VERSION is set (env file), any
request on a protected endpoint whose version is missing, malformed or lower gets HTTP 426
``{"detail": {"code": "update_required", "min_version": "x.y.z", ...}}``. Unset/empty
MIN_APP_VERSION disables the check entirely (pre-1.0.2 clients send no header).
"""
from __future__ import annotations

import logging
import re

from fastapi import HTTPException, Request

from .config import get_settings

APP_VERSION_HEADER = "X-DedSec-App-Version"
UPDATE_REQUIRED_STATUS = 426
UPDATE_REQUIRED_MSG = "This version of DedSec CCTV is out of date. Please update to keep using the app."

_VERSION_RE = re.compile(r"^(0|[1-9][0-9]{0,3})(\.(0|[1-9][0-9]{0,3})){0,3}$")
log = logging.getLogger("dedsec.minversion")

# Public / browser endpoints that must keep working for any client (or no client at all).
_EXEMPT: frozenset[tuple[str, str]] = frozenset({("GET", "/auth/verify-email")})


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """'1.0.2' -> (1, 0, 2). Anything that isn't 1-4 dot-separated small integers -> None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    if len(v) > 32 or not _VERSION_RE.match(v):
        return None
    parts = tuple(int(p) for p in v.split("."))
    return parts + (0,) * (4 - len(parts))  # 1.0 == 1.0.0


def min_version() -> str:
    """The configured minimum ('' = no minimum). An invalid value is ignored (logged) rather than
    locking every user out."""
    raw = (get_settings().min_app_version or "").strip()
    if raw and parse_version(raw) is None:
        log.warning("MIN_APP_VERSION=%r is not a valid x.y.z version; minimum version check disabled", raw[:40])
        return ""
    return raw


def is_too_old(client_version: str | None, minimum: str) -> bool:
    if not minimum:
        return False
    need = parse_version(minimum)
    if need is None:
        return False
    have = parse_version(client_version)
    return have is None or have < need


def require_app_version(request: Request) -> None:
    """FastAPI dependency: 426 when the client is older than MIN_APP_VERSION."""
    minimum = min_version()
    if not minimum or (request.method.upper(), request.url.path) in _EXEMPT:
        return
    sent = request.headers.get(APP_VERSION_HEADER)
    if not is_too_old(sent, minimum):
        return
    shown = "missing" if sent is None else repr("".join(c for c in sent[:20] if c.isprintable()))
    log.warning("app_version_rejected path=%s version=%s min=%s", request.url.path, shown, minimum)
    raise HTTPException(
        status_code=UPDATE_REQUIRED_STATUS,
        detail={"code": "update_required", "min_version": minimum, "message": UPDATE_REQUIRED_MSG},
        headers={"X-Error-Code": "update_required"},
    )
