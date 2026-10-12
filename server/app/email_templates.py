"""Branded transactional emails (verification / device-reset codes). HTML + plain text, no tracking, no emojis.

Preview / test (sends nothing unless --test is given, touches no account state):
  cd /path/to/server && python \\
      -m app.email_templates --kind verify [--out preview.html] [--test you@example.com]
"""
from __future__ import annotations

import html

# Branding comes from settings (EMAIL_ASSET_BASE, SUPPORT_EMAIL, DISCORD_URL, GITHUB_URL in .env).


def _brand() -> tuple[str, str, str, str]:
    from .config import get_settings
    s = get_settings()
    img = s.email_asset_base if s.email_asset_base.endswith("/") else s.email_asset_base + "/"
    return img, s.discord_url, s.support_email, s.github_url
FONT = "'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "Consolas,'SFMono-Regular',Menlo,'Courier New',monospace"
C = dict(bg="#07090b", card="#0e1418", panel="#0a1116", line="#1d2a30", text="#cfdcdf", mute="#7f9399",
         white="#ffffff", teal="#00b3b3")


def _ttl_text(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


KINDS = {
    "verify": dict(
        subject="Your DedSec CCTV verification code: {code}",
        heading="Verify your email",
        line="Enter this code in the DedSec CCTV app to confirm your email address and finish creating your account.",
        preheader="Your verification code is {code}. It expires in {ttl}.",
        after="",
    ),
    "device_reset": dict(
        subject="Your DedSec CCTV device reset code: {code}",
        heading="Move your account to a new PC",
        line="Someone asked to link your DedSec CCTV account to a new PC. Enter this code in the app on that PC to confirm.",
        preheader="Your device reset code is {code}. It expires in {ttl}.",
        after="After the reset your account works only on the new PC and every other session is signed out. "
              "If this wasn't you, change your password: your account stays locked to your current PC.",
    ),
}


def render(kind: str, code: str, ttl_minutes: int) -> tuple[str, str, str, str]:
    """Return (subject, preheader, html, text)."""
    raw_brand = _brand()
    IMG, DISCORD, SUPPORT, GITHUB = (html.escape(x, quote=True) for x in raw_brand)
    k = KINDS[kind]
    ttl = _ttl_text(ttl_minutes)
    subject = k["subject"].format(code=code)
    pre = k["preheader"].format(code=code, ttl=ttl)
    spaced = html.escape(code)
    after = (f'<p style="margin:18px 0 0;font-family:{FONT};font-size:14px;line-height:22px;color:{C["text"]}">'
             f'{html.escape(k["after"])}</p>') if k["after"] else ""
    page = f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{html.escape(subject)}</title>
<style>body{{margin:0;padding:0;width:100%!important;-webkit-text-size-adjust:100%}}table{{border-collapse:collapse}}
@media only screen and (max-width:620px){{.container{{width:100%!important}}.px{{padding-left:20px!important;padding-right:20px!important}}.code{{font-size:30px!important;letter-spacing:8px!important}}}}</style>
</head><body style="margin:0;padding:0;background:{C["bg"]}" bgcolor="{C["bg"]}">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{C["bg"]};opacity:0">{html.escape(pre)}{"&#847;&zwnj;&nbsp;" * 50}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{C["bg"]}"><tr><td align="center" style="padding:28px 10px">
<!--[if mso]><table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table role="presentation" class="container" width="560" cellpadding="0" cellspacing="0" border="0" bgcolor="{C["card"]}" style="width:560px;max-width:560px;border:1px solid {C["line"]};border-top:4px solid {C["teal"]};border-radius:8px">
<tr><td class="px" style="padding:22px 36px;border-bottom:1px solid {C["line"]}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="44" valign="middle"><img src="{IMG}dedsec_logo.png" width="40" height="40" alt="DedSec Lab" style="display:block;border:0;border-radius:6px"></td>
<td valign="middle" style="padding-left:12px;font-family:{FONT};font-size:17px;font-weight:700;color:{C["white"]};letter-spacing:.5px">DedSec <span style="color:{C["teal"]}">CCTV</span></td>
<td align="right" valign="middle" style="font-family:{FONT};font-size:12px;color:{C["mute"]};letter-spacing:1px;text-transform:uppercase">Account security</td>
</tr></table></td></tr>
<tr><td class="px" style="padding:34px 36px 8px">
<h1 style="margin:0 0 12px;font-family:{FONT};font-size:26px;line-height:32px;color:{C["white"]};font-weight:800">{html.escape(k["heading"])}</h1>
<p style="margin:0;font-family:{FONT};font-size:15px;line-height:24px;color:{C["text"]}">{html.escape(k["line"])}</p>
</td></tr>
<tr><td class="px" style="padding:24px 36px 6px" align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{C["panel"]}" style="border:1px solid #1f3a40;border-radius:8px"><tr>
<td align="center" style="padding:22px 10px 8px;font-family:{FONT};font-size:11px;letter-spacing:2px;text-transform:uppercase;color:{C["mute"]}">Your code</td></tr>
<tr><td align="center" class="code" style="padding:0 10px 22px 22px;font-family:{MONO};font-size:38px;line-height:44px;font-weight:700;letter-spacing:12px;color:{C["teal"]}">{spaced}</td></tr></table>
</td></tr>
<tr><td class="px" style="padding:12px 36px 0" align="center">
<p style="margin:0;font-family:{FONT};font-size:14px;color:{C["white"]};font-weight:600">This code expires in {ttl}.</p>
<p style="margin:6px 0 0;font-family:{FONT};font-size:12px;color:{C["mute"]}">It can be used once. Never share it with anyone.</p>
</td></tr>
<tr><td class="px" style="padding:6px 36px 30px">{after}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:22px"><tr>
<td style="padding:14px 16px;border-left:3px solid {C["teal"]};background:#0b1317;font-family:{FONT};font-size:13px;line-height:20px;color:{C["text"]}">
If you didn't request this, you can ignore this email. We will never ask for your password.</td></tr></table>
</td></tr></table>
<table role="presentation" class="container" width="560" cellpadding="0" cellspacing="0" border="0" style="width:560px;max-width:560px"><tr>
<td style="padding:22px 36px;font-family:{FONT};font-size:12px;line-height:19px;color:{C["mute"]};text-align:center">
<p style="margin:0 0 8px"><strong style="color:{C["text"]}">DedSec Lab</strong></p>
<p style="margin:0 0 8px"><a href="{DISCORD}" style="color:{C["teal"]};text-decoration:none">Discord</a>&nbsp;&nbsp;|&nbsp;&nbsp;<a href="mailto:{SUPPORT}" style="color:{C["teal"]};text-decoration:none">Support: {SUPPORT}</a>&nbsp;&nbsp;|&nbsp;&nbsp;<a href="{GITHUB}" style="color:{C["teal"]};text-decoration:none">GitHub</a></p>
<p style="margin:0">This is an automated security email about your DedSec CCTV account.</p>
</td></tr></table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>"""
    text = "\n".join([
        k["heading"], "", k["line"], "", f"Your code: {code}", "", f"This code expires in {ttl}. It can be used once.",
        *(["", k["after"]] if k["after"] else []), "",
        "If you didn't request this, you can ignore this email. We will never ask for your password.", "",
        "--", "DedSec Lab", f"Discord: {raw_brand[1]}", f"Support: {raw_brand[2]}",
    ]) + "\n"
    return subject, pre, page, text


if __name__ == "__main__":  # preview / single test send; changes no account state
    import argparse
    import re
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=sorted(KINDS), default="verify")
    ap.add_argument("--code", default="")
    ap.add_argument("--ttl", type=int, default=0, help="minutes (default: server setting)")
    ap.add_argument("--out")
    ap.add_argument("--test", metavar="EMAIL", help="send ONE rendered sample to this single address")
    a = ap.parse_args()
    from .config import get_settings
    s = get_settings()
    ttl = a.ttl or (s.email_verification_ttl_hours * 60 if a.kind == "verify" else s.device_reset_ttl_minutes)
    import secrets
    code = a.code or f"{secrets.randbelow(10**6):06d}"
    subject, pre, page, text = render(a.kind, code, ttl)
    if a.out:
        open(a.out, "w").write(page)
        open(a.out.rsplit(".", 1)[0] + ".txt", "w").write(text)
        print(f"wrote {a.out}")
    if a.test:
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", a.test):
            sys.exit("--test takes exactly one address")
        from .emailer import SmtpSender
        resp = SmtpSender().send(a.test, subject, text, html_body=page)
        print(f"sent sample {a.kind} email: {resp}")
