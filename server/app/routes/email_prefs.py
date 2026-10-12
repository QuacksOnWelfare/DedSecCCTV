"""Product-update email consent: authenticated preference API + public one-click unsubscribe (RFC 8058)."""
from __future__ import annotations

import html
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..email_prefs import ensure_token, token_hash
from ..minversion import require_app_version
from ..models import User
from ..ratelimit import limit_ip
from ..security import utcnow

router = APIRouter(dependencies=[Depends(limit_ip)])

def _img_base() -> str:
    base = get_settings().email_asset_base
    return base if base.endswith("/") else base + "/"


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src https:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


class EmailPrefsOut(BaseModel):
    email_updates: bool


class EmailPrefsIn(BaseModel):
    email_updates: bool


@router.get("/account/email-preferences", response_model=EmailPrefsOut, tags=["account"], dependencies=[Depends(require_app_version)])
def get_prefs(user: User = Depends(current_user)) -> EmailPrefsOut:
    return EmailPrefsOut(email_updates=bool(user.email_updates))


@router.put("/account/email-preferences", response_model=EmailPrefsOut, tags=["account"], dependencies=[Depends(require_app_version)])
def put_prefs(body: EmailPrefsIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> EmailPrefsOut:
    user = db.get(User, user.id)
    if bool(user.email_updates) != body.email_updates:
        user.email_updates = body.email_updates
        user.email_updates_changed_at = utcnow()
    ensure_token(user)
    db.commit()
    return EmailPrefsOut(email_updates=bool(user.email_updates))


def _page(title: str, msg: str, form_token: str | None = None, status: int = 200) -> HTMLResponse:
    form = ""
    if form_token:
        form = (f'<form method="post" action="/email/unsubscribe?t={html.escape(form_token)}" style="margin:24px 0 0">'
                '<button type="submit" style="background:#00b3b3;color:#0b0f12;border:0;border-radius:6px;'
                'padding:12px 22px;font-weight:700;font-size:15px;cursor:pointer">Confirm unsubscribe</button></form>')
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)} - DedSec CCTV</title></head>
<body style="margin:0;background:#0b0f12;color:#d7e3e6;font-family:Segoe UI,Helvetica,Arial,sans-serif">
<div style="max-width:520px;margin:64px auto;padding:32px;background:#121a1f;border:1px solid #1f2d33;border-top:4px solid #00b3b3;border-radius:8px">
<img src="{html.escape(_img_base())}dedsec_logo.png" width="56" height="56" alt="DedSec Lab" style="display:block;margin:0 0 16px">
<h1 style="margin:0 0 12px;font-size:22px;color:#ffffff">{html.escape(title)}</h1>
<p style="margin:0;line-height:1.6;font-size:15px">{msg}</p>{form}
<p style="margin:28px 0 0;font-size:12px;color:#7d9097">You can turn product updates back on in the app under Settings &gt; Account.<br>DedSec Lab &middot; {html.escape(get_settings().support_email)}</p>
</div></body></html>"""
    return HTMLResponse(body, status_code=status, headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store",
                                                           "Referrer-Policy": "no-referrer"})


def _unsubscribe(db: Session, t: str) -> bool:
    if not _TOKEN_RE.match(t or ""):
        return False
    user = db.scalars(select(User).where(User.email_unsub_token_hash == token_hash(t))).first()
    if user is None:
        return False
    if user.email_updates:
        user.email_updates = False
        user.email_updates_changed_at = utcnow()
        db.commit()
    return True


_BAD = ("Link not valid", "This unsubscribe link is invalid or has expired. You can turn product updates off in the app under Settings &gt; Account.")


@router.get("/email/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe_get(t: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    # GET unsubscribes immediately (one click from the email footer). Link scanners that prefetch are a risk,
    # but under PECR an easy opt-out matters more; the user can opt back in from the app.
    if not _unsubscribe(db, t):
        return _page(*_BAD, status=400)
    return _page("You're unsubscribed", "You won't get DedSec CCTV product update emails any more. "
                 "Account and security emails (like verification codes) are not affected.")


@router.post("/email/unsubscribe", include_in_schema=False)
async def unsubscribe_post(request: Request, t: str = "", db: Session = Depends(get_db)) -> Response:
    # RFC 8058: mailbox providers POST "List-Unsubscribe=One-Click" to the List-Unsubscribe URL.
    ok = _unsubscribe(db, t)
    if "text/html" in request.headers.get("accept", ""):
        return _page("You're unsubscribed", "You won't get DedSec CCTV product update emails any more.") if ok \
            else _page(*_BAD, status=400)
    return Response(status_code=200 if ok else 400, content="unsubscribed" if ok else "invalid token",
                    media_type="text/plain")
