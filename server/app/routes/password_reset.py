"""Forgot password (1.0.3).

1. App: POST /auth/password/forgot {email}  -> always the same 202 answer; a 6-digit code is emailed
   only if the account exists (no account enumeration). Rate-limited per IP, per address, per account.
2. App: POST /auth/password/verify-code {email, code} -> only a correct, unexpired code (max
   PASSWORD_RESET_MAX_ATTEMPTS guesses) makes the server email a single-use reset link.
3. Browser: GET /account/reset-password?t=... shows the form; POST sets the new password (same rules as
   sign-up), ends every session and burns the link. CSRF: the unguessable link token travels in the POST
   body, plus a SameSite=Strict double-submit cookie and an Origin check.

Authenticator / email 2FA codes are never accepted here: proof of email ownership is the only factor.
"""
from __future__ import annotations

import hmac
import html
import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..emailer import send_code_email, send_password_changed_email, send_reset_link_email
from ..minversion import require_app_version
from ..models import PasswordReset, RefreshToken, User
from ..ratelimit import TOO_MANY, client_ip, limit_account, limit_ip, limiter
from ..schemas import ForgotPasswordIn, MessageOut, ResetCodeIn
from ..security import (as_aware, constant_time_eq, hash_password, hash_purpose_code, hash_token, new_numeric_code,
                        new_opaque_token, normalize_code, password_problems, utcnow)

api = APIRouter(prefix="/auth/password", tags=["auth"], dependencies=[Depends(limit_ip), Depends(require_app_version)])
page = APIRouter(dependencies=[Depends(limit_ip)])  # browser page: no app-version header

FORGOT_MSG = "If an account exists for this email, a 6-digit code is on its way."
LINK_MSG = "Code confirmed. We've emailed you a link to set a new password."
INVALID_CODE = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired code.")
PURPOSE = "password-reset"
PAGE_PATH = "/account/reset-password"
CSRF_COOKIE = "dsr_csrf"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_CODE_RE = re.compile(r"^\d{6}$")


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _live_reset(db: Session, user_id: str) -> PasswordReset | None:
    return db.scalar(select(PasswordReset).where(PasswordReset.user_id == user_id, PasswordReset.used.is_(False),
                                                 PasswordReset.code_used.is_(False))
                     .order_by(PasswordReset.id.desc()))


# ---------------------------------------------------------------------------
# app API
# ---------------------------------------------------------------------------

@api.post("/forgot", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def forgot_password(body: ForgotPasswordIn, request: Request, tasks: BackgroundTasks,
                    db: Session = Depends(get_db)) -> MessageOut:
    s = get_settings()
    email = _norm_email(body.email)
    # Same limits whether or not the account exists, so a 429 says nothing either.
    if not limiter.hit(f"pwforgot-ip:{client_ip(request)}", 10, 3600):
        raise TOO_MANY
    if not limiter.hit(f"pwforgot:{email}", s.password_reset_requests_per_hour, 3600):
        raise TOO_MANY
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.is_active and limiter.hit(f"pwforgot-mail:{user.id}",
                                                           s.password_reset_emails_per_hour, 3600):
        db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used.is_(False))
                   .values(used=True))  # only the newest request is valid
        code = new_numeric_code()
        db.add(PasswordReset(user_id=user.id, code_hash=hash_purpose_code(PURPOSE, user.id, code), attempts=0,
                             code_expires_at=utcnow() + timedelta(minutes=s.password_reset_code_ttl_minutes)))
        db.commit()
        # Sent after the response: SMTP time must not reveal that the account exists.
        tasks.add_task(send_code_email, "password_reset", user.email, code, s.password_reset_code_ttl_minutes)
    return MessageOut(message=FORGOT_MSG)


@api.post("/verify-code", response_model=MessageOut)
def verify_reset_code(body: ResetCodeIn, tasks: BackgroundTasks, db: Session = Depends(get_db)) -> MessageOut:
    s = get_settings()
    email = _norm_email(body.email)
    limit_account(f"pwcode:{email}")
    code = normalize_code(body.code)
    if not _CODE_RE.match(code):
        raise INVALID_CODE
    user = db.scalar(select(User).where(User.email == email))
    row = _live_reset(db, user.id) if user is not None and user.is_active else None
    if row is None:
        raise INVALID_CODE
    if as_aware(row.code_expires_at) <= utcnow() or (row.attempts or 0) >= s.password_reset_max_attempts:
        row.used = True
        db.commit()
        raise INVALID_CODE
    if not constant_time_eq(row.code_hash, hash_purpose_code(PURPOSE, user.id, code)):
        row.attempts = (row.attempts or 0) + 1
        if row.attempts >= s.password_reset_max_attempts:
            row.used = True  # burned: request a new code
        db.commit()
        raise INVALID_CODE
    token = new_opaque_token()
    row.code_used = True
    row.link_token_hash = hash_token(token)
    row.link_expires_at = utcnow() + timedelta(minutes=s.password_reset_link_ttl_minutes)
    user.email_verified = True  # the code proves the address belongs to this user
    db.commit()
    url = f"{s.public_base_url.rstrip('/')}{PAGE_PATH}?t={token}"
    tasks.add_task(send_reset_link_email, user.email, url, s.password_reset_link_ttl_minutes)
    return MessageOut(message=LINK_MSG)


# ---------------------------------------------------------------------------
# browser page
# ---------------------------------------------------------------------------

def _row_for_token(db: Session, token: str) -> tuple[PasswordReset, User] | None:
    if not _TOKEN_RE.match(token or ""):
        return None
    row = db.scalar(select(PasswordReset).where(PasswordReset.link_token_hash == hash_token(token)))
    if row is None or row.used or not row.code_used or row.link_expires_at is None:
        return None
    if as_aware(row.link_expires_at) <= utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return row, user


def _secure_cookie() -> bool:
    return get_settings().public_base_url.lower().startswith("https://")


def _expected_origin() -> str:
    u = urlparse(get_settings().public_base_url)
    return f"{u.scheme}://{u.netloc}".lower()


_STYLE = """
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:#07090b;color:#cfdcdf;
font-family:'Segoe UI',Helvetica,Arial,sans-serif;display:flex;align-items:center;justify-content:center;padding:24px}
.card{width:100%;max-width:460px;background:#0e1418;border:1px solid #1d2a30;border-top:4px solid #00b3b3;
border-radius:10px;padding:30px 32px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
.brand{display:flex;align-items:center;gap:12px;margin:0 0 22px}.brand img{border-radius:6px}
.brand b{font-size:17px;color:#fff;letter-spacing:.5px}.brand b span{color:#00b3b3}
h1{margin:0 0 8px;font-size:24px;color:#fff;font-weight:800}p{margin:0 0 14px;line-height:1.55;font-size:14px}
.who{font-family:Consolas,Menlo,monospace;color:#7fe9e9;font-size:13px}
label{display:block;font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#7f9399;margin:16px 0 6px}
.field{position:relative}input[type=password],input[type=text]{width:100%;padding:12px 74px 12px 14px;font-size:15px;
color:#e8f0ff;background:#0a1116;border:1px solid #1f3a40;border-radius:8px;outline:none}
input:focus{border-color:#00b3b3;box-shadow:0 0 0 3px rgba(0,179,179,.15)}
.toggle{position:absolute;right:8px;top:7px;background:transparent;border:1px solid #1f3a40;color:#7fe9e9;
border-radius:6px;padding:5px 9px;font-size:12px;cursor:pointer}
ul.rules{list-style:none;padding:0;margin:12px 0 0;font-size:13px}ul.rules li{margin:4px 0;color:#7f9399}
ul.rules li:before{content:'\\2022';display:inline-block;width:16px;color:#3b5157}
ul.rules li.ok{color:#7dffb0}ul.rules li.ok:before{content:'\\2713';color:#7dffb0}
.err{background:#2a1215;border:1px solid #5c2026;color:#ff9aa4;border-radius:8px;padding:10px 12px;font-size:13px;margin:0 0 14px}
.err ul{margin:6px 0 0 18px;padding:0}
button.go{width:100%;margin:22px 0 0;background:#00b3b3;color:#06202a;border:0;border-radius:8px;padding:13px;
font-size:16px;font-weight:800;cursor:pointer}button.go:hover{background:#22d3ee}
.foot{margin:22px 0 0;font-size:12px;color:#7f9399}.ok{color:#7dffb0}
"""

_SCRIPT = """
(function(){var pw=document.getElementById('pw'),pw2=document.getElementById('pw2');if(!pw)return;
var em=(document.body.getAttribute('data-local')||'').toLowerCase();
function cls(s){var n=0;if(/[a-z]/.test(s))n++;if(/[A-Z]/.test(s))n++;if(/[0-9]/.test(s))n++;if(/[^A-Za-z0-9]/.test(s))n++;return n;}
function upd(){var s=pw.value,ok={len:s.length>=12&&s.length<=128,mix:cls(s)>=3||s.length>=20,
dist:new Set(s.split('')).size>=6,mail:!(em.length>=4&&s.toLowerCase().indexOf(em)>=0),match:s.length>0&&s===pw2.value};
for(var k in ok){var li=document.getElementById('r-'+k);if(li)li.className=ok[k]?'ok':'';}}
pw.addEventListener('input',upd);pw2.addEventListener('input',upd);
document.querySelectorAll('.toggle').forEach(function(b){b.addEventListener('click',function(){
var f=document.getElementById(b.getAttribute('data-for'));var show=f.type==='password';f.type=show?'text':'password';
b.textContent=show?'Hide':'Show';});});upd();})();
"""


def _html(title: str, inner: str, nonce: str, status_code: int = 200, local: str = "") -> HTMLResponse:
    s = get_settings()
    asset = (s.email_asset_base if s.email_asset_base.endswith("/") else s.email_asset_base + "/")
    # CSP img-src: allow the configured asset host only (scheme+host of EMAIL_ASSET_BASE).
    from urllib.parse import urlparse as _urlparse
    _pu = _urlparse(asset)
    img_origin = f"{_pu.scheme}://{_pu.netloc}" if _pu.scheme and _pu.netloc else "'none'"
    logo = asset + "dedsec_logo.png"
    support = s.support_email or "support@example.com"
    csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
           f"img-src {img_origin}; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer"><title>{html.escape(title)} - DedSec CCTV</title>
<style nonce="{nonce}">{_STYLE}</style></head><body data-local="{html.escape(local, quote=True)}"><main class="card">
<div class="brand"><img src="{html.escape(logo, quote=True)}" width="36" height="36" alt="">
<b>DedSec <span>CCTV</span></b></div>
{inner}
<p class="foot">DedSec Lab &middot; {html.escape(support)}</p></main>
<script nonce="{nonce}">{_SCRIPT}</script></body></html>"""
    return HTMLResponse(body, status_code=status_code, headers={
        "Content-Security-Policy": csp, "Cache-Control": "no-store", "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex"})


def _invalid_page(nonce: str) -> HTMLResponse:
    return _html("Link not valid", "<h1>This link has expired</h1>"
                 "<p>Password reset links work once and only for a short time. Open DedSec CCTV, choose "
                 "<b>Forgot password?</b> on the sign-in screen and request a new code.</p>", nonce, 400)


def _form_page(token: str, csrf: str, email: str, nonce: str, errors: list[str] | None = None,
               status_code: int = 200) -> HTMLResponse:
    err = ""
    if errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        err = f'<div class="err" role="alert">Please fix the following:<ul>{items}</ul></div>'
    inner = f"""<h1>Choose a new password</h1>
<p>For <span class="who">{html.escape(email)}</span>. Setting it signs you out on every device.</p>{err}
<form method="post" action="{PAGE_PATH}" autocomplete="off">
<input type="hidden" name="t" value="{html.escape(token, quote=True)}">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<input type="text" name="username" value="{html.escape(email, quote=True)}" autocomplete="username" hidden>
<label for="pw">New password</label>
<div class="field"><input id="pw" name="password" type="password" autocomplete="new-password" required minlength="12"
maxlength="128" autofocus><button type="button" class="toggle" data-for="pw">Show</button></div>
<label for="pw2">Confirm new password</label>
<div class="field"><input id="pw2" name="confirm" type="password" autocomplete="new-password" required minlength="12"
maxlength="128"><button type="button" class="toggle" data-for="pw2">Show</button></div>
<ul class="rules"><li id="r-len">12 to 128 characters</li>
<li id="r-mix">3 of: lowercase, uppercase, digits, symbols (or 20+ characters)</li>
<li id="r-dist">At least 6 different characters</li><li id="r-mail">Doesn't contain your email name</li>
<li id="r-match">Both passwords match</li></ul>
<button class="go" type="submit">Set new password</button></form>"""
    local = email.split("@", 1)[0]
    resp = _html("Choose a new password", inner, nonce, status_code, local=local)
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=get_settings().password_reset_link_ttl_minutes * 60, path=PAGE_PATH,
                    secure=_secure_cookie(), httponly=True, samesite="strict")
    return resp


@page.get(PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
def reset_form(request: Request, t: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    # GET never changes state, so link scanners that prefetch the URL can't burn it.
    nonce = secrets.token_urlsafe(16)
    found = _row_for_token(db, t)
    if found is None:
        return _invalid_page(nonce)
    _row, user = found
    csrf = request.cookies.get(CSRF_COOKIE, "")
    if not _TOKEN_RE.match(csrf):  # reuse it so a second tab with the same link still works
        csrf = secrets.token_urlsafe(32)
    return _form_page(t, csrf, user.email, nonce)


@page.post(PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
async def reset_submit(request: Request, tasks: BackgroundTasks, db: Session = Depends(get_db)) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
        return _invalid_page(nonce)
    raw = await request.body()  # <= 16 KB (main.py middleware + Caddy)
    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items() if v}
    token, csrf = form.get("t", ""), form.get("csrf", "")
    pw, pw2 = form.get("password", ""), form.get("confirm", "")
    # CSRF: same-site double-submit cookie + Origin check (the link token in the body is the main guard).
    origin = (request.headers.get("origin") or "").lower()
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if (origin and origin != _expected_origin()) or not csrf or not cookie or not hmac.compare_digest(csrf, cookie):
        return _html("Request blocked", "<h1>Request blocked</h1><p>This form has to be sent from the page in the "
                     "reset email. Open the link from the email again.</p>", nonce, 403)
    found = _row_for_token(db, token)
    if found is None:
        return _invalid_page(nonce)
    row, user = found
    limit_account(f"pwreset-form:{user.id}")
    errors = [f"Password {p}" for p in password_problems(pw, user.email)]
    if pw != pw2:
        errors.append("The passwords don't match")
    if errors:
        return _form_page(token, csrf, user.email, nonce, errors, 422)
    user.password_hash = hash_password(pw)
    user.failed_login_count = 0
    user.locked_until = None
    user.token_version += 1  # every access token dies now
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
    db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used.is_(False))
               .values(used=True))
    row.used = True
    db.commit()
    tasks.add_task(send_password_changed_email, user.email)
    resp = _html("Password changed", '<h1 class="ok">Password changed</h1><p>Your new password is set and every '
                 "device was signed out. Go back to DedSec CCTV and sign in with your new password.</p>"
                 "<p>You can close this page.</p>", nonce)
    resp.delete_cookie(CSRF_COOKIE, path=PAGE_PATH, secure=_secure_cookie(), httponly=True, samesite="strict")
    return resp
