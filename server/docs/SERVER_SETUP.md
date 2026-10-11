# Server setup guide

Placeholders used below: `auth.example.com` (your domain), `203.0.113.10` (your VPS IP),
`deploy` (your admin user), `dedsec` (service user), `/opt/dedsec-cctv-server` (install dir).

## 1. Get a VPS
Any provider (Hetzner, OVH, DigitalOcean, Vultr, ...). 1 vCPU / 1 GB RAM is plenty.
Choose Ubuntu 24.04+ or Debian 12+. Note the public IP.

## 2. Create an admin user and SSH keys
On your PC: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519`
```bash
ssh root@203.0.113.10
adduser deploy && usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/   # or paste your .pub
chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys
```
Test `ssh deploy@203.0.113.10` in a new terminal, then harden `/etc/ssh/sshd_config`:
`PasswordAuthentication no`, `PermitRootLogin no` (optionally `Port <your-port>`), and `sudo systemctl restart ssh`.

## 3. Firewall
`sudo apt install -y ufw` then review and run `deploy/ufw-setup.example.sh`
(`SSH_PORT=<port> sudo -E bash deploy/ufw-setup.example.sh`). Keep your SSH session open until verified.

## 4. Install Python and clone
```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo useradd --system --home /opt/dedsec-cctv-server --shell /usr/sbin/nologin dedsec
sudo git clone https://github.com/<you>/<repo>.git /opt/dedsec-cctv-server
sudo python3 -m venv /opt/dedsec-cctv-server/.venv
sudo /opt/dedsec-cctv-server/.venv/bin/pip install -r /opt/dedsec-cctv-server/server/requirements.txt
sudo mkdir -p /opt/dedsec-cctv-server/data && sudo chown dedsec:dedsec /opt/dedsec-cctv-server/data
```

## 5. Configure `.env`
```bash
cd /opt/dedsec-cctv-server/server
sudo cp .env.example .env && sudo chown root:dedsec .env && sudo chmod 640 .env
python3 -c "import secrets;print(secrets.token_urlsafe(64))"                                  # JWT_SECRET
/opt/dedsec-cctv-server/.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"  # TOTP_ENCRYPTION_KEY
sudoedit .env
```
Set at least: `JWT_SECRET`, `TOTP_ENCRYPTION_KEY`, `DATABASE_URL=sqlite:////opt/dedsec-cctv-server/data/auth.db`,
`PUBLIC_BASE_URL=https://auth.example.com`, `ALLOWED_HOSTS=["auth.example.com","localhost","127.0.0.1"]`,
`TRUST_PROXY_HEADERS=true` (behind Caddy), and the SMTP block. Every `CHANGE_ME` must be gone or the
server will refuse to start in production. Keep the TOTP key safe: losing it breaks all 2FA enrolments.

## 6. SMTP account
Any provider that offers SMTP submission works (Brevo, Mailgun, Postmark, SES, Fastmail, ...). You need host,
port, username, password, and a verified sender address/domain.

Brevo example:
1. Sign up at brevo.com (free tier is fine for verification mails).
2. Senders, Domains & Dedicated IPs → add and verify your sender (ideally authenticate your domain: add the DKIM/SPF/DMARC DNS records it shows).
3. SMTP & API → SMTP tab → "Generate a new SMTP key".
4. `.env`: `SMTP_HOST=smtp-relay.brevo.com`, `SMTP_PORT=587`, `SMTP_STARTTLS=true`, `SMTP_USERNAME=<SMTP login shown on that page>`,
   `SMTP_PASSWORD=<SMTP key>`, `SMTP_FROM=DedSec CCTV <no-reply@example.com>` (your verified sender).

## 7. Domain + DNS
Buy a domain at any registrar. Create `A auth.example.com → 203.0.113.10` (and `AAAA` if you have IPv6).
Check: `dig +short auth.example.com`.

## 8. Optional: Cloudflare
1. Add the domain to Cloudflare, switch nameservers at the registrar.
2. Make the `auth` record **Proxied** (orange cloud).
3. SSL/TLS → mode **Full (strict)**. Caddy's ACME HTTP-01/TLS-ALPN may not pass through the proxy;
   either create a Cloudflare Origin Certificate and use `tls /etc/caddy/origin.pem /etc/caddy/origin.key`,
   or build Caddy with the `caddy-dns/cloudflare` plugin for DNS-01.
4. Uncomment the `trusted_proxies` block in the Caddyfile with the current ranges
   (`curl -s https://www.cloudflare.com/ips-v4`, `.../ips-v6`), so rate limiting sees real client IPs.
5. Lock the origin: run the ufw script with `CLOUDFLARE_ONLY=true`. Re-check the ranges occasionally.

## 9. Caddy
Install from the official repo (https://caddyserver.com/docs/install#debian-ubuntu-raspbian), then:
```bash
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # edit the domain
sudo mkdir -p /var/www/dedsec-cctv-updates /var/log/caddy
sudo systemctl reload caddy
```

## 10. systemd
```bash
sudo cp deploy/dedsec-cctv-server.service.example /etc/systemd/system/dedsec-cctv-server.service
sudo systemctl daemon-reload && sudo systemctl enable --now dedsec-cctv-server
journalctl -u dedsec-cctv-server -f
```

## 11. Test
```bash
curl -s http://127.0.0.1:8000/healthz      # on the server -> {"status":"ok"}
curl -s https://auth.example.com/healthz   # from anywhere
```

## 12. First account
Register from the desktop app (or `curl -X POST https://auth.example.com/auth/register -H 'Content-Type: application/json'
-H 'X-Device-Id: <32-hex>' -d '{"email":"you@example.com","password":"..."}'`), then enter the emailed code.
If mail isn't arriving yet, temporarily run with `EMAIL_BACKEND=console` in development only and read the code from the journal.

## 13. Publishing app updates
See `deploy/updates/README.md`: upload `/var/www/dedsec-cctv-updates/<version>/`, verify the hash, update `latest.json` last.

## 14. Backups
```bash
sudo sqlite3 /opt/dedsec-cctv-server/data/auth.db ".backup '/var/backups/auth-$(date +%F).db'"
```
Also back up `.env` (separately, encrypted). Cron it daily and copy off-server; test a restore.

## 15. Optional: fail2ban
`sudo apt install -y fail2ban` — the default `sshd` jail is enough for most; the app has its own rate limits.

## 16. Troubleshooting
| Symptom | Fix |
|---|---|
| `Refusing to start in production` | A value still contains `CHANGE_ME`, or `EMAIL_BACKEND` isn't `smtp`. |
| `400 Invalid host header` | Add your domain to `ALLOWED_HOSTS` (JSON array). |
| Everyone rate-limited together | `TRUST_PROXY_HEADERS=true` and (with Cloudflare) `trusted_proxies` in Caddy. |
| No emails | Check `journalctl -u dedsec-cctv-server`, SMTP key, sender verification, port 587 egress. |
| DB `readonly`/permission errors | `data/` must be owned by `dedsec` and listed in `ReadWritePaths`. |
| TLS errors via Cloudflare | Use Full (strict) with an origin cert or DNS-01. |
