<div align="center">

<img src="app/assets/dedsec_logo.png" alt="DedSec CCTV logo" width="120" />

# DedSec CCTV

**A free, open-source CCTV viewer for Windows: watch live public feeds and your own cameras on one multi-camera wall.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6.svg?logo=windows&logoColor=white)](#download)
[![UI: PyQt6](https://img.shields.io/badge/UI-PyQt6-41CD52.svg?logo=qt&logoColor=white)](https://pypi.org/project/PyQt6/)
[![Player: VLC](https://img.shields.io/badge/Player-libVLC-FF8800.svg?logo=vlcmediaplayer&logoColor=white)](https://www.videolan.org/)
[![Backend: FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Bug bounty: active](https://img.shields.io/badge/bug%20bounty-active-red.svg)](SECURITY.md)

[Download](#download) · [Features](#features) · [Official app or self-host?](#two-ways-to-use-dedsec-cctv) · [Quick start](#quick-start) · [Roadmap](#updates--roadmap)


<a href="SECURITY.md"><img src="docs/bug-bounty-banner.png" alt="Bug bounty program" width="100%" /></a>

[Read the bounty rules](SECURITY.md) · [Report a vulnerability](https://github.com/QuacksOnWelfare/DedSecCCTV/security/advisories/new)


</div>

Full screen camera wall

<img src="docs/screenshots/fullscreen.jpg" width="100%">

Sign in

<img src="docs/screenshots/login.jpg" width="100%">

Optional two-factor setup

<img src="docs/screenshots/setup-2fa.jpg" width="100%">

Empty wall ready for feeds

<img src="docs/screenshots/wall-empty.jpg" width="100%">

Feeds loaded and ready to play

<img src="docs/screenshots/wall-ready.jpg" width="100%">

Camera wall with four feeds live

<img src="docs/screenshots/wall.jpg" width="100%">

Add CCTV: browse the catalog by country

<img src="docs/screenshots/add-cctv.jpg" width="100%">

Settings: Default theme

<img src="docs/screenshots/settings-theme-default.jpg" width="100%">

Settings: Dark theme

<img src="docs/screenshots/settings-theme-dark.jpg" width="100%">

Settings: Light theme

<img src="docs/screenshots/settings-theme-light.jpg" width="100%">

Settings: Account

<img src="docs/screenshots/settings-account.jpg" width="100%">

Settings: About and updates

<img src="docs/screenshots/settings.jpg" width="100%">

---

DedSec CCTV is a Windows desktop app (PyQt6 + libVLC) for viewing live camera streams, plus the FastAPI
authentication server behind it. Sign-up is free and accounts are protected with two-step verification.

The **official app** ships with DedSec Lab's curated catalog of **1,507 live feeds across 26 countries**, and more
countries and feeds will come in future updates. The **complete source code** is in this repository too, so you
can see exactly what the app does, build it yourself, or run the whole stack on your own server.

Current release: **1.0.2**. Builds from this repository ship only the `user_streams/` templates, not the official catalog.

## Community / Support

Questions, bug reports, or want to chat with other users? Join the DedSec Discord community:
https://discord.com/invite/4gRcYtTQ5c

## Download

The official prebuilt Windows build is published on **[GitHub Releases][releases]**.

1. Download the latest release zip from [Releases][releases].
2. Extract the whole folder somewhere you like (for example `C:\Apps\DedSecCCTV`).
3. Run `DedSecCCTV.exe`, create a free account, and verify your email.

> **Requirements:** Windows 10 or 11 (64-bit) with **[VLC media player](https://www.videolan.org/) (64-bit)** installed.
> The app plays feeds through libVLC, so it needs the 64-bit VLC libraries.

Once installed, the app updates itself. See [Updates & roadmap](#updates--roadmap).

## Features

### Desktop app

**Viewing**
- **Multi-camera wall:** watch up to **4 feeds side by side**, with a **fullscreen wall** mode.
- **Per-tile controls:** play/stop, mute/unmute, stream **info** (host, ISP and location lookup), and **remove from wall**.
- **Wide stream support:** **HLS** (`.m3u8`), **RTSP** and **HTTP(S)/MJPEG** streams, all played through libVLC.
- **IPCamLive support:** IPCamLive player links are resolved to their current live HLS stream automatically.
- **CDN-friendly playback:** a standard browser User-Agent is sent so CDN-hosted cameras that reject unknown players still load.
- **Crash-safe VLC shutdown:** every player is managed centrally and released cleanly, so closing tiles or the app doesn't hang or crash.

**Finding cameras**
- **Add CCTV library:** browse cameras **by country**, with **flags** and **banner previews** on every card.
- **Traffic / Residential filters** to narrow down what you're looking at.
- **My streams:** add your own cameras per country through simple `user_streams/<Country>/streams.json` files.
  A ready-made template folder with example entries is included for every country.

**Personalisation and settings**
- **Themes:** **Default (DedSec)**, **Dark** and **Light**. They apply instantly to every window and are remembered.
- **Settings page** with **Appearance**, **Account** and **About** sections.

**Account and security**
- **Free sign-up** with **email verification codes**.
- **Two-step verification (TOTP 2FA)** with any authenticator app; setup shows a QR code.
- **Remember me:** your credentials are stored in **Windows Credential Manager**, never in a plain file.
- **Always-show login:** the sign-in screen appears every time the app starts. With Remember me on, your details are filled in for you but never submitted automatically.
- **Device (HWID) lock:** each account is tied to one PC. If you change PCs, you can reset the lock with a code sent to your email.

**Updates**
- **Built-in auto-updater:** the app checks for new versions and installs them with a separate updater.

### Auth server (`server/`)

- **FastAPI** backend with a small, focused API (register, verify email, login, TOTP, refresh, logout, device reset).
- **Argon2id** password hashing.
- **JWT access tokens + rotating refresh tokens.**
- **Email verification codes** over **SMTP**. Works with any SMTP provider (Brevo, Mailgun, Postmark, SES, ...).
- **TOTP 2FA**, with secrets **encrypted at rest**.
- **Rate limiting** per IP and per account, with temporary **account lockout**.
- **Device / HWID lock** with emailed reset codes.
- **Production deployment** behind **Caddy** (automatic HTTPS), with **optional Cloudflare** in front.
- **Update hosting:** Caddy serves the app's release files and `latest.json` manifest for the auto-updater.
- **Safe defaults:** refuses to start in production while any setting is still a `CHANGE_ME` placeholder.
- Test suite included (`pytest`).

## Screenshots

> 📸 *Screenshots are coming soon.* They will show the camera wall, the Add CCTV library and the Settings page.
>
> <!-- Add images to docs/screenshots/ and reference them here, e.g.
> ![Camera wall](docs/screenshots/wall.png) -->


- Multi-camera wall with stable tiles (no flash on remove), failed-feed retry, and full-screen per feed
- Custom RTSP/HTTP feeds (My feeds) and stream search by id, name, city, country, host
- Settings → Updates: signed announcements and changelogs (same Ed25519 key as app updates)
- Min-version gate, VPN-exit block list, terms acceptance, Discord link, product-update email preference

## Two ways to use DedSec CCTV

| | **Official app** | **Build & self-host** |
|---|---|---|
| **Who it's for** | Anyone who just wants to watch | Developers, tinkerers, people who want full control |
| **How to get it** | Prebuilt `.exe` from [GitHub Releases][releases] | Build from this repository |
| **Account server** | DedSec Lab's official servers | Your own server (`server/`) |
| **Stream catalog** | DedSec Lab's curated catalog: **1,507 feeds, 26 countries**, growing | **No catalog included.** Add your own in `user_streams/` |
| **Your own cameras** | ✅ via *My streams* | ✅ via *My streams* |
| **Updates** | Automatic, via the in-app updater | You publish your own releases to your own update URL |
| **Cost** | Free | Free (you pay for your own hosting) |

**Please note:** the open-source code does **not** include the official stream catalog. Builds made from this
repository only show the cameras **you** add to `user_streams/`. The curated catalog is part of the official
app and DedSec Lab's servers.

## Quick start

### Use the official app
Go to [Download](#download). Nothing else is needed.

### Build & self-host

**1. Clone the repository**

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
```

**2. Set up the auth server.** You'll need a small Linux VPS, a domain and an SMTP account.

Follow **[server/docs/SERVER_SETUP.md](server/docs/SERVER_SETUP.md)**. It covers the VPS, firewall, `.env` secrets,
SMTP, DNS, optional Cloudflare, Caddy, systemd, backups and troubleshooting.

To try it locally first:

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # set ENVIRONMENT=development, EMAIL_BACKEND=console, generate secrets
pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**3. Set up and build the desktop app** (Windows, Python 3.10–3.13 64-bit, VLC 64-bit).

Follow **[app/docs/APP_SETUP.md](app/docs/APP_SETUP.md)**. It covers prerequisites, pointing the app at your server,
adding your streams, running from source, building the exe with Nuitka, and publishing updates.

```bat
cd app
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy config.ini.example config.ini   & rem set api_url and update_url to your server
venv\Scripts\python app.py
```

**4. Add your streams.** Edit `app/user_streams/<Country>/streams.json`:

```json
[
  {
    "id": "home-front-door",
    "name": "Front door",
    "city": "London",
    "rtsp": "rtsp://user:pass@192.0.2.10:554/stream1",
    "banner": "banners/front-door.jpg",
    "category": "residential"
  }
]
```

The `rtsp` field accepts `rtsp://`, `http(s)://` MJPEG or `.m3u8` HLS URLs. `category` is `residential` or `traffic`.
Entries marked `"example": true` are templates and are ignored. Reopen **Add CCTV** after editing.
See [app/user_streams/README.txt](app/user_streams/README.txt) for details.

## Project layout

```
.
├── LICENSE                     MIT license
├── README.md                   You are here
├── app/                        Windows desktop app (PyQt6 + libVLC)
│   ├── app.py                  The application: wall, Add CCTV library, settings, sign-in
│   ├── dedsec_auth_client.py   Client for the auth server (tokens, 2FA, device id)
│   ├── updater.py              Stand-alone updater used by the auto-update feature
│   ├── build.py                Nuitka build script -> DedSecCCTV\DedSecCCTV.exe
│   ├── config.ini.example      Server / update URL configuration
│   ├── .env.example            Same settings as environment variables
│   ├── requirements.txt
│   ├── assets/                 Logo, icon, wallpaper
│   ├── flags/                  Country flag images
│   ├── user_streams/           Your stream catalog (one template folder per country)
│   └── docs/APP_SETUP.md       App setup, build and release guide
└── server/                     FastAPI auth backend
    ├── app/                    API: config, models, security, rate limiting, emailer, routes
    ├── tests/                  pytest suite + smoke tests
    ├── deploy/                 Caddyfile, systemd unit, ufw script, update-hosting guide
    ├── .env.example
    ├── requirements.txt / requirements-dev.txt
    └── docs/SERVER_SETUP.md    Step-by-step production setup
```

## Updates & roadmap

- **Official releases** are published on **[GitHub Releases][releases]**, and existing installs pick them up
  automatically through the **in-app updater**.
- **More countries and more live feeds** are planned for the official catalog in future updates, building on today's
  1,309 feeds across 23 countries.
- The open-source code in this repository is updated alongside the releases.
- Self-hosters publish their own updates: bump `APP_VERSION`, build, and upload the release plus `latest.json`
  as described in [server/deploy/updates/README.md](server/deploy/updates/README.md).

## Security & trust

- **Open source, not a closed binary.** All of the app and server code is here, so you can audit what the
  official build does, or build your own from the same source.
- **Self-hostable authentication.** You can run the auth server on your own infrastructure, so accounts, password
  hashes and 2FA secrets stay with you.
- **How credentials are handled:** passwords are hashed with Argon2id on the server, and TOTP secrets are encrypted
  at rest. On the desktop, sign-in tokens and Remember me data are kept in Windows Credential Manager (with a
  DPAPI-encrypted fallback). The device lock sends only a hashed device ID, never raw hardware details.
- **Outbound connections, stated plainly.** Besides your auth/update server and the camera streams you open, the app
  contacts a few public services: a public-IP lookup at sign-in (`api.ipify.org` / `ifconfig.me`), the **Info**
  button's host/ISP lookup (`ip-api.com`), and IPCamLive's API when you play an IPCamLive feed. You can check all of
  this in [`app/app.py`](app/app.py).
- **Use it responsibly.** Only add cameras you own or are allowed to view, and follow the laws where you live.

Found a security issue? Please report it privately to the maintainers, for example through GitHub's
*Report a vulnerability* feature if it's enabled, rather than opening a public issue.

## Terms of use

By using DedSec CCTV you agree to the [Terms of use](TERMS.md).

## Security and bug bounty

Found a security problem? Please report it privately through GitHub's Report a vulnerability button on the Security tab, not in public issues or on Discord. See [SECURITY.md](SECURITY.md) for the full policy, what is in scope, and how the bug bounty works.

## Contributing

Contributions are welcome: bug reports, fixes, documentation and ideas.

1. Fork the repository and create a branch for your change.
2. Keep changes focused. For server changes, run `pytest -q` in `server/`.
3. Never commit secrets, real `.env` / `config.ini` files, or private stream lists.
4. Open a pull request that explains what changed and why.

For bigger changes, please open an issue first so we can talk about the approach.

## License

Released under the **[MIT License](LICENSE)**. Copyright © 2026 DedSec Lab.

[releases]: ../../releases/latest
