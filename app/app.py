#!/usr/bin/env python3
"""Private CCTV wall. Plays only RTSP addresses the user saved."""

import base64
import hashlib
import json
import re
import shutil
import tempfile
import time
import zipfile
from collections import OrderedDict
from urllib.request import Request, urlopen
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

from PyQt6.QtCore import (Qt, QTimer, QObject, pyqtSignal, QEventLoop, QRectF, QSize, QPointF, QEvent, QPoint,
                          QPropertyAnimation, QEasingCurve, pyqtProperty)
from PyQt6.QtGui import (
    QFont, QFontMetrics, QPixmap, QPainter, QColor, QIcon, QPalette, QImage, QLinearGradient, QPen, QPainterPath,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QDialog, QLineEdit, QComboBox, QCheckBox,
    QListWidget, QListWidgetItem, QMessageBox, QSizePolicy, QStackedWidget, QMenu, QProgressBar,
    QScrollArea, QFileDialog,
)

from dedsec_auth_client import AuthClient, AuthError, DeviceLocked, SavedLogin, TotpRequired

# ---------------------------------------------------------------------------
# Themes
# Every stylesheet in this file is written in the DedSec ("Default") colours. A theme is a
# palette dict that maps those Default colours to its own: explicit role overrides first,
# then a generated mapping for the remaining shades (neutral / accent / status families).
# setStyleSheet, QLabel rich text and the drawn icons all pass through _tc()/themed_css(),
# so switching theme re-styles every open window. "Default" is an identity map.
# ---------------------------------------------------------------------------
import colorsys
from PyQt6 import sip
from PyQt6.QtWidgets import QAbstractButton as _QAbstractButton

THEMES = ("Default", "Dark", "Light")
_THEME_STATE = {"name": "Default"}

# Role overrides (Default colour -> theme colour). Anything not listed is generated.
PALETTES = {
    "Default": {},
    "Dark": {
        "#22d3ee": "#9fb3c8",   # accent (cyan) -> subtle steel
        "#67e8f9": "#c3d2e0",
        "#06202a": "#0b0b0c",   # text on accent buttons
        "#060606": "#0b0b0c",   # window background
        "#0a1624": "#141416",
        "#e8f0ff": "#f2f2f2",   # main text
        "#ffffff": "#ffffff",
    },
    "Light": {
        "#22d3ee": "#0e7490",   # accent -> deep teal (readable on light)
        "#67e8f9": "#0b5f75",
        "#06202a": "#ffffff",   # text on accent buttons
        "#060606": "#eef1f5",   # window background
        "#050505": "#eef1f5",
        "#0a1624": "#ffffff",   # panels / dialogs
        "#07111c": "#f4f6f9",
        "#101820": "#ffffff",
        "#e8f0ff": "#16202b",   # main text
        "#ffffff": "#0f1720",   # bright headings
        "#d5dde8": "#2a3644",
        "#cfe3f2": "#26394b",
        "#c9d6e6": "#2a3644",
        "#8ea0b5": "#566578",   # muted text
        "#5f7a92": "#6b7a8c",
        "#1d4e68": "#c3ced9",   # borders
        "#16324a": "#cdd6df",
        "#7dffb0": "#13733f",   # status pill: live / ok
        "#14361f": "#dcf3e4",
        "#2fbf6a": "#2f9e5c",
        "#ff5c6c": "#c0263a",   # status pill: error
        "#3a1418": "#fbe1e4",
        "#fcd34d": "#8a5a00",   # status pill: warning
        "#2a2210": "#fbf0d5",
        "#ff3b3b": "#d11f1f",
        "#c42b1c": "#c42b1c",   # title-bar close hover stays red
        "#a3241a": "#a3241a",
    },
}
_GEN_CACHE: dict = {}
_HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGBA_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(,\s*[\d.]+\s*)?\)")


def current_theme() -> str:
    return _THEME_STATE["name"]


def _family(h: float, l: float, s: float) -> str:
    deg = h * 360
    if s > 0.35 and l > 0.08 and (deg < 20 or deg > 330 or 30 <= deg <= 65 or 90 <= deg <= 170):
        return "status"
    if s > 0.45 and 170 < deg <= 215 and l >= 0.35:
        return "accent"
    return "neutral"


def _gen_rgb(rgb: tuple, theme: str) -> tuple:
    r, g, b = (c / 255 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    fam = _family(h, l, s)
    if theme == "Dark":
        if fam == "neutral":
            h, s = 0.6, min(s, 0.05)
        elif fam == "accent":
            s = min(s, 0.22)
    elif theme == "Light":
        if fam == "neutral":
            l, s = 0.06 + 0.92 * (1 - l), min(s, 0.25)
        elif fam == "accent":
            l = min(l, 0.5) * 0.72
        else:
            l = 0.32 if l > 0.5 else (0.91 if l < 0.3 else l * 0.85)
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return round(r * 255), round(g * 255), round(b * 255)


def _theme_hex(value: str, theme: str | None = None) -> str:
    theme = theme or _THEME_STATE["name"]
    if theme == "Default":
        return value
    v = value.lower()
    if len(v) == 4:
        v = "#" + "".join(ch * 2 for ch in v[1:])
    hit = PALETTES.get(theme, {}).get(v)
    if hit:
        return hit
    key = (theme, v)
    if key not in _GEN_CACHE:
        rgb = tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
        _GEN_CACHE[key] = "#%02x%02x%02x" % _gen_rgb(rgb, theme)
    return _GEN_CACHE[key]


def themed_css(css: str, theme: str | None = None) -> str:
    theme = theme or _THEME_STATE["name"]
    if not css or theme == "Default":
        return css
    css = _HEX_RE.sub(lambda m: _theme_hex(m.group(0), theme), css)

    def _rgba(m):
        rgb = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        hx = _theme_hex("#%02x%02x%02x" % rgb, theme)
        r, g, b = (int(hx[i:i + 2], 16) for i in (1, 3, 5))
        alpha = m.group(4) or ""
        return f"rgba({r}, {g}, {b}{alpha})" if alpha else f"rgb({r}, {g}, {b})"
    return _RGBA_RE.sub(_rgba, css)


def _tc(*args) -> QColor:
    """QColor in the current theme (same arguments as QColor)."""
    col = QColor(*args)
    if _THEME_STATE["name"] == "Default" or not col.isValid():
        return col
    out = QColor(_theme_hex(col.name()))
    out.setAlpha(col.alpha())
    return out


# Route every stylesheet / rich-text label / drawn icon through the theme.
_ORIG_SET_STYLESHEET = QWidget.setStyleSheet
_ORIG_LABEL_SET_TEXT = QLabel.setText
_ORIG_SET_ICON = _QAbstractButton.setIcon


def _themed_set_stylesheet(self, css):
    css = css or ""
    self.setProperty("_dsBaseCss", css)
    _ORIG_SET_STYLESHEET(self, css if self.property("_dsNoTheme") else themed_css(css))


def _themed_label_set_text(self, text):
    text = text if text is not None else ""
    if "color" in text and "#" in text:
        self.setProperty("_dsBaseText", text)
        text = themed_css(text)
    elif self.property("_dsBaseText") is not None:
        self.setProperty("_dsBaseText", None)
    _ORIG_LABEL_SET_TEXT(self, text)


def _themed_set_icon(self, icon):
    spec = getattr(icon, "_ds_spec", None)
    self.setProperty("_dsIconSpec", json.dumps(spec) if spec else None)
    _ORIG_SET_ICON(self, icon)


QWidget.setStyleSheet = _themed_set_stylesheet
QLabel.setText = _themed_label_set_text
_QAbstractButton.setIcon = _themed_set_icon


class _RichTextThemer(QObject):
    """Labels built as QLabel("<span style=...>") skip setText; theme them when first polished."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Polish and isinstance(obj, QLabel) and obj.property("_dsBaseText") is None:
            text = obj.text()
            if "color" in text and "#" in text:
                _themed_label_set_text(obj, text)
        return False


_RICH_THEMER = None


def apply_theme(name: str, save: bool = True) -> None:
    """Switch theme and re-style every live widget."""
    if name not in THEMES:
        name = "Default"
    _THEME_STATE["name"] = name
    _PIX_CACHE.clear() if "_PIX_CACHE" in globals() else None
    if save:
        save_setting("theme", name)
    app = QApplication.instance()
    if app is None:
        return
    global _RICH_THEMER
    if _RICH_THEMER is None:
        _RICH_THEMER = _RichTextThemer()
        app.installEventFilter(_RICH_THEMER)
    _apply_app_palette(app)
    for w in app.allWidgets():
        if isinstance(w, QLabel) and w.property("_dsBaseText") is None:
            text = w.text()
            if "color" in text and "#" in text:
                w.setProperty("_dsBaseText", text)
        base = w.property("_dsBaseCss")
        if base is not None:
            _ORIG_SET_STYLESHEET(w, base if w.property("_dsNoTheme") else themed_css(base))
        if isinstance(w, QLabel):
            btext = w.property("_dsBaseText")
            if btext is not None:
                _ORIG_LABEL_SET_TEXT(w, themed_css(btext))
        if isinstance(w, _QAbstractButton):
            spec = w.property("_dsIconSpec")
            if spec:
                fn, args = json.loads(spec)
                maker = globals().get(fn)
                if maker is not None:
                    _ORIG_SET_ICON(w, maker(*args))
        hook = getattr(w, "on_theme_changed", None)
        if callable(hook):
            try:
                hook()
            except Exception:
                pass
        w.update()


def _apply_app_palette(app) -> None:
    pal = app.palette()
    for role, base in ((QPalette.ColorRole.Window, "#060606"), (QPalette.ColorRole.Base, "#060606"),
                       (QPalette.ColorRole.Button, "#060606"), (QPalette.ColorRole.WindowText, "#e8f0ff"),
                       (QPalette.ColorRole.Text, "#e8f0ff"), (QPalette.ColorRole.ButtonText, "#e8f0ff")):
        if role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText) \
                and _THEME_STATE["name"] == "Default":
            continue  # Default keeps Fusion's own text colours, as before
        pal.setColor(role, _tc(base))
    app.setPalette(pal)


# ---------------------------------------------------------------------------
# App settings: kept in the existing %APPDATA%/DedSecCctv/cctv_wall.json under "settings".
# ---------------------------------------------------------------------------
def _settings_file() -> Path:
    base = Path.home() / "AppData" / "Roaming" / "DedSecCctv"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cctv_wall.json"


def load_settings() -> dict:
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8"))
        settings = data.get("settings") if isinstance(data, dict) else None
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def save_setting(key: str, value) -> None:
    path = _settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    settings[key] = value
    data["settings"] = settings
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App version (semver). Bump it for every release you publish; see docs/APP_SETUP.md and server/deploy/updates/README.md.
# ---------------------------------------------------------------------------
APP_VERSION = "1.0.0"
# Auto-update: static files served by Caddy on the same VPS (never the API). Only HTTPS, and only
# files under this folder are ever downloaded; the zip's SHA-256 must match latest.json.
def _load_app_config() -> dict:
    """Optional config.ini next to app.py / the exe ([server] api_url, update_url, blocked_ips)."""
    import configparser
    cfg = configparser.ConfigParser()
    roots = []
    try:
        roots.append(Path(sys.argv[0]).resolve().parent)
    except Exception:
        pass
    roots.append(Path(__file__).resolve().parent)
    for root in roots:
        path = root / "config.ini"
        if path.is_file():
            try:
                cfg.read(path, encoding="utf-8")
                break
            except Exception as exc:
                print(f"config.ini could not be read: {exc}", file=sys.stderr)
    return dict(cfg["server"]) if cfg.has_section("server") else {}


_APP_CONFIG = _load_app_config()


_RUNNING_COMPILED = bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()
# Developer mode only for source runs; a compiled build never honours DEDSEC_DEV.
DEDSEC_DEV = (not _RUNNING_COMPILED) and os.environ.get("DEDSEC_DEV", "").strip() == "1"


def _config_value(env: str, key: str, default: str) -> str:
    """Source runs: environment variable > config.ini [server] key > default.

    Compiled builds ignore environment variables, so nobody can silently redirect an installed
    app to another server; self-hosters set their server in the visible config.ini next to the exe.
    """
    env_val = "" if _RUNNING_COMPILED else os.environ.get(env, "").strip()
    return (env_val or str(_APP_CONFIG.get(key, "")).strip() or default)


# Placeholders: point these at YOUR self-hosted backend (see docs/APP_SETUP.md).
DEFAULT_API_URL = "https://auth.example.com"
DEFAULT_UPDATE_URL = "https://auth.example.com/updates/"
UPDATE_BASE_URL = _config_value("DEDSEC_UPDATE_URL", "update_url", DEFAULT_UPDATE_URL)
if not UPDATE_BASE_URL.endswith("/"):
    UPDATE_BASE_URL += "/"
UPDATE_CHECK_TIMEOUT = 8.0
UPDATE_MAX_BYTES = 1024 * 1024 * 1024  # refuse anything bigger than 1 GiB
UPDATER_EXE_NAME = "DedSecUpdater.exe"
UPDATER_SCRIPT_NAME = "updater.py"
# Ed25519 public key (base64, 32 raw bytes) that signs latest.json. Generate your own keypair offline
# with tools/sign_update.py keygen; set it here or as update_public_key in config.ini [server].
# While it is the placeholder, every update is rejected (see docs/UPDATE_SIGNING.md).
UPDATE_PUBLIC_KEY_B64 = str(_APP_CONFIG.get("update_public_key", "")).strip() or "PASTE_ED25519_PUBLIC_KEY_HERE"
UPDATE_SIG_SUFFIX = ".sig"

# ---------------------------------------------------------------------------
# Account server (self-hosted backend from server/, see server/docs/SERVER_SETUP.md)
# Set with the DEDSEC_API_URL environment variable or api_url in config.ini.
# Must be https:// in release builds. DEDSEC_DEV=1 additionally allows plain http://
# to 127.0.0.1/localhost (local testing only).
# ---------------------------------------------------------------------------
API_BASE_URL = _config_value("DEDSEC_API_URL", "api_url", DEFAULT_API_URL).rstrip("/")
AUTH_TIMEOUT_SECONDS = 10.0


MAX_CAMERAS = 4
WALL_SLOTS = 4
# Optional public IPs that may not use the app (comma-separated), e.g. known VPN exits.
BLOCKED_VPN_IPS = {ip.strip() for ip in _config_value("DEDSEC_BLOCKED_IPS", "blocked_ips", "").split(",") if ip.strip()}
_PUBLIC_IP = ""
_IP_CHECKED = False


def _read_public_ip() -> str:
    global _PUBLIC_IP, _IP_CHECKED
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            req = Request(url, headers={"User-Agent": "DedSecCCTV"})
            with urlopen(req, timeout=4) as resp:
                ip = resp.read().decode().strip()
            if ip:
                _PUBLIC_IP = ip
                break
        except Exception:
            continue
    _IP_CHECKED = True


def _vpn_blocked() -> bool:
    return _PUBLIC_IP in BLOCKED_VPN_IPS

ALL_COUNTRIES = (
    "Afghanistan","Albania","Algeria","Andorra","Angola","Antigua and Barbuda","Argentina","Armenia","Australia","Austria",
    "Azerbaijan","Bahamas","Bahrain","Bangladesh","Barbados","Belarus","Belgium","Belize","Benin","Bhutan",
    "Bolivia","Bosnia and Herzegovina","Botswana","Brazil","Brunei","Bulgaria","Burkina Faso","Burundi","Cabo Verde","Cambodia",
    "Cameroon","Canada","Central African Republic","Chad","Chile","China","Colombia","Comoros","Congo","Costa Rica",
    "Croatia","Cuba","Cyprus","Czechia","Denmark","Djibouti","Dominica","Dominican Republic","Ecuador","Egypt",
    "El Salvador","Equatorial Guinea","Eritrea","Estonia","Eswatini","Ethiopia","Fiji","Finland","France","Gabon",
    "Gambia","Georgia","Germany","Ghana","Greece","Grenada","Guatemala","Guinea","Guinea-Bissau","Guyana",
    "Haiti","Honduras","Hungary","Iceland","India","Indonesia","Iran","Iraq","Ireland","Israel",
    "Italy","Jamaica","Japan","Jordan","Kazakhstan","Kenya","Kiribati","Kuwait","Kyrgyzstan","Laos",
    "Latvia","Lebanon","Lesotho","Liberia","Libya","Liechtenstein","Lithuania","Luxembourg","Madagascar","Malawi",
    "Malaysia","Maldives","Mali","Malta","Marshall Islands","Mauritania","Mauritius","Mexico","Micronesia","Moldova",
    "Monaco","Mongolia","Montenegro","Morocco","Mozambique","Myanmar","Namibia","Nauru","Nepal","Netherlands",
    "New Zealand","Nicaragua","Niger","Nigeria","North Korea","North Macedonia","Norway","Oman","Pakistan","Palau",
    "Palestine","Panama","Papua New Guinea","Paraguay","Peru","Philippines","Poland","Portugal","Qatar","Romania",
    "Russia","Rwanda","Saint Kitts and Nevis","Saint Lucia","Saint Vincent and the Grenadines","Samoa","San Marino","Sao Tome and Principe","Saudi Arabia","Senegal",
    "Serbia","Seychelles","Sierra Leone","Singapore","Slovakia","Slovenia","Solomon Islands","Somalia","South Africa","South Korea",
    "South Sudan","Spain","Sri Lanka","Sudan","Suriname","Sweden","Switzerland","Syria","Taiwan","Tajikistan",
    "Tanzania","Thailand","Timor-Leste","Togo","Tonga","Trinidad and Tobago","Tunisia","Turkey","Turkmenistan","Tuvalu",
    "Uganda","Ukraine","United Arab Emirates","United Kingdom","United States","Uruguay","Uzbekistan","Vanuatu","Vatican City","Venezuela",
    "Vietnam","Yemen","Zambia","Zimbabwe",
)


# ---------------------------------------------------------------------------
# Stream catalog: "My streams" only.
#   user_streams/<Country>/streams.json next to the exe (writable, the user's own).
# ---------------------------------------------------------------------------
SOURCE_USER = "user"
_ROWS_CACHE: dict = {}


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent


_USER_ROOT = None


def user_streams_root() -> Path:
    """user_streams/ next to the exe; %APPDATA%/DedSecCctv/user_streams if that folder is read-only."""
    global _USER_ROOT
    if _USER_ROOT is None:
        root = _exe_dir() / "user_streams"
        if not root.is_dir():
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                root = Path.home() / "AppData" / "Roaming" / "DedSecCctv" / "user_streams"
        _USER_ROOT = root
    return _USER_ROOT


def _source_root(source: str = SOURCE_USER) -> Path:
    return user_streams_root()


def _read_folder_rows(folder: Path):
    rows = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("streams") or data.get("cameras") or []
        if not isinstance(data, list):
            continue
        for row in data:
            if isinstance(row, dict) and not row.get("example"):  # template examples are ignored
                rows.append(row)
    return rows


def _load_country_streams(country: str, source: str = SOURCE_USER):
    return _read_folder_rows(_source_root(source) / country)


def _source_folder_names(source: str = SOURCE_USER):
    root = _source_root(source)
    try:
        return [p.name for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    except OSError:
        return []


def catalog_available(source: str = SOURCE_USER) -> bool:
    return _source_root(source).is_dir()


def catalog_countries(query: str = "", source: str = SOURCE_USER):
    needle = (query or "").strip().lower()
    names = list(ALL_COUNTRIES)
    for name in _source_folder_names(source):
        if name not in names:
            names.append(name)
    if not needle:
        return names
    return [name for name in names if needle in name.lower()]


def catalog_streams(country: str, source: str = SOURCE_USER):
    out = []
    for row in _load_country_streams(country, source):
        if not isinstance(row, dict) or not _valid_rtsp(row.get("rtsp", "")):
            continue
        if not row.get("category"):
            rtsp = str(row.get("rtsp") or "").strip().lower()
            row["category"] = "traffic" if rtsp.startswith(("http://", "https://")) else "residential"
        row["_source"] = SOURCE_USER
        out.append(row)
    return out


def stream_counts(source: str = SOURCE_USER):
    counts = {}
    total = 0
    names = list(ALL_COUNTRIES)
    for name in _source_folder_names(source):
        if name not in names:
            names.append(name)
    for name in names:
        count = len(catalog_streams(name, source))
        if count:
            counts[name] = count
            total += count
    return counts, total


def user_country_names():
    """Every country the app knows (ALL_COUNTRIES + one name per extra flag code)."""
    names = list(ALL_COUNTRIES)
    seen = {COUNTRY_CODES.get(n) for n in names}
    for name, code in COUNTRY_CODES.items():
        if code in seen or any(ch in name for ch in ",()") or not name.isascii():
            continue
        seen.add(code)
        names.append(name)
    return sorted(dict.fromkeys(names), key=str.lower)


USER_STREAMS_README = """DedSec CCTV - your own stream catalog
=====================================

This folder is yours. The app reads it and shows it in Add CCTV under "My streams".
It is never overwritten (missing country folders are re-created, nothing else is touched).

Layout
  user_streams/<Country>/streams.json     list of your streams for that country
  user_streams/<Country>/banners/         optional preview images (PNG/JPG/WebP)

streams.json format (a JSON list, or {"streams": [...]}):
  [
    {
      "id": "uk-home-1",                      unique id (letters, digits, - _ .)
      "name": "Front door",                   shown on the card
      "city": "London",
      "rtsp": "rtsp://user:pass@192.0.2.10:554/stream1",
                                              rtsp://, http(s):// MJPEG, or .m3u8 HLS URL
      "banner": "banners/front-door.jpg",     optional, relative to this country folder
      "category": "residential"               "residential" or "traffic"
    }
  ]

Entries with "example": true are templates and are ignored by the app.
Copy one, remove the "example" line, and fill in your own camera.
The example addresses (192.0.2.x, example.com) are documentation-only and do not exist.
Reopen Add CCTV after editing.
"""


def _user_template(country: str) -> list:
    slug = re.sub(r"[^a-z0-9]+", "-", country.lower()).strip("-") or "country"
    return [
        {"example": True, "id": f"{slug}-example-rtsp", "name": "Example RTSP camera", "city": "Example City",
         "rtsp": "rtsp://username:password@192.0.2.10:554/stream1",
         "banner": "banners/example-rtsp.jpg", "category": "residential"},
        {"example": True, "id": f"{slug}-example-mjpeg", "name": "Example MJPEG camera", "city": "Example City",
         "rtsp": "http://192.0.2.20:8080/video.mjpg",
         "banner": "banners/example-mjpeg.jpg", "category": "traffic"},
        {"example": True, "id": f"{slug}-example-hls", "name": "Example HLS stream", "city": "Example City",
         "rtsp": "https://example.com/live/stream.m3u8",
         "banner": "", "category": "traffic"},
    ]


def ensure_user_streams(root: Path | None = None) -> Path:
    """Create user_streams/ with a folder per country (template json, banners/, README) if missing."""
    root = root or user_streams_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        readme = root / "README.txt"
        if not readme.exists():
            readme.write_text(USER_STREAMS_README, encoding="utf-8")
        for name in user_country_names():
            folder = root / name
            (folder / "banners").mkdir(parents=True, exist_ok=True)
            js = folder / "streams.json"
            if not js.exists():
                js.write_text(json.dumps(_user_template(name), indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"user_streams could not be created: {exc}", file=sys.stderr)
    return root


# ---------------------------------------------------------------------------
# Stream banners / screenshots
# A stream row may carry an optional preview image under one of BANNER_KEYS (first hit
# wins). The value is a filename or relative path, looked up in this order:
#   streams/<Country>/<value>
#   streams/<Country>/banners/<value>
#   streams/<value>                      (path relative to the streams root)
# Absolute paths are honoured too. Only png/jpg/jpeg/webp/bmp/gif files are used.
# See user_streams/README.txt for the JSON shape.
# ---------------------------------------------------------------------------
BANNER_KEYS = ("banner", "thumb", "thumbnail", "image", "preview")
BANNER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
BANNER_W, BANNER_H = 220, 124
_PIX_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_PIX_CACHE_MAX = 160


def _banner_store_dir() -> Path:
    path = _store_path().parent / "banners"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _usable_image(path: Path) -> bool:
    try:
        return path.suffix.lower() in BANNER_EXTS and path.is_file()
    except Exception:
        return False


def _stream_banner_path(country: str, row, source: str = SOURCE_USER):
    if not isinstance(row, dict):
        return None
    root = _source_root(source)
    folder = root / (country or "")
    for key in BANNER_KEYS:
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        rel = Path(value.strip().replace("\\", "/"))
        candidates = [rel] if rel.is_absolute() else [folder / rel, folder / "banners" / rel, root / rel]
        for path in candidates:
            if _usable_image(path):
                return path
    return None


def _camera_banner_path(cam) -> Path | None:
    """Banner for a saved camera on the main wall.

    Tries, in order:
    1. Absolute / still-valid ``banner`` path stored on the camera
    2. AppData copy (%APPDATA%/DedSecCctv/banners/<id>.png)
    3. Re-resolve from the catalog (country + stream id) so relative
       ``banners/foo.jpg`` paths and moved streams folders still work
    """
    if not isinstance(cam, dict):
        return None
    value = cam.get("banner")
    if isinstance(value, str) and value.strip():
        path = Path(value.strip())
        if _usable_image(path):
            return path
        # Relative path that was saved as-is: try streams/<country>/…
        country = str(cam.get("country") or "")
        if country and not path.is_absolute():
            found = _stream_banner_path(country, {"banner": value.strip()})
            if found is not None:
                return found
    cam_id = str(cam.get("id") or "")
    if cam_id and re.fullmatch(r"[A-Za-z0-9_.-]+", cam_id):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            path = _store_path().parent / "banners" / f"{cam_id}{ext}"
            if _usable_image(path):
                return path
    # Catalog stream: look the banner up again from the catalog it came from
    country = str(cam.get("country") or "")
    if country and cam_id:
        row = next((r for r in catalog_streams(country, SOURCE_USER) if r.get("id") == cam_id), None)
        if row is not None:
            found = _stream_banner_path(country, row, SOURCE_USER)
            if found is not None:
                return found
    return None


def _placeholder_pixmap(w: int, h: int, text: str = "No preview") -> QPixmap:
    pix = QPixmap(w, h)
    pix.fill(_tc("#07111c"))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0.0, _tc("#0a1624"))
    grad.setColorAt(1.0, _tc("#060606"))
    p.fillRect(pix.rect(), grad)
    p.setPen(_tc(34, 211, 238, 22))
    for y in range(0, h, 4):  # faint scanlines
        p.drawLine(0, y, w, y)
    s = max(14, min(w, h) // 4)
    cx, cy = w // 2, h // 2 - (s // 3 if h >= 60 else 0)
    p.setPen(QPen(_tc("#2b6f86"), max(2, s // 12)))
    p.setBrush(_tc("#0d2234"))
    p.drawRoundedRect(QRectF(cx - s * 0.9, cy - s * 0.45, s * 1.4, s * 0.9), s * 0.15, s * 0.15)
    p.drawEllipse(QRectF(cx - s * 0.48, cy - s * 0.3, s * 0.6, s * 0.6))
    tri = QPainterPath()
    tri.moveTo(cx + s * 0.5, cy - s * 0.12)
    tri.lineTo(cx + s * 0.9, cy - s * 0.38)
    tri.lineTo(cx + s * 0.9, cy + s * 0.38)
    tri.lineTo(cx + s * 0.5, cy + s * 0.12)
    tri.closeSubpath()
    p.drawPath(tri)
    p.setBrush(_tc("#22d3ee"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(cx - s * 0.25, cy - s * 0.07, s * 0.14, s * 0.14))
    if text and h >= 60:
        font = QFont("Consolas", max(8, h // 14))
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(font)
        p.setPen(_tc("#5f7a92"))
        p.drawText(QRectF(0, cy + s * 0.55, w, h - (cy + s * 0.55)),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, text.upper())
    p.end()
    return pix


def _banner_pixmap(path: Path | None, w: int, h: int, placeholder: str = "No preview") -> QPixmap:
    """Cover-cropped w×h pixmap of ``path`` (LRU-cached), or a dark placeholder."""
    key = None
    if path is not None:
        try:
            key = (str(path), path.stat().st_mtime_ns, w, h)
        except OSError:
            key = None
    if key is None:
        key = ("<placeholder>", placeholder, _THEME_STATE["name"], w, h)
    hit = _PIX_CACHE.get(key)
    if hit is not None:
        _PIX_CACHE.move_to_end(key)
        return hit
    pix = None
    if path is not None and key[0] != "<placeholder>":
        img = QImage(str(path))
        if not img.isNull():
            scaled = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            x = max(0, (scaled.width() - w) // 2)
            y = max(0, (scaled.height() - h) // 2)
            pix = QPixmap.fromImage(scaled.copy(x, y, w, h))
    if pix is None:
        pix = _placeholder_pixmap(w, h, placeholder)
    _PIX_CACHE[key] = pix
    while len(_PIX_CACHE) > _PIX_CACHE_MAX:
        _PIX_CACHE.popitem(last=False)
    return pix


def _stream_banner_pixmap(country: str, row, w: int = BANNER_W, h: int = BANNER_H,
                          source: str = SOURCE_USER) -> QPixmap:
    return _banner_pixmap(_stream_banner_path(country, row, source), w, h)


def _save_camera_banner(src: str, cam_id: str) -> str:
    """Copy a picked image to %APPDATA%/DedSecCctv/banners/<id>.png so it survives; return the path."""
    dest = _banner_store_dir() / f"{cam_id}.png"
    img = QImage(src)
    if not img.isNull():
        if img.width() > 1280:
            img = img.scaledToWidth(1280, Qt.TransformationMode.SmoothTransformation)
        if img.save(str(dest), "PNG"):
            return str(dest)
    dest = dest.with_suffix(Path(src).suffix.lower() or ".png")
    shutil.copyfile(src, dest)
    return str(dest)


def _store_path() -> Path:
    base = Path.home() / "AppData" / "Roaming" / "DedSecCctv"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cctv_wall.json"


def _mask_rtsp(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return "no address saved"
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or ""
        return f"rtsp://{host}{port}{path}" if host else "rtsp address saved"
    except Exception:
        return "rtsp address saved"


def _valid_rtsp(url: str) -> bool:
    raw = (url or "").strip()
    if not raw.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return False
    parsed = urlparse(raw)
    return bool(parsed.hostname)


CLIP_RELOAD_MS = 180_000


def _is_clip_url(url: str) -> bool:
    try:
        return urlparse((url or "").strip()).path.lower().endswith(".mp4")
    except Exception:
        return False


def _is_clip(row) -> bool:
    if not isinstance(row, dict):
        return False
    return str(row.get("type") or "").lower() == "mp4" or _is_clip_url(row.get("rtsp", ""))


CLIP_LABEL = "Near-live (updates every few min)"


def _cache_bust(url: str) -> str:
    import time as _t
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_t={int(_t.time())}"


def _resolve_ipcamlive(url: str) -> str:
    """IPCamLive player URLs rotate their HLS stream id; look up the current one."""
    try:
        parsed = urlparse(url or "")
        if not (parsed.hostname or "").endswith("ipcamlive.com") or "player.php" not in parsed.path:
            return url
        from urllib.parse import parse_qs, quote
        import urllib.request, json as _json
        alias = (parse_qs(parsed.query).get("alias") or [""])[0]
        if not alias:
            return url
        req = urllib.request.Request(
            "https://g0.ipcamlive.com/player/getcamerastreamstate.php?alias=" + quote(alias),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            d = _json.loads(resp.read().decode("utf-8", "replace")).get("details") or {}
        addr, sid = str(d.get("address") or ""), str(d.get("streamid") or "")
        if not addr or not sid:
            return url
        addr = addr.replace("http://", "https://", 1)
        if not addr.endswith("/"):
            addr += "/"
        return f"{addr}streams/{sid}/stream.m3u8"
    except Exception:
        return url


def _ipcamlive_cached(url: str):
    """URL to play now, or None when an IPCamLive lookup (network) is needed first."""
    try:
        parsed = urlparse(url or "")
        if (parsed.hostname or "").endswith("ipcamlive.com") and "player.php" in parsed.path:
            return None
    except Exception:
        pass
    return url


_VLC_INSTANCE = None
_VLC_DETAIL = "VLC not loaded"


def shared_vlc():
    global _VLC_INSTANCE, _VLC_DETAIL
    if _VLC_INSTANCE is not None:
        return _VLC_INSTANCE, "LIVE"
    try:
        import vlc
        base = ("--intf", "dummy", "--no-video-title-show", "--network-caching=300", "--quiet")
        _VLC_INSTANCE = vlc.Instance(*base, "--rtsp-tcp")
        if _VLC_INSTANCE is None:  # builds without the live555 RTSP module reject --rtsp-tcp
            _VLC_INSTANCE = vlc.Instance(*base)
        _VLC_DETAIL = "LIVE" if _VLC_INSTANCE is not None else "VLC failed to start"
    except Exception as exc:
        _VLC_INSTANCE = None
        _VLC_DETAIL = f"VLC missing · {exc.__class__.__name__}"
    return _VLC_INSTANCE, _VLC_DETAIL


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _web_headers(media, url: str) -> None:
    """Some public webcam CDNs block VLC's user agent; present as a browser."""
    if (url or "").lower().startswith(("http://", "https://")):
        media.add_option(":http-user-agent=" + BROWSER_UA)
        if "whatsupcams.com" in url:
            media.add_option(":http-referrer=https://www.whatsupcams.com/")


class _PoolBridge(QObject):
    """Delivers worker-thread results (IPCamLive lookups) to the UI thread and owns the pool's timers."""
    resolved = pyqtSignal(str, int, str)


# Players whose stop() did not return before shutdown gave up waiting. They are never
# released (releasing a player that is still stopping is a use-after-free inside libvlc)
# and main() exits with os._exit so interpreter teardown can't race the VLC threads.
_ABANDONED_PLAYERS: list = []


def _vlc_busy() -> bool:
    return any(t.is_alive() for t in list(VlcPool._reapers))


class VlcPool:
    """Owns every libvlc media player. Lifecycle rules (these are what keep shutdown crash-free):
    * a player is stopped and released on a worker thread, stop() first, release() only after
      stop() returned - never release while a stream is still opening;
    * every deferred callback (audio kick, clip reload, IPCamLive lookup) checks that the pool is
      alive and that the same player still owns its key before touching it;
    * shutdown() disarms all of that and waits (pumping Qt events, so Windows can deliver the
      child-window messages VLC needs while it tears its video window down) until the players
      have stopped, before the video widgets are destroyed."""

    _reapers: list = []  # class-wide: main() checks these before interpreter teardown

    def __init__(self):
        self._players = {}
        self._clip_timers = {}
        self._kick_timers = {}
        self._tokens = {}
        self._token_seq = 0
        self._widgets = {}
        self._alive = True
        self._bridge = _PoolBridge()
        self._bridge.resolved.connect(self._on_resolved)
        self._instance, self.detail = shared_vlc()
        self.ready = self._instance is not None

    # -- media ----------------------------------------------------------------
    def _clip_media(self, url: str, bust: bool):
        media = self._instance.media_new(_cache_bust(url) if bust else url)
        # Loop the short clip so the tile never goes black between refreshes.
        media.add_option("input-repeat=65535")
        _web_headers(media, url)
        return media

    def _reload_clip(self, key: str, url: str, player) -> None:
        if not self._alive or self._players.get(key) is not player or not self._instance:
            self._stop_timer(self._clip_timers, key)
            return
        try:
            # Swap media in place on the same window: last frame stays until the new clip's first one.
            player.set_media(self._clip_media(url, True))
            player.play()
        except Exception:
            pass

    @staticmethod
    def _stop_timer(table: dict, key: str) -> None:
        timer = table.pop(key, None)
        if timer is not None:
            try:
                timer.stop()
                timer.timeout.disconnect()
                timer.deleteLater()
            except Exception:
                pass

    def _timer(self, table: dict, key: str, ms: int, fn, single: bool) -> None:
        self._stop_timer(table, key)
        timer = QTimer(self._bridge)
        timer.setSingleShot(single)
        timer.setInterval(ms)
        timer.timeout.connect(fn)
        table[key] = timer
        timer.start()

    # -- play -----------------------------------------------------------------
    def play(self, key: str, rtsp: str, widget: QWidget) -> bool:
        if not self._alive or not self._instance or not rtsp or widget is None:
            return False
        try:
            player = self._players.get(key)
            if player is None:
                player = self._instance.media_player_new()
                self._players[key] = player
            self._stop_timer(self._clip_timers, key)
            self._stop_timer(self._kick_timers, key)
            self._token_seq += 1
            token = self._token_seq
            self._tokens[key] = token
            self._widgets[key] = widget
            resolved = _ipcamlive_cached(rtsp)
            if resolved is None:
                # IPCamLive needs an HTTP lookup: do it off the UI thread, attach when it returns.
                bridge = self._bridge

                def work(k=key, t=token, u=rtsp):
                    out = _resolve_ipcamlive(u)
                    try:
                        bridge.resolved.emit(k, t, out)
                    except RuntimeError:  # pool already torn down
                        pass
                threading.Thread(target=work, daemon=True, name="ipcamlive-resolve").start()
                return True
            return self._attach(key, player, rtsp, resolved, widget)
        except Exception:
            return False

    def _on_resolved(self, key: str, token: int, url: str) -> None:
        if not self._alive or self._tokens.get(key) != token:
            return
        player = self._players.get(key)
        widget = self._widgets.get(key)
        if player is None or widget is None or sip.isdeleted(widget):
            return
        self._attach(key, player, url, url, widget)

    def _attach(self, key: str, player, rtsp: str, url: str, widget) -> bool:
        try:
            clip = _is_clip_url(rtsp)
            media = self._clip_media(rtsp, True) if clip else self._instance.media_new(url)
            _web_headers(media, rtsp)
            player.set_media(media)
            if clip:
                self._timer(self._clip_timers, key, CLIP_RELOAD_MS,
                            lambda k=key, u=rtsp, pl=player: self._reload_clip(k, u, pl), False)
            hwnd = int(widget.winId())
            if sys.platform.startswith("linux"):
                player.set_xwindow(hwnd)
            elif sys.platform == "darwin":
                player.set_nsobject(hwnd)
            else:
                player.set_hwnd(hwnd)
            player.play()
            self._timer(self._kick_timers, key, 500, lambda k=key, pl=player: self._kick_audio(k, pl), True)
            return True
        except Exception:
            return False

    def _kick_audio(self, key: str, player) -> None:
        self._kick_timers.pop(key, None)
        if not self._alive or self._players.get(key) is not player:
            return  # stopped / released meanwhile: touching it would be a use-after-free
        try:
            player.audio_set_volume(100)
            player.audio_set_mute(True)
            player.audio_set_mute(False)
        except Exception:
            pass

    # -- stop / release ---------------------------------------------------------
    def _detach(self, key: str):
        self._stop_timer(self._clip_timers, key)
        self._stop_timer(self._kick_timers, key)
        self._tokens.pop(key, None)
        self._widgets.pop(key, None)
        return self._players.pop(key, None)

    @staticmethod
    def _reap(players: list) -> list:
        """Stop then release each player on its own worker thread (stop() can block for seconds
        while a stream is still connecting). Returns the threads."""
        threads = []
        for player in players:
            def work(pl=player):
                try:
                    pl.stop()
                except Exception:
                    pass
                try:
                    pl.set_media(None)
                except Exception:
                    pass
                if any(a is pl for a in _ABANDONED_PLAYERS):
                    return  # shutdown gave up on it; the process is exiting
                try:
                    pl.release()
                except Exception:
                    pass
            t = threading.Thread(target=work, daemon=True, name="vlc-stop")
            t.player = player
            t.start()
            threads.append(t)
            VlcPool._reapers.append(t)
        VlcPool._reapers[:] = [t for t in VlcPool._reapers if t.is_alive()]
        return threads

    @staticmethod
    def _wait(threads: list, timeout: float) -> bool:
        """Wait for the worker threads while still pumping Qt events (no user input)."""
        deadline = time.monotonic() + max(0.0, timeout)
        app = QApplication.instance()
        while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
            if app is not None:
                app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 20)
            for t in threads:
                t.join(0.01)
        left = [t for t in threads if t.is_alive()]
        for t in left:
            _ABANDONED_PLAYERS.append(t.player)
        return not left

    def stop(self, key: str) -> None:
        player = self._detach(key)
        if player is not None:
            self._reap([player])

    def stop_keys(self, keys, wait: float = 0.0) -> bool:
        players = [pl for pl in (self._detach(k) for k in list(keys)) if pl is not None]
        threads = self._reap(players)
        return self._wait(threads, wait) if (threads and wait) else True

    def stop_all(self, wait: float = 1.5) -> bool:
        """Stop everything; waits (bounded) so the video windows are free before they are reused."""
        return self.stop_keys(list(self._players), wait)

    def shutdown(self, wait: float = 3.0) -> bool:
        """Final teardown: no callback may touch a player after this. Idempotent."""
        self._alive = False
        for table in (self._clip_timers, self._kick_timers):
            for key in list(table):
                self._stop_timer(table, key)
        try:
            self._bridge.resolved.disconnect()
        except Exception:
            pass
        ok = self.stop_all(wait)
        pending = [t for t in VlcPool._reapers if t.is_alive()]
        if pending:  # earlier per-feed stops still in flight
            ok = self._wait(pending, wait) and ok
        return ok


def _host_of(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""


def lookup_isp(url: str) -> dict:
    host = _host_of(url)
    if not host:
        return {"text": "No host in this address.", "code": ""}
    try:
        raw = urlopen(Request(
            f"http://ip-api.com/json/{host}?fields=status,query,country,countryCode,regionName,city,isp,org,as",
            headers={"User-Agent": "DedSecCCTV"},
        ), timeout=8).read().decode()
        data = json.loads(raw)
    except Exception as exc:
        return {"text": f"Lookup failed for {host}\n{exc}", "code": ""}
    if data.get("status") != "success":
        return {"text": f"Host: {host}\nNo public ISP record.", "code": ""}
    code = (data.get("countryCode") or "").upper()
    lines = [
        f"Host: {host}",
        f"IP: {data.get('query') or '—'}",
        f"Country: {data.get('country') or '—'}",
        f"ISP: {data.get('isp') or '—'}",
        f"Org: {data.get('org') or '—'}",
        f"ASN: {data.get('as') or '—'}",
        f"Location: {', '.join(p for p in (data.get('city'), data.get('regionName'), data.get('country')) if p) or '—'}",
    ]
    return {"text": "\n".join(lines), "code": code}


# ---------------------------------------------------------------------------
# Country flags
# PNG flags live in a ``flags`` folder next to app.py / the exe (also accepted under
# assets/flags), named by lowercase ISO 3166-1 alpha-2 code: flags/gb.png, flags/us.png, ...
# Catalog streams are grouped by country folder name ("United Kingdom"), and a stream row
# or saved camera may carry a "country" field (a name or a 2-letter code); both map to ISO
# codes through COUNTRY_CODES (pycountry is used as an optional fallback when installed).
# ---------------------------------------------------------------------------
COUNTRY_CODES = {
    "Afghanistan": "af",
    "Aland Islands": "ax",
    "Albania": "al",
    "Algeria": "dz",
    "America": "us",
    "American Samoa": "as",
    "Andorra": "ad",
    "Angola": "ao",
    "Anguilla": "ai",
    "Antarctica": "aq",
    "Antigua and Barbuda": "ag",
    "Argentina": "ar",
    "Armenia": "am",
    "Aruba": "aw",
    "Australia": "au",
    "Austria": "at",
    "Azerbaijan": "az",
    "Bahamas": "bs",
    "Bahamas, The": "bs",
    "Bahrain": "bh",
    "Bangladesh": "bd",
    "Barbados": "bb",
    "Belarus": "by",
    "Belgium": "be",
    "Belize": "bz",
    "Benin": "bj",
    "Bermuda": "bm",
    "Bhutan": "bt",
    "Bolivia": "bo",
    "Bolivia (Plurinational State of)": "bo",
    "Bosnia and Herzegovina": "ba",
    "Botswana": "bw",
    "Bouvet Island": "bv",
    "Brazil": "br",
    "Britain": "gb",
    "British Indian Ocean Territory": "io",
    "British Virgin Islands": "vg",
    "Brunei": "bn",
    "Brunei Darussalam": "bn",
    "Bulgaria": "bg",
    "Burkina Faso": "bf",
    "Burma": "mm",
    "Burundi": "bi",
    "Cabo Verde": "cv",
    "Cambodia": "kh",
    "Cameroon": "cm",
    "Canada": "ca",
    "Cape Verde": "cv",
    "Caribbean Netherlands": "bq",
    "Cayman Islands": "ky",
    "Central African Republic": "cf",
    "Chad": "td",
    "Chile": "cl",
    "China": "cn",
    "Christmas Island": "cx",
    "Cocos (Keeling) Islands": "cc",
    "Colombia": "co",
    "Comoros": "km",
    "Congo": "cg",
    "Congo-Brazzaville": "cg",
    "Congo-Kinshasa": "cd",
    "Cook Islands": "ck",
    "Costa Rica": "cr",
    "Cote d'Ivoire": "ci",
    "Croatia": "hr",
    "Cuba": "cu",
    "Curacao": "cw",
    "Curaçao": "cw",
    "Cyprus": "cy",
    "Czech Republic": "cz",
    "Czechia": "cz",
    "Côte d'Ivoire (Ivory Coast)": "ci",
    "Democratic Republic of the Congo": "cd",
    "Denmark": "dk",
    "Djibouti": "dj",
    "Dominica": "dm",
    "Dominican Republic": "do",
    "DR Congo": "cd",
    "DRC": "cd",
    "East Timor": "tl",
    "Ecuador": "ec",
    "Egypt": "eg",
    "El Salvador": "sv",
    "England": "gb-eng",
    "Equatorial Guinea": "gq",
    "Eritrea": "er",
    "Estonia": "ee",
    "Eswatini": "sz",
    "Eswatini (Swaziland)": "sz",
    "Ethiopia": "et",
    "European Union": "eu",
    "Falkland Islands": "fk",
    "Faroe Islands": "fo",
    "Federated States of Micronesia": "fm",
    "Fiji": "fj",
    "Finland": "fi",
    "France": "fr",
    "French Guiana": "gf",
    "French Polynesia": "pf",
    "French Southern and Antarctic Lands": "tf",
    "Gabon": "ga",
    "Gambia": "gm",
    "Georgia": "ge",
    "Germany": "de",
    "Ghana": "gh",
    "Gibraltar": "gi",
    "Great Britain": "gb",
    "Greece": "gr",
    "Greenland": "gl",
    "Grenada": "gd",
    "Guadeloupe": "gp",
    "Guam": "gu",
    "Guatemala": "gt",
    "Guernsey": "gg",
    "Guinea": "gn",
    "Guinea-Bissau": "gw",
    "Guyana": "gy",
    "Haiti": "ht",
    "Heard Island and McDonald Islands": "hm",
    "Holland": "nl",
    "Holy See": "va",
    "Honduras": "hn",
    "Hong Kong": "hk",
    "Hong Kong SAR": "hk",
    "Hungary": "hu",
    "Iceland": "is",
    "India": "in",
    "Indonesia": "id",
    "Iran": "ir",
    "Iran, Islamic Republic of": "ir",
    "Iraq": "iq",
    "Ireland": "ie",
    "Isle of Man": "im",
    "Israel": "il",
    "Italy": "it",
    "Ivory Coast": "ci",
    "Jamaica": "jm",
    "Japan": "jp",
    "Jersey": "je",
    "Jordan": "jo",
    "Kazakhstan": "kz",
    "Kenya": "ke",
    "Kiribati": "ki",
    "Korea": "kr",
    "Kosovo": "xk",
    "Kuwait": "kw",
    "Kyrgyzstan": "kg",
    "Lao PDR": "la",
    "Laos": "la",
    "Latvia": "lv",
    "Lebanon": "lb",
    "Lesotho": "ls",
    "Liberia": "lr",
    "Libya": "ly",
    "Liechtenstein": "li",
    "Lithuania": "lt",
    "Luxembourg": "lu",
    "Macao": "mo",
    "Macau": "mo",
    "Macedonia": "mk",
    "Madagascar": "mg",
    "Malawi": "mw",
    "Malaysia": "my",
    "Maldives": "mv",
    "Mali": "ml",
    "Malta": "mt",
    "Marshall Islands": "mh",
    "Martinique": "mq",
    "Mauritania": "mr",
    "Mauritius": "mu",
    "Mayotte": "yt",
    "Mexico": "mx",
    "Micronesia": "fm",
    "Micronesia (Federated States of)": "fm",
    "Moldova": "md",
    "Moldova, Republic of": "md",
    "Monaco": "mc",
    "Mongolia": "mn",
    "Montenegro": "me",
    "Montserrat": "ms",
    "Morocco": "ma",
    "Mozambique": "mz",
    "Myanmar": "mm",
    "Namibia": "na",
    "Nauru": "nr",
    "Nepal": "np",
    "Netherlands": "nl",
    "New Caledonia": "nc",
    "New Zealand": "nz",
    "Nicaragua": "ni",
    "Niger": "ne",
    "Nigeria": "ng",
    "Niue": "nu",
    "Norfolk Island": "nf",
    "North Korea": "kp",
    "North Macedonia": "mk",
    "Northern Ireland": "gb-nir",
    "Northern Mariana Islands": "mp",
    "Norway": "no",
    "Oman": "om",
    "Pakistan": "pk",
    "Palau": "pw",
    "Palestine": "ps",
    "Palestinian Territories": "ps",
    "Panama": "pa",
    "Papua New Guinea": "pg",
    "Paraguay": "py",
    "Peru": "pe",
    "Philippines": "ph",
    "Pitcairn Islands": "pn",
    "Poland": "pl",
    "Portugal": "pt",
    "Puerto Rico": "pr",
    "Qatar": "qa",
    "Republic of Ireland": "ie",
    "Republic of Korea": "kr",
    "Republic of the Congo": "cg",
    "Reunion": "re",
    "Romania": "ro",
    "Russia": "ru",
    "Russian Federation": "ru",
    "Rwanda": "rw",
    "Réunion": "re",
    "Saint Barthelemy": "bl",
    "Saint Barthélemy": "bl",
    "Saint Helena, Ascension and Tristan da Cunha": "sh",
    "Saint Kitts and Nevis": "kn",
    "Saint Lucia": "lc",
    "Saint Martin": "mf",
    "Saint Martin (French part)": "mf",
    "Saint Pierre and Miquelon": "pm",
    "Saint Vincent and the Grenadines": "vc",
    "Samoa": "ws",
    "San Marino": "sm",
    "Sao Tome and Principe": "st",
    "Saudi Arabia": "sa",
    "Scotland": "gb-sct",
    "Senegal": "sn",
    "Serbia": "rs",
    "Seychelles": "sc",
    "Sierra Leone": "sl",
    "Singapore": "sg",
    "Sint Maarten": "sx",
    "Sint Maarten (Dutch part)": "sx",
    "Slovakia": "sk",
    "Slovenia": "si",
    "Solomon Islands": "sb",
    "Somalia": "so",
    "South Africa": "za",
    "South Georgia": "gs",
    "South Korea": "kr",
    "South Sudan": "ss",
    "Spain": "es",
    "Sri Lanka": "lk",
    "State of Palestine": "ps",
    "Sudan": "sd",
    "Suriname": "sr",
    "Svalbard and Jan Mayen": "sj",
    "Swaziland": "sz",
    "Sweden": "se",
    "Switzerland": "ch",
    "Syria": "sy",
    "Syrian Arab Republic": "sy",
    "São Tomé and Príncipe": "st",
    "Taiwan": "tw",
    "Taiwan, Province of China": "tw",
    "Tajikistan": "tj",
    "Tanzania": "tz",
    "Tanzania, United Republic of": "tz",
    "Thailand": "th",
    "The Bahamas": "bs",
    "The Gambia": "gm",
    "The Netherlands": "nl",
    "Timor-Leste": "tl",
    "Togo": "tg",
    "Tokelau": "tk",
    "Tonga": "to",
    "Trinidad and Tobago": "tt",
    "Tunisia": "tn",
    "Turkey": "tr",
    "Turkiye": "tr",
    "Turkmenistan": "tm",
    "Turks and Caicos Islands": "tc",
    "Tuvalu": "tv",
    "Türkiye": "tr",
    "UAE": "ae",
    "Uganda": "ug",
    "UK": "gb",
    "Ukraine": "ua",
    "United Arab Emirates": "ae",
    "United Kingdom": "gb",
    "United Nations": "un",
    "United States": "us",
    "United States Minor Outlying Islands": "um",
    "United States of America": "us",
    "United States Virgin Islands": "vi",
    "Uruguay": "uy",
    "US": "us",
    "USA": "us",
    "Uzbekistan": "uz",
    "Vanuatu": "vu",
    "Vatican": "va",
    "Vatican City": "va",
    "Vatican City (Holy See)": "va",
    "Venezuela": "ve",
    "Venezuela (Bolivarian Republic of)": "ve",
    "Viet Nam": "vn",
    "Vietnam": "vn",
    "Wales": "gb-wls",
    "Wallis and Futuna": "wf",
    "Western Sahara": "eh",
    "Yemen": "ye",
    "Zambia": "zm",
    "Zimbabwe": "zw",
    "Åland Islands": "ax",
}
FLAG_BADGE_W, FLAG_BADGE_H = 72, 48


def _country_key(name: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


_COUNTRY_INDEX = {_country_key(name): code for name, code in COUNTRY_CODES.items()}
_KNOWN_CODES = set(COUNTRY_CODES.values())


def country_code(value: str) -> str:
    """ISO 3166-1 alpha-2 code (lowercase) for a country name or code; "" when unknown."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low in _KNOWN_CODES or (len(low) == 2 and low.isalpha()):
        return low
    key = _country_key(raw)
    if key in _COUNTRY_INDEX:
        return _COUNTRY_INDEX[key]
    if key.startswith("the ") and key[4:] in _COUNTRY_INDEX:
        return _COUNTRY_INDEX[key[4:]]
    if "," in raw:  # "Korea, South" / "Bahamas, The"
        head, _, tail = raw.partition(",")
        flipped = _country_key(f"{tail} {head}")
        if flipped in _COUNTRY_INDEX:
            return _COUNTRY_INDEX[flipped]
    try:  # optional: never required
        import pycountry
        found = pycountry.countries.lookup(raw)
        return str(found.alpha_2).lower()
    except Exception:
        pass
    try:
        import pycountry
        return str(pycountry.countries.search_fuzzy(raw)[0].alpha_2).lower()
    except Exception:
        return ""


_FLAG_CACHE: dict = {}


def _flag_path(code: str) -> Path | None:
    code = (code or "").strip().lower()
    if not code or not re.fullmatch(r"[a-z]{2}(-[a-z]{2,3})?", code):
        return None
    if code in _FLAG_CACHE:
        return _FLAG_CACHE[code]
    found = None
    for root in _asset_roots():
        for folder in (root / "flags", root / "assets" / "flags"):
            for ext in (".png", ".webp", ".jpg"):
                path = folder / f"{code}{ext}"
                if path.is_file():
                    found = path
                    break
            if found:
                break
        if found:
            break
    if found is None and "-" in code:  # gb-sct missing -> fall back to gb
        found = _flag_path(code.split("-", 1)[0])
    _FLAG_CACHE[code] = found
    return found


def flag_badge(code: str, w: int = FLAG_BADGE_W, h: int = FLAG_BADGE_H) -> QPixmap:
    """Rounded flag badge from flags/<code>.png; a rounded text badge with the code if missing."""
    code = (code or "").strip().lower()
    ratio = 2.0
    screen = QApplication.primaryScreen() if QApplication.instance() else None
    if screen is not None:
        ratio = max(1.0, screen.devicePixelRatio())
    pix = QPixmap(int(w * ratio), int(h * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    rect = QRectF(1, 1, w - 2, h - 2)
    radius = min(w, h) * 0.18
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    path = _flag_path(code)
    flag = QPixmap(str(path)) if path else QPixmap()
    if not flag.isNull():
        painter.save()
        painter.setClipPath(clip)
        scaled = flag.scaled(int(rect.width() * ratio), int(rect.height() * ratio),
                             Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        sw, sh = scaled.width() / ratio, scaled.height() / ratio
        painter.drawPixmap(QPointF(rect.x() + (rect.width() - sw) / 2, rect.y() + (rect.height() - sh) / 2), scaled)
        painter.restore()
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
    else:
        painter.fillPath(clip, _tc("#10261c"))
        painter.setPen(_tc("#d8ffe9"))
        painter.setFont(QFont("Consolas", max(9, int(h * 0.3)), QFont.Weight.Bold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, code.upper() if code else "--")
        painter.setPen(QPen(_tc("#2fbf6a"), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(clip)
    painter.end()
    return pix


def country_icon(code: str) -> QPixmap:
    """Kept for older callers: the flag badge for a code or country name."""
    return flag_badge(country_code(code) or code)


def show_stream_info(parent, url: str, country: str = "", code: str = "") -> None:
    """ISP lookup for a stream. The flag comes from the camera's catalog country when known
    (stream "country" field / country folder name), otherwise from the IP lookup's country code."""
    info = lookup_isp(url)
    code = country_code(code) or country_code(country) or (info.get("code") or "").lower()
    text = info.get("text") or "No info"
    if country:
        text = f"Stream country: {country}\n" + text
    box = QMessageBox(parent)
    box.setWindowTitle("Stream info")
    box.setText(text)
    box.setIconPixmap(flag_badge(code))
    box.exec()


def _glyph_icon(kind: str, color: str = "#d5dde8", size: int = 16) -> QIcon:
    """Small vector icons for the main screen (drawn, so no icon files are needed)."""
    icon = _glyph_icon_raw(kind, _theme_hex(color) if color.startswith("#") else color, size)
    icon._ds_spec = ["_glyph_icon", [kind, color, size]]
    return icon


def _glyph_icon_raw(kind: str, color: str, size: int) -> QIcon:
    scale = 3
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(scale, scale)
    col = QColor(color)
    s = float(size)
    pen = QPen(col, max(1.4, s / 10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if kind == "play":
        path = QPainterPath()
        path.moveTo(s * 0.30, s * 0.20)
        path.lineTo(s * 0.80, s * 0.50)
        path.lineTo(s * 0.30, s * 0.80)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPath(path)
    elif kind == "stop":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(s * 0.26, s * 0.26, s * 0.48, s * 0.48), s * 0.08, s * 0.08)
    elif kind in ("sound", "mute"):
        body = QPainterPath()
        body.moveTo(s * 0.14, s * 0.38)
        body.lineTo(s * 0.30, s * 0.38)
        body.lineTo(s * 0.50, s * 0.20)
        body.lineTo(s * 0.50, s * 0.80)
        body.lineTo(s * 0.30, s * 0.62)
        body.lineTo(s * 0.14, s * 0.62)
        body.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPath(body)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "sound":
            p.drawArc(QRectF(s * 0.44, s * 0.32, s * 0.24, s * 0.36), -60 * 16, 120 * 16)
            p.drawArc(QRectF(s * 0.44, s * 0.18, s * 0.42, s * 0.64), -60 * 16, 120 * 16)
        else:
            p.drawLine(QPointF(s * 0.62, s * 0.36), QPointF(s * 0.86, s * 0.64))
            p.drawLine(QPointF(s * 0.62, s * 0.64), QPointF(s * 0.86, s * 0.36))
    elif kind == "info":
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72))
        p.drawLine(QPointF(s * 0.5, s * 0.47), QPointF(s * 0.5, s * 0.66))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QRectF(s * 0.455, s * 0.30, s * 0.09, s * 0.09))
    elif kind == "plus":
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.5, s * 0.20), QPointF(s * 0.5, s * 0.80))
        p.drawLine(QPointF(s * 0.20, s * 0.5), QPointF(s * 0.80, s * 0.5))
    elif kind == "expand":
        p.setPen(pen)
        a, b, k = s * 0.18, s * 0.82, s * 0.24
        for (x, y, dx, dy) in ((a, a, 1, 1), (b, a, -1, 1), (a, b, 1, -1), (b, b, -1, -1)):
            path = QPainterPath()
            path.moveTo(x + dx * k, y)
            path.lineTo(x, y)
            path.lineTo(x, y + dy * k)
            p.drawPath(path)
    elif kind == "user":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QRectF(s * 0.34, s * 0.14, s * 0.32, s * 0.32))
        path = QPainterPath()
        path.moveTo(s * 0.16, s * 0.86)
        path.cubicTo(s * 0.18, s * 0.56, s * 0.82, s * 0.56, s * 0.84, s * 0.86)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == "gear":
        import math
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        c = QPointF(s / 2, s / 2)
        path = QPainterPath()
        teeth = 8
        for i in range(teeth * 2):
            a0 = math.pi * 2 * i / (teeth * 2) - math.pi / (teeth * 2)
            a1 = a0 + math.pi * 2 / (teeth * 2)
            r = s * (0.46 if i % 2 == 0 else 0.34)
            for a in (a0, a1):
                pt = QPointF(c.x() + r * math.cos(a), c.y() + r * math.sin(a))
                if i == 0 and a == a0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
        path.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(c, s * 0.15, s * 0.15)
        p.drawPath(path.subtracted(hole))
    elif kind == "trash":
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(s * 0.18, s * 0.28), QPointF(s * 0.82, s * 0.28))
        p.drawLine(QPointF(s * 0.40, s * 0.17), QPointF(s * 0.60, s * 0.17))
        body = QPainterPath()
        body.moveTo(s * 0.27, s * 0.30)
        body.lineTo(s * 0.32, s * 0.84)
        body.lineTo(s * 0.68, s * 0.84)
        body.lineTo(s * 0.73, s * 0.30)
        p.drawPath(body)
        p.drawLine(QPointF(s * 0.44, s * 0.42), QPointF(s * 0.45, s * 0.72))
        p.drawLine(QPointF(s * 0.56, s * 0.42), QPointF(s * 0.55, s * 0.72))
    elif kind == "camera":
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(s * 0.10, s * 0.30, s * 0.56, s * 0.42), s * 0.08, s * 0.08)
        path = QPainterPath()
        path.moveTo(s * 0.66, s * 0.44)
        path.lineTo(s * 0.90, s * 0.32)
        path.lineTo(s * 0.90, s * 0.70)
        path.lineTo(s * 0.66, s * 0.58)
        p.drawPath(path)
    p.end()
    pix.setDevicePixelRatio(scale)
    return QIcon(pix)


def _paint_btn(btn, on: bool) -> None:
    """Toggle the 'active' look of a tile control (Stop / Muted) via a stylesheet property."""
    if btn is None:
        return
    btn.setObjectName("CctvIcon")
    btn.setStyleSheet("")
    btn.setProperty("active", "on" if on else "off")
    btn.style().unpolish(btn)
    btn.style().polish(btn)


# Shared tile chrome for the main wall and the full-screen wall.
_TILE_STYLE = """
    QFrame#CctvTile {
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0c1825, stop:1 #070b11);
        border: 1px solid #16324a;
        border-radius: 10px;
    }
    QFrame#CctvTile[hover="on"] { border-color: #2b8fb0; }
    QFrame#CctvTile[state="live"] { border-color: #1f6b47; }
    QFrame#CctvTile[state="live"][hover="on"] { border-color: #2fbf6a; }
    QFrame#CctvTile[state="error"] { border-color: #4a2026; }
    QFrame#CctvHead, QFrame#CctvBar { background: transparent; border: none; }
    QFrame#CctvVideo { background: #000; border: none; }
    QLabel#CctvSlot {
        color: #22d3ee; background: rgba(34, 211, 238, 0.10); border: 1px solid rgba(34, 211, 238, 0.35);
        border-radius: 5px; padding: 1px 7px; font-family: Consolas, monospace; font-size: 11px;
        font-weight: 800; letter-spacing: 1px;
    }
    QLabel#CctvName { color: #e8f0ff; background: transparent; font-size: 12px; font-weight: 700; }
    QLabel#CctvMeta { color: #a3b6c9; background: transparent; font-family: Consolas, monospace; font-size: 11px; font-weight: 600; }
    QLabel#CctvState {
        color: #8ea0b5; background: #10161e; border: 1px solid #2a3846; border-radius: 5px;
        padding: 1px 8px; font-family: Consolas, monospace; font-size: 10px; font-weight: 800; letter-spacing: 1px;
    }
    QLabel#CctvState[tone="live"] { color: #7dffb0; background: #0f2a1b; border-color: #2fbf6a; }
    QLabel#CctvState[tone="ready"] { color: #67e8f9; background: #0d2433; border-color: #1d6f8a; }
    QLabel#CctvState[tone="held"] { color: #fcd34d; background: #2a2210; border-color: #8a6d1d; }
    QLabel#CctvState[tone="busy"] { color: #fcd34d; background: #2a2210; border-color: #8a6d1d; }
    QLabel#CctvState[tone="error"] { color: #fca5a5; background: #2a1215; border-color: #7a2b33; }
    QPushButton#CctvIcon {
        background: rgba(16, 22, 30, 0.85); color: #c3d0de; border: 1px solid #253b50;
        border-radius: 6px; padding: 0 10px 0 8px; min-height: 26px; max-height: 26px;
        font-size: 12px; font-weight: 700;
    }
    QFrame#CctvBar[hover="on"] QPushButton#CctvIcon { color: #e8f0ff; border-color: #2e4a63; background: #121c27; }
    QPushButton#CctvIcon:hover { background: #18283a; border-color: #22d3ee; color: #ffffff; }
    QPushButton#CctvIcon:pressed { background: #0d2433; }
    QPushButton#CctvIcon[active="on"] { background: #0f2a1b; color: #7dffb0; border-color: #2fbf6a; }
    QPushButton#CctvIcon[active="on"]:hover { background: #163a26; }
    QPushButton#CctvIcon[danger="true"], QFrame#CctvBar[hover="on"] QPushButton#CctvIcon[danger="true"] {
        background: transparent; color: #ff8a95; border: 1px solid #7a2b33; }
    QPushButton#CctvIcon[danger="true"]:hover { background: #3a1418; color: #ffb3ba; border-color: #ff5c6c; }
    QPushButton#CctvIcon[danger="true"]:pressed { background: #2a1215; }
"""


class _TileCover(QLabel):
    """What a tile shows while no video is on screen: the camera's banner (dimmed) or a dark
    gradient, CCTV corner brackets and a status line. Lives inside the video widget, exactly
    like the old watermark label, and is hidden while VLC draws live video."""

    ICONS = {"empty": "plus", "ready": "play", "held": "expand", "busy": "camera", "error": "info"}
    ACCENT = {"empty": "#3c5a74", "ready": "#22d3ee", "held": "#f5b942", "busy": "#f5b942", "error": "#f07a86"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CctvWatermark")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.mode = "empty"
        self.subtitle = ""
        self._banner_path = None
        self._banner = None
        self._scaled = None
        self._scaled_key = None

    def set_banner(self, path) -> None:
        path = str(path) if path else None
        if path == self._banner_path:
            return
        self._banner_path = path
        self._banner = None
        self._scaled = None
        self._scaled_key = None
        if path:
            img = QImage(path)
            if not img.isNull():
                if img.width() > 1600:
                    img = img.scaledToWidth(1600, Qt.TransformationMode.SmoothTransformation)
                self._banner = img
        self.update()

    def set_state(self, mode: str, title: str, subtitle: str = "") -> None:
        self.mode = mode
        self.subtitle = subtitle
        self.setText(title)
        self.update()

    def _cover(self, w: int, h: int):
        if self._banner is None or w <= 0 or h <= 0:
            return None
        if self._scaled_key != (w, h):
            scaled = self._banner.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                         Qt.TransformationMode.SmoothTransformation)
            x = max(0, (scaled.width() - w) // 2)
            y = max(0, (scaled.height() - h) // 2)
            self._scaled = QPixmap.fromImage(scaled.copy(x, y, w, h))
            self._scaled_key = (w, h)
        return self._scaled

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cover = self._cover(w, h) if self.mode != "empty" else None
        if cover is not None:
            p.drawPixmap(0, 0, cover)
            dim = {"ready": 150, "held": 185, "busy": 165, "error": 205}.get(self.mode, 170)
            p.fillRect(self.rect(), QColor(4, 8, 14, dim))
        else:
            grad = QLinearGradient(0, 0, w, h)
            grad.setColorAt(0.0, _tc("#0b1a29"))
            grad.setColorAt(1.0, _tc("#05080c"))
            p.fillRect(self.rect(), grad)
            p.setPen(_tc(34, 211, 238, 9))
            step = 28
            for x in range(step, w, step):
                p.drawLine(x, 0, x, h)
            for y in range(step, h, step):
                p.drawLine(0, y, w, y)
        shade = QLinearGradient(0, 0, 0, h)  # soft inner shadow top/bottom
        sc = QColor if cover is not None else _tc  # over a picture keep the dark vignette
        shade.setColorAt(0.0, sc(0, 0, 0, 110))
        shade.setColorAt(0.18, sc(0, 0, 0, 0))
        shade.setColorAt(0.82, sc(0, 0, 0, 0))
        shade.setColorAt(1.0, sc(0, 0, 0, 120 if cover is not None else 40))
        p.fillRect(self.rect(), shade)

        accent = _tc(self.ACCENT.get(self.mode, "#3c5a74"))
        bracket = QColor(accent)
        bracket.setAlpha(110 if self.mode != "empty" else 70)
        pen = QPen(bracket, 2)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        p.setPen(pen)
        inset, arm = 12, max(12, min(w, h) // 12)
        for (x, y, dx, dy) in ((inset, inset, 1, 1), (w - inset, inset, -1, 1),
                               (inset, h - inset, 1, -1), (w - inset, h - inset, -1, -1)):
            p.drawLine(x, y, x + dx * arm, y)
            p.drawLine(x, y, x, y + dy * arm)

        title = self.text()
        big = h >= 120
        d = 46 if big else 32
        title_h = 22 if big else 18
        sub_h = 18 if (self.subtitle and big) else 0
        total = d + 10 + title_h + sub_h
        top = max(4, (h - total) // 2)
        cx = w / 2
        ring = QRectF(cx - d / 2, top, d, d)
        fill = QColor(accent)
        fill.setAlpha(34)
        p.setBrush(fill)
        edge = QColor(accent)
        edge.setAlpha(170)
        p.setPen(QPen(edge, 1.5))
        p.drawEllipse(ring)
        icon = _glyph_icon(self.ICONS.get(self.mode, "camera"), accent.name(), int(d * 0.5))
        isz = int(d * 0.5)
        icon.paint(p, int(cx - isz / 2 + (2 if self.mode == "ready" else 0)), int(top + (d - isz) / 2), isz, isz)

        font = QFont(self.font())
        font.setPixelSize(15 if big else 12)
        font.setBold(True)
        p.setFont(font)
        p.setPen(sc("#e8f0ff"))  # over a picture keep light text in every theme
        text_rect = QRectF(8, top + d + 10, w - 16, title_h)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(font).elidedText(title, Qt.TextElideMode.ElideRight, max(10, w - 24)))
        if sub_h:
            font2 = QFont(self.font())
            font2.setPixelSize(12)
            p.setFont(font2)
            p.setPen(sc("#a9b8c9"))
            p.drawText(QRectF(8, top + d + 10 + title_h, w - 16, sub_h),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(font2).elidedText(self.subtitle, Qt.TextElideMode.ElideRight, max(10, w - 24)))
        p.end()


class CameraTile(QFrame):
    """One feed on the wall: name/status strip, the native video widget VLC draws into,
    and a control bar (Play/Stop, Mute, Info). Strips sit outside the video widget so they
    never collide with video."""

    def __init__(self, slot: int, compact: bool = False, removable: bool = False):
        super().__init__()
        self.setObjectName("CctvTile")
        self.removable = removable
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 160)
        self.setProperty("state", "empty")
        self.setProperty("hover", "off")
        self.state = "empty"
        self._muted = False
        self._cam_name = ""
        self._conn_timer = QTimer(self)
        self._conn_timer.setSingleShot(True)
        self._conn_timer.timeout.connect(self._connected)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        head = QFrame()
        head.setObjectName("CctvHead")
        head.setFixedHeight(34)
        top = QHBoxLayout(head)
        top.setContentsMargins(10, 4, 10, 4)
        top.setSpacing(8)
        self.slot_lbl = QLabel(f"CAM {slot:02d}")
        self.slot_lbl.setObjectName("CctvSlot")
        self.name_lbl = QLabel("")
        self.name_lbl.setObjectName("CctvName")
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.state_lbl = QLabel("EMPTY")
        self.state_lbl.setObjectName("CctvState")
        self.state_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.slot_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.name_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.state_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(head)

        # VLC draws into this native child window (winId() → set_hwnd / set_xwindow).
        self.video = QFrame()
        self.video.setObjectName("CctvVideo")
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        layout.addWidget(self.video, 1)
        self.watermark = _TileCover(self.video)
        self.slot = slot
        self.compact = compact
        self.play_btn = None
        self.mute_btn = None
        self.info_btn = None
        self.remove_btn = None
        self.bar = None
        self.meta_lbl = None
        if not compact:
            bar = QFrame()
            bar.setObjectName("CctvBar")
            bar.setFixedHeight(40)
            bar.setProperty("hover", "off")
            row = QHBoxLayout(bar)
            row.setContentsMargins(10, 6, 8, 7)
            row.setSpacing(6)
            self.meta_lbl = QLabel("")
            self.meta_lbl.setObjectName("CctvMeta")
            self.meta_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.play_btn = self._ctl("Play", "play", "Play / stop this feed")
            self.mute_btn = self._ctl("Mute", "sound", "Mute / unmute audio")
            self.mute_btn.setCheckable(False)
            self.info_btn = self._ctl("Info", "info", "Who hosts this stream (ISP lookup)")
            row.addWidget(self.meta_lbl, 1)
            row.addWidget(self.play_btn)
            row.addWidget(self.mute_btn)
            row.addWidget(self.info_btn)
            if removable:
                self.remove_btn = self._ctl("Remove", "trash", "Take this feed off the wall (it stays in your camera list)")
                self.remove_btn.setIcon(_glyph_icon("trash", "#ff8a95", 14))
                self.remove_btn.setProperty("danger", "true")
                row.addWidget(self.remove_btn)
                self.remove_btn.hide()
            layout.addWidget(bar)
            self.bar = bar
            self.play_btn.hide()
            self.mute_btn.hide()
            self.info_btn.hide()
        self.show_empty()

    @staticmethod
    def _ctl(text: str, icon: str, tip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("CctvIcon")
        btn.setIcon(_glyph_icon(icon, "#cfe3f2", 14))
        btn.setIconSize(QSize(14, 14))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip(tip)
        btn.setProperty("active", "off")
        return btn

    # -- camera / chrome ------------------------------------------------------
    def set_camera(self, cam) -> None:
        """Name chip, meta line and banner preview for the camera in this slot (None = empty)."""
        if not isinstance(cam, dict):
            self._cam_name = ""
            self.watermark.set_banner(None)
            if self.meta_lbl is not None:
                self.meta_lbl.setText("")
            self._elide_name()
            return
        self._cam_name = str(cam.get("name") or "")
        self.watermark.set_banner(_camera_banner_path(cam))
        if self.meta_lbl is not None:
            if cam.get("source") == "catalog":
                meta = " · ".join(x for x in (cam.get("country") or "", CLIP_LABEL if _is_clip(cam) else "catalog") if x)
            else:
                meta = _host_of(cam.get("rtsp", "")) or "private"
            self.meta_lbl.setText(meta)
        self._elide_name()

    def _elide_name(self) -> None:
        name = self._cam_name
        if name.strip().upper() == self.slot_lbl.text():
            name = ""
        width = max(10, self.name_lbl.width())
        self.name_lbl.setText(QFontMetrics(self.name_lbl.font()).elidedText(name, Qt.TextElideMode.ElideRight, width))
        self.name_lbl.setToolTip(self._cam_name)

    def _set_state(self, state: str, badge: str, tone: str) -> None:
        self.state = state
        self.state_lbl.setText(badge)
        _set_prop(self.state_lbl, "tone", tone)
        _set_prop(self, "state", state)

    def _set_hover(self, on: bool) -> None:
        _set_prop(self, "hover", "on" if on else "off")
        if self.bar is not None:
            _set_prop(self.bar, "hover", "on" if on else "off")
            for btn in (self.play_btn, self.mute_btn, self.info_btn, self.remove_btn):
                if btn is None:
                    continue
                btn.style().unpolish(btn)
                btn.style().polish(btn)

    def enterEvent(self, event):
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hover(False)
        super().leaveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.watermark.setGeometry(0, 0, self.video.width(), self.video.height())
        self._elide_name()

    def _cover(self, mode: str, title: str, subtitle: str = "") -> None:
        self.watermark.set_state(mode, title, subtitle)
        self.watermark.setGeometry(0, 0, self.video.width(), self.video.height())
        self.watermark.show()
        self.watermark.raise_()

    def _controls(self, visible: bool) -> None:
        for btn in (self.play_btn, self.mute_btn, self.info_btn):
            if btn is not None:
                btn.setVisible(visible)
        if self.remove_btn is not None:  # any slot holding a camera can be cleared (also on error)
            self.remove_btn.setVisible(self.state != "empty" and self.state != "held")

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self.mute_btn is None:
            return
        self.mute_btn.setText("Muted" if muted else "Mute")
        self.mute_btn.setIcon(_glyph_icon("mute" if muted else "sound", "#7dffb0" if muted else "#cfe3f2", 14))
        _paint_btn(self.mute_btn, muted)

    def _set_play_look(self, live: bool) -> None:
        if self.play_btn is None:
            return
        self.play_btn.setText("Stop" if live else "Play")
        self.play_btn.setIcon(_glyph_icon("stop" if live else "play", "#7dffb0" if live else "#cfe3f2", 14))
        _paint_btn(self.play_btn, live)

    # -- states (called by CctvPage / CctvWallWindow) ---------------------------
    def show_empty(self) -> None:
        self._conn_timer.stop()
        self.set_camera(None)
        self._set_state("empty", "EMPTY", "")
        if self.meta_lbl is not None:
            self.meta_lbl.setText("Slot free")
        self._cover("empty", "No camera in this slot", "Use Add CCTV to put a feed on the wall")
        self._controls(False)

    def show_held(self) -> None:
        self._conn_timer.stop()
        self._set_state("held", "HELD", "held")
        self._cover("held", "Playing in full screen", "Press Esc on the full-screen wall to return")
        self._controls(False)

    def show_ready(self) -> None:
        self._conn_timer.stop()
        self._set_state("ready", "OFFLINE", "")
        self._cover("ready", "Ready", "Press Play to go live")
        if self.play_btn is not None:
            self._set_play_look(False)
            self.set_muted(False)
            self._controls(True)

    def show_live(self) -> None:
        self.watermark.hide()
        self.set_muted(False)  # a fresh VLC player starts unmuted (VlcPool._kick_audio)
        self._set_state("live", "CONNECTING", "busy")
        self._conn_timer.start(1500)
        if self.play_btn is not None:
            self._set_play_look(True)
            self._controls(True)

    def _connected(self) -> None:
        if self.state == "live":
            self._set_state("live", "● LIVE", "live")

    def show_error(self, text: str) -> None:
        self._conn_timer.stop()
        self._set_state("error", "ERROR", "error")
        if str(text).upper().startswith("VLC"):
            title, sub = "Video engine unavailable", f"{text} · install VLC media player and restart"
        elif text == "STREAM FAILED":
            title, sub = "Stream failed to start", "Check the address, then press Play to retry"
        else:
            title, sub = text, ""
        self._cover("error", title, sub)
        self._set_play_look(False)
        if self.remove_btn is not None:
            self.remove_btn.setVisible(True)


_ADD_CCTV_STYLE = """
    QDialog#AddCctv, QDialog#CameraDialog { background: #060606; }
    QWidget { color: #e8f0ff; }
    QLabel { background: transparent; font-size: 13px; }
    QFrame#AcHeader { background: #0a1624; border: 1px solid rgba(34, 211, 238, 0.35); border-radius: 12px; }
    QLabel#AcTitle { font-size: 24px; font-weight: 800; letter-spacing: 1px; }
    QLabel#AcHelp { color: #9aa8bc; font-size: 13px; }
    QLabel#AcChip { color: #22d3ee; background: #123246; border: 1px solid #22d3ee; border-radius: 8px;
                    font-size: 12px; font-weight: 800; letter-spacing: 1px; padding: 6px 12px; }
    QLabel#AcChip[tone="green"] { color: #50ff85; background: #14361f; border-color: #2fbf6a; }
    QLabel#AcChip[tone="grey"] { color: #d5dde8; background: #10141a; border-color: #3c4a5c; }
    QFrame#AcPanel { background: #08111b; border: 1px solid #16324a; border-radius: 12px; }
    QLabel#AcSection { color: #ffffff; font-size: 15px; font-weight: 800; }
    QLabel#AcMuted { color: #8ea0b5; font-size: 12px; }
    QLabel#AcCount { color: #22d3ee; font-size: 12px; font-weight: 700; }
    QFrame#AcCard, QFrame#AcStream { background: #0a1624; border: 1px solid #1d4e68; border-radius: 10px; }
    QFrame#AcCard:hover, QFrame#AcStream:hover { border-color: #2b8fb0; }
    QFrame#AcCard[onwall="true"], QFrame#AcStream[onwall="true"] { border-color: #2fbf6a; background: #0b1a17; }
    QFrame#AcStream[selected="true"] { border: 1px solid #22d3ee; }
    QLabel#AcBanner { background: #07111c; border: 1px solid #13293d; border-radius: 6px; }
    QLabel#AcName { color: #ffffff; font-size: 14px; font-weight: 800; }
    QLabel#AcSub { color: #8ea0b5; font-size: 12px; }
    QLabel#AcRtsp { color: #7ee0ff; font-family: Consolas, monospace; font-size: 11px; }
    QLabel#AcBadge { color: #9fd8ea; background: #0f2638; border: 1px solid #1d4e68; border-radius: 5px;
                     font-family: Consolas, monospace; font-size: 10px; font-weight: 800; letter-spacing: 1px; padding: 2px 7px; }
    QLabel#AcBadge[tone="green"] { color: #7dffb0; background: #14361f; border-color: #2fbf6a; }
    QPushButton { background: #10141a; color: #d5dde8; border: 1px solid #3c4a5c; border-radius: 7px;
                  padding: 0 12px; min-height: 30px; font-weight: 700; }
    QPushButton:hover { background: #1a222c; border-color: #6d89a8; }
    QPushButton:disabled { color: #4b5b6c; border-color: #22303d; background: #0b0f14; }
    QPushButton#AcPrimary { background: #22d3ee; color: #06202a; border: none; font-weight: 800; }
    QPushButton#AcPrimary:hover { background: #67e8f9; }
    QPushButton#AcPrimary:disabled { background: #1d4e68; color: #8ea0b5; }
    QPushButton#AcOnWall { background: #14361f; color: #7dffb0; border: 1px solid #2fbf6a; font-weight: 800; }
    QPushButton#AcOnWall:hover { background: #3a1418; color: #ff8a95; border-color: #ff5c6c; }
    QPushButton#AcCyan { background: #123246; color: #55c7ff; border: 1px solid #55c7ff; }
    QPushButton#AcCyan:hover { background: #1a4660; }
    QPushButton#AcDanger:hover { background: #3a1418; color: #ff8a95; border-color: #ff5c6c; }
    QPushButton#AcSmall { min-height: 26px; padding: 0 9px; font-size: 12px; }
    QPushButton#AcSmallDanger { min-height: 26px; padding: 0 9px; font-size: 12px; }
    QPushButton#AcFilter { min-height: 26px; padding: 0 12px; font-size: 12px; border-radius: 13px; }
    QPushButton#AcFilter:checked { background: #123246; color: #22d3ee; border: 1px solid #22d3ee; }
    QPushButton#AcSmallDanger:hover { background: #3a1418; color: #ff8a95; border-color: #ff5c6c; }
    QLineEdit { background: rgba(8, 20, 34, 0.9); color: #e8f0ff; border: 1px solid #1d4e68; border-radius: 9px;
                padding: 8px 10px; selection-background-color: #22d3ee; selection-color: #06202a; }
    QLineEdit:focus { border: 1px solid #22d3ee; }
    QCheckBox { color: #d5dde8; spacing: 7px; font-size: 12px; }
    QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px; border: 1px solid #1d4e68; background: #08141f; }
    QCheckBox::indicator:hover { border-color: #22d3ee; }
    QCheckBox::indicator:checked { background: #22d3ee; border-color: #22d3ee; }
    QListWidget#AcCountries { background: #07111c; border: 1px solid #16324a; border-radius: 9px; outline: 0; padding: 4px; }
    QListWidget#AcCountries::item { border-radius: 6px; padding: 0px; margin: 1px 0; }
    QListWidget#AcCountries::item:hover { background: #0f2132; }
    QListWidget#AcCountries::item:selected { background: #123246; }
    QLabel#AcCountryName { color: #d5dde8; font-size: 13px; }
    QLabel#AcCountryName[ready="true"] { color: #ffffff; font-weight: 700; }
    QLabel#AcCountryNum { color: #4b5b6c; font-family: Consolas, monospace; font-size: 11px; font-weight: 700; }
    QLabel#AcCountryNum[ready="true"] { color: #22d3ee; background: #0f2638; border-radius: 5px; padding: 1px 6px; }
    QScrollArea { background: transparent; border: none; }
    QWidget#AcScrollBody { background: transparent; }
    QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
    QScrollBar::handle:vertical { background: #1d4e68; border-radius: 3px; min-height: 30px; }
    QScrollBar::handle:vertical:hover { background: #22d3ee; }
    QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
    QScrollBar::handle:horizontal { background: #1d4e68; border-radius: 3px; min-width: 30px; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
    QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
    QFrame#AcEmpty { background: #07111c; border: 1px dashed #1d4e68; border-radius: 12px; }
    QLabel#AcEmptyTitle { color: #ffffff; font-size: 15px; font-weight: 800; }
    QLabel#AcEmptyText { color: #8ea0b5; font-size: 12px; }
    QLabel#AcCode { color: #7ee0ff; font-family: Consolas, monospace; font-size: 12px; background: #08141f;
                    border: 1px solid #16324a; border-radius: 6px; padding: 6px 8px; }
    QLabel#CamField { color: #d5dde8; font-size: 13px; font-weight: 700; }
    QMessageBox { background: #0a1624; }
    QMessageBox QLabel { color: #e8f0ff; }
"""


def _set_prop(widget, name: str, value) -> None:
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _chip(text: str, tone: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("AcChip")
    if tone:
        label.setProperty("tone", tone)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def _badge(text: str, green: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName("AcBadge")
    if green:
        label.setProperty("tone", "green")
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return label


def _empty_state(title: str, text: str, code: str = "") -> QFrame:
    box = QFrame()
    box.setObjectName("AcEmpty")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(22, 22, 22, 22)
    lay.setSpacing(8)
    icon = QLabel()
    icon.setPixmap(_placeholder_pixmap(96, 54, ""))
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(icon)
    head = QLabel(title)
    head.setObjectName("AcEmptyTitle")
    head.setAlignment(Qt.AlignmentFlag.AlignCenter)
    head.setWordWrap(True)
    lay.addWidget(head)
    body = QLabel(text)
    body.setObjectName("AcEmptyText")
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.setWordWrap(True)
    lay.addWidget(body)
    if code:
        snippet = QLabel(code)
        snippet.setObjectName("AcCode")
        snippet.setWordWrap(True)
        snippet.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(snippet)
    return box


class CameraDialog(QDialog):
    PREVIEW_W, PREVIEW_H = 320, 180

    def __init__(self, parent=None, camera: dict | None = None):
        super().__init__(parent)
        self.setObjectName("CameraDialog")
        self.setWindowTitle("Camera")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.setStyleSheet(_ADD_CCTV_STYLE)
        self._camera = dict(camera or {})
        self._camera.setdefault("id", uuid.uuid4().hex[:12])
        self._banner_src = ""        # newly picked file (copied on Save)
        self._banner_cleared = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        title = QLabel("Edit camera" if camera else "New camera")
        title.setObjectName("AcSection")
        layout.addWidget(title)
        label = QLabel("RTSP address")
        label.setObjectName("CamField")
        layout.addWidget(label)
        self.rtsp_edit = QLineEdit()
        self.rtsp_edit.setPlaceholderText("rtsp://192.168.1.20:554/stream1")
        self.rtsp_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.rtsp_edit)
        hint = QLabel("Only an address you add is played. Empty slots stay offline.")
        hint.setObjectName("AcMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        show_row = QHBoxLayout()
        self.reveal_btn = QPushButton("Show address")
        self.reveal_btn.setObjectName("AcSmall")
        self.reveal_btn.setCheckable(True)
        self.reveal_btn.toggled.connect(self._toggle_reveal)
        show_row.addWidget(self.reveal_btn)
        show_row.addStretch()
        layout.addLayout(show_row)

        layout.addSpacing(6)
        blabel = QLabel("Banner / screenshot  ·  optional")
        blabel.setObjectName("CamField")
        layout.addWidget(blabel)
        brow = QHBoxLayout()
        brow.setSpacing(12)
        self.banner_preview = QLabel()
        self.banner_preview.setObjectName("AcBanner")
        self.banner_preview.setFixedSize(self.PREVIEW_W + 2, self.PREVIEW_H + 2)
        brow.addWidget(self.banner_preview)
        bcol = QVBoxLayout()
        bcol.setSpacing(6)
        self.banner_btn = QPushButton("Browse…")
        self.banner_btn.setObjectName("AcCyan")
        self.banner_btn.clicked.connect(self._browse_banner)
        self.banner_clear = QPushButton("Clear")
        self.banner_clear.setObjectName("AcSmallDanger")
        self.banner_clear.clicked.connect(self._clear_banner)
        self.banner_name = QLabel("")
        self.banner_name.setObjectName("AcMuted")
        self.banner_name.setWordWrap(True)
        bnote = QLabel("PNG, JPG or WebP. A copy is kept in AppData/DedSecCctv/banners.")
        bnote.setWordWrap(True)
        bnote.setObjectName("AcMuted")
        bcol.addWidget(self.banner_btn)
        bcol.addWidget(self.banner_clear)
        bcol.addWidget(self.banner_name)
        bcol.addStretch()
        bcol.addWidget(bnote)
        brow.addLayout(bcol, 1)
        layout.addLayout(brow)

        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("AcPrimary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        if camera:
            self.rtsp_edit.setText(camera.get("rtsp", ""))
        self._show_banner()

    def _current_banner(self) -> Path | None:
        if self._banner_src:
            return Path(self._banner_src)
        if self._banner_cleared:
            return None
        return _camera_banner_path(self._camera)

    def _show_banner(self) -> None:
        path = self._current_banner()
        self.banner_preview.setPixmap(_banner_pixmap(path, self.PREVIEW_W, self.PREVIEW_H, "No banner"))
        self.banner_name.setText(path.name if path else "No image picked")
        self.banner_clear.setEnabled(path is not None)

    def _browse_banner(self) -> None:
        start = str(Path(self._banner_src).parent) if self._banner_src else str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self, "Pick a banner / screenshot", start, "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not path:
            return
        if QImage(path).isNull() and Path(path).suffix.lower() not in BANNER_EXTS:
            QMessageBox.warning(self, "Banner", "That file is not an image this app can read.")
            return
        self._banner_src = path
        self._banner_cleared = False
        self._show_banner()

    def _clear_banner(self) -> None:
        self._banner_src = ""
        self._banner_cleared = True
        self._show_banner()

    def _toggle_reveal(self, checked: bool) -> None:
        self.rtsp_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self.reveal_btn.setText("Hide address" if checked else "Show address")

    def _save(self) -> None:
        rtsp = self.rtsp_edit.text().strip()
        if not _valid_rtsp(rtsp):
            QMessageBox.warning(self, "Camera", "Address must start with rtsp:// or rtsps:// and include a host.")
            return
        self._camera["rtsp"] = rtsp
        if self._banner_src:
            try:
                self._camera["banner"] = _save_camera_banner(self._banner_src, self._camera["id"])
            except Exception as exc:
                QMessageBox.warning(self, "Banner", f"Could not copy the image.\n{exc}")
                return
        elif self._banner_cleared:
            old = _camera_banner_path(self._camera)
            self._camera.pop("banner", None)
            if old is not None and old.parent == _store_path().parent / "banners":
                try:
                    old.unlink()
                except Exception:
                    pass
        self.accept()

    def camera(self) -> dict:
        return self._camera


class _MineCard(QFrame):
    """Card for one of the user's own cameras in Add CCTV."""

    THUMB_W, THUMB_H = 128, 72

    def __init__(self, dialog: "AddCctvDialog", cam: dict):
        super().__init__()
        self.setObjectName("AcCard")
        on_wall = bool(cam.get("pinned"))
        self.setProperty("onwall", "true" if on_wall else "false")
        cam_id = cam.get("id")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        thumb = QLabel()
        thumb.setObjectName("AcBanner")
        thumb.setFixedSize(self.THUMB_W + 2, self.THUMB_H + 2)
        thumb.setPixmap(_banner_pixmap(_camera_banner_path(cam), self.THUMB_W, self.THUMB_H, ""))
        top.addWidget(thumb)
        info = QVBoxLayout()
        info.setSpacing(4)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name = QLabel(cam.get("name") or "Camera")
        name.setObjectName("AcName")
        name_row.addWidget(name)
        name_row.addStretch()
        name_row.addWidget(_badge("ON WALL" if on_wall else "SAVED", green=on_wall))
        info.addLayout(name_row)
        rtsp = QLabel(_mask_rtsp(cam.get("rtsp", "")))
        rtsp.setObjectName("AcRtsp")
        rtsp.setToolTip(rtsp.text())
        rtsp.setMinimumWidth(60)
        rtsp.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(rtsp)
        info.addStretch()
        top.addLayout(info, 1)
        lay.addLayout(top)
        row = QHBoxLayout()
        row.setSpacing(6)
        edit = QPushButton("Edit")
        edit.setObjectName("AcSmall")
        edit.clicked.connect(lambda _=False: dialog._edit(cam_id))
        remove = QPushButton("Remove")
        remove.setObjectName("AcSmallDanger")
        remove.clicked.connect(lambda _=False: dialog._remove(cam_id))
        pin = QPushButton("Take off wall" if on_wall else "Show on wall")
        pin.setObjectName("AcOnWall" if on_wall else "AcPrimary")
        pin.setMinimumHeight(26)
        pin.clicked.connect(lambda _=False: dialog._pin(cam_id))
        row.addWidget(edit)
        row.addWidget(remove)
        row.addStretch()
        row.addWidget(pin)
        lay.addLayout(row)


class _StreamCard(QFrame):
    """Card for a catalog stream (streams/<Country>/*.json) in Add CCTV."""

    def __init__(self, dialog: "AddCctvDialog", country: str, row: dict, on_wall: bool, selected: bool):
        super().__init__()
        self.setObjectName("AcStream")
        self.dialog = dialog
        self.stream_id = row.get("id")
        self._country = country
        self._row = row
        self._full_name = row.get("name") or "Stream"
        self.setProperty("onwall", "true" if on_wall else "false")
        self.setProperty("selected", "true" if selected else "false")
        self.setFixedWidth(BANNER_W + 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Double-click to add to the wall or take it off")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        banner = QLabel()
        banner.setObjectName("AcBanner")
        banner.setFixedSize(BANNER_W + 2, BANNER_H + 2)
        banner.setPixmap(_stream_banner_pixmap(country, row, BANNER_W, BANNER_H))
        self._banner = banner
        lay.addWidget(banner)
        full = row.get("name") or "Stream"
        name = QLabel(full)
        name.setObjectName("AcName")
        name.setToolTip(full)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bold = QFont(name.font())
        bold.setPixelSize(14)
        bold.setBold(True)
        name.setText(QFontMetrics(bold).elidedText(full, Qt.TextElideMode.ElideRight, BANNER_W))
        self._name = name
        self._bold = bold
        self._card_w = BANNER_W + 22
        lay.addWidget(name)
        sub_row = QHBoxLayout()
        sub_row.setSpacing(6)
        city = QLabel(" · ".join(part for part in (row.get("city") or "", country) if part))
        city.setObjectName("AcSub")
        if _is_clip(row):
            city.setText(f"{city.text()} · {CLIP_LABEL}")
            city.setToolTip(CLIP_LABEL)
        elif row.get("live") is True:
            city.setToolTip("Continuous live stream")
        city.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sub_row.addWidget(city, 1)
        mine_src = row.get("_source") == SOURCE_USER
        sub_row.addWidget(_badge("ON WALL" if on_wall else ("MY STREAM" if mine_src else "CATALOG"), green=on_wall))
        lay.addLayout(sub_row)
        btn = QPushButton("On wall · remove" if on_wall else "Add to wall")
        btn.setObjectName("AcOnWall" if on_wall else "AcPrimary")
        btn.clicked.connect(lambda _=False: dialog._add_country(self.stream_id))
        lay.addWidget(btn)

    def set_card_width(self, card_w: int) -> None:
        """Grow or shrink the card (and its 16:9 banner) to fill its grid column."""
        card_w = max(BANNER_W + 22, int(card_w))
        if card_w == self._card_w:
            return
        self._card_w = card_w
        bw = card_w - 22
        bh = round(bw * 9 / 16)
        self.setFixedWidth(card_w)
        self._banner.setFixedSize(bw + 2, bh + 2)
        self._banner.setPixmap(_stream_banner_pixmap(self._country, self._row, bw, bh))
        self._name.setText(QFontMetrics(self._bold).elidedText(self._full_name, Qt.TextElideMode.ElideRight, bw))

    def mousePressEvent(self, event):
        self.dialog._select_stream(self.stream_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.dialog._add_country(self.stream_id)


class _FlowGrid(QWidget):
    """Grid that re-flows fixed-width cards into as many columns as fit."""

    def __init__(self, card_w: int, spacing: int = 12):
        super().__init__()
        self.setObjectName("AcScrollBody")
        self.card_w = card_w
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(spacing)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.items: list[QWidget] = []
        self.footer: QWidget | None = None
        self._cols = 0

    def set_items(self, items: list, footer=None) -> None:
        for widget in self.items + ([self.footer] if self.footer else []):
            self.grid.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self.items = list(items)
        self.footer = footer
        self._cols = 0
        self._relayout()

    def _columns(self) -> int:
        width = self.width() or (self.parentWidget().width() if self.parentWidget() else self.card_w)
        return max(1, (width - 4 + self.grid.spacing()) // (self.card_w + self.grid.spacing()))

    def _fill_width(self, cols: int) -> None:
        width = self.width() or (self.parentWidget().width() if self.parentWidget() else self.card_w)
        spacing = self.grid.spacing()
        card_w = (width - 4 - spacing * (cols - 1)) // cols
        card_w = min(card_w, 520)  # don't let a single card get huge
        for widget in self.items:
            if hasattr(widget, "set_card_width"):
                widget.set_card_width(card_w)

    def _relayout(self) -> None:
        cols = self._columns()
        self._fill_width(cols)
        if cols == self._cols and all(self.grid.indexOf(w) >= 0 for w in self.items):
            return
        self._cols = cols
        for widget in self.items + ([self.footer] if self.footer else []):
            self.grid.removeWidget(widget)
        for index, widget in enumerate(self.items):
            self.grid.addWidget(widget, index // cols, index % cols)
        if self.footer is not None:
            span = cols if self.items else 1
            self.grid.addWidget(self.footer, (len(self.items) + cols - 1) // cols, 0, 1, span)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()



# ---------------------------------------------------------------------------
# Custom window bar
# One frameless title bar (logo, title, minimize / maximize-restore / close) shared by the
# main window and the Add CCTV window. Drag to move (system move, Aero snap works on
# Windows), double-click to maximize/restore, drag a window edge to resize.
# ---------------------------------------------------------------------------
TITLEBAR_H = 34
_RESIZE_MARGIN = 5

_TITLEBAR_STYLE = """
    QFrame#AppTitleBar { background: #05080c; border: 0; border-bottom: 1px solid #10202e; }
    QLabel#AppTitleText { color: #c9d6e6; background: transparent; font-size: 12px; font-weight: 700; letter-spacing: 1px; }
    QLabel#AppTitleLogo { background: transparent; }
    QPushButton#AppTitleBtn, QPushButton#AppTitleClose {
        background: transparent; border: 0; border-radius: 0; padding: 0; margin: 0; min-height: 0; }
    QPushButton#AppTitleBtn:hover { background: #16222e; }
    QPushButton#AppTitleBtn:pressed { background: #1f3040; }
    QPushButton#AppTitleClose:hover { background: #c42b1c; }
    QPushButton#AppTitleClose:pressed { background: #a3241a; }
"""


def _titlebar_icon(kind: str, color: str = "#c9d6e6", size: int = 10) -> QIcon:
    icon = _titlebar_icon_raw(kind, _theme_hex(color) if color.startswith("#") else color, size)
    icon._ds_spec = ["_titlebar_icon", [kind, color, size]]
    return icon


def _titlebar_icon_raw(kind: str, color: str, size: int) -> QIcon:
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 1.1)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    s = float(size)
    if kind == "min":
        painter.drawLine(QPointF(0.5, s / 2), QPointF(s - 0.5, s / 2))
    elif kind == "max":
        painter.drawRect(QRectF(0.5, 0.5, s - 1.5, s - 1.5))
    elif kind == "restore":
        painter.drawRect(QRectF(0.5, 2.5, s - 3.5, s - 3.5))
        painter.drawLine(QPointF(2.5, 0.5), QPointF(s - 0.5, 0.5))
        painter.drawLine(QPointF(s - 0.5, 0.5), QPointF(s - 0.5, s - 3.0))
    elif kind == "close":
        painter.drawLine(QPointF(0.5, 0.5), QPointF(s - 0.5, s - 0.5))
        painter.drawLine(QPointF(s - 0.5, 0.5), QPointF(0.5, s - 0.5))
    painter.end()
    return QIcon(pix)


class WindowTitleBar(QFrame):
    """Frameless window bar used by CctvApp and AddCctvDialog.

    ``WindowTitleBar.install(window, title)`` turns ``window`` frameless, puts the bar at the
    top of its (vertical) layout and handles move / resize / maximize for it.
    """

    def __init__(self, window: QWidget, title: str = "", rich_title: str = ""):
        super().__init__(window)
        self._win = window
        self._drag_from = None
        self.setObjectName("AppTitleBar")
        self.setFixedHeight(TITLEBAR_H)
        self.setStyleSheet(_TITLEBAR_STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 0, 0)
        row.setSpacing(8)
        logo = QLabel()
        logo.setObjectName("AppTitleLogo")
        logo_pix = _logo_pixmap(18)
        if logo_pix.isNull():
            logo_pix = _glyph_icon("camera", "#22d3ee", 16).pixmap(16, 16)
        logo.setPixmap(logo_pix)
        logo.setFixedSize(20, 20)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("AppTitleText")
        self.set_title(title or window.windowTitle(), rich_title)
        row.addWidget(self.title_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        self.min_btn = self._button("AppTitleBtn", "min", "Minimize", window.showMinimized)
        self.max_btn = self._button("AppTitleBtn", "max", "Maximize", self.toggle_maximized)
        self.close_btn = self._button("AppTitleClose", "close", "Close", window.close)
        for btn in (self.min_btn, self.max_btn, self.close_btn):
            row.addWidget(btn)
        window.installEventFilter(self)
        window.setMouseTracking(True)
        self._sync_state()

    @classmethod
    def install(cls, window: QWidget, title: str = "", rich_title: str = "") -> "WindowTitleBar":
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        flags |= (Qt.WindowType.WindowMinimizeButtonHint | Qt.WindowType.WindowMaximizeButtonHint
                  | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowSystemMenuHint)
        window.setWindowFlags(flags)
        bar = cls(window, title, rich_title)
        lay = QWidget.layout(window)
        if isinstance(lay, QVBoxLayout):
            lay.insertWidget(0, bar)
        window._title_bar = bar
        return bar

    def set_title(self, title: str, rich_title: str = "") -> None:
        if rich_title:
            self.title_lbl.setTextFormat(Qt.TextFormat.RichText)
            self.title_lbl.setText(rich_title)
        else:
            self.title_lbl.setTextFormat(Qt.TextFormat.PlainText)
            self.title_lbl.setText(title)

    def _button(self, name: str, icon: str, tip: str, slot) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName(name)
        btn.setIcon(_titlebar_icon(icon))
        btn.setIconSize(QSize(10, 10))
        btn.setFixedSize(46, TITLEBAR_H)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip(tip)
        btn.clicked.connect(lambda _=False: slot())
        return btn

    # -- window state ---------------------------------------------------------
    def _is_big(self) -> bool:
        return bool(self._win.windowState() & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen))

    def toggle_maximized(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    def _sync_state(self) -> None:
        big = self._is_big()
        self.max_btn.setIcon(_titlebar_icon("restore" if big else "max"))
        self.max_btn.setToolTip("Restore" if big else "Maximize")
        lay = QWidget.layout(self._win)  # CctvApp shadows .layout with an attribute
        if lay is not None:  # thin border strip so the edges can be grabbed for resizing
            m = 0 if big else _RESIZE_MARGIN
            lay.setContentsMargins(m, m, m, m)

    # -- move -------------------------------------------------------------------
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(event)
        if (event.position().toPoint() - self._drag_from).manhattanLength() < 4:
            return
        win = self._win
        if self._is_big():
            # Pull a maximized window off the top: restore it under the cursor, then move.
            share = self._drag_from.x() / max(1, self.width())
            win.showNormal()
            gp = event.globalPosition().toPoint()
            win.move(gp.x() - int(win.width() * share), gp.y() - self._drag_from.y())
        self._drag_from = None
        handle = win.windowHandle()
        if handle is not None:
            handle.startSystemMove()
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_from = None
        super().mouseReleaseEvent(event)

    # -- resize (edges of the frameless window) -------------------------------
    def _edges_at(self, pos: QPoint):
        if self._is_big():
            return None
        w, h, m = self._win.width(), self._win.height(), _RESIZE_MARGIN + 1
        edges = Qt.Edge(0)
        if pos.x() <= m:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= w - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= m:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= h - m:
            edges |= Qt.Edge.BottomEdge
        return edges if edges != Qt.Edge(0) else None

    @staticmethod
    def _cursor_for(edges) -> Qt.CursorShape:
        E = Qt.Edge
        if edges in (E.LeftEdge | E.TopEdge, E.RightEdge | E.BottomEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (E.RightEdge | E.TopEdge, E.LeftEdge | E.BottomEdge):
            return Qt.CursorShape.SizeBDiagCursor
        if edges & (E.LeftEdge | E.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    def eventFilter(self, obj, event):
        if obj is self._win:
            kind = event.type()
            if kind == QEvent.Type.WindowStateChange:
                self._sync_state()
            elif kind == QEvent.Type.WindowTitleChange and self.title_lbl.textFormat() == Qt.TextFormat.PlainText:
                self.title_lbl.setText(self._win.windowTitle())
            elif kind == QEvent.Type.MouseMove and not event.buttons():
                edges = self._edges_at(event.position().toPoint())
                if edges:
                    self._win.setCursor(self._cursor_for(edges))
                else:
                    self._win.unsetCursor()
            elif kind == QEvent.Type.Leave:
                self._win.unsetCursor()
            elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                edges = self._edges_at(event.position().toPoint())
                handle = self._win.windowHandle()
                if edges and handle is not None:
                    handle.startSystemResize(edges)
                    return True
        return super().eventFilter(obj, event)


class WallFullDialog(QDialog):
    """Shown when the wall already has WALL_SLOTS feeds: pick feeds to take off to make room."""

    THUMB_W, THUMB_H = 96, 54

    def __init__(self, parent, wall_cams: list, incoming: str = ""):
        super().__init__(parent)
        self.setObjectName("CameraDialog")
        self.setWindowTitle("Wall full")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(560)
        self.setStyleSheet(_ADD_CCTV_STYLE)
        self._checks = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        title = QLabel("The wall is full")
        title.setObjectName("AcSection")
        layout.addWidget(title)
        what = f"“{incoming}”" if incoming else "this feed"
        info = QLabel(f"The wall already shows {WALL_SLOTS} feeds. Would you like to remove some to make room "
                      f"for {what}? Pick the feeds to take off the wall.")
        info.setObjectName("AcMuted")
        info.setWordWrap(True)
        layout.addWidget(info)
        for cam in wall_cams:
            row = QFrame()
            row.setObjectName("AcCard")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(10, 8, 10, 8)
            lay.setSpacing(10)
            check = QCheckBox()
            check.toggled.connect(self._sync)
            lay.addWidget(check)
            thumb = QLabel()
            thumb.setObjectName("AcBanner")
            try:
                thumb.setPixmap(_banner_pixmap(_camera_banner_path(cam), self.THUMB_W, self.THUMB_H))
            except Exception:
                thumb.setPixmap(_placeholder_pixmap(self.THUMB_W, self.THUMB_H, ""))
            thumb.setFixedSize(self.THUMB_W + 2, self.THUMB_H + 2)
            lay.addWidget(thumb)
            text = QVBoxLayout()
            text.setSpacing(2)
            name = QLabel(str(cam.get("name") or "Camera"))
            name.setObjectName("AcName")
            sub_bits = [str(cam.get(k)) for k in ("city", "country") if cam.get(k)]
            if not sub_bits:
                sub_bits = ["My camera" if cam.get("source") != "catalog" else "Catalog stream"]
            sub = QLabel("  ·  ".join(sub_bits))
            sub.setObjectName("AcSub")
            text.addWidget(name)
            text.addWidget(sub)
            lay.addLayout(text, 1)
            remove = QPushButton("Remove")
            remove.setObjectName("AcSmallDanger")
            remove.setCheckable(True)
            remove.toggled.connect(check.setChecked)
            check.toggled.connect(remove.setChecked)
            lay.addWidget(remove)
            row.mousePressEvent = lambda _e, c=check: c.toggle()
            self._checks[cam.get("id")] = check
            layout.addWidget(row)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok_btn = QPushButton("Remove selected && add new")
        self.ok_btn.setObjectName("AcPrimary")
        self.ok_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok_btn)
        layout.addLayout(buttons)
        self._sync()

    def _sync(self, *_args) -> None:
        self.ok_btn.setEnabled(bool(self.selected_ids()))

    def selected_ids(self) -> list:
        return [cam_id for cam_id, check in self._checks.items() if check.isChecked()]


class AddCctvDialog(QDialog):
    STREAM_BATCH = 48

    def __init__(self, page: "CctvPage"):
        super().__init__(page)
        self.page = page
        self.setObjectName("AddCctv")
        self.setWindowTitle("Add CCTV")
        self.setModal(True)
        icon = _asset("dedsec_logo.ico", "dedsec_logo.png", "applogo.png")
        if icon:
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet(_ADD_CCTV_STYLE)
        self.resize(1280, 800)
        self.setMinimumSize(1060, 660)
        self._selected_stream = ""
        self._stream_limit = self.STREAM_BATCH
        self._counts: dict = {}
        self._source = SOURCE_USER  # user_streams/ ("My streams")
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        # Same frameless window bar as the main window (inserted at the top of ``outer``).
        self.title_bar = WindowTitleBar.install(
            self, "Add CCTV",
            '<span style="color:#ffffff">DedSec</span>&nbsp;<span style="color:#22d3ee">CCTV</span>'
            '&nbsp;&nbsp;<span style="color:#5f7a92">·&nbsp;&nbsp;Add CCTV</span>')
        root = QVBoxLayout()
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)
        outer.addLayout(root, 1)

        # Header ------------------------------------------------------------
        header = QFrame()
        header.setObjectName("AcHeader")
        hrow = QHBoxLayout(header)
        hrow.setContentsMargins(18, 12, 16, 12)
        hrow.setSpacing(10)
        htext = QVBoxLayout()
        htext.setSpacing(2)
        title = QLabel('<span style="color:#ffffff">Add </span><span style="color:#22d3ee">CCTV</span>')
        title.setObjectName("AcTitle")
        title.setTextFormat(Qt.TextFormat.RichText)
        htext.addWidget(title)
        note = QLabel("Save up to 4 of your own RTSP cameras, or pick packed streams by country. "
                      "Up to 4 feeds show on the wall at once.")
        note.setObjectName("AcHelp")
        note.setWordWrap(True)
        htext.addWidget(note)
        hrow.addLayout(htext, 1)
        self.mine_chip = _chip("MINE 0/4", "grey")
        self.wall_chip = _chip("WALL 0/4", "green")
        self.total_chip = _chip("0 STREAMS")
        for chip in (self.mine_chip, self.wall_chip, self.total_chip):
            hrow.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(12)

        # Left: my cameras ----------------------------------------------------
        mine_panel = QFrame()
        mine_panel.setObjectName("AcPanel")
        mine_panel.setFixedWidth(380)
        mine = QVBoxLayout(mine_panel)
        mine.setContentsMargins(14, 14, 14, 14)
        mine.setSpacing(10)
        mhead = QHBoxLayout()
        mtitle = QVBoxLayout()
        mtitle.setSpacing(0)
        lbl = QLabel("My cameras")
        lbl.setObjectName("AcSection")
        mtitle.addWidget(lbl)
        self.mine_hint = QLabel("4 max · RTSP you add yourself")
        self.mine_hint.setObjectName("AcMuted")
        mtitle.addWidget(self.mine_hint)
        mhead.addLayout(mtitle, 1)
        self.add_btn = QPushButton("+ Add camera")
        self.add_btn.setObjectName("AcPrimary")
        self.add_btn.clicked.connect(self._add)
        mhead.addWidget(self.add_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        mine.addLayout(mhead)
        self.mine_scroll = QScrollArea()
        self.mine_scroll.setWidgetResizable(True)
        self.mine_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mine_body = QWidget()
        self.mine_body.setObjectName("AcScrollBody")
        self.mine_cards = QVBoxLayout(self.mine_body)
        self.mine_cards.setContentsMargins(0, 0, 4, 0)
        self.mine_cards.setSpacing(10)
        self.mine_scroll.setWidget(self.mine_body)
        mine.addWidget(self.mine_scroll, 1)
        body.addWidget(mine_panel)

        # Right: country catalog ---------------------------------------------
        cat_panel = QFrame()
        cat_panel.setObjectName("AcPanel")
        cat = QHBoxLayout(cat_panel)
        cat.setContentsMargins(14, 14, 14, 14)
        cat.setSpacing(14)

        picker = QVBoxLayout()
        picker.setSpacing(8)
        self._source_btns = {}
        clbl = QLabel("My streams")
        clbl.setObjectName("AcSection")
        self._source_label = clbl
        picker.addWidget(clbl)
        self.total_label = QLabel("All countries  ·  0 streams")
        self.total_label.setObjectName("AcCount")
        picker.addWidget(self.total_label)
        self.country_search = QLineEdit()
        self.country_search.setPlaceholderText("Search country or stream")
        self.country_search.setClearButtonEnabled(True)
        self.country_search.textChanged.connect(self._filter_countries)
        picker.addWidget(self.country_search)
        self.only_ready = QCheckBox("Only countries with streams")
        self.only_ready.toggled.connect(lambda _checked: self._filter_countries(self.country_search.text()))
        picker.addWidget(self.only_ready)
        self.sort_by_count = QCheckBox("List by most streams")
        self.sort_by_count.setChecked(True)
        self.sort_by_count.toggled.connect(lambda _checked: self._filter_countries(self.country_search.text()))
        picker.addWidget(self.sort_by_count)
        self.country_box = QListWidget()
        self.country_box.setObjectName("AcCountries")
        self.country_box.setFixedWidth(232)
        self.country_box.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.country_box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.country_box.currentItemChanged.connect(lambda _cur, _prev: self._reload_country())
        picker.addWidget(self.country_box, 1)
        cat.addLayout(picker)

        streams = QVBoxLayout()
        streams.setSpacing(8)
        shead = QHBoxLayout()
        self.country_title = QLabel("")
        self.country_title.setObjectName("AcSection")
        shead.addWidget(self.country_title)
        shead.addStretch()
        self.country_hint = QLabel("")
        self.country_hint.setObjectName("AcCount")
        shead.addWidget(self.country_hint)
        streams.addLayout(shead)
        # Category filter: All / Traffic cams / Residential
        self._category = "all"
        frow = QHBoxLayout()
        frow.setSpacing(6)
        self._filter_btns = {}
        for key, text in (("all", "All"), ("traffic", "Traffic cams"), ("residential", "Residential")):
            fb = QPushButton(text)
            fb.setObjectName("AcFilter")
            fb.setCheckable(True)
            fb.setChecked(key == "all")
            fb.setCursor(Qt.CursorShape.PointingHandCursor)
            fb.clicked.connect(lambda _checked, k=key: self._set_category(k))
            frow.addWidget(fb)
            self._filter_btns[key] = fb
        frow.addStretch()
        streams.addLayout(frow)
        self.stream_scroll = QScrollArea()
        self.stream_scroll.setWidgetResizable(True)
        self.stream_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stream_grid = _FlowGrid(300)  # min card width; cards stretch to fill
        self.stream_scroll.setWidget(self.stream_grid)
        streams.addWidget(self.stream_scroll, 1)
        tip = QLabel("Tip: double-click a card to add it to the wall or take it off.")
        tip.setObjectName("AcMuted")
        streams.addWidget(tip)
        cat.addLayout(streams, 1)
        body.addWidget(cat_panel, 1)
        root.addLayout(body, 1)

        close_row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("AcMuted")
        close_row.addWidget(self.status, 1)
        close_btn = QPushButton("Done")
        close_btn.setObjectName("AcCyan")
        close_btn.setMinimumWidth(110)
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)
        self._filter_countries("")
        self._reload()

    # -- data helpers -------------------------------------------------------
    def _mine(self):
        return [cam for cam in self.page.cameras if cam.get("source") != "catalog"]

    def _pinned_count(self) -> int:
        return sum(1 for cam in self.page.cameras if cam.get("pinned"))

    def _wall_full(self, incoming: str = "") -> bool:
        """Wall is full: offer to take feeds off. Returns True once there is a free slot."""
        wall = [cam for cam in self.page.cameras if cam.get("pinned")]
        dialog = WallFullDialog(self, wall, incoming)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        drop = set(dialog.selected_ids())
        if not drop:
            return False
        for cam in self.page.cameras:
            if cam.get("id") in drop:
                cam["pinned"] = False
        self.page.save_and_refresh()  # persist + update the wall live
        self._refresh_keep_scroll()
        return self._pinned_count() < WALL_SLOTS

    # -- my cameras -----------------------------------------------------------
    def _reload(self) -> None:
        while self.mine_cards.count():
            item = self.mine_cards.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        mine = self._mine()
        pinned = self._pinned_count()
        for cam in mine:
            self.mine_cards.addWidget(_MineCard(self, cam))
        if not mine:
            self.mine_cards.addWidget(_empty_state(
                "No cameras yet",
                "Add your own RTSP camera with + Add camera. You can give it a banner screenshot too.",
            ))
        self.mine_cards.addStretch(1)
        self.add_btn.setEnabled(len(mine) < MAX_CAMERAS)
        self.add_btn.setToolTip("" if len(mine) < MAX_CAMERAS else "Your own list is full. 4 max.")
        self.mine_hint.setText(f"{len(mine)} of {MAX_CAMERAS} saved · RTSP you add yourself")
        self.mine_chip.setText(f"MINE {len(mine)}/{MAX_CAMERAS}")
        self.wall_chip.setText(f"WALL {pinned}/{WALL_SLOTS}")
        self.setWindowTitle(f"Add CCTV  ·  mine {len(mine)}/{MAX_CAMERAS}  ·  wall {pinned}/{WALL_SLOTS}")
        self.status.setText("Wall is full. Take a feed off to add another." if pinned >= WALL_SLOTS
                            else f"{WALL_SLOTS - pinned} wall slot{'s' if WALL_SLOTS - pinned != 1 else ''} free.")
        self._reload_country()

    # -- countries ------------------------------------------------------------
    def _selected_country(self) -> str:
        item = self.country_box.currentItem()
        if not item:
            return ""
        return item.data(Qt.ItemDataRole.UserRole) or ""

    def _country_row(self, name: str, count: int) -> QWidget:
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(6)
        label = QLabel(name)
        label.setObjectName("AcCountryName")
        label.setProperty("ready", "true" if count else "false")
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        num = QLabel(str(count))
        num.setObjectName("AcCountryNum")
        num.setProperty("ready", "true" if count else "false")
        lay.addWidget(label, 1)
        lay.addWidget(num)
        return row

    def _filter_countries(self, text: str) -> None:
        current = self._selected_country()
        counts, total = stream_counts(self._source)
        self._counts = counts
        ready = sum(1 for _name, count in counts.items() if count)
        self.total_label.setText(f"All countries  ·  {total} streams  ·  {ready} countries")
        self.total_chip.setText(f"{total} STREAMS")
        names = catalog_countries(text, self._source)
        needle = (text or "").strip().lower()
        if needle:  # also surface countries whose streams match the search (name / city)
            for name in counts:
                if name in names:
                    continue
                if any(needle in f"{row.get('name','')} {row.get('city','')}".lower() for row in catalog_streams(name, self._source)):
                    names.append(name)
        if self.only_ready.isChecked():
            names = [name for name in names if counts.get(name, 0)]
        names.sort(key=lambda name: (-counts.get(name, 0) if self.sort_by_count.isChecked() else 0, name.lower()))
        self.country_box.blockSignals(True)
        self.country_box.clear()
        for name in names:
            count = counts.get(name, 0)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(f"{name} · {count} stream{'s' if count != 1 else ''}")
            row = self._country_row(name, count)
            item.setSizeHint(QSize(200, 32))
            self.country_box.addItem(item)
            self.country_box.setItemWidget(item, row)
        picked = None
        for i in range(self.country_box.count()):
            if self.country_box.item(i).data(Qt.ItemDataRole.UserRole) == current:
                picked = self.country_box.item(i)
                break
        if picked is None and self.country_box.count():
            picked = self.country_box.item(0)
        if picked is not None:
            self.country_box.setCurrentItem(picked)
            self.country_box.scrollToItem(picked)
        self.country_box.blockSignals(False)
        self._reload_country()

    # -- stream grid ----------------------------------------------------------
    def _reload_country(self, _index: int = -1) -> None:
        country = self._selected_country()
        if country != getattr(self, "_shown_country", None):
            self._stream_limit = self.STREAM_BATCH
            self._shown_country = country
        self._render_streams()

    def on_theme_changed(self) -> None:
        """Re-render cards so banners / placeholders pick up the new theme."""
        QTimer.singleShot(0, self._reload)  # after apply_theme has finished walking the widgets

    def _set_source(self, key: str) -> None:
        self._source = key
        for k, sb in self._source_btns.items():
            sb.setChecked(k == key)
        self._source_label.setText("My streams" if key == SOURCE_USER else "Country streams")
        self._selected_stream = ""
        self._stream_limit = self.STREAM_BATCH
        self._shown_country = None
        self._filter_countries(self.country_search.text())

    def _set_category(self, key: str) -> None:
        self._category = key
        for k, fb in self._filter_btns.items():
            fb.setChecked(k == key)
        self._stream_limit = self.STREAM_BATCH
        self._render_streams()

    def _render_streams(self) -> None:
        country = self._selected_country()
        needle = self.country_search.text().strip().lower()
        on_wall = {cam.get("id") for cam in self.page.cameras if cam.get("pinned")}
        source = self._source
        rows = catalog_streams(country, source)
        if needle and needle not in country.lower():
            rows = [row for row in rows if needle in f"{row.get('name','')} {row.get('city','')}".lower()]
        category = getattr(self, "_category", "all")
        if category != "all":
            rows = [row for row in rows if row.get("category") == category]
        self.country_title.setText(country or "No country match")
        self.country_hint.setText(f"{len(rows)} available" if country else "")
        if not rows:
            folder_missing = not catalog_available(source)
            sample = ('{"streams": [{"id": "uk-cam-1", "name": "London Bridge", "city": "London",\n'
                      '  "rtsp": "rtsp://…", "banner": "banners/london.jpg"}]}')
            if folder_missing:
                empty = _empty_state(
                    "No user_streams folder yet" if source == SOURCE_USER else "No streams catalog found",
                    ("Restart the app to re-create user_streams/ next to it, then add streams to "
                     "user_streams/<Country>/streams.json." if source == SOURCE_USER else
                     "Create a streams folder next to the app, then drop JSON files and banner images "
                     "into streams/<Country>/ (for example streams/United Kingdom/)."),
                    sample,
                )
            elif not country:
                empty = _empty_state("No country matches", "Try another search, or turn off “Only countries with streams”.")
            elif category != "all" and catalog_streams(country, source):
                label = "Traffic cams" if category == "traffic" else "Residential"
                empty = _empty_state(f"No {label} streams in {country}",
                                     "Switch the filter back to All to see every stream.")
            elif needle and needle not in country.lower() and catalog_streams(country, source):
                empty = _empty_state(f"Nothing matches “{self.country_search.text().strip()}”",
                                     f"No stream name or city in {country} contains that text.")
            elif source == SOURCE_USER:
                empty = _empty_state(
                    f"No streams of yours for {country} yet",
                    f"Edit user_streams/{country}/streams.json next to the app (see README.txt there; "
                    "entries marked \"example\": true are ignored) and reopen this window.",
                    sample,
                )
            else:
                empty = _empty_state(
                    f"No streams for {country} yet",
                    "Add some to user_streams/<Country>/streams.json.",
                )
            empty.setMaximumWidth(520)
            self.stream_grid.set_items([], empty)
            return
        shown = rows[: self._stream_limit]
        if self._selected_stream not in {row.get("id") for row in shown}:
            self._selected_stream = ""
        cards = [
            _StreamCard(self, country, row, row.get("id") in on_wall, row.get("id") == self._selected_stream)
            for row in shown
        ]
        footer = None
        if len(rows) > len(shown):
            footer = QPushButton(f"Show more  ·  {len(rows) - len(shown)} left")
            footer.setObjectName("AcCyan")
            footer.clicked.connect(self._more_streams)
        self.stream_grid.set_items(cards, footer)

    def _more_streams(self) -> None:
        value = self.stream_scroll.verticalScrollBar().value()
        self._stream_limit += self.STREAM_BATCH
        self._render_streams()
        QTimer.singleShot(0, lambda: self.stream_scroll.verticalScrollBar().setValue(value))

    def _select_stream(self, stream_id: str) -> None:
        self._selected_stream = stream_id or ""
        for card in self.stream_grid.items:
            if isinstance(card, _StreamCard):
                _set_prop(card, "selected", "true" if card.stream_id == stream_id else "false")

    def _refresh_keep_scroll(self) -> None:
        mine_v = self.mine_scroll.verticalScrollBar().value()
        grid_v = self.stream_scroll.verticalScrollBar().value()
        self._reload()
        QTimer.singleShot(0, lambda: (self.mine_scroll.verticalScrollBar().setValue(mine_v),
                                      self.stream_scroll.verticalScrollBar().setValue(grid_v)))

    # -- actions --------------------------------------------------------------
    def _selected_id(self) -> str:
        return getattr(self, "_mine_selected", "") or ""

    def _add(self) -> None:
        if len(self._mine()) >= MAX_CAMERAS:
            QMessageBox.information(self, "Add CCTV", "Your own list is full. 4 max.")
            return
        dialog = CameraDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        cam = dialog.camera()
        cam["id"] = cam.get("id") or uuid.uuid4().hex[:12]
        cam["name"] = f"CAM {len(self._mine())+1:02d}"
        cam["source"] = "mine"
        if self._pinned_count() >= WALL_SLOTS:
            self._wall_full(cam["name"])  # declined -> still saved to My cameras, just off the wall
        cam["pinned"] = self._pinned_count() < WALL_SLOTS
        self.page.cameras.append(cam)
        self.page.save_and_refresh()
        self._refresh_keep_scroll()

    def _edit(self, cam_id: str = "") -> None:
        cam = self.page.camera_by_id(cam_id or self._selected_id())
        if not cam or cam.get("source") == "catalog":
            return
        dialog = CameraDialog(self, cam)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.camera()
        if "banner" not in updated:
            cam.pop("banner", None)
        cam.update(updated)
        self.page.save_and_refresh()
        self._refresh_keep_scroll()

    def _remove(self, cam_id: str = "") -> None:
        cam_id = cam_id or self._selected_id()
        if not cam_id:
            return
        cam = self.page.camera_by_id(cam_id)
        self.page.cameras = [row for row in self.page.cameras if row.get("id") != cam_id]
        self.page.save_and_refresh()
        if cam and cam.get("source") != "catalog":  # drop our private banner copy
            path = _camera_banner_path(cam)
            if path is not None and path.parent == _store_path().parent / "banners":
                try:
                    path.unlink()
                except Exception:
                    pass
        self._refresh_keep_scroll()

    def _pin(self, cam_id: str = "") -> None:
        cam = self.page.camera_by_id(cam_id or self._selected_id())
        if not cam:
            return
        if cam.get("pinned"):
            cam["pinned"] = False
        else:
            if self._pinned_count() >= WALL_SLOTS and not self._wall_full(cam.get("name") or ""):
                return
            cam["pinned"] = True
        self.page.save_and_refresh()
        self._refresh_keep_scroll()

    def _add_country(self, stream_id: str = "") -> None:
        stream_id = stream_id or self._selected_stream
        if not stream_id:
            return
        self._selected_stream = stream_id
        country = self._selected_country()
        source = self._source
        row = next((entry for entry in catalog_streams(country, source) if entry.get("id") == stream_id), None)
        if not row:
            return
        existing = self.page.camera_by_id(stream_id)
        if existing:
            if existing.get("pinned"):
                existing["pinned"] = False
                self.page.save_and_refresh()
                self._refresh_keep_scroll()
                return
            if self._pinned_count() >= WALL_SLOTS and not self._wall_full(row.get("name") or country):
                return
            existing["pinned"] = True
            existing["country"] = country
            existing["country_code"] = country_code(str(row.get("country") or "")) or country_code(country)
            existing["name"] = row.get("name") or existing.get("name") or country
            existing["rtsp"] = row.get("rtsp") or existing.get("rtsp")
            existing["catalog"] = source
            banner = _stream_banner_path(country, row, source)
            if banner is not None:
                # Copy into AppData so the main wall still finds it if streams/ moves
                existing["banner"] = _save_camera_banner(str(banner), stream_id)
            self.page.save_and_refresh()
            self._refresh_keep_scroll()
            return
        if self._pinned_count() >= WALL_SLOTS and not self._wall_full(row.get("name") or country):
            return
        cam = {
            "id": stream_id,
            "name": row.get("name") or country,
            "rtsp": row.get("rtsp"),
            "pinned": True,
            "source": "catalog",
            "category": row.get("category") or "residential",
            "country": country,
            "country_code": country_code(str(row.get("country") or "")) or country_code(country),
            "catalog": source,
        }
        banner = _stream_banner_path(country, row, source)
        if banner is not None:
            cam["banner"] = _save_camera_banner(str(banner), stream_id)
        self.page.cameras.append(cam)
        self.page.save_and_refresh()
        self._refresh_keep_scroll()


class CctvWallWindow(QWidget):
    """Fullscreen monitor. Feeds only. Esc closes it."""

    def __init__(self, cameras: list[dict], pool: VlcPool, on_closed=None, autoplay=None, parent=None):
        self.on_closed = on_closed
        self.autoplay = list(autoplay or [])
        self._alive = True
        super().__init__(parent)
        self.setWindowTitle("DedSec CCTV")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.cameras = [dict(cam) for cam in cameras[:WALL_SLOTS]]
        self.pool = pool
        self.setObjectName("CctvWallRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#CctvWallRoot { background: #050505; }
            QWidget { color: #e8f0ff; }
            QWidget#CctvWallGrid { background: transparent; }
            QLabel#CctvWallTitle { background: transparent; font-size: 13px; font-weight: 800; letter-spacing: 1px; }
            QLabel#CctvEsc { color: #9fb0c3; background: #0a1520; border: 1px solid #1d3a52; border-radius: 6px;
                             padding: 3px 10px; font-family: Consolas, monospace; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
        """ + _TILE_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)
        top = QHBoxLayout()
        top.setContentsMargins(2, 0, 0, 0)
        wall_title = QLabel('<span style="color:#ffffff">DedSec</span>&nbsp;<span style="color:#22d3ee">CCTV</span>'
                            '&nbsp;&nbsp;<span style="color:#4f667c; font-weight:600">FULL SCREEN</span>')
        wall_title.setObjectName("CctvWallTitle")
        wall_title.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(wall_title)
        top.addStretch()
        esc = QLabel("ESC  ·  CLOSE")
        esc.setObjectName("CctvEsc")
        top.addWidget(esc)
        root.addLayout(top)
        grid_host = QWidget()
        grid_host.setObjectName("CctvWallGrid")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.tiles = []
        for index in range(WALL_SLOTS):
            tile = CameraTile(index + 1)
            self.tiles.append(tile)
            grid.addWidget(tile, index // 2, index % 2)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addWidget(grid_host, 1)
        self._arm_tiles()

    def _arm_tiles(self) -> None:
        for index, tile in enumerate(self.tiles):
            cam = self.cameras[index] if index < len(self.cameras) else None
            if not cam:
                tile.show_empty()
                continue
            tile.set_camera(cam)
            if not self.pool.ready:
                tile.show_error(self.pool.detail)
                continue
            tile.show_ready()
            tile.play_btn.clicked.connect(lambda _=False, i=index: self.toggle_play(i))
            tile.mute_btn.clicked.connect(lambda _=False, i=index: self.toggle_mute(i, not getattr(self.tiles[i], '_muted', False)))
            tile.info_btn.clicked.connect(lambda _=False, i=index: self.show_info(i))

    def toggle_play(self, index: int) -> None:
        if not self._alive or not (0 <= index < len(self.cameras)):
            return
        cam = self.cameras[index]
        tile = self.tiles[index]
        key = f"fs-{index}"
        if key in self.pool._players:
            self.pool.stop(key)
            tile.show_ready()
            return
        tile.video.winId()
        ok = self.pool.play(key, cam.get("rtsp", ""), tile.video)
        tile.show_live() if ok else tile.show_error("STREAM FAILED")

    def start_selected(self) -> None:
        if not self._alive:
            return
        for offset, index in enumerate(self.autoplay):
            # Timers are children of this window: they die with it instead of firing into a deleted object.
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda i=index: self.toggle_play(i))
            timer.start(offset * 450)

    def toggle_mute(self, index: int, checked: bool) -> None:
        player = self.pool._players.get(f"fs-{index}")
        if not player:
            return
        try:
            player.audio_set_mute(checked)
        except Exception:
            pass
        self.tiles[index].set_muted(checked)  # also records _muted so the next click unmutes

    def show_info(self, index: int) -> None:
        cam = self.cameras[index] if index < len(self.cameras) else None
        if cam:
            show_stream_info(self, cam.get("rtsp", ""), str(cam.get("country") or ""), str(cam.get("country_code") or ""))

    def disconnect_streams(self) -> None:
        self.autoplay = []
        for timer in self.findChildren(QTimer):
            timer.stop()
        keys = [k for k in list(self.pool._players) if str(k).startswith("fs-")]
        if keys:  # wait (bounded) so VLC lets go of these windows before they are destroyed
            self.pool.stop_keys(keys, wait=2.0)

    def shutdown(self) -> None:
        self._alive = False
        self.disconnect_streams()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.disconnect_streams()
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._alive = False
        self.disconnect_streams()
        done = self.on_closed
        self.on_closed = None
        super().closeEvent(event)
        if done:
            QTimer.singleShot(0, done)


# ---------------------------------------------------------------------------
# Settings page (header gear button)
# ---------------------------------------------------------------------------
class ToggleSwitch(QCheckBox):
    """Animated pill switch. Colours are resolved through the theme at paint time."""

    def __init__(self, parent=None, checked: bool = False):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(46, 26)
        self.setChecked(checked)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, v: float) -> None:
        self._pos = float(v)
        self.update()

    knob = pyqtProperty(float, _get_knob, _set_knob)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self):
        return QSize(46, 26)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        t = self._pos
        off_bg, on_bg = QColor(_theme_hex("#1a2a3a")), QColor(_theme_hex("#22d3ee"))
        if current_theme() == "Light":
            off_bg = QColor("#c9d2dc")
        bg = QColor(int(off_bg.red() + (on_bg.red() - off_bg.red()) * t),
                    int(off_bg.green() + (on_bg.green() - off_bg.green()) * t),
                    int(off_bg.blue() + (on_bg.blue() - off_bg.blue()) * t))
        if not self.isEnabled():
            bg.setAlpha(110)
        p.setPen(QPen(QColor(_theme_hex("#2b4a63")) if t < 0.5 else bg, 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * t
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QRectF(x, r.top() + 4, d, d))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))
        p.end()


class _ThemeSwatch(QWidget):
    """Mini mock-up of a theme (window, panel, accent, text) painted in that theme's colours."""

    def __init__(self, theme: str, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setFixedSize(92, 58)

    def paintEvent(self, _event):
        th = self.theme
        c = lambda hx: QColor(_theme_hex(hx, th))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(c("#1d4e68"), 1))
        p.setBrush(c("#060606"))
        p.drawRoundedRect(r, 8, 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c("#0a1624"))
        p.drawRoundedRect(QRectF(7, 7, 78, 13), 3, 3)
        p.setBrush(c("#22d3ee"))
        p.drawRoundedRect(QRectF(64, 10, 17, 7), 2, 2)
        p.setBrush(c("#e8f0ff"))
        p.drawRoundedRect(QRectF(11, 11, 26, 5), 2, 2)
        for i in range(2):
            for j in range(2):
                p.setBrush(c("#0a1624"))
                p.drawRoundedRect(QRectF(7 + i * 40, 25 + j * 14, 38, 12), 3, 3)
        p.setBrush(c("#8ea0b5"))
        p.drawRoundedRect(QRectF(11, 29, 16, 4), 2, 2)
        p.end()


THEME_BLURBS = {
    "Default": "The DedSec look: deep navy with cyan accents.",
    "Dark": "Neutral charcoal with soft steel accents. Easy on the eyes at night.",
    "Light": "Bright panels and teal accents for well-lit rooms.",
}

_SETTINGS_STYLE = """
    QDialog#SettingsDialog { background: #060606; }
    QWidget#SettingsBody { background: #060606; }
    QFrame#SettingsSidebar { background: #07111c; border-right: 1px solid #16324a; }
    QLabel#SettingsSideTitle { color: #ffffff; font-size: 18px; font-weight: 800; background: transparent; padding: 4px 6px; }
    QLabel#SettingsSideSub { color: #5f7a92; font-size: 11px; background: transparent; padding: 0 6px 10px 6px; }
    QPushButton#SettingsNav { text-align: left; background: transparent; color: #8ea0b5; border: 1px solid transparent;
        border-radius: 8px; padding: 9px 12px; font-size: 13px; font-weight: 700; }
    QPushButton#SettingsNav:hover { background: #0f2132; color: #e8f0ff; }
    QPushButton#SettingsNav:checked { background: #123246; color: #22d3ee; border-color: #1d4e68; }
    QLabel#SettingsVersion { color: #5f7a92; font-family: Consolas, monospace; font-size: 11px; background: transparent; padding: 6px; }
    QScrollArea#SettingsScroll { background: transparent; border: none; }
    QWidget#SettingsContent { background: #060606; }
    QLabel#SettingsH1 { color: #ffffff; font-size: 22px; font-weight: 800; background: transparent; }
    QLabel#SettingsLead { color: #8ea0b5; font-size: 12px; background: transparent; }
    QLabel#SettingsGroup { color: #5f7a92; font-size: 11px; font-weight: 800; letter-spacing: 1px; background: transparent; padding-top: 6px; }
    QFrame#SettingsCard { background: #0a1624; border: 1px solid #16324a; border-radius: 12px; }
    QFrame#SettingsCard[current="true"] { border: 2px solid #22d3ee; background: #0c1d2e; }
    QLabel#CardTitle { color: #e8f0ff; font-size: 14px; font-weight: 800; background: transparent; }
    QLabel#CardText { color: #8ea0b5; font-size: 12px; background: transparent; }
    QLabel#CardValue { color: #e8f0ff; font-size: 13px; font-family: Consolas, monospace; background: transparent; }
    QLabel#CurrentBadge { background: #22d3ee; color: #06202a; border-radius: 9px; padding: 1px 9px;
        font-size: 10px; font-weight: 800; letter-spacing: 1px; }
    QLabel#OkBadge { background: #14361f; color: #7dffb0; border-radius: 9px; padding: 2px 10px; font-size: 11px; font-weight: 800; }
    QLabel#WarnBadge { background: #2a2210; color: #fcd34d; border-radius: 9px; padding: 2px 10px; font-size: 11px; font-weight: 800; }
    QFrame#SettingsDivider { background: #16324a; border: none; max-height: 1px; min-height: 1px; }
    QPushButton#SettingsPrimary { background: #22d3ee; color: #06202a; border: 1px solid #22d3ee; border-radius: 8px;
        padding: 0 16px; min-height: 34px; font-weight: 800; }
    QPushButton#SettingsPrimary:hover { background: #67e8f9; border-color: #67e8f9; }
    QPushButton#SettingsPrimary:disabled { background: #16324a; border-color: #16324a; color: #5f7a92; }
    QPushButton#SettingsSecondary { background: #0f1d2b; color: #e8f0ff; border: 1px solid #1d4e68; border-radius: 8px;
        padding: 0 16px; min-height: 34px; font-weight: 700; }
    QPushButton#SettingsSecondary:hover { border-color: #22d3ee; }
    QPushButton#SettingsDanger { background: transparent; color: #ff5c6c; border: 1px solid #ff5c6c; border-radius: 8px;
        padding: 0 16px; min-height: 34px; font-weight: 800; }
    QPushButton#SettingsDanger:hover { background: #3a1418; }
    QWidget#SettingsRow { background: transparent; }
    QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px; }
    QScrollBar::handle:vertical { background: #1d4e68; border-radius: 3px; min-height: 30px; }
    QScrollBar::handle:vertical:hover { background: #22d3ee; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
    QToolTip { background: #0a1624; color: #e8f0ff; border: 1px solid #1d4e68; padding: 4px 6px; }
"""


class SettingsDialog(QDialog):
    """Modal settings page: Appearance (themes), Account, About."""

    SECTIONS = (("appearance", "Appearance", "gear"), ("account", "Account", "user"), ("about", "About", "info"))

    def __init__(self, parent=None, account=None, section: str = "appearance"):
        super().__init__(parent)
        self.account = account
        self.pending_action = None   # account action to run once the dialog has closed
        self.theme_toggles: dict = {}
        self.theme_cards: dict = {}
        self.setObjectName("SettingsDialog")
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_SETTINGS_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QWidget()
        body.setObjectName("SettingsBody")
        row = QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        outer.addWidget(body, 1)
        self.title_bar = WindowTitleBar.install(self, "Settings")
        self.title_bar.min_btn.hide()

        side = QFrame()
        side.setObjectName("SettingsSidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(4)
        t = QLabel("Settings")
        t.setObjectName("SettingsSideTitle")
        sub = QLabel("DedSec CCTV preferences")
        sub.setObjectName("SettingsSideSub")
        sl.addWidget(t)
        sl.addWidget(sub)
        self.stack = QStackedWidget()
        self.nav: dict = {}
        builders = {"appearance": self._build_appearance, "account": self._build_account, "about": self._build_about}
        for key, label, icon in self.SECTIONS:
            btn = QPushButton("  " + label)
            btn.setObjectName("SettingsNav")
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setIcon(_glyph_icon(icon, "#8ea0b5", 15))
            btn.setIconSize(QSize(15, 15))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            idx = self.stack.addWidget(self._scroll(builders[key]()))
            btn.clicked.connect(lambda _=False, i=idx: self.stack.setCurrentIndex(i))
            sl.addWidget(btn)
            self.nav[key] = (btn, idx)
        sl.addStretch(1)
        ver = QLabel(f"v{APP_VERSION}")
        ver.setObjectName("SettingsVersion")
        sl.addWidget(ver)
        row.addWidget(side)
        row.addWidget(self.stack, 1)
        self.show_section(section)

        w, h = 980, 660
        host = parent.window() if parent is not None else None
        if host is not None and host.isVisible():
            g = host.geometry()
            w, h = min(w, max(720, g.width() - 80)), min(h, max(520, g.height() - 80))
            self.resize(w, h)
            self.move(g.x() + (g.width() - w) // 2, g.y() + (g.height() - h) // 2)
        else:
            self.resize(w, h)

    # -- helpers ---------------------------------------------------------------
    def show_section(self, key: str) -> None:
        btn, idx = self.nav.get(key, self.nav["appearance"])
        btn.setChecked(True)
        self.stack.setCurrentIndex(idx)

    def _scroll(self, content: QWidget) -> QScrollArea:
        sc = QScrollArea()
        sc.setObjectName("SettingsScroll")
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setWidget(content)
        return sc

    def _page(self, title: str, lead: str):
        w = QWidget()
        w.setObjectName("SettingsContent")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(36, 30, 36, 30)
        lay.setSpacing(12)
        h = QLabel(title)
        h.setObjectName("SettingsH1")
        ld = QLabel(lead)
        ld.setObjectName("SettingsLead")
        ld.setWordWrap(True)
        lay.addWidget(h)
        lay.addWidget(ld)
        lay.addSpacing(8)
        return w, lay

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(name)
        lbl.setWordWrap(name == "CardText")
        return lbl

    def _card(self):
        card = QFrame()
        card.setObjectName("SettingsCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        return card, lay

    def _row(self, title: str, text: str, right: QWidget | None) -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsRow")
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)
        col = QVBoxLayout()
        col.setSpacing(3)
        col.addWidget(self._label(title, "CardTitle"))
        if text:
            col.addWidget(self._label(text, "CardText"))
        h.addLayout(col, 1)
        if right is not None:
            h.addWidget(right, 0, Qt.AlignmentFlag.AlignVCenter)
        return w

    @staticmethod
    def _divider() -> QFrame:
        d = QFrame()
        d.setObjectName("SettingsDivider")
        return d

    def _button(self, text: str, kind: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName(kind)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False: slot())
        return b

    # -- Appearance ------------------------------------------------------------
    def _build_appearance(self) -> QWidget:
        w, lay = self._page("Appearance", "Pick a theme. It applies instantly to every window and is remembered next time.")
        lay.addWidget(self._label("THEME", "SettingsGroup"))
        for name in THEMES:
            card, cl = self._card()
            h = QHBoxLayout()
            h.setSpacing(18)
            h.addWidget(_ThemeSwatch(name))
            col = QVBoxLayout()
            col.setSpacing(4)
            top = QHBoxLayout()
            top.setSpacing(8)
            top.addWidget(self._label(name, "CardTitle"))
            badge = QLabel("CURRENT")
            badge.setObjectName("CurrentBadge")
            top.addWidget(badge)
            top.addStretch(1)
            col.addStretch(1)
            col.addLayout(top)
            col.addWidget(self._label(THEME_BLURBS.get(name, ""), "CardText"))
            col.addStretch(1)
            h.addLayout(col, 1)
            sw = ToggleSwitch(checked=(name == current_theme()))
            sw.setToolTip(f"Use the {name} theme")
            sw.toggled.connect(lambda on, n=name: self._on_theme_toggle(n, on))
            h.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
            cl.addLayout(h)
            card.mousePressEvent = lambda _e, s=sw: s.setChecked(True)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            self.theme_toggles[name] = sw
            self.theme_cards[name] = (card, badge)
            lay.addWidget(card)
        lay.addStretch(1)
        self._sync_theme_ui()
        return w

    def _on_theme_toggle(self, name: str, on: bool) -> None:
        if not on:
            if name == current_theme():   # exclusive: the active theme can't be switched off directly
                QTimer.singleShot(0, self._sync_theme_ui)
            return
        if name != current_theme():
            apply_theme(name)
        self._sync_theme_ui()

    def _sync_theme_ui(self) -> None:
        cur = current_theme()
        for name, sw in self.theme_toggles.items():
            want = name == cur
            if sw.isChecked() != want:
                sw.blockSignals(True)
                sw.setChecked(want)
                sw.blockSignals(False)
                sw._animate(want)
            card, badge = self.theme_cards[name]
            card.setProperty("current", "true" if want else "false")
            card.style().unpolish(card)
            card.style().polish(card)
            badge.setVisible(want)

    def on_theme_changed(self) -> None:
        self._sync_theme_ui()

    # -- Account ---------------------------------------------------------------
    def _build_account(self) -> QWidget:
        w, lay = self._page("Account", "Your DedSec account on this device.")
        user = getattr(self.account, "user", None) if self.account is not None else None
        lay.addWidget(self._label("PROFILE", "SettingsGroup"))
        card, cl = self._card()
        email = self._label(user.email if user is not None else "Not signed in", "CardValue")
        email.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self._row("Signed in as", "The email address of this account.", email))
        lay.addWidget(card)

        lay.addWidget(self._label("SECURITY", "SettingsGroup"))
        card, cl = self._card()
        if user is not None and getattr(user, "totp_enabled", False):
            badge = QLabel("● On")
            badge.setObjectName("OkBadge")
            cl.addWidget(self._row("Two-factor authentication (2FA)",
                                   "Sign-ins need a code from your authenticator app.", badge))
        else:
            self.tfa_btn = self._button("Set up 2FA…", "SettingsPrimary", lambda: self._defer("setup_2fa"))
            self.tfa_btn.setEnabled(self.account is not None and user is not None)
            cl.addWidget(self._row("Two-factor authentication (2FA)",
                                   "Off. Add a code from an authenticator app to every sign-in.", self.tfa_btn))
        lay.addWidget(card)

        lay.addWidget(self._label("SESSION", "SettingsGroup"))
        card, cl = self._card()
        self.signout_btn = self._button("Sign out", "SettingsDanger", lambda: self._defer("sign_out"))
        self.signout_btn.setEnabled(self.account is not None)
        cl.addWidget(self._row("Sign out", "Stops all feeds and returns to the sign-in screen.", self.signout_btn))
        lay.addWidget(card)
        lay.addStretch(1)
        return w

    def _defer(self, method: str) -> None:
        """Account handlers tear down the CCTV page, so close Settings first and run them after."""
        fn = getattr(self.account, method, None)
        if callable(fn):
            self.pending_action = fn
            self.accept()

    # -- About -----------------------------------------------------------------
    def _build_about(self) -> QWidget:
        w, lay = self._page("About", "DedSec CCTV: a live camera wall.")
        lay.addWidget(self._label("APPLICATION", "SettingsGroup"))
        card, cl = self._card()
        cl.addWidget(self._row("Version", "", self._label(APP_VERSION, "CardValue")))
        cl.addWidget(self._divider())
        host = urlparse(API_BASE_URL).netloc or API_BASE_URL
        cl.addWidget(self._row("Server", "Account and update server.", self._label(host, "CardValue")))
        lay.addWidget(card)

        lay.addWidget(self._label("UPDATES", "SettingsGroup"))
        card, cl = self._card()
        self.update_status = self._label("Updates are checked automatically when the app starts.", "CardText")
        self.update_btn = self._button("Check for updates", "SettingsSecondary", self._check_updates)
        col = QWidget()
        col.setObjectName("SettingsRow")
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        v.addWidget(self._label("Software update", "CardTitle"))
        v.addWidget(self.update_status)
        h = QHBoxLayout()
        h.setSpacing(16)
        h.addWidget(col, 1)
        h.addWidget(self.update_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        cl.addLayout(h)
        lay.addWidget(card)
        lay.addStretch(1)
        return w

    def _check_updates(self) -> None:
        self.update_btn.setEnabled(False)
        self.update_status.setText("Checking for updates…")

        def done(info) -> None:
            global _UPDATE_DIALOG
            self.update_btn.setEnabled(True)
            if info is None:
                self.update_status.setText(f"You're up to date (v{APP_VERSION}).")
                return
            self.update_status.setText(f"Version {info.version} is available.")
            if _UPDATE_DIALOG is None:
                _UPDATE_DIALOG = UpdateDialog(info, self)
                _UPDATE_DIALOG.finished.connect(lambda _r: globals().__setitem__("_UPDATE_DIALOG", None))
                _UPDATE_DIALOG.show()
                _UPDATE_DIALOG.raise_()

        def fail(exc) -> None:
            self.update_btn.setEnabled(True)
            reason = str(exc) if isinstance(exc, UpdateError) else _friendly_update_error(exc)
            self.update_status.setText(f"Couldn't check: {reason}")

        run_bg(check_for_update, done, fail)


class CctvPage(QWidget):
    def __init__(self, app_window=None, account=None):
        super().__init__()
        self.app_window = app_window
        self.account = account
        self.cameras: list[dict] = []
        self.pool = VlcPool()
        self._wall = None
        self._closing = False
        self._build()
        self._apply_style()
        self.load()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        accent = QFrame()
        accent.setObjectName("CctvAccent")
        accent.setFixedHeight(2)
        outer.addWidget(accent)
        body = QWidget()
        body.setObjectName("CctvBody")
        outer.addWidget(body, 1)
        root = QVBoxLayout(body)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(12)

        banner = QFrame()
        banner.setObjectName("CctvBanner")
        banner.setFixedHeight(54)
        header = QHBoxLayout(banner)
        header.setContentsMargins(12, 8, 10, 8)
        header.setSpacing(8)
        logo = QLabel()
        logo.setObjectName("BannerLogo")
        logo_pix = _logo_pixmap(28)
        if not logo_pix.isNull():
            logo.setPixmap(logo_pix)
        else:
            logo.setPixmap(_glyph_icon("camera", "#22d3ee", 26).pixmap(26, 26))
        logo.setFixedSize(30, 30)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("DedSec CCTV")
        title.setObjectName("BannerTitle")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setText('<span style="color:#ffffff">DedSec</span>&nbsp;<span style="color:#22d3ee">CCTV</span>')
        header.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addSpacing(6)
        header.addWidget(self._vsep(), 0, Qt.AlignmentFlag.AlignVCenter)
        header.addSpacing(6)

        def status_chip(name: str, tip: str) -> QLabel:
            chip = QLabel("")
            chip.setObjectName(name)
            chip.setTextFormat(Qt.TextFormat.RichText)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setFixedHeight(26)
            chip.setToolTip(tip)
            header.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
            return chip

        # Names kept from the old header: count_badge / available_badge.
        self.count_badge = status_chip("ActionsLiveBadge", "Feeds playing right now")
        self.wall_chip = status_chip("WallChip", "Wall slots in use")
        self.available_badge = status_chip("AvailableBadge", "Streams available across all countries")
        header.addStretch(1)

        self.clock_lbl = QLabel("")
        self.clock_lbl.setObjectName("BannerClock")
        self.clock_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.clock_lbl.setToolTip("Local time")
        header.addWidget(self.clock_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self.clock_sep = self._vsep()
        header.addSpacing(4)
        header.addWidget(self.clock_sep, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addSpacing(4)

        self.library_btn = QPushButton("Add CCTV")
        self.library_btn.setObjectName("ActionButton_cyan")
        self.library_btn.setIcon(_glyph_icon("plus", "#06202a", 14))
        self.library_btn.setIconSize(QSize(14, 14))
        self.library_btn.setFixedHeight(34)
        self.library_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.library_btn.setToolTip("Add your own RTSP cameras or pick streams from the catalog")
        self.library_btn.clicked.connect(self.open_library)
        self.full_btn = QPushButton("Full screen")
        self.full_btn.setObjectName("ActionButton_green")
        self.full_btn.setIcon(_glyph_icon("expand", "#cfe3f2", 14))
        self.full_btn.setIconSize(QSize(14, 14))
        self.full_btn.setFixedHeight(34)
        self.full_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.full_btn.setToolTip("Show the playing feeds on a full-screen wall (Esc to return)")
        self.full_btn.clicked.connect(self.open_fullscreen)
        header.addWidget(self.library_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.full_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setObjectName("ActionButton_settings")
        self.settings_btn.setIcon(_glyph_icon("gear", "#c5d2e0", 14))
        self.settings_btn.setIconSize(QSize(14, 14))
        self.settings_btn.setFixedHeight(34)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("Appearance, account and app settings")
        self.settings_btn.clicked.connect(self.open_settings)
        header.addWidget(self.settings_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(banner)

        wall = QFrame()
        wall.setObjectName("CctvWall")
        wall.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        grid = QGridLayout(wall)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        self.tiles = []
        for index in range(WALL_SLOTS):
            tile = CameraTile(index + 1, removable=True)
            self.tiles.append(tile)
            grid.addWidget(tile, index // 2, index % 2)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addWidget(wall, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(4, 0, 4, 0)
        foot.setSpacing(8)
        self.hint_lbl = QLabel("Press Play on a feed to go live  ·  Full screen moves live feeds to a dedicated wall  ·  Esc returns")
        self.hint_lbl.setObjectName("StatusLine")
        self.hint_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        engine = QLabel(("VLC ready" if self.pool.ready else "VLC unavailable") + f"  ·  v{APP_VERSION}")
        engine.setObjectName("StatusVersion")
        foot.addWidget(self.hint_lbl, 1)
        foot.addWidget(engine)
        root.addLayout(foot)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._tick()

    @staticmethod
    def _vsep() -> QFrame:
        sep = QFrame()
        sep.setObjectName("BannerSep")
        sep.setFixedSize(1, 24)
        return sep

    def _tick(self) -> None:
        now = time.localtime()
        self.clock_lbl.setText(
            f'<span style="color:#e8f0ff">{time.strftime("%H:%M:%S", now)}</span>'
            f'&nbsp;&nbsp;<span style="color:#a3b6c9">{time.strftime("%a %d %b", now)}</span>')
        wide = self.width() >= 1180 or not self.isVisible()
        self.clock_lbl.setVisible(wide)
        self.clock_sep.setVisible(wide)
        self._update_status()

    def _update_status(self) -> None:
        """Header chips: LIVE n/4 (feeds playing), WALL n/4 (slots used), catalog size."""
        keys = [str(k) for k in self.pool._players]
        prefix = "fs-" if self._wall is not None else "wall-"
        live = sum(1 for k in keys if k.startswith(prefix))
        # Numbers go green (theme success colour; _tc/themed_css maps #7dffb0 per theme) when > 0.
        ok, muted, label = "#7dffb0", "#8ea0b5", "#a3b6c9"

        def num(n, text):
            return f'<span style="color:{ok if n else muted}">{text}</span>'
        dot = "#2fbf6a" if live else "#3c4a5c"
        pinned = len(getattr(self, "_pinned", []) or [])
        total = int(getattr(self, "_catalog_total", 0) or 0)
        html = (f'<span style="color:{dot}">●</span>&nbsp;<span style="color:{label}">LIVE</span>&nbsp;'
                + num(live, f"{live}/{WALL_SLOTS}"))
        wall = f'<span style="color:{label}">WALL</span>&nbsp;' + num(pinned, f"{pinned}/{WALL_SLOTS}")
        cat = f'<span style="color:{label}">CATALOG</span>&nbsp;' + num(total, str(total))
        for chip, text in ((self.count_badge, html), (self.wall_chip, wall), (self.available_badge, cat)):
            if chip.property("_dsBaseText") != text:  # the clock calls this every second
                chip.setText(text)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QWidget { background: transparent; color: #e8f0ff; }
            QFrame#CctvAccent { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(34, 211, 238, 0), stop:0.5 rgba(34, 211, 238, 0.75), stop:1 rgba(34, 211, 238, 0)); border: none; }
            QFrame#CctvBanner { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0c1a2a, stop:1 #08121d);
                                border: 1px solid #15324a; border-radius: 10px; }
            QLabel#BannerTitle { font-size: 17px; font-weight: 800; letter-spacing: 0.5px; background: transparent; }
            QFrame#BannerSep { background: #1d3a52; border: none; }
            QLabel#BannerClock { font-family: Consolas, monospace; font-size: 12px; background: transparent; padding: 0 4px; }
            QLabel#ActionsLiveBadge, QLabel#WallChip, QLabel#AvailableBadge {
                background: #0a1520; border: 1px solid #1d3a52; border-radius: 6px; padding: 0 10px;
                font-family: Consolas, monospace; font-size: 11px; font-weight: 800; letter-spacing: 1px; }
            QLabel#StatusLine { color: #93a7bb; background: transparent; font-size: 11px; font-weight: 600; }
            QLabel#StatusVersion { color: #93a7bb; background: transparent; font-family: Consolas, monospace; font-size: 11px; font-weight: 600; }
            QPushButton#ActionButton_cyan { background: #22d3ee; color: #06202a; border: 1px solid #22d3ee; border-radius: 8px;
                padding: 0 14px 0 11px; min-height: 34px; max-height: 34px; font-weight: 800; }
            QPushButton#ActionButton_cyan:hover { background: #67e8f9; border-color: #67e8f9; }
            QPushButton#ActionButton_cyan:pressed { background: #0fb5cf; }
            QPushButton#ActionButton_green { background: #0f1d2b; color: #e8f0ff; border: 1px solid #1d4e68; border-radius: 8px;
                padding: 0 14px 0 11px; min-height: 34px; max-height: 34px; font-weight: 700; }
            QPushButton#ActionButton_green:hover { background: #142a3d; border-color: #22d3ee; }
            QPushButton#ActionButton_settings { background: transparent; color: #c5d2e0; border: 1px solid #253546; border-radius: 8px;
                padding: 0 12px 0 10px; min-height: 34px; max-height: 34px; font-weight: 700; }
            QPushButton#ActionButton_settings:hover { background: #121c27; border-color: #3c5a74; }
            QToolTip { background: #0a1624; color: #e8f0ff; border: 1px solid #1d4e68; padding: 4px 6px; }
        """ + _TILE_STYLE)

    def open_settings(self, section: str = "appearance") -> "SettingsDialog":
        dlg = SettingsDialog(self.window(), account=self.account, section=section)
        self._settings_dlg = dlg
        dlg.exec()
        self._settings_dlg = None
        action = dlg.pending_action
        if action is not None and self.account is not None:
            QTimer.singleShot(0, action)  # after the modal has closed (these tear the page down)
        return dlg

    def load(self) -> None:
        path = _store_path()
        self.cameras = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = data.get("cameras", []) if isinstance(data, dict) else []
                if not isinstance(rows, list):
                    rows = []
                # Keep every saved camera (catalog + mine). Only "mine" is capped at MAX_CAMERAS.
                # Older builds truncated to 4 total, which dropped wall streams after restart.
                mine_kept = 0
                for row in rows:
                    if not isinstance(row, dict) or not _valid_rtsp(row.get("rtsp", "")):
                        continue
                    source = str(row.get("source") or "mine")
                    if source != "catalog":
                        if mine_kept >= MAX_CAMERAS:
                            continue
                        mine_kept += 1
                        source = "mine"
                    cam = {
                        "id": str(row.get("id") or uuid.uuid4().hex[:12]),
                        "name": str(row.get("name") or "Camera")[:40],
                        "rtsp": str(row.get("rtsp")).strip(),
                        "pinned": bool(row.get("pinned")),
                        "source": source,
                    }
                    for extra in ("country", "country_code", "banner"):
                        value = row.get(extra)
                        if isinstance(value, str) and value.strip():
                            cam[extra] = value.strip()[:400]
                    self.cameras.append(cam)
            except Exception:
                self.cameras = []
        self._cap_pins()
        QTimer.singleShot(250, self.refresh)

    def _cap_pins(self) -> None:
        kept = 0
        for cam in self.cameras:
            if cam.get("pinned") and kept < WALL_SLOTS:
                kept += 1
            else:
                cam["pinned"] = False

    def save_and_refresh(self) -> None:
        self._cap_pins()
        try:
            # Save the full list. Truncating to MAX_CAMERAS used to drop wall streams
            # that sat past the first 4 entries (e.g. 1 mine + 4 catalog → only 3 stayed).
            _store_path().write_text(json.dumps({"cameras": self.cameras, "settings": load_settings()}, indent=2),
                                     encoding="utf-8")
        except Exception:
            pass
        self.refresh()

    def camera_by_id(self, cam_id: str) -> dict | None:
        for cam in self.cameras:
            if cam.get("id") == cam_id:
                return cam
        return None

    def refresh(self) -> None:
        if self._wall is not None or self._closing:
            return
        self.pool.stop_all()
        if self._closing:  # torn down while waiting for the players to stop
            return
        pinned = [cam for cam in self.cameras if cam.get("pinned")][:WALL_SLOTS]
        self._pinned = pinned
        for index, tile in enumerate(self.tiles):
            cam = pinned[index] if index < len(pinned) else None
            for btn in (tile.play_btn, tile.mute_btn, tile.info_btn, tile.remove_btn):
                if btn is None:
                    continue
                try:  # drop old slots so repeated refreshes don't stack (mute fired twice = no-op)
                    btn.clicked.disconnect()
                except Exception:
                    pass
            if not cam:
                tile.show_empty()
                continue
            tile.set_camera(cam)
            if tile.remove_btn is not None:
                tile.remove_btn.clicked.connect(lambda _=False, i=index: self.remove_from_wall(i))
            if not self.pool.ready:
                tile.show_error(self.pool.detail)
                continue
            tile.show_ready()
            tile.play_btn.clicked.connect(lambda _=False, i=index: self.toggle_play(i))
            tile.mute_btn.clicked.connect(lambda _=False, i=index: self.toggle_mute(i, not getattr(self.tiles[i], '_muted', False)))
            tile.info_btn.clicked.connect(lambda _=False, i=index: self.show_info(i))
        _counts, total = stream_counts()
        self._catalog_total = total
        self._update_status()

    def toggle_mute(self, index: int, checked: bool) -> None:
        player = self.pool._players.get(f"wall-{index}")
        if player:
            try:
                player.audio_set_mute(checked)
            except Exception:
                pass
        self.tiles[index].set_muted(checked)

    def show_info(self, index: int) -> None:
        cam = self._pinned[index] if index < len(getattr(self, "_pinned", [])) else None
        if not cam:
            return
        show_stream_info(self, cam.get("rtsp", ""), str(cam.get("country") or ""), str(cam.get("country_code") or ""))

    def remove_from_wall(self, index: int) -> None:
        """Tile 'Remove': stop that feed and take it off the wall. The camera stays saved in the
        list (same as WallFullDialog). Feeds still playing in other slots are resumed."""
        if self._wall is not None or self._closing:
            return
        cam = self._pinned[index] if index < len(getattr(self, "_pinned", [])) else None
        if not cam:
            return
        self.pool.stop_keys([f"wall-{index}"], wait=1.5)
        playing = {self._pinned[i].get("id") for i in range(len(self._pinned))
                   if i != index and f"wall-{i}" in self.pool._players}
        cam["pinned"] = False
        self.save_and_refresh()  # persist + re-lay the wall + counts
        if self._closing:
            return
        for i, row in enumerate(self._pinned):
            if row.get("id") in playing and f"wall-{i}" not in self.pool._players:
                self.toggle_play(i)
        self._update_status()

    def toggle_play(self, index: int) -> None:
        if self._wall is not None or self._closing:
            return
        cam = self._pinned[index] if index < len(self._pinned) else None
        if not cam:
            return
        tile = self.tiles[index]
        key = f"wall-{index}"
        if key in self.pool._players:
            self.pool.stop(key)
            tile.show_ready()
            self._update_status()
            return
        tile.video.winId()
        ok = self.pool.play(key, cam.get("rtsp", ""), tile.video)
        if ok:
            tile.show_live()
        else:
            tile.show_error("STREAM FAILED")
        self._update_status()

    def open_library(self) -> None:
        dlg = AddCctvDialog(self)
        # Open maximized, the same way main() shows the main window.
        dlg.setWindowState(dlg.windowState() | Qt.WindowState.WindowMaximized)
        dlg.exec()

    def open_fullscreen(self) -> None:
        pinned = [cam for cam in self.cameras if cam.get("pinned")][:WALL_SLOTS]
        if not pinned:
            QMessageBox.information(self, "CCTV", "Add a camera and put it on the wall first.")
            return
        playing = [index for index in range(len(pinned)) if f"wall-{index}" in self.pool._players]
        if not playing:
            QMessageBox.information(self, "CCTV", "Play a stream on the wall first.")
            return
        if self._wall is not None:
            try:
                self._wall.close()
            except Exception:
                pass
            self._wall = None
        # Stop every main-screen stream and give VLC time to let go of the
        # video windows before the fullscreen wall reconnects them.
        self.pool.stop_all()
        for index in playing:
            if index < len(self.tiles):
                self.tiles[index].show_held()
        self._wall = CctvWallWindow(pinned, self.pool, on_closed=self._wall_closed, autoplay=playing)
        self._wall.show()
        self._wall.showFullScreen()
        QTimer.singleShot(700, self._wall.start_selected)
        self._update_status()

    def _wall_closed(self) -> None:
        self._wall = None
        for tile in self.tiles:
            if tile.play_btn is None:
                continue
            if tile.state == "empty":
                continue
            tile.show_ready()
        self._update_status()

    def shutdown(self, wait: float = 3.0) -> bool:
        """Tear down every feed before this page (and its video windows) is destroyed.
        Used by app close / aboutToQuit, sign-out and the Settings account actions. Idempotent."""
        if self._closing:
            return True
        self._closing = True
        try:
            self._clock.stop()
        except Exception:
            pass
        for tile in self.tiles:
            try:
                tile._conn_timer.stop()
            except Exception:
                pass
        wall = self._wall
        self._wall = None
        if wall is not None and not sip.isdeleted(wall):
            wall.on_closed = None
            wall.shutdown()
            wall.close()
        return self.pool.shutdown(wait)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _asset_roots() -> list:
    """Every folder an ``assets`` dir may sit in: next to app.py, the exe (PyInstaller sets
    sys.frozen, Nuitka sets __compiled__ instead), the launched script, or the working dir."""
    roots = [_app_root()]
    try:  # running as a script: the folder app.py lives in, even if _app_root() points elsewhere
        roots.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    compiled = globals().get("__compiled__")
    if compiled is not None and getattr(compiled, "containing_dir", None):
        roots.append(Path(compiled.containing_dir))
    for raw in (sys.executable, sys.argv[0] if sys.argv else ""):
        if raw:
            try:
                roots.append(Path(raw).resolve().parent)
            except Exception:
                pass
    roots.append(Path.cwd())
    out = []
    for root in roots:
        if root not in out:
            out.append(root)
    return out


_ASSET_CACHE: dict = {}


def _asset(*names: str) -> Path | None:
    if names in _ASSET_CACHE:
        return _ASSET_CACHE[names]
    found = None
    for root in _asset_roots():
        for name in names:
            path = root / "assets" / name
            if path.is_file():
                found = path
                break
        if found:
            break
    _ASSET_CACHE[names] = found
    return found


LOGIN_LOGO_SIZE = 150  # px, right panel of the sign-in screen


def _logo_pixmap(size: int) -> QPixmap:
    """App logo for the sign-in screen: dedsec_logo.png, applogo.png, else the largest .ico frame."""
    icon = _asset("dedsec_logo.png", "applogo.png")
    if icon:
        pix = QPixmap(str(icon))
        if not pix.isNull():
            return pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
    ico = _asset("dedsec_logo.ico")
    if ico:
        pix = QIcon(str(ico)).pixmap(256, 256)
        if not pix.isNull():
            return pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
    return QPixmap()


def _lock_path() -> Path:
    """Legacy local lock-screen secret (pre-account versions). Only read for 2FA migration."""
    return Path.home() / "AppData" / "Roaming" / "DedSecCctv" / "lock.json"


def _load_secret() -> str:
    path = _lock_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("secret") or "")
    except Exception:
        return ""


def _qr_pixmap(payload: str, size: int = 180) -> QPixmap:
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=1)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    pix = QPixmap(size, size)
    pix.fill(QColor("#ffffff"))
    painter = QPainter(pix)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0b1220"))
    cell = size / n
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                painter.drawRect(int(c * cell), int(r * cell), int(cell) + 1, int(cell) + 1)
    painter.end()
    return pix


# ---------------------------------------------------------------------------
# account (server) authentication
# ---------------------------------------------------------------------------

_AUTH_CLIENT = None
_AUTH_INIT_ERROR = ""


def auth_client():
    """Shared AuthClient, created on first use. Returns None if API_BASE_URL is unusable."""
    global _AUTH_CLIENT, _AUTH_INIT_ERROR
    if _AUTH_CLIENT is None and not _AUTH_INIT_ERROR:
        try:
            _AUTH_CLIENT = AuthClient(API_BASE_URL, allow_insecure_localhost=DEDSEC_DEV,
                                      timeout=AUTH_TIMEOUT_SECONDS)
        except Exception as exc:
            _AUTH_INIT_ERROR = f"Server address is not valid ({exc})."
    return _AUTH_CLIENT


class _Task(QObject):
    """Runs a blocking callable on a daemon thread and delivers the result on the UI thread."""
    done = pyqtSignal(object)
    failed = pyqtSignal(object)
    _live: set = set()

    def __init__(self, fn, on_ok=None, on_err=None):
        super().__init__()
        self._fn = fn
        self.done.connect(lambda value: self._deliver(on_ok, value))
        self.failed.connect(lambda exc: self._deliver(on_err, exc))
        _Task._live.add(self)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            value = self._fn()
        except BaseException as exc:  # delivered to the UI thread, never raised here
            self.failed.emit(exc)
        else:
            self.done.emit(value)

    def _deliver(self, callback, value) -> None:
        _Task._live.discard(self)
        if callback is None:
            return
        try:
            callback(value)
        except RuntimeError as exc:
            if "deleted" not in str(exc):  # the widget went away while we were waiting
                raise


_SAVED_LOGIN = None


def saved_login():
    """Shared "Remember me" store (password only ever in the OS keyring). None if unusable."""
    global _SAVED_LOGIN
    if _SAVED_LOGIN is None:
        try:
            _SAVED_LOGIN = SavedLogin()
        except Exception:
            _SAVED_LOGIN = None
    return _SAVED_LOGIN


_NO_KEYRING_HINT = ("Your password can't be remembered on this PC (no secure credential store). "
                    "Only your email is saved.")


def run_bg(fn, on_ok=None, on_err=None) -> "_Task":
    return _Task(fn, on_ok, on_err)


def _friendly_error(exc, action: str = "") -> str:
    """Turn client/network exceptions into short messages for people, not developers."""
    if isinstance(exc, DeviceLocked):
        return "This account is locked to another PC. Request a device reset."
    if isinstance(exc, AuthError):
        status = exc.status
        detail = str(exc)
        if status == 0:
            return ("Can't reach the DedSec server. Check your internet connection "
                    "(or VPN) and try again.")
        if status == 429:
            return "Too many attempts. Please wait a minute, then try again."
        if status == 401 and action == "login":
            return ("Wrong email or password. After 5 failed attempts the account is "
                    "locked for 15 minutes.")
        if status == 401 and action == "devconfirm":
            return "That reset code is wrong or has expired. Check the email, or send a new code."
        if status == 401 and action in ("totp", "enable2fa"):
            return "That code is wrong or has expired. Enter the current 6-digit code."
        if status == 401:
            return "Your session has expired. Please sign in again."
        if status == 403 and "another pc" in detail.lower():
            return "This account is locked to another PC. Request a device reset."
        if status in (426, 428):  # server needs the device id this version sends
            return "This version of DedSec CCTV is out of date. Please install the latest version."
        if status == 403 and "verif" in detail.lower():
            return "Please verify your email address first. Check your inbox for the code."
        if status == 400 and action == "verify":
            return "That verification code is invalid or expired. Use Resend code to get a new one."
        if status == 423:
            return "This account is temporarily locked. Try again in 15 minutes."
        if status >= 500:
            return "The DedSec server had a problem. Please try again shortly."
        if status == 422 and action == "verify":
            return "Paste the full code from the email."
        return detail or "Something went wrong. Please try again."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error: {exc.__class__.__name__}"


_AUTH_STYLE = """
    QFrame#LockCard { background: transparent; border: 1px solid rgba(80, 140, 180, 0.28); border-radius: 22px; }
    QFrame#LockLeft { background: rgba(10, 22, 36, 228); border-top-left-radius: 22px; border-bottom-left-radius: 22px; }
    QFrame#LockRight { background: rgba(8, 28, 44, 236); border-top-right-radius: 22px; border-bottom-right-radius: 22px; border-left: 1px solid rgba(80, 140, 180, 0.18); }
    QLabel#LockWelcome { color: #ffffff; font-size: 34px; font-weight: 800; background: transparent; }
    QLabel#LockSub { color: #9aa8bc; font-size: 14px; background: transparent; }
    QLabel#LockLabel { color: #d5dde8; font-size: 14px; background: transparent; }
    QLabel#LockKey { color: #7ee0ff; font-family: Consolas, monospace; font-size: 13px; background: transparent; }
    QLabel#LockBrand { color: #ffffff; font-size: 32px; font-weight: 800; letter-spacing: 1px; background: transparent; }
    QLabel#LockPortal { color: #8ea0b5; font-size: 15px; background: transparent; }
    QLabel#LockVpn { color: #22d3ee; font-size: 13px; font-weight: 700; background: transparent; }
    QLabel#AuthErr { color: #ff5c6c; font-size: 13px; background: transparent; }
    QLabel#AuthInfo { color: #7dffb0; font-size: 13px; background: transparent; }
    QWidget#AuthPage { background: transparent; }
    QLineEdit#LockCode { background: rgba(8, 20, 34, 0.9); color: #e8f0ff; border: 1px solid #1d4e68; border-radius: 12px; font-size: 18px; letter-spacing: 4px; padding: 12px; }
    QLineEdit#AuthField { background: rgba(8, 20, 34, 0.9); color: #e8f0ff; border: 1px solid #1d4e68; border-radius: 12px; font-size: 15px; padding: 10px 12px; }
    QLineEdit#AuthField:focus, QLineEdit#LockCode:focus { border: 1px solid #22d3ee; }
    QPushButton#LockButton { background: #22d3ee; color: #06202a; border: none; border-radius: 22px; font-size: 16px; font-weight: 800; }
    QPushButton#LockButton:hover { background: #67e8f9; }
    QPushButton#LockButton:disabled { background: #1d4e68; color: #8ea0b5; }
    QPushButton#AuthTab { background: transparent; color: #8ea0b5; border: none; border-bottom: 2px solid transparent; font-size: 14px; font-weight: 700; padding: 6px 2px; }
    QPushButton#AuthTab:checked { color: #22d3ee; border-bottom: 2px solid #22d3ee; }
    QPushButton#AuthTab:hover { color: #e8f0ff; }
    QPushButton#AuthLink { background: transparent; color: #55c7ff; border: none; font-size: 13px; text-align: left; padding: 2px 0; }
    QPushButton#AuthLink:hover { color: #a5e9ff; text-decoration: underline; }
    QPushButton#AuthLink:disabled { color: #4b5b6c; }
    QCheckBox#AuthRemember { color: #d5dde8; font-size: 13px; spacing: 8px; background: transparent; }
    QCheckBox#AuthRemember:disabled { color: #4b5b6c; }
    QCheckBox#AuthRemember::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #1d4e68; background: rgba(8, 20, 34, 0.9); }
    QCheckBox#AuthRemember::indicator:hover { border: 1px solid #22d3ee; }
    QCheckBox#AuthRemember::indicator:checked { background: #22d3ee; border: 1px solid #22d3ee; }
    QLabel#AuthHint { color: #8ea0b5; font-size: 12px; background: transparent; }
"""


class AuthScreen(QWidget):
    """Account gate that replaces the old local LockScreen.

    Modes: restore (silent session resume), sign-in, create account, verify email,
    two-factor code, and an optional "secure your account" offer after sign-in.
    ``on_authenticated(user)`` is called after a successful sign-in *and* VPN check.
    """

    MODE_SIGNIN, MODE_SIGNUP, MODE_VERIFY, MODE_TOTP, MODE_BUSY, MODE_OFFER, MODE_DEVICE = range(7)

    def __init__(self, on_authenticated, try_restore: bool = True, notice: str = ""):
        super().__init__()
        self.on_authenticated = on_authenticated
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._closing = False
        self._busy = False
        self._user = None
        self._pending = ("", "")  # (email, password) kept only until the email is verified
        # "Remember me": (email, password, ticked) of the sign-in in flight, held in memory only
        # until it succeeds (possibly after the 2FA step) or fails.
        self._remember_pending = None
        self._saved_pw = None  # password prefilled from the OS keyring (to detect it going stale)
        self._used_saved_pw = False
        self._device_pending = ("", "")  # (email, password) while moving the account to this PC
        threading.Thread(target=_read_public_ip, daemon=True).start()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        card = QFrame()
        card.setObjectName("LockCard")
        card.setFixedSize(860, 560)
        card_row = QHBoxLayout(card)
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.setSpacing(0)

        left = QFrame()
        left.setObjectName("LockLeft")
        lay = QVBoxLayout(left)
        lay.setContentsMargins(36, 30, 28, 24)
        lay.setSpacing(8)
        self.title = QLabel("Welcome back")
        self.title.setObjectName("LockWelcome")
        self.sub = QLabel("Sign in to your DedSec CCTV account")
        self.sub.setObjectName("LockSub")
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addSpacing(6)
        tabs = QHBoxLayout()
        tabs.setSpacing(18)
        self.tab_signin = self._tab("Sign in", self.MODE_SIGNIN)
        self.tab_signup = self._tab("Create account", self.MODE_SIGNUP)
        tabs.addWidget(self.tab_signin)
        tabs.addWidget(self.tab_signup)
        tabs.addStretch(1)
        self.tabs_box = QWidget()
        self.tabs_box.setObjectName("AuthPage")
        self.tabs_box.setLayout(tabs)
        lay.addWidget(self.tabs_box)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_signin())
        self.stack.addWidget(self._build_signup())
        self.stack.addWidget(self._build_verify())
        self.stack.addWidget(self._build_totp())
        self.stack.addWidget(self._build_busy())
        self.stack.addWidget(self._build_offer())
        self.stack.addWidget(self._build_device())
        lay.addWidget(self.stack, 1)
        self.err = QLabel("")
        self.err.setObjectName("AuthErr")
        self.err.setWordWrap(True)
        self.info = QLabel("")
        self.info.setObjectName("AuthInfo")
        self.info.setWordWrap(True)
        lay.addWidget(self.err)
        lay.addWidget(self.info)

        right = QFrame()
        right.setObjectName("LockRight")
        side = QVBoxLayout(right)
        side.setContentsMargins(24, 24, 24, 24)
        side.addStretch(2)
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("background: transparent;")
        logo.setMinimumHeight(LOGIN_LOGO_SIZE)
        pix = _logo_pixmap(LOGIN_LOGO_SIZE)
        if not pix.isNull():
            logo.setPixmap(pix)
        brand = QLabel('<span style="color:#ffffff">DEDSEC</span> <span style="color:#22d3ee">LAB</span>')
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setObjectName("LockBrand")
        brand.setTextFormat(Qt.TextFormat.RichText)
        portal = QLabel("Private portal")
        portal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        portal.setObjectName("LockPortal")
        vpn = QLabel("Use a VPN before running this app.")
        vpn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vpn.setWordWrap(True)
        vpn.setObjectName("LockVpn")
        side.addWidget(logo)
        side.addSpacing(10)
        side.addWidget(brand)
        side.addWidget(portal)
        side.addSpacing(12)
        side.addWidget(vpn)
        side.addStretch(3)
        card_row.addWidget(left, 1)
        card_row.addWidget(right, 1)
        row.addWidget(card)
        row.addStretch(1)
        root.addLayout(row)
        root.addStretch(1)
        self.setStyleSheet("AuthScreen { background: #060606; } QScrollBar { height: 0px; width: 0px; }" + _AUTH_STYLE)

        client = auth_client()
        if client is None:
            self.set_mode(self.MODE_SIGNIN)
            self._show_error(_AUTH_INIT_ERROR + " Set DEDSEC_API_URL / API_BASE_URL.")
            for btn in (self.signin_btn, self.signup_btn):
                btn.setEnabled(False)
        elif try_restore:
            self.set_mode(self.MODE_BUSY)
            self.busy_label.setText("Restoring your session…")
            run_bg(self._restore_job, self._restore_ok, self._restore_failed)
        else:
            self.set_mode(self.MODE_SIGNIN)
        self._load_saved_login()
        if notice:
            self._show_info(notice)

    # -- widgets ---------------------------------------------------------------
    def _tab(self, text: str, mode: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("AuthTab")
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self.set_mode(mode))
        return btn

    @staticmethod
    def _field(placeholder: str, password: bool = False) -> QLineEdit:
        edit = QLineEdit()
        edit.setObjectName("AuthField")
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        return edit

    @staticmethod
    def _primary(text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("LockButton")
        btn.setMinimumHeight(46)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _link(text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("AuthLink")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _page():
        page = QWidget()
        page.setObjectName("AuthPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)
        return page, lay

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("LockLabel")
        lbl.setWordWrap(True)
        return lbl

    def _build_signin(self) -> QWidget:
        page, lay = self._page()
        self.si_email = self._field("Email")
        self.si_pw = self._field("Password", password=True)
        self.signin_btn = self._primary("Sign in", self._do_signin)
        self.si_email.returnPressed.connect(self.si_pw.setFocus)
        self.si_pw.returnPressed.connect(self._do_signin)
        self.remember = QCheckBox("Remember me")
        self.remember.setObjectName("AuthRemember")
        self.remember.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remember.setToolTip("Save your email and password in this PC's secure credential store "
                                 "(Windows Credential Manager). Untick and sign in to forget them.")
        self.forget_btn = self._link("Forget saved login", self._forget_saved_login)
        self.forget_btn.setVisible(False)
        remember_row = QHBoxLayout()
        remember_row.setContentsMargins(0, 0, 0, 0)
        remember_row.addWidget(self.remember)
        remember_row.addStretch(1)
        remember_row.addWidget(self.forget_btn)
        self.remember_hint = QLabel("")
        self.remember_hint.setObjectName("AuthHint")
        self.remember_hint.setWordWrap(True)
        self.remember_hint.setVisible(False)
        lay.addWidget(self.si_email)
        lay.addWidget(self.si_pw)
        lay.addLayout(remember_row)
        lay.addWidget(self.remember_hint)
        lay.addSpacing(2)
        lay.addWidget(self.signin_btn)
        lay.addWidget(self._link("Have a verification code? Enter it here", self._goto_verify))
        lay.addStretch(1)
        return page

    def _build_signup(self) -> QWidget:
        page, lay = self._page()
        self.su_email = self._field("Email")
        self.su_pw = self._field("Password (at least 12 characters)", password=True)
        self.su_pw2 = self._field("Confirm password", password=True)
        self.signup_btn = self._primary("Create account", self._do_signup)
        self.su_email.returnPressed.connect(self.su_pw.setFocus)
        self.su_pw.returnPressed.connect(self.su_pw2.setFocus)
        self.su_pw2.returnPressed.connect(self._do_signup)
        for w in (self.su_email, self.su_pw, self.su_pw2):
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(self.signup_btn)
        lay.addStretch(1)
        return page

    def _build_verify(self) -> QWidget:
        page, lay = self._page()
        self.verify_hint = self._label("Check your email for a verification code, then paste it below.")
        self.verify_code = self._field("Verification code from the email")
        self.verify_code.returnPressed.connect(self._do_verify)
        self.verify_btn = self._primary("Verify email", self._do_verify)
        self.resend_btn = self._link("Didn't get it? Resend code", self._do_resend)
        lay.addWidget(self.verify_hint)
        lay.addWidget(self.verify_code)
        lay.addSpacing(6)
        lay.addWidget(self.verify_btn)
        lay.addWidget(self.resend_btn)
        lay.addWidget(self._link("Already clicked the link in the email? Sign in", lambda: self.set_mode(self.MODE_SIGNIN)))
        lay.addStretch(1)
        return page

    def _build_totp(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(self._label("Two-factor authentication is on.\nEnter the 6-digit code from your authenticator app."))
        self.code = QLineEdit()
        self.code.setPlaceholderText("000000")
        self.code.setMaxLength(6)
        self.code.setObjectName("LockCode")
        self.code.returnPressed.connect(self._do_totp)
        self.totp_btn = self._primary("Verify", self._do_totp)
        lay.addWidget(self.code)
        lay.addSpacing(6)
        lay.addWidget(self.totp_btn)
        lay.addWidget(self._link("Back to sign in", lambda: self.set_mode(self.MODE_SIGNIN)))
        lay.addStretch(1)
        return page

    def _build_busy(self) -> QWidget:
        page, lay = self._page()
        lay.addStretch(1)
        self.busy_label = self._label("Please wait…")
        self.busy_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.busy_label)
        lay.addStretch(2)
        return page

    def _build_offer(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(self._label("Protect your account with two-factor authentication. "
                                  "You'll scan a QR code with an authenticator app (Google Authenticator, "
                                  "Aegis, Authy…) and enter a 6-digit code when you sign in."))
        lay.addSpacing(6)
        self.offer_btn = self._primary("Set up 2FA", self._do_offer_setup)
        lay.addWidget(self.offer_btn)
        lay.addWidget(self._link("Not now, continue to the app", self._finish))
        lay.addStretch(1)
        return page

    def _build_device(self) -> QWidget:
        page, lay = self._page()
        lay.addWidget(self._label("This account is locked to another PC. Each account works on one PC. "
                                  "To move it to this PC, request a device reset: we email a 6-digit code "
                                  "to the account address. The other PC is signed out."))
        self.dev_email = self._field("Email")
        self.dev_email.setReadOnly(True)
        self.dev_request_btn = self._primary("Request device reset", self._do_device_request)
        self.dev_code = QLineEdit()
        self.dev_code.setPlaceholderText("000000")
        self.dev_code.setMaxLength(6)
        self.dev_code.setObjectName("LockCode")
        self.dev_code.returnPressed.connect(self._do_device_confirm)
        self.dev_confirm_btn = self._primary("Confirm reset", self._do_device_confirm)
        self.dev_resend = self._link("Didn't get it? Send a new code", self._do_device_request)
        for w in (self.dev_code, self.dev_confirm_btn, self.dev_resend):
            w.setVisible(False)
        lay.addWidget(self.dev_email)
        lay.addWidget(self.dev_request_btn)
        lay.addWidget(self.dev_code)
        lay.addWidget(self.dev_confirm_btn)
        lay.addWidget(self.dev_resend)
        lay.addWidget(self._link("Back to sign in", lambda: self.set_mode(self.MODE_SIGNIN)))
        lay.addStretch(1)
        return page

    # -- state -------------------------------------------------------------------
    def set_mode(self, mode: int) -> None:
        if self._busy and mode != self.MODE_BUSY:
            return
        self.stack.setCurrentIndex(mode)
        self.tab_signin.setChecked(mode == self.MODE_SIGNIN)
        self.tab_signup.setChecked(mode == self.MODE_SIGNUP)
        self.tabs_box.setVisible(mode in (self.MODE_SIGNIN, self.MODE_SIGNUP))
        titles = {
            self.MODE_SIGNIN: ("Welcome back", "Sign in to your DedSec CCTV account"),
            self.MODE_SIGNUP: ("Create account", "One account for your DedSec CCTV"),
            self.MODE_VERIFY: ("Verify email", "One more step to activate your account"),
            self.MODE_TOTP: ("Two-factor", "Confirm it's really you"),
            self.MODE_BUSY: ("Welcome back", "Unlock your DedSec CCTV"),
            self.MODE_OFFER: ("Secure account", "Optional, but recommended"),
            self.MODE_DEVICE: ("Device locked", "This account is linked to another PC"),
        }
        t, s = titles[mode]
        self.title.setText(t)
        self.sub.setText(s)
        self._show_error("")
        self._show_info("")
        focus = {self.MODE_SIGNIN: self.si_email if not self.si_email.text() else self.si_pw,
                 self.MODE_SIGNUP: self.su_email, self.MODE_VERIFY: self.verify_code,
                 self.MODE_TOTP: self.code,
                 self.MODE_DEVICE: self.dev_code if self.dev_code.isVisible() else self.dev_request_btn}.get(mode)
        if focus is not None:
            focus.setFocus()

    def _show_error(self, text: str) -> None:
        self.err.setText(text)
        self.err.setVisible(bool(text))

    def _show_info(self, text: str) -> None:
        self.info.setText(text)
        self.info.setVisible(bool(text))

    def _set_busy(self, busy: bool, button: QPushButton | None = None, text: str = "") -> None:
        self._busy = busy
        for w in (self.signin_btn, self.signup_btn, self.verify_btn, self.totp_btn, self.offer_btn,
                  self.resend_btn, self.tab_signin, self.tab_signup, self.remember, self.forget_btn,
                  self.dev_request_btn, self.dev_confirm_btn, self.dev_resend):
            w.setEnabled(not busy)
        if button is not None:
            if busy:
                button.setProperty("idle_text", button.text())
                button.setText(text)
            elif button.property("idle_text"):
                button.setText(button.property("idle_text"))
        if busy:
            self._show_error("")
            self._show_info("")

    def _run(self, button: QPushButton, text: str, job, on_ok, action: str) -> None:
        if self._busy or self._closing:
            return
        self._set_busy(True, button, text)

        def ok(value):
            self._set_busy(False, button)
            on_ok(value)

        def err(exc):
            self._set_busy(False, button)
            self._on_error(exc, action)

        run_bg(job, ok, err)

    def _on_error(self, exc, action: str) -> None:
        device_locked = isinstance(exc, DeviceLocked) or (
            isinstance(exc, AuthError) and exc.status == 403 and "another pc" in str(exc).lower())
        if device_locked:  # hardware lock: never treat as "verify your email"
            self._show_device_reset()
            self._show_error(_friendly_error(exc, action))
            return
        if action == "login":
            status = exc.status if isinstance(exc, AuthError) else -1
            if status != 403:  # 403 = verify email first; keep it for the auto sign-in after
                self._remember_pending = None
            if status == 401 and self._used_saved_pw:
                # The remembered password no longer works: forget it (the box stays ticked,
                # so the next successful sign-in remembers the new password).
                self._forget_saved_login(quiet=True)
                self.si_pw.clear()
        if isinstance(exc, AuthError) and exc.status == 403 and action == "login":
            self._pending = (self.si_email.text().strip(), self.si_pw.text())
            self.set_mode(self.MODE_VERIFY)
        if isinstance(exc, AuthError) and exc.status == 400 and action == "totp":
            self.set_mode(self.MODE_SIGNIN)  # challenge expired / used up
            self._show_error("Your sign-in attempt expired. Please sign in again.")
            return
        self._show_error(_friendly_error(exc, action))

    # -- hardware lock: move the account to this PC -------------------------------
    def _show_device_reset(self) -> None:
        email = self.si_email.text().strip()
        pw = self.si_pw.text() or (self._remember_pending[1] if self._remember_pending else "")
        self._device_pending = (email, pw)
        self.dev_email.setText(email)
        self.dev_code.clear()
        for w in (self.dev_code, self.dev_confirm_btn, self.dev_resend):
            w.setVisible(False)
        self.dev_request_btn.setVisible(True)
        self.set_mode(self.MODE_DEVICE)

    def _do_device_request(self) -> None:
        email, pw = self._device_pending
        if not email or not pw:
            self.set_mode(self.MODE_SIGNIN)
            self._show_error("Enter your email and password, then sign in again to request a reset.")
            return
        client = auth_client()

        def ok(_msg):
            self.dev_request_btn.setVisible(False)
            for w in (self.dev_code, self.dev_confirm_btn, self.dev_resend):
                w.setVisible(True)
            self.dev_code.clear()
            self.dev_code.setFocus()
            self._show_info(f"If the details are right, a 6-digit code is on its way to {email}. "
                            "It expires in 15 minutes.")

        button = self.dev_request_btn if self.dev_request_btn.isVisible() else self.dev_resend
        self._run(button, "Sending…", lambda: client.request_device_reset(email, pw), ok, "devreset")

    def _do_device_confirm(self) -> None:
        code = "".join(ch for ch in self.dev_code.text() if ch.isdigit())
        if len(code) != 6:
            self._show_error("Enter the 6-digit code from the email.")
            return
        email, pw = self._device_pending
        client = auth_client()

        def job():
            client.confirm_device_reset(email, code)
            try:  # continue straight to sign-in on this (now linked) PC
                return self._with_vpn_check(client.login(email, pw))
            except TotpRequired:
                return "totp"
            except AuthError:
                return "reset"

        def ok(result):
            self.dev_code.clear()
            self._device_pending = ("", "")
            if result == "reset":
                self.set_mode(self.MODE_SIGNIN)
                self._show_info("Device reset. This PC is now linked to your account. Sign in to continue.")
            elif result == "totp":
                self.code.clear()
                self.set_mode(self.MODE_TOTP)
                self._show_info("Device reset. Enter your 2FA code to finish signing in.")
            else:
                self._apply_remember()
                self.si_pw.clear()
                self._authenticated(result, offer_2fa=True)

        self._run(self.dev_confirm_btn, "Checking…", job, ok, "devconfirm")

    # -- jobs (run on worker threads; no widget access in here) ---------------
    @staticmethod
    def _with_vpn_check(user):
        if not _IP_CHECKED:
            _read_public_ip()
        return user

    def _restore_job(self):
        client = auth_client()
        if not client.try_resume(raise_network_errors=True):
            return None
        return self._with_vpn_check(client.current_user())

    # -- actions -----------------------------------------------------------------
    def _restore_ok(self, user) -> None:
        self._busy = False
        if user is None:
            self.set_mode(self.MODE_SIGNIN)
            return
        self._authenticated(user, offer_2fa=False)

    def _restore_failed(self, exc) -> None:
        self._busy = False
        self.set_mode(self.MODE_SIGNIN)
        msg = _friendly_error(exc)
        self._show_error("Couldn't restore your session. " + msg)

    # -- "Remember me" ------------------------------------------------------------
    def _load_saved_login(self) -> None:
        """Prefill the sign-in form from the remembered login (never auto-submits)."""
        store = saved_login()
        try:
            creds = store.load() if store is not None else None
        except Exception:
            creds = None
        self._saved_pw = None
        if store is None or not store.can_store_password:
            self.remember.setToolTip(_NO_KEYRING_HINT)
        if creds is None:
            self.remember.setChecked(False)
            self.forget_btn.setVisible(False)
            self._set_remember_hint("")
            return
        self.si_email.setText(creds.email)
        if creds.password:
            self._saved_pw = creds.password
            self.si_pw.setEchoMode(QLineEdit.EchoMode.Password)
            self.si_pw.setText(creds.password)
        self.remember.setChecked(creds.remember)
        self.forget_btn.setVisible(True)
        self._set_remember_hint("" if creds.password or (store and store.can_store_password)
                                else _NO_KEYRING_HINT)

    def _set_remember_hint(self, text: str) -> None:
        self.remember_hint.setText(text)
        self.remember_hint.setVisible(bool(text))

    def _forget_saved_login(self, quiet: bool = False) -> None:
        store = saved_login()
        try:
            if store is not None:
                store.clear()
        except Exception:
            pass
        self._saved_pw = None
        self._used_saved_pw = False
        self.forget_btn.setVisible(False)
        self._set_remember_hint("")
        if not quiet:
            self.si_pw.clear()
            self.remember.setChecked(False)
            self._show_error("")
            self._show_info("Saved login forgotten on this PC.")

    def _apply_remember(self) -> None:
        """After a successful sign-in: save or clear the remembered login per the checkbox."""
        pending, self._remember_pending = self._remember_pending, None
        if pending is None:
            return
        email, pw, ticked = pending
        store = saved_login()
        if store is None:
            return
        try:
            if ticked:
                if not store.save(email, pw):
                    self.remember.setToolTip(_NO_KEYRING_HINT)
                    self._set_remember_hint(_NO_KEYRING_HINT)
            else:
                store.clear()
        except Exception:
            pass

    def _do_signin(self) -> None:
        email, pw = self.si_email.text().strip(), self.si_pw.text()
        if not email or not pw:
            self._show_error("Enter your email and password.")
            return
        client = auth_client()
        self._used_saved_pw = bool(self._saved_pw) and pw == self._saved_pw
        self._remember_pending = (email, pw, self.remember.isChecked())

        def job():
            try:
                user = client.login(email, pw)
            except TotpRequired:
                return "totp"
            return self._with_vpn_check(user)

        def ok(result):
            if result == "totp":
                self.code.clear()
                self.set_mode(self.MODE_TOTP)
                return
            self._apply_remember()
            self.si_pw.clear()
            self._authenticated(result, offer_2fa=True)

        self._run(self.signin_btn, "Signing in…", job, ok, "login")

    def _do_totp(self) -> None:
        code = "".join(ch for ch in self.code.text() if ch.isdigit())
        if len(code) != 6:
            self._show_error("Enter the 6-digit code.")
            return
        client = auth_client()

        def ok(user):
            self.code.clear()
            self._apply_remember()  # 2FA step completed: the sign-in really succeeded
            self.si_pw.clear()
            self._authenticated(user, offer_2fa=False)

        self._run(self.totp_btn, "Checking…", lambda: self._with_vpn_check(client.submit_totp(code)), ok, "totp")

    def _do_signup(self) -> None:
        email, pw, pw2 = self.su_email.text().strip(), self.su_pw.text(), self.su_pw2.text()
        if "@" not in email or "." not in email.split("@")[-1]:
            self._show_error("Enter a valid email address.")
            return
        if len(pw) < 12:
            self._show_error("Use a password with at least 12 characters.")
            return
        if pw != pw2:
            self._show_error("The passwords don't match.")
            return
        client = auth_client()

        def ok(_msg):
            self._pending = (email, pw)
            self.si_email.setText(email)
            self.su_pw.clear()
            self.su_pw2.clear()
            self.verify_code.clear()
            self.set_mode(self.MODE_VERIFY)
            self.verify_hint.setText(f"Check your email ({email}) for a verification code, then paste it below.")

        self._run(self.signup_btn, "Creating account…", lambda: client.register(email, pw), ok, "register")

    def _goto_verify(self) -> None:
        self.verify_hint.setText("Check your email for a verification code, then paste it below.")
        self.set_mode(self.MODE_VERIFY)

    def _do_verify(self) -> None:
        token = self.verify_code.text().strip()
        if len(token) < 10:
            self._show_error("Paste the full code from the email.")
            return
        client = auth_client()
        email, pw = self._pending

        def job():
            client.verify_email(token)
            if not (email and pw):
                return "verified"
            try:  # sign straight in with the credentials just entered
                return self._with_vpn_check(client.login(email, pw))
            except TotpRequired:
                return "totp"
            except AuthError:
                return "verified"

        def ok(result):
            self._pending = ("", "")
            self.verify_code.clear()
            if result == "verified":
                self.set_mode(self.MODE_SIGNIN)
                self._show_info("Email verified. You can sign in now.")
            elif result == "totp":
                self.set_mode(self.MODE_TOTP)
            else:
                if self._remember_pending and self._remember_pending[:2] == (email, pw):
                    self._apply_remember()
                self._authenticated(result, offer_2fa=True)

        self._run(self.verify_btn, "Verifying…", job, ok, "verify")

    def _do_resend(self) -> None:
        email, pw = self._pending
        if not (email and pw):
            self.set_mode(self.MODE_SIGNUP)
            self._show_info("Enter your email and password again to get a new code.")
            return
        client = auth_client()
        self._run(self.verify_btn, "Sending…", lambda: client.register(email, pw),
                  lambda _m: self._show_info("A new code is on its way (if the account isn't verified yet)."),
                  "register")

    def _do_offer_setup(self) -> None:
        dlg = TwoFactorSetupDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Enabling 2FA ends every session on the server: sign in again with a code.
            self.si_pw.clear()
            self.set_mode(self.MODE_SIGNIN)
            self._show_info("Two-factor authentication is on. Sign in again and enter your code.")

    def _authenticated(self, user, offer_2fa: bool) -> None:
        if self._closing:
            return
        if _vpn_blocked():
            self._block_and_close()
            return
        self._user = user
        if offer_2fa and user is not None and not user.totp_enabled:
            self.set_mode(self.MODE_OFFER)
            return
        self._finish()

    def _finish(self) -> None:
        self.on_authenticated(self._user)

    def paintEvent(self, event):
        p = QPainter(self)
        wall = _asset("site-wallpaper.png", "login_wallpaper.png", "wallpaper.png", "background.png")
        if wall:
            pm = QPixmap(str(wall))
            if not pm.isNull():
                img = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                p.drawPixmap((self.width() - img.width()) // 2, (self.height() - img.height()) // 2, img)
                p.fillRect(self.rect().adjusted(0, 0, 0, 6), QColor(6, 10, 18, 110))
                p.end()
                return
        p.fillRect(self.rect().adjusted(0, 0, 0, 6), _tc("#071018"))
        p.end()

    def _block_and_close(self) -> None:
        self._closing = True
        self.setEnabled(False)
        box = QDialog(self)
        box.setWindowTitle("DedSec VPN detected")
        box.setModal(True)
        box.setFixedSize(680, 380)
        box.setStyleSheet("QDialog { background: #070b12; } QLabel { background: transparent; }")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(22, 22, 22, 22)
        card = QFrame()
        card.setStyleSheet("QFrame { background: #101820; border: 1px solid #1d4e68; border-radius: 14px; }")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(28, 26, 28, 26)
        inner.setSpacing(8)
        badge = QLabel("BLOCKED")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet("color: #7dffb0; background: #14361f; border: 1px solid #2fbf6a; border-radius: 8px; font-size: 12px; font-weight: 800; letter-spacing: 2px; padding: 6px 12px;")
        badge.setFixedHeight(28)
        title = QLabel("DEDSEC VPN DETECTED")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #ff3b3b; font-size: 30px; font-weight: 800; border: 0;")
        msg = QLabel("You can not run this app on my servers.\nPlease disconnect and use your own.")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #d7e4f2; font-size: 16px; border: 0;")
        count = QLabel("Closing in 10")
        count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        count.setStyleSheet("color: #22d3ee; font-size: 40px; font-weight: 800; border: 0;")
        inner.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)
        inner.addWidget(title)
        inner.addWidget(msg)
        inner.addSpacing(8)
        inner.addWidget(count)
        lay.addWidget(card)
        self._left = 10
        self._box = box
        self._count = count
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._block_tick)
        self._tick.start(1000)
        box.show()

    def _block_tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._tick.stop()
            QApplication.quit()
            return
        self._count.setText(f"Closing in {self._left}")


class TwoFactorSetupDialog(QDialog):
    """QR enrolment for server-side 2FA. Accepted => 2FA enabled (all sessions were ended)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set up two-factor authentication")
        self.setModal(True)
        self.setFixedSize(520, 560)
        icon = _asset("dedsec_logo.ico", "dedsec_logo.png", "applogo.png")
        if icon:
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet("QDialog { background: #0a1624; }" + _AUTH_STYLE)
        self._busy = False
        self._legacy = _load_secret()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 26, 30, 24)
        lay.setSpacing(10)
        title = QLabel("Two-factor setup")
        title.setObjectName("LockWelcome")
        title.setStyleSheet("font-size: 26px;")
        self.hint = QLabel("Preparing your QR code…")
        self.hint.setObjectName("LockLabel")
        self.hint.setWordWrap(True)
        self.qr = QLabel()
        self.qr.setProperty("_dsNoTheme", True)  # QR code must stay black on white in every theme
        self.qr.setStyleSheet("background: #ffffff; border-radius: 8px; padding: 6px;")
        self.qr.setFixedSize(172, 172)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.key = QLabel("")
        self.key.setObjectName("LockKey")
        self.key.setWordWrap(True)
        self.key.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code = QLineEdit()
        self.code.setPlaceholderText("000000")
        self.code.setMaxLength(6)
        self.code.setObjectName("LockCode")
        self.code.returnPressed.connect(self._enable)
        self.err = QLabel("")
        self.err.setObjectName("AuthErr")
        self.err.setWordWrap(True)
        self.enable_btn = QPushButton("Enable 2FA")
        self.enable_btn.setObjectName("LockButton")
        self.enable_btn.setMinimumHeight(46)
        self.enable_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.enable_btn.clicked.connect(self._enable)
        self.enable_btn.setEnabled(False)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("AuthLink")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        lay.addWidget(title)
        lay.addWidget(self.hint)
        lay.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.key)
        lay.addWidget(self.code)
        lay.addWidget(self.err)
        lay.addWidget(self.enable_btn)
        lay.addWidget(cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        self._start_setup()

    def _start_setup(self) -> None:
        client = auth_client()
        legacy = self._legacy

        def job():
            if legacy:
                try:  # keep the authenticator entry from the old local lock screen working
                    return client.setup_2fa(existing_secret=legacy), True
                except AuthError as exc:
                    if exc.status != 422:
                        raise
            return client.setup_2fa(), False

        self._busy = True
        run_bg(job, self._setup_ok, self._setup_failed)

    def _setup_ok(self, result) -> None:
        self._busy = False
        data, migrated = result
        secret, uri = data.get("secret", ""), data.get("otpauth_uri", "")
        if migrated:
            self.hint.setText("Your existing DedSec authenticator entry will keep working. "
                              "Enter its current 6-digit code to switch it to your account.")
        else:
            self.hint.setText("Scan the code with your authenticator app, then enter the 6-digit code.")
        try:
            self.qr.setPixmap(_qr_pixmap(uri, 160))
        except Exception:
            self.qr.hide()
            self.hint.setText("QR library missing. Type the key below into your authenticator.")
        self.key.setText(secret)
        self.enable_btn.setEnabled(True)
        self.code.setFocus()

    def _setup_failed(self, exc) -> None:
        self._busy = False
        self.qr.hide()
        self.hint.setText("Couldn't start two-factor setup.")
        self.err.setText(_friendly_error(exc, "setup2fa"))

    def _enable(self) -> None:
        if self._busy or not self.enable_btn.isEnabled():
            return
        code = "".join(ch for ch in self.code.text() if ch.isdigit())
        if len(code) != 6:
            self.err.setText("Enter the 6-digit code.")
            return
        self._busy = True
        self.enable_btn.setEnabled(False)
        self.enable_btn.setText("Enabling…")
        self.err.setText("")
        client = auth_client()
        run_bg(lambda: client.enable_2fa(code), self._enabled, self._enable_failed)

    def _enabled(self, _value) -> None:
        self._busy = False
        if self._legacy:
            try:  # the secret now lives (encrypted) on the server; drop the local plaintext copy
                _lock_path().unlink()
            except OSError:
                pass
        self.accept()

    def _enable_failed(self, exc) -> None:
        self._busy = False
        self.enable_btn.setEnabled(True)
        self.enable_btn.setText("Enable 2FA")
        self.code.clear()
        self.err.setText(_friendly_error(exc, "enable2fa"))

    def reject(self) -> None:
        if not self._busy:
            super().reject()


class CctvApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DedSec CCTV")
        self.resize(1280, 800)
        self.setMinimumSize(980, 640)
        self.setObjectName("CctvWindow")
        icon = _asset("dedsec_logo.ico", "dedsec_logo.png", "applogo.png")
        if icon:
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet("QWidget#CctvWindow { background: #060606; border: 0; }")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.title_bar = WindowTitleBar.install(
            self, "DedSec CCTV",
            '<span style="color:#ffffff">DedSec</span>&nbsp;<span style="color:#22d3ee">CCTV</span>')
        self.page = None
        self.user = None
        self.lock = None
        self._show_auth(try_restore=False)  # always show the sign-in screen on launch

    def _show_auth(self, try_restore: bool = False, notice: str = "") -> None:
        self.lock = AuthScreen(self._open_wall, try_restore=try_restore, notice=notice)
        self.layout.addWidget(self.lock)
        self.lock.si_email.setFocus()

    def _open_wall(self, user=None):
        self.user = user
        self.page = CctvPage(None, account=self)
        self.lock.hide()
        self.layout.removeWidget(self.lock)
        self.lock.deleteLater()
        self.lock = None
        self.layout.addWidget(self.page)

    def _close_page(self) -> None:
        page = self.page
        self.page = None
        if page is None:
            return
        page.shutdown()  # players stopped before the page's video windows go away
        page.hide()
        self.layout.removeWidget(page)
        page.deleteLater()

    def sign_out(self) -> None:
        client = auth_client()
        self._close_page()
        self.user = None
        if client is not None:
            run_bg(client.logout)  # best effort server-side revoke; local tokens are wiped too
        self._show_auth(notice="You have been signed out.")

    def setup_2fa(self) -> None:
        dlg = TwoFactorSetupDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._close_page()
            self.user = None
            self._show_auth(notice="Two-factor authentication is on. Sign in again and enter your code.")

    def shutdown(self) -> None:
        """Stop all feeds; safe to call repeatedly (closeEvent, aboutToQuit, update quit)."""
        if self.page is not None and not sip.isdeleted(self.page):
            self.page.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        event.accept()


def _vlc_installer_visible() -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        user32 = ctypes.windll.user32
        found = {"ok": False}

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()
            if "vlc" in title or ("installer" in title and "windows" in title):
                found["ok"] = True
                return False
            return True

        user32.EnumWindows(enum, 0)
        return found["ok"]
    except Exception:
        return False


def _relaunch() -> None:
    target = Path(sys.argv[0]) if sys.argv else Path(__file__)
    if not target.is_file():
        target = Path(__file__)
    subprocess.Popen([sys.executable, str(target.resolve())])
    os._exit(0)


def _ensure_vlc(app: QApplication) -> None:
    try:
        import vlc
        if vlc.Instance("--intf", "dummy") is not None:
            return
    except Exception:
        pass
    box = QDialog()
    box.setWindowTitle("VLC required")
    box.setModal(True)
    box.setFixedSize(460, 230)
    box.setStyleSheet("QDialog { background: #101820; } QLabel { color: #e8eef6; background: transparent; } QPushButton { background: #1a222c; color: #e8eef6; border: 1px solid #3c4a5c; border-radius: 6px; min-height: 32px; padding: 0 14px; }")
    icon = _asset("dedsec_logo.ico", "dedsec_logo.png", "applogo.png")
    if icon:
        box.setWindowIcon(QIcon(str(icon)))
    lay = QVBoxLayout(box)
    lay.setContentsMargins(22, 18, 22, 18)
    title = QLabel("VLC is not installed.")
    title.setStyleSheet("font-size: 16px; font-weight: 700;")
    status = QLabel("The camera feeds need VLC. Install it now and this app will restart.")
    status.setWordWrap(True)
    row = QHBoxLayout()
    install = QPushButton("Install and restart")
    skip = QPushButton("Not now")
    row.addStretch(1)
    row.addWidget(install)
    row.addWidget(skip)
    lay.addWidget(title)
    lay.addWidget(status)
    lay.addStretch(1)
    lay.addLayout(row)
    state = {"proc": None, "dots": 0, "started": False}
    spin = QTimer(box)

    def tick() -> None:
        state["dots"] = (state["dots"] + 1) % 4
        word = "Installing VLC" if state["started"] else "Downloading VLC"
        status.setText(word + "." * state["dots"])

    def watch() -> None:
        proc = state["proc"]
        if proc is None:
            QTimer.singleShot(250, watch)
            return
        if proc.poll() is None:
            if not state["started"] and _vlc_installer_visible():
                state["started"] = True
                box.hide()
            QTimer.singleShot(400, watch)
            return
        spin.stop()
        _relaunch()

    def begin() -> None:
        install.setEnabled(False)
        skip.setEnabled(False)
        status.setText("Downloading VLC")
        spin.timeout.connect(tick)
        spin.start(400)
        state["armed"] = True
        def launch() -> None:
            if sys.platform.startswith("win"):
                cmd = ["winget", "install", "-e", "--id", "VideoLAN.VLC", "--accept-package-agreements", "--accept-source-agreements"]
            elif sys.platform == "darwin":
                cmd = ["brew", "install", "--cask", "vlc"]
            else:
                cmd = ["sudo", "apt-get", "install", "-y", "vlc"]
            state["proc"] = subprocess.Popen(cmd)
        threading.Thread(target=launch, daemon=True).start()
        QTimer.singleShot(250, watch)

    install.clicked.connect(begin)
    skip.clicked.connect(box.reject)
    box.exec()
    if state.get("armed"):
        while state["proc"] is None or state["proc"].poll() is None:
            app.processEvents()
            time.sleep(0.2)
        _relaunch()


# ---------------------------------------------------------------------------
# auto-update
# ---------------------------------------------------------------------------

class UpdateError(Exception):
    pass


_SEMVER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(text: str) -> tuple:
    """Sort key for a semver string. ``1.2`` == ``1.2.0``; ``1.2.0-beta`` < ``1.2.0``; build metadata ignored."""
    m = _SEMVER_RE.match(str(text or "").strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    core = tuple(int(x or 0) for x in m.group(1, 2, 3))
    pre = m.group(4)
    if not pre:
        return core + (1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return core + (0, ids)


def is_newer_version(remote: str, current: str = APP_VERSION) -> bool:
    try:
        return parse_version(remote) > parse_version(current)
    except ValueError:
        return False


class UpdateInfo:
    def __init__(self, version: str, url: str, sha256: str, mandatory: bool = True, notes: str = ""):
        self.version, self.url, self.sha256, self.mandatory, self.notes = version, url, sha256, mandatory, notes

    def __repr__(self) -> str:
        return f"UpdateInfo({self.version!r}, {self.url!r}, mandatory={self.mandatory})"


def _update_session():
    import requests
    s = requests.Session()
    s.trust_env = False  # no proxy env vars
    s.verify = True
    s.headers["User-Agent"] = f"DedSecCCTV/{APP_VERSION}"
    return s


def resolve_update_url(url: str, base: str = UPDATE_BASE_URL) -> str:
    """Resolve latest.json's ``url`` (relative name or absolute) and refuse anything outside ``base``."""
    b = urlparse(base)
    full = urljoin(base, str(url or "").strip())
    u = urlparse(full)
    if (u.scheme != "https" or b.scheme != "https" or u.netloc.lower() != b.netloc.lower()
            or not u.path.startswith(b.path) or ".." in u.path.split("/") or u.query or u.fragment):
        raise UpdateError(f"update url outside {base}: {url!r}")
    return full


def parse_latest(data, current: str = APP_VERSION, base: str = UPDATE_BASE_URL):
    """latest.json -> UpdateInfo if it describes a newer, well-formed release, else None."""
    if not isinstance(data, dict) or not data.get("version"):
        return None
    version = str(data["version"]).strip()
    if not is_newer_version(version, current):
        return None
    sha = str(data.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise UpdateError("latest.json has no valid sha256")
    url = resolve_update_url(data.get("url") or "DedSecCCTV.zip", base)
    mandatory = data.get("mandatory", True)
    mandatory = mandatory if isinstance(mandatory, bool) else str(mandatory).lower() not in ("false", "0", "no")
    return UpdateInfo(version, url, sha, mandatory, str(data.get("notes") or "")[:2000])


def _update_public_key():
    """Pinned Ed25519 key, or None if still the placeholder / malformed (=> reject all updates)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        raw = base64.b64decode(UPDATE_PUBLIC_KEY_B64, validate=True)
        if len(raw) != 32:
            return None
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        return None


def verify_manifest_signature(manifest: bytes, sig_b64, public_key=None) -> bool:
    """True only if ``sig_b64`` is a valid Ed25519 signature of the exact latest.json bytes."""
    key = public_key or _update_public_key()
    if key is None or not manifest or not sig_b64:
        return False
    try:
        if isinstance(sig_b64, bytes):
            sig_b64 = sig_b64.decode("ascii")
        sig = base64.b64decode(sig_b64.strip(), validate=True)
        if len(sig) != 64:
            return False
        key.verify(sig, manifest)
        return True
    except Exception:
        return False


def _fetch_manifest_sig(session, base: str):
    try:
        r = session.get(urljoin(base, "latest.json" + UPDATE_SIG_SUFFIX), timeout=UPDATE_CHECK_TIMEOUT,
                        allow_redirects=False)
        if r.status_code != 200 or len(r.content) > 1024:
            return None
        return r.content
    except Exception:
        return None


def check_for_update(base: str = UPDATE_BASE_URL, current: str = APP_VERSION, session=None):
    """Fetch latest.json. Missing/empty file, network trouble or an older version -> None."""
    if urlparse(base).scheme != "https":
        return None
    s = session or _update_session()
    try:
        r = s.get(urljoin(base, "latest.json"), timeout=UPDATE_CHECK_TIMEOUT, allow_redirects=False, stream=True)
    except Exception:
        return None
    try:
        if r.status_code != 200:
            return None
        raw = r.raw.read(64 * 1024 + 1, decode_content=True) if hasattr(r, "raw") and r.raw else r.content
        if len(raw) > 64 * 1024 or not raw.strip():
            return None
        if not verify_manifest_signature(raw, _fetch_manifest_sig(s, base)):
            return None  # unsigned or badly signed manifest: never trust it
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except ValueError:
            return None
        try:
            return parse_latest(data, current, base)
        except UpdateError:
            return None  # malformed/unsafe manifest: ignore rather than run anything
    finally:
        try:
            r.close()
        except Exception:
            pass


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_update(info: UpdateInfo, dest_dir=None, progress=None, session=None) -> Path:
    """Stream the zip to %TEMP%, verify size + SHA-256 + zip structure. Returns the zip path."""
    resolve_update_url(info.url)  # re-check: never download from anywhere else
    dest_dir = Path(dest_dir or tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_ver = re.sub(r"[^0-9A-Za-z.+-]", "_", info.version)
    final = dest_dir / f"DedSecCCTV-{safe_ver}.zip"
    part = final.with_suffix(".zip.part")
    s = session or _update_session()
    h = hashlib.sha256()
    done = 0
    try:
        with s.get(info.url, timeout=(UPDATE_CHECK_TIMEOUT, 60), stream=True, allow_redirects=False) as r:
            if r.status_code != 200:
                raise UpdateError(f"download failed (HTTP {r.status_code})")
            total = int(r.headers.get("Content-Length") or 0)
            if total > UPDATE_MAX_BYTES:
                raise UpdateError("update file is too large")
            with open(part, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    done += len(chunk)
                    if done > UPDATE_MAX_BYTES:
                        raise UpdateError("update file is too large")
                    h.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(done, total)
        if h.hexdigest() != info.sha256:
            raise UpdateError("the download is corrupted (SHA-256 mismatch)")
        if not zipfile.is_zipfile(part):
            raise UpdateError("the download is not a zip file")
        os.replace(part, final)
        return final
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def _is_compiled() -> bool:
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def install_dir() -> Path:
    """Folder the app runs from (exe folder for builds, app.py folder for source runs)."""
    if _is_compiled():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def updater_command(zip_path, sha256: str, pid: int | None = None, workdir=None) -> list:
    """Copy the updater out of the install folder (so it can itself be replaced) and build its argv."""
    target = install_dir()
    workdir = Path(workdir or tempfile.mkdtemp(prefix="dedsec-updater-"))
    exe = target / UPDATER_EXE_NAME
    script = target / UPDATER_SCRIPT_NAME
    pid = os.getpid() if pid is None else pid
    if _is_compiled():
        launch = [str(Path(sys.executable).resolve())]
    else:
        launch = [str(Path(__file__).resolve())]
    if exe.is_file() and sys.platform == "win32":
        copy = workdir / UPDATER_EXE_NAME
        shutil.copy2(exe, copy)
        cmd = [str(copy)]
    elif script.is_file() and not _is_compiled():
        copy = workdir / UPDATER_SCRIPT_NAME
        shutil.copy2(script, copy)
        cmd = [sys.executable, str(copy), "--python", sys.executable]
    else:
        raise UpdateError(f"{UPDATER_EXE_NAME if _is_compiled() else UPDATER_SCRIPT_NAME} is missing "
                          f"from {target}. Reinstall DedSec CCTV.")
    return cmd + ["--pid", str(pid), "--zip", str(zip_path), "--sha256", sha256,
                  "--target", str(target), "--launch", launch[0]]


def launch_updater(zip_path, sha256: str) -> None:
    """Start the updater detached (same user, no elevation). The caller must then quit."""
    cmd = updater_command(zip_path, sha256)
    kw: dict = {"close_fds": True, "cwd": tempfile.gettempdir()}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


class _DownloadWorker(QObject):
    progress = pyqtSignal(object, object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, info: UpdateInfo):
        super().__init__()
        self.info = info

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            path = download_update(self.info, progress=lambda d, t: self.progress.emit(d, t))
        except BaseException as exc:
            self.failed.emit(exc)
        else:
            self.finished.emit(path)


class UpdateDialog(QDialog):
    """"New update available" prompt. Mandatory updates can't be dismissed."""

    def __init__(self, info: UpdateInfo, parent=None):
        super().__init__(parent)
        self.info = info
        self._worker = None
        self._busy = False
        self.setWindowTitle("DedSec CCTV update")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedSize(500, 300)
        if info.mandatory:
            self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint
                                | Qt.WindowType.WindowTitleHint)
        icon = _asset("dedsec_logo.ico", "dedsec_logo.png", "applogo.png")
        if icon:
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet("QDialog { background: #0a1624; } QProgressBar { background: #08141f; color: #e8f0ff; "
                           "border: 1px solid #1d4e68; border-radius: 8px; text-align: center; height: 18px; } "
                           "QProgressBar::chunk { background: #22d3ee; border-radius: 7px; }" + _AUTH_STYLE)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.setSpacing(10)
        title = QLabel("New update available")
        title.setObjectName("LockWelcome")
        title.setStyleSheet("font-size: 24px;")
        text = f"DedSec CCTV {info.version} is available (you have {APP_VERSION})."
        if info.mandatory:
            text += " This update is required to keep using the app."
        msg = QLabel(text)
        msg.setObjectName("LockLabel")
        msg.setWordWrap(True)
        notes = QLabel(info.notes)
        notes.setObjectName("AuthHint")
        notes.setWordWrap(True)
        notes.setVisible(bool(info.notes))
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.status = QLabel("")
        self.status.setObjectName("AuthErr")
        self.status.setWordWrap(True)
        self.download_btn = QPushButton("Download now")
        self.download_btn.setObjectName("LockButton")
        self.download_btn.setMinimumHeight(44)
        self.download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_btn.clicked.connect(self._start)
        row = QHBoxLayout()
        self.later_btn = QPushButton("Later")
        self.later_btn.setObjectName("AuthLink")
        self.later_btn.clicked.connect(self.reject)
        self.later_btn.setVisible(not info.mandatory)
        self.quit_btn = QPushButton("Quit")
        self.quit_btn.setObjectName("AuthLink")
        self.quit_btn.clicked.connect(lambda: os._exit(0))
        self.quit_btn.setVisible(info.mandatory)
        row.addWidget(self.later_btn)
        row.addWidget(self.quit_btn)
        row.addStretch(1)
        lay.addWidget(title)
        lay.addWidget(msg)
        lay.addWidget(notes)
        lay.addStretch(1)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.download_btn)
        lay.addLayout(row)

    def _start(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.download_btn.setEnabled(False)
        self.download_btn.setText("Downloading…")
        self.later_btn.setEnabled(False)
        self.status.setText("")
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self._worker = _DownloadWorker(self.info)
        self._worker.progress.connect(self._progress)
        self._worker.finished.connect(self._downloaded)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _progress(self, done, total) -> None:
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            self.bar.setFormat(f"{done / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.bar.setFormat(f"{done / 1048576:.1f} MB")

    def _downloaded(self, path) -> None:
        self.download_btn.setText("Installing…")
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000)
        try:
            launch_updater(path, self.info.sha256)
        except Exception as exc:
            self._failed(exc)
            return
        self.status.setStyleSheet("color: #7dffb0;")
        self.status.setText("Installing the update. DedSec CCTV will restart in a moment.")
        QTimer.singleShot(600, _quit_for_update)

    def _failed(self, exc) -> None:
        self._busy = False
        self.bar.setVisible(False)
        self.download_btn.setEnabled(True)
        self.download_btn.setText("Retry download")
        self.later_btn.setEnabled(True)
        reason = str(exc) if isinstance(exc, UpdateError) else _friendly_update_error(exc)
        self.status.setText(f"Update failed: {reason}")

    def reject(self) -> None:  # Esc / Later / close button
        if self.info.mandatory or self._busy:
            return
        super().reject()

    def closeEvent(self, event):
        if self.info.mandatory or self._busy:
            event.ignore()
            return
        super().closeEvent(event)


def _friendly_update_error(exc) -> str:
    name = exc.__class__.__name__
    if "SSL" in name or "Connection" in name or "Timeout" in name:
        return "can't reach the update server. Check your connection and try again."
    return f"{name}. Try again."


def _quit_for_update() -> None:
    """Close every window (stops the camera players) and exit so the updater can replace files."""
    app = QApplication.instance()
    if app is not None:
        for w in app.topLevelWidgets():
            if isinstance(w, CctvApp):
                w.close()
        app.quit()
    QTimer.singleShot(3000, lambda: os._exit(0))  # hard stop if something keeps the loop alive


_UPDATE_DIALOG = None


def start_update_check(parent=None) -> None:
    """Background check on start; shows the (modal) update dialog when a newer version exists."""
    if DEDSEC_DEV and os.environ.get("DEDSEC_NO_UPDATE", "").strip() == "1":
        return

    def show(info) -> None:
        global _UPDATE_DIALOG
        if info is None or _UPDATE_DIALOG is not None:
            return
        _UPDATE_DIALOG = UpdateDialog(info, parent)
        _UPDATE_DIALOG.show()
        _UPDATE_DIALOG.raise_()
        _UPDATE_DIALOG.activateWindow()

    run_bg(check_for_update, show, lambda _exc: None)


def main():
    app = QApplication(sys.argv)
    _ensure_vlc(app)
    app.setStyle("Fusion")
    apply_theme(str(load_settings().get("theme") or "Default"), save=False)  # also sets the palette
    ensure_user_streams()
    window = CctvApp()
    app.aboutToQuit.connect(window.shutdown)
    window.showMaximized()
    start_update_check(window)
    rc = app.exec()
    window.shutdown()
    _exit_cleanly(app, window, rc)


def _exit_cleanly(app, window, rc: int) -> None:
    """Destroy the widgets while QApplication still exists (letting Python's GC do it during
    interpreter teardown segfaults), then exit. If a VLC player never finished stopping, skip
    interpreter teardown entirely: its threads would run against freed libvlc/Qt state."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    if _vlc_busy() or _ABANDONED_PLAYERS:
        os._exit(rc)
    try:
        window.hide()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        app.processEvents()
    except Exception:
        pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
