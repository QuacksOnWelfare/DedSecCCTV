#!/usr/bin/env bash
# Example firewall setup. Review before running: sudo bash ufw-setup.example.sh
set -euo pipefail
SSH_PORT="${SSH_PORT:-22}"            # change if you moved sshd to another port
HTTPS_PORT="${HTTPS_PORT:-443}"
HTTP_PORT="${HTTP_PORT:-80}"          # needed for Let's Encrypt HTTP-01 / redirects
CLOUDFLARE_ONLY="${CLOUDFLARE_ONLY:-false}"   # true = only Cloudflare may reach 80/443

ufw default deny incoming
ufw default allow outgoing
ufw limit "${SSH_PORT}/tcp" comment ssh
if [[ "$CLOUDFLARE_ONLY" == "true" ]]; then
  for ip in $(curl -fsS https://www.cloudflare.com/ips-v4) $(curl -fsS https://www.cloudflare.com/ips-v6); do
    ufw allow from "$ip" to any port "$HTTPS_PORT" proto tcp comment cloudflare
  done
else
  ufw allow "${HTTP_PORT}/tcp"
  ufw allow "${HTTPS_PORT}/tcp"
fi
ufw --force enable
ufw status verbose
