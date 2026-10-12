"""GET /net/check: does the caller's connection look like a VPN/datacenter or a home ISP?

No auth. Rate-limited by the shared per-IP limiter. The client IP comes from client_ip(),
which behind your own reverse proxy (TRUST_PROXY_HEADERS=true) is the client IP the proxy
writes into X-Forwarded-For (see deploy/Caddyfile.example). The IP is never returned, never logged here, and only kept in an in-memory
1-hour cache (keyed by a salted hash) so repeat checks don't hit the upstream lookup.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import threading
import time
import urllib.request

from fastapi import APIRouter, Depends, Request

from ..ratelimit import client_ip, limit_ip

router = APIRouter(prefix="/net", tags=["net"], dependencies=[Depends(limit_ip)])

_TTL = 3600.0
_MAX = 50_000
_SALT = os.urandom(16)  # per-process; cache keys are not reversible to IPs
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()
UNKNOWN = {"vpn": None, "isp_type": "unknown", "country": None}


def _key(ip: str) -> str:
    return hashlib.sha256(_SALT + ip.encode()).hexdigest()


def _lookup(ip: str) -> dict:
    # ip-api.com free tier: no key, HTTP only, 45 req/min. Only the needed fields are requested.
    url = f"http://ip-api.com/json/{ip}?fields=status,countryCode,proxy,hosting,mobile"
    req = urllib.request.Request(url, headers={"User-Agent": "dedsec-auth/1.0"})
    with urllib.request.urlopen(req, timeout=4) as r:  # noqa: S310 (fixed host)
        d = json.loads(r.read(4096).decode("utf-8", "replace"))
    if d.get("status") != "success":
        return dict(UNKNOWN)
    vpn = bool(d.get("proxy")) or bool(d.get("hosting"))
    cc = d.get("countryCode")
    return {"vpn": vpn, "isp_type": "hosting" if vpn else "residential",
            "country": cc if isinstance(cc, str) and len(cc) == 2 else None}


@router.get("/check")
def net_check(request: Request) -> dict:
    ip = client_ip(request)
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return dict(UNKNOWN)
    if not addr.is_global:
        return dict(UNKNOWN)
    k, now = _key(ip), time.monotonic()
    with _lock:
        hit = _cache.get(k)
        if hit and now - hit[0] < _TTL:
            return dict(hit[1])
    try:
        res = _lookup(ip)
    except Exception:  # upstream down / timeout / rate-limited: say unknown, don't cache
        return dict(UNKNOWN)
    with _lock:
        if len(_cache) >= _MAX:
            for kk in [kk for kk, (t, _) in _cache.items() if now - t >= _TTL]:
                del _cache[kk]
            if len(_cache) >= _MAX:
                _cache.clear()
        _cache[k] = (now, res)
    return dict(res)
