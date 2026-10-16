# DedSec CCTV desktop app — setup

> **Just want to use the app?** An official prebuilt Windows release is published separately.
> This guide is for people who want to run it from source, point it at their own backend, or build it.

## 1. Install the prerequisites (Windows 10/11, 64-bit)
1. **Python 3.10–3.13, 64-bit** from https://www.python.org/downloads/windows/ — tick
   "Add python.exe to PATH". (`winget install Python.Python.3.12` also works.)
2. **VLC media player, 64-bit** from https://www.videolan.org/ or `winget install VideoLAN.VLC`.
   The feeds are played through libVLC (`python-vlc` needs the installed VLC DLLs; 32-bit VLC will not load).
3. **Python packages** (PyQt6, python-vlc, requests, keyring, qrcode, cryptography, Nuitka):
   ```bat
   cd app
   python -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```

## 2. Point the app at your backend
The app has **no working server built in** — the defaults are placeholders (`https://auth.example.com`).
Host the backend first: [../../server/docs/SERVER_SETUP.md](../../server/docs/SERVER_SETUP.md).

Then pick one (highest priority first):
* Environment variables: `DEDSEC_API_URL`, `DEDSEC_UPDATE_URL` (see `.env.example`):
  ```bat
  set DEDSEC_API_URL=https://auth.yourdomain.com
  set DEDSEC_UPDATE_URL=https://auth.yourdomain.com/updates/
  ```
* `config.ini` next to `app.py` / `DedSecCCTV.exe` (copy `config.ini.example`):
  ```ini
  [server]
  api_url = https://auth.yourdomain.com
  update_url = https://auth.yourdomain.com/updates/
  blocked_ips =
  ```
* Or edit `DEFAULT_API_URL` / `DEFAULT_UPDATE_URL` near the top of `app.py` before building.

Both URLs must be `https://`. For a local dev server only: `set DEDSEC_DEV=1` +
`set DEDSEC_API_URL=http://127.0.0.1:8000` (plain http is then allowed for localhost only).
`DEDSEC_DEV=1` + `DEDSEC_NO_UPDATE=1` skips the update check.

## 3. Your streams (`user_streams/`)
The app only shows **My streams**: `user_streams/<Country>/streams.json` next to the app/exe
(or `%APPDATA%\DedSecCctv\user_streams` if that folder is read-only). The repo ships a template
(one folder per country, 3 example entries each, empty `banners/`, `README.txt`); the app also
re-creates missing folders on start and never overwrites your files.

```json
[
  {"id": "uk-home-1", "name": "Front door", "city": "London",
   "rtsp": "rtsp://user:pass@192.168.1.20:554/stream1",
   "banner": "banners/front-door.jpg", "category": "residential"}
]
```
* `rtsp` may be `rtsp://`, `http(s)://` MJPEG, or an `.m3u8` HLS URL.
* `category`: `residential` or `traffic`. `banner` is optional, relative to the country folder.
* Entries with `"example": true` are ignored — copy one, delete that line, fill in your camera.
* Reopen **Add CCTV** after editing. Only add cameras you own or are allowed to view.

## 4. Run from source
```bat
venv\Scripts\python app.py
```

## 5. Build a Windows exe with Nuitka
```bat
venv\Scripts\python build.py
```
`build.py` uses `venv\Scripts\python.exe` if present, installs Nuitka/deps if missing, and builds a
standalone, console-less `DedSecCCTV\DedSecCCTV.exe` with `assets\` and `flags\` bundled. It also
copies `config.ini` (if you created one) and the `user_streams\` template next to the exe.
No stream catalog is packed. The first build downloads a C compiler (MinGW64) or uses MSVC — answer "yes".
Ship the whole `DedSecCCTV\` folder.

Since 1.0.3, `build.py` also builds the updater and bundles it inside the app as
`DedSecCCTV\assets\updater\DedSecUpdater.bin` (no separate copy in the install root). Rebuild just the
updater with menu option 13, or:
```bat
venv\Scripts\python -m nuitka --onefile --windows-console-mode=disable --enable-plugin=tk-inter --output-filename=DedSecUpdater.exe updater.py
```

## 6. Publishing updates
Bump `APP_VERSION` in `app.py`, build, zip the **contents** of `DedSecCCTV\`, and publish it with a
`latest.json` under your `update_url` — exact format in
[../../server/deploy/updates/README.md](../../server/deploy/updates/README.md).

## Notes
* Sign-in tokens live in Windows Credential Manager (`DedSecCCTV-auth`), falling back to a
  DPAPI-encrypted `%APPDATA%\DedSecCctv\session.bin`.
* If a build is missing the keyring backend or certificates, check `--include-distribution-metadata=keyring`
  and `--include-package-data=certifi` in `build.py`.
