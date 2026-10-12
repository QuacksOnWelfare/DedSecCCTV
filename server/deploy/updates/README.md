# Updates folder (served by Caddy at `https://<domain>/updates/`)

The desktop app (`app/app.py`, `check_for_update` / `parse_latest`) fetches
`<update_url>latest.json` on every start (`update_url` = `DEDSEC_UPDATE_URL` or `config.ini`,
must end in `/updates/` and be `https://`). Copy this layout to the directory in the Caddyfile
(default `/var/www/dedsec-cctv-updates`):

```
/var/www/dedsec-cctv-updates/
├── latest.json          # no file / empty file = no update
└── DedSecCCTV.zip       # or a sub-path, e.g. 1.0.1/DedSecCCTV.zip
```

## latest.json (exactly what the app reads)
```json
{
  "version": "1.0.1",
  "url": "DedSecCCTV.zip",
  "sha256": "<64 lowercase hex chars of the zip>",
  "mandatory": true,
  "notes": "What changed (max 2000 chars shown)."
}
```
* `version` (required): semver, only applied if **newer** than the app's `APP_VERSION`.
* `sha256` (required): SHA-256 of the zip, 64 hex chars; invalid → update refused.
* `url` (optional, default `DedSecCCTV.zip`): relative to `/updates/`, or an absolute URL — but it must be
  **https, the same host, and under `/updates/`** (no `..`, query or fragment), otherwise it is refused.
* `mandatory` (optional, default `true`): `true` = Download now / Quit only; `false` = adds **Later**.
* `notes` (optional): shown in the dialog.
* Download size limit: 1 GiB. Redirects are not followed.

## The zip
A **zip** (not an installer): the contents of the built `DedSecCCTV\` folder — `DedSecCCTV.exe`,
`DedSecUpdater.exe`, `assets\`, DLLs… — at the zip root (or inside one top folder containing
`DedSecCCTV.exe`). The updater (`app/updater.py`) re-checks the SHA-256, backs up every file it
replaces, never deletes files, never overwrites existing files under `user_streams\`, refuses
absolute/`..`/symlink entries, and restores the backup on failure. Log: `%TEMP%\DedSecUpdater.log`.

```powershell
Compress-Archive -Path .\DedSecCCTV\* -DestinationPath .\DedSecCCTV.zip -Force
(Get-FileHash .\DedSecCCTV.zip -Algorithm SHA256).Hash.ToLower()
```

## Publish (zip first, latest.json last, atomically)
```bash
scp DedSecCCTV.zip latest.json you@your-server:/tmp/
ssh you@your-server 'cd /var/www/dedsec-cctv-updates && \
  cp /tmp/DedSecCCTV.zip .DedSecCCTV.zip.tmp && mv .DedSecCCTV.zip.tmp DedSecCCTV.zip && \
  cp /tmp/latest.json .latest.json.tmp && mv .latest.json.tmp latest.json && \
  chmod 644 DedSecCCTV.zip latest.json && sha256sum DedSecCCTV.zip'
curl -s https://<domain>/updates/latest.json
```
Dot-files are hidden and directory listing is off (see Caddyfile.example).
Withdraw: delete `latest.json`. Roll back: republish old code with a **higher** version.

## Announcements / changelogs (`news.json`)

The desktop app Settings → Updates panel fetches `news.json` (+ `news.json.sig`)
from the same `/updates/` folder. Both files are Ed25519-signed with the **same
key pair** as `latest.json` (see `app/tools/sign_news.py` and
`docs/UPDATE_SIGNING.md`).

```
/var/www/dedsec-cctv-updates/
├── latest.json
├── latest.json.sig
├── news.json
├── news.json.sig
└── DedSecCCTV.zip
```

Publish by copying the signed pair into this directory (sig first, then json; mode 0644).
Body markdown is limited (headings, bullets, bold, code); remote HTML/images are never rendered.
