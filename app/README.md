# DedSec CCTV — desktop app (open source)

PyQt6 + libVLC camera wall for Windows: up to 4 of your own RTSP/MJPEG/HLS cameras plus your own
per-country stream lists ("My streams", in `user_streams/`). Sign-in, 2FA, device (HWID) binding,
remember-me and auto-update talk to a backend **you host yourself** (`../server/`).

* Setup, run from source, build: [docs/APP_SETUP.md](docs/APP_SETUP.md)
* Server: [../server/docs/SERVER_SETUP.md](../server/docs/SERVER_SETUP.md)
* Just want to use it? The official prebuilt release is distributed separately; this repo is for self-hosting/building.

Quick start (Windows, Python 3.10–3.13 64-bit, VLC 64-bit installed):
```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy config.ini.example config.ini   & rem then set api_url / update_url
venv\Scripts\python app.py
```
License: MIT (see ../LICENSE).
