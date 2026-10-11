"""Build DedSecCCTV.exe (Windows) with Nuitka. Usage: python build.py  (see docs/APP_SETUP.md)"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ENTRY = APP_DIR / "app.py"
ICON = APP_DIR / "assets" / "dedsec_logo.ico"
BUILD_DIR = APP_DIR / "nuitka_build"
OUT_DIR = APP_DIR / "DedSecCCTV"
EXE_NAME = "DedSecCCTV.exe"


def python_bin() -> str:
    venv = APP_DIR / "venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    return sys.executable


def ensure_nuitka(py: str) -> None:
    probe = subprocess.run([py, "-c", "import nuitka"], capture_output=True)
    if probe.returncode == 0:
        return
    print("Installing Nuitka...")
    subprocess.check_call([py, "-m", "pip", "install", "nuitka", "ordered-set", "zstandard"])


def ensure_app_deps(py: str) -> None:
    probe = subprocess.run([py, "-c", "import PyQt6, vlc, qrcode, requests, keyring, cryptography"], capture_output=True)
    if probe.returncode == 0:
        return
    req = APP_DIR / "requirements.txt"
    print("Installing app dependencies...")
    if req.is_file():
        subprocess.check_call([py, "-m", "pip", "install", "-r", str(req)])
    else:
        subprocess.check_call([py, "-m", "pip", "install", "PyQt6", "python-vlc", "qrcode",
                               "requests", "keyring", "cryptography"])


def build() -> None:
    if not ENTRY.is_file():
        raise SystemExit(f"Missing {ENTRY}")
    py = python_bin()
    print(f"Python: {py}")
    ensure_nuitka(py)
    ensure_app_deps(py)
    if not (APP_DIR / "dedsec_auth_client.py").is_file():
        raise SystemExit(f"Missing {APP_DIR / 'dedsec_auth_client.py'} (must sit next to app.py)")
    print("Building DedSecCCTV.exe")
    cmd = [
        py, "-m", "nuitka",
        "--standalone",
        "--enable-plugin=pyqt6",
        "--windows-console-mode=disable",
        "--assume-yes-for-downloads",
        f"--output-dir={BUILD_DIR}",
        f"--output-filename={EXE_NAME}",
        "--include-package=vlc",
        # account sign-in (dedsec_auth_client.py next to app.py) and its dependencies
        "--include-module=dedsec_auth_client",
        "--include-package=requests",
        "--include-package=urllib3",
        "--include-package=charset_normalizer",
        "--include-package=idna",
        "--include-package=certifi",
        "--include-package-data=certifi",
        "--include-package=keyring",
        "--include-package=keyring.backends",
        "--include-distribution-metadata=keyring",
        "--include-package=jaraco",
        "--include-package=win32ctypes",
        "--include-module=cryptography.fernet",
        "--include-module=cryptography.hazmat.primitives.ciphers.aead",
        "--include-package=qrcode",
    ]
    if os.environ.get("DEDSEC_WINE_BUILD") == "1":
        # Cross-building under Wine: Nuitka's DLL dependency scanner (Dependency Walker)
        # does not work there, so use the pefile scanner and force MinGW64.
        cmd += ["--mingw64", "--experimental=force-dependencies-pefile"]
    if ICON.is_file():
        cmd.append(f"--windows-icon-from-ico={ICON}")
    assets = APP_DIR / "assets"
    if assets.is_dir():
        cmd.append(f"--include-data-dir={assets}=assets")
    flags = APP_DIR / "flags"  # country flag PNGs (flags/<iso2>.png) for the stream info badge
    if flags.is_dir():
        cmd.append(f"--include-data-dir={flags}=flags")
    cmd.append(str(ENTRY))
    subprocess.check_call(cmd, cwd=APP_DIR)
    dist = BUILD_DIR / "app.dist"
    if not dist.is_dir():
        raise SystemExit(f"Build folder missing: {dist}")
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(dist, OUT_DIR)
    # Ship a config.ini next to the exe if you created one (server URLs).
    cfg = APP_DIR / "config.ini"
    if cfg.is_file():
        shutil.copy2(cfg, OUT_DIR / "config.ini")
    # Ship the user_streams/ template so users see the per-country examples straight away.
    tpl = APP_DIR / "user_streams"
    if tpl.is_dir() and not (OUT_DIR / "user_streams").exists():
        shutil.copytree(tpl, OUT_DIR / "user_streams")
    # user_streams/ (the user's own catalog) is created by the app on first run next to the exe.
    print(f"OK  {OUT_DIR / EXE_NAME}")


if __name__ == "__main__":
    build()
