# DedSec CCTV – auth server (v2, open source)

FastAPI backend for the DedSec CCTV desktop app:

- Argon2id password hashing, email verification codes
- JWT access tokens + rotating refresh tokens
- TOTP 2FA (secrets encrypted at rest)
- Device/HWID lock (one account = one PC) with emailed reset codes
- Per-IP and per-account rate limiting, account lockout
- Generic SMTP (any provider), static app updates served by Caddy

## Quick start (dev)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # set ENVIRONMENT=development, EMAIL_BACKEND=console, generate secrets
pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/healthz
```

Production: see [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md) and `deploy/`.
The server refuses to start with `ENVIRONMENT=production` while any setting contains `CHANGE_ME`.

License: MIT, see [../LICENSE](../LICENSE).
