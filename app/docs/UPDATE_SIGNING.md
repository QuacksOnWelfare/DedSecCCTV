# DedSec CCTV – signed updates (Ed25519)

The app only installs an update if `latest.json` has a valid Ed25519 signature (`latest.json.sig`)
from the key pinned in `app.py`. `latest.json` contains the zip's SHA-256, so the one signature covers the zip too.
Until a real key is pinned, the app rejects every update.

**The private key never leaves your PC.** Do not put it on the VPS, the build box, GitHub or Discord.

## 1. Generate the keypair (once, on your PC)
    pip install cryptography
    python tools\sign_update.py keygen --out %USERPROFILE%\dedsec-signing
- Use a passphrase. Back up `update_signing_key.pem` offline (USB or password manager).
  If you lose it, you have to ship a new app build by hand to every user.
- Copy the **public key** line it prints.

## 2. Put the public key in the app
In `app.py`, replace the placeholder:

    UPDATE_PUBLIC_KEY_B64 = "PASTE_ED25519_PUBLIC_KEY_HERE"

with the printed base64 string (44 chars ending in `=`). The public key is not secret.

## 3. Rebuild
Bump `APP_VERSION` and build as usual (`python build.py`, see BUILD.md). Zip the output folder as `DedSecCCTV.zip`.
Note: users on builds from before this change don't check signatures. They need to install this first signed build by hand
(or get it once through the old flow), and after that every update is verified.

## 4. Sign the release (on your PC)
    python tools\sign_update.py sign --key %USERPROFILE%\dedsec-signing\update_signing_key.pem ^
        --zip DedSecCCTV.zip --version 1.0.2 --notes "What changed"
    python tools\sign_update.py verify --pub <PUBLIC_KEY> --dir .
This writes `latest.json` and `latest.json.sig` next to the zip. Add `--optional` for a non-mandatory update.
Do not edit `latest.json` after signing, or the signature breaks and the app ignores the update.

## 5. Publish to the VPS
    scp -P YOUR_SSH_PORT -i %USERPROFILE%\.ssh\YOUR_KEY DedSecCCTV.zip latest.json latest.json.sig YOUR_USER@YOUR_SERVER_IP:YOUR_STAGING_DIR/
    ssh -p YOUR_SSH_PORT -i %USERPROFILE%\.ssh\YOUR_KEY YOUR_USER@YOUR_SERVER_IP "cd YOUR_STAGING_DIR && sudo install -m 0644 DedSecCCTV.zip latest.json.sig latest.json /var/www/dedsec-cctv-updates/ && rm -rf YOUR_STAGING_DIR"
(Create the folder first with `ssh ... "mkdir -p YOUR_STAGING_DIR"`.)
Install the zip first, then the .sig, then latest.json last.
Keep the update folder root-owned and read-only for the web server user, so the web server can read it but not change it.

Unpublish (stop offering an update): `sudo rm /var/www/dedsec-cctv-updates/latest.json`

## Key rotation / compromise
Generate a new keypair, pin the new public key, rebuild, and get users onto that build by hand. The old key then becomes useless.
