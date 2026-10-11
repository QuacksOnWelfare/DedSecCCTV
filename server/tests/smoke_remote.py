"""Live smoke test against the deployed server over HTTPS (no -k / full TLS verification).

Reads the verification code from the server's journal over SSH (EMAIL_BACKEND=console).
    python tests/smoke_remote.py https://auth.example.com deploy@auth.example.com ~/.ssh/id_ed25519
Prints the test email on the last line so it can be deleted from the live DB afterwards.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dedsec_auth_client import AuthClient, TokenStore, TotpRequired  # noqa: E402

from app.security import TOTP_STEP, totp_now  # noqa: E402

base, host, key = sys.argv[1], sys.argv[2], sys.argv[3]
store = TokenStore("smoke", use_keyring=False, file_path=Path(tempfile.mkdtemp()) / "session.bin")
c = AuthClient(base, token_store=store)  # https, default verify=True
email, pw = f"smoke-{uuid.uuid4().hex[:6]}@example.com", "Smoke-Test-Passw0rd!"


def code_for(addr: str) -> str:
    for _ in range(10):
        out = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", host,
                              f"sudo dedsec-codes {addr} -10min"], capture_output=True, text=True).stdout
        m = re.findall(r"paste this code into the app: (\S+)", out)
        if m:
            return m[-1]
        time.sleep(1)
    raise SystemExit("verification code not found in journal")


print("register:", c.register(email, pw))
c.verify_email(code_for(email)); print("verify-email: ok (code read from journal over ssh)")
print("login:", c.login(email, pw))
setup = c.setup_2fa(); print("2fa/setup:", setup["otpauth_uri"][:40], "...")
c.enable_2fa(totp_now(setup["secret"])); print("2fa/enable: ok")
try:
    c.login(email, pw); raise SystemExit("expected TOTP challenge")
except TotpRequired:
    print("login: totp_required")
print("login/totp:", c.submit_totp(totp_now(setup["secret"], time.time() + TOTP_STEP)))
old = c._refresh
c._access_exp = 0
print("refresh+me:", c.current_user().email, "rotated:", c._refresh != old)
c2 = AuthClient(base, token_store=store)
print("resume from encrypted store:", c2.try_resume())
c2.logout(); print("logout: ok; stored token cleared:", store.load() is None)
print("SMOKE OK")
print("TEST_EMAIL", email)
