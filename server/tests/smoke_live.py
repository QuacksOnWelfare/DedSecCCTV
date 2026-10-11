"""Live smoke test: run against a real uvicorn server using the desktop client module.

    EMAIL_BACKEND=memory is not shared across processes, so this script reads the
    verification code from the server log file passed as argv[1] (console backend).
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dedsec_auth_client import AuthClient, TokenStore, TotpRequired  # noqa: E402

from app.security import TOTP_STEP, totp_now  # noqa: E402

log = Path(sys.argv[1])
base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"
store = TokenStore("smoke", use_keyring=False, file_path=Path(tempfile.mkdtemp()) / "session.bin")
c = AuthClient(base, allow_insecure_localhost=True, token_store=store)
email, pw = f"smoke-{uuid.uuid4().hex[:6]}@example.com", "Smoke-Test-Passw0rd!"

print("register:", c.register(email, pw))
time.sleep(0.3)
token = re.findall(rf"DEV EMAIL to {re.escape(email)}.*?paste this code into the app: (\S+)", log.read_text(), re.S)[-1]
c.verify_email(token); print("verify-email: ok")
print("login:", c.login(email, pw))
setup = c.setup_2fa(); print("2fa/setup:", setup["otpauth_uri"][:60], "...")
c.enable_2fa(totp_now(setup["secret"])); print("2fa/enable: ok")
try:
    c.login(email, pw); raise SystemExit("expected TOTP challenge")
except TotpRequired:
    print("login: totp_required")
print("login/totp:", c.submit_totp(totp_now(setup["secret"], time.time() + TOTP_STEP)))
old = c._refresh
c._access_exp = 0  # force automatic refresh on next call
print("refresh+me:", c.current_user().email, "rotated:", c._refresh != old)
c2 = AuthClient(base, allow_insecure_localhost=True, token_store=store)
print("resume from encrypted store:", c2.try_resume())
c2.logout(); print("logout: ok; stored token cleared:", store.load() is None)
print("SMOKE OK")
