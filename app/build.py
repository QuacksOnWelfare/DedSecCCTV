"""Build DedSecCCTV.exe (Windows) with Nuitka. Usage: python build.py  (see docs/APP_SETUP.md)"""
import os
import re
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
UPDATE_ZIP = APP_DIR / "DedSecCCTV.zip"   # auto-update package (contents of OUT_DIR)


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



def _tool(name: str) -> str | None:
    """Return the path to an optional post-build tool (strip/upx) if it is on PATH."""
    return shutil.which(name)


def post_build_harden(out_dir: Path, exe_name: str) -> None:
    """Optional, best-effort hardening applied to the finished build folder.

    Both steps are guarded by an existence check and never fail the build: if the
    tool is missing we simply skip it. (Optional hardening only.)
    """
    exe = out_dir / exe_name
    targets = [exe] + sorted(out_dir.glob("*.dll")) + sorted(out_dir.glob("*.pyd"))
    targets = [p for p in targets if p.is_file()]

    # Strip debug symbols from MinGW-produced binaries if `strip` is available.
    strip = _tool("strip")
    if strip:
        print("Stripping debug symbols...")
        for p in targets:
            subprocess.run([strip, "--strip-unneeded", str(p)], check=False)
    else:
        print("strip not found on PATH - skipping symbol strip (optional)")

    # Optionally UPX-compress the exe/DLLs if UPX is installed. Skipped when absent.
    upx = _tool("upx")
    if upx:
        print("Compressing with UPX...")
        for p in targets:
            subprocess.run([upx, "--best", "--lzma", "-q", str(p)], check=False)
    else:
        print("upx not found on PATH - skipping UPX compression (optional)")


def build(debug: bool = False, installer: bool = True) -> None:
    if not ENTRY.is_file():
        raise SystemExit(f"Missing {ENTRY}")
    py = python_bin()
    print(f"Python: {py}")
    ensure_nuitka(py)
    ensure_app_deps(py)
    if not (APP_DIR / "dedsec_auth_client.py").is_file():
        raise SystemExit(f"Missing {APP_DIR / 'dedsec_auth_client.py'} (must sit next to app.py)")
    version = app_version()
    if not re.match(r"^\d+(\.\d+){1,3}$", version):
        raise SystemExit(f"APP_VERSION in app.py is not a valid version: {version}")
    print(f"Building DedSecCCTV.exe {version}")
    cmd = [
        py, "-m", "nuitka",
        "--standalone",
        "--enable-plugin=pyqt6",
        "--windows-console-mode=" + ("force" if debug else "disable"),
        "--assume-yes-for-downloads",
        # --- Hardening / anti-casual-reverse-engineering flags --------------
        "--remove-output",              # delete the Nuitka build scratch dir after a successful build
        # Note: Nuitka has no portable "disable module name table" flag across versions; compiled
        #       modules already ship as machine code, not readable .py source. Stripping of debug
        #       symbols (and optional UPX compression) is done as a post-build step below.
        f"--output-dir={BUILD_DIR}",
        f"--output-filename={EXE_NAME}",
        # Version resource from APP_VERSION (the installer reads ProductVersion from the exe).
        f"--file-version={win_version(version)}",
        f"--product-version={version}",
        "--company-name=DedSec Lab",
        "--product-name=DedSec CCTV",
        "--file-description=DedSec CCTV",
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
    if not debug:
        cmd += ["--python-flag=no_asserts", "--python-flag=no_docstrings"]
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
    # Optional post-build hardening: strip symbols and (if installed) UPX-compress.
    add_updater()
    if debug:
        print("DEBUG build: console window on, symbols kept, not hardened. Do NOT ship this one.")
    else:
        post_build_harden(OUT_DIR, EXE_NAME)
    print(f"OK  {OUT_DIR / EXE_NAME}")
    if installer and not debug:
        build_installer()


UPDATER_SRC = APP_DIR / "updater.py"
UPDATER_BUILD = APP_DIR / "updater_build"
UPDATER_EXE = "DedSecUpdater.exe"
# Since 1.0.3 the updater ships inside the app, not in the install root. The app copies it to
# %TEMP% as DedSecUpdater.exe when an update is applied (app.py UPDATER_BUNDLED).
UPDATER_BUNDLED = Path("assets") / "updater" / "DedSecUpdater.bin"


def win_version(version: str) -> str:
    """1.0.3 -> 1.0.3.0 (Windows version resources need four numbers)."""
    parts = version.split(".")
    return ".".join((parts + ["0", "0", "0"])[:4])


def build_updater(force: bool = False) -> Path | None:
    """Build DedSecUpdater.exe (one file) into updater_build/. Reused if updater.py is unchanged."""
    if not UPDATER_SRC.is_file():
        print(f"WARNING: {UPDATER_SRC} not found - cannot build {UPDATER_EXE}")
        return None
    exe = UPDATER_BUILD / UPDATER_EXE
    if exe.is_file() and not force and exe.stat().st_mtime >= UPDATER_SRC.stat().st_mtime:
        print(f"Updater up to date, reusing {exe}")
        return exe
    py = python_bin()
    ensure_nuitka(py)
    print(f"Building {UPDATER_EXE}...")
    version = app_version()
    # 1.0.3: the updater shows a small tkinter progress window, so Nuitka must bundle Tcl/Tk.
    cmd = [py, "-m", "nuitka", "--onefile", "--windows-console-mode=disable",
           "--enable-plugin=tk-inter",
           "--assume-yes-for-downloads", "--remove-output",
           f"--output-dir={UPDATER_BUILD}", f"--output-filename={UPDATER_EXE}"]
    if re.match(r"^\d+(\.\d+){1,3}$", version):
        cmd += [f"--file-version={win_version(version)}", f"--product-version={version}",
                "--product-name=DedSec CCTV Updater", "--file-description=DedSec CCTV Updater"]
    if ICON.is_file():
        cmd.append(f"--windows-icon-from-ico={ICON}")
    cmd.append(str(UPDATER_SRC))
    subprocess.check_call(cmd, cwd=APP_DIR)
    return exe if exe.is_file() else None


def add_updater() -> None:
    """Put the updater inside the build as assets/updater/DedSecUpdater.bin (not in the root)."""
    exe = build_updater()
    if exe and OUT_DIR.is_dir():
        dest = OUT_DIR / UPDATER_BUNDLED
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, dest)
        legacy = OUT_DIR / UPDATER_EXE
        if legacy.exists():
            legacy.unlink()
        print(f"OK  {dest}")
    else:
        print(f"WARNING: {UPDATER_EXE} missing - auto-update will not work in this build")


def make_update_zip() -> None:
    """Zip the build folder contents for auto-update (DedSecCCTV.zip next to build.py).

    A root DedSecUpdater.exe is never packed: 1.0.2 copies its own one to %TEMP% before it
    extracts this zip, and 1.0.3 removes the old root copy itself."""
    import hashlib
    import zipfile
    if not (OUT_DIR / EXE_NAME).is_file():
        print(f"No app build found at {OUT_DIR / EXE_NAME} - build the app first")
        return
    if not (OUT_DIR / UPDATER_BUNDLED).is_file():
        print(f"WARNING: {UPDATER_BUNDLED} is not in the build - updates from this version will fail")
    tmp = UPDATE_ZIP.with_suffix(".zip.tmp")
    count = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(OUT_DIR.rglob("*")):
            rel = path.relative_to(OUT_DIR)
            if path.is_dir() or rel.as_posix().lower() == UPDATER_EXE.lower():
                continue
            if rel.parts[0].lower() in ("streams", "user_streams"):
                continue
            zf.write(path, rel.as_posix())
            count += 1
    os.replace(tmp, UPDATE_ZIP)
    digest = hashlib.sha256(UPDATE_ZIP.read_bytes()).hexdigest()
    print(f"OK  {UPDATE_ZIP}  ({count} files, {UPDATE_ZIP.stat().st_size} bytes)")
    print(f"    version {app_version()}  sha256 {digest}")


def build_installer() -> None:
    """Build the Inno Setup installer (installer/build_installer.ps1). Skip with --no-installer."""
    script = APP_DIR / "installer" / "build_installer.ps1"
    if not script.is_file():
        print("installer/build_installer.ps1 not found - skipping installer")
        return
    if os.name != "nt":
        print("Not on Windows - skipping installer")
        return
    print("Building installer...")
    if not (OUT_DIR / EXE_NAME).is_file():
        print(f"No app build found at {OUT_DIR / EXE_NAME} - build the app first")
        return
    version = app_version()
    if not re.match(r"^\d+(\.\d+){1,3}$", version):
        print(f"APP_VERSION in app.py is not a valid version: {version}")
        return
    rc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                         "-DistDir", str(OUT_DIR), "-AppVersion", version], cwd=APP_DIR).returncode
    if rc != 0:
        print("WARNING: installer build failed (the app build above is still fine). "
              "Is Inno Setup installed? winget install JRSoftware.InnoSetup")
        return
    out = sorted((APP_DIR / "installer" / "Output").glob("DedSecCCTV-Setup-*.exe"),
                 key=lambda p: p.stat().st_mtime)
    if out:
        print(f"OK  {out[-1]}")



# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
INSTALLER_OUT = APP_DIR / "installer" / "Output"

C = {"t": "\033[38;5;44m", "g": "\033[38;5;245m", "y": "\033[33m", "r": "\033[31m",
     "ok": "\033[32m", "b": "\033[1m", "x": "\033[0m"}

BANNER = r"""
  ____           _ ____             ____ ____ _______     __
 |  _ \  ___  __| / ___|  ___  ___ / ___/ ___|_   _\ \   / /
 | | | |/ _ \/ _` \___ \ / _ \/ __| |  | |     | |  \ \ / /
 | |_| |  __/ (_| |___) |  __/ (__| |__| |___  | |   \ V /
 |____/ \___|\__,_|____/ \___|\___|\____\____| |_|    \_/
"""


def app_version() -> str:
    try:
        m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', ENTRY.read_text(encoding="utf-8"))
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def status_line() -> str:
    exe = OUT_DIR / EXE_NAME
    app = "built" if exe.is_file() else "not built"
    setups = sorted(INSTALLER_OUT.glob("DedSecCCTV-Setup-*.exe")) if INSTALLER_OUT.is_dir() else []
    inst = setups[-1].name if setups else "none"
    return f"Version {app_version()}  |  App: {app}  |  Installer: {inst}"


def clean_builds() -> None:
    removed = 0
    for p in (OUT_DIR, BUILD_DIR, INSTALLER_OUT, UPDATE_ZIP):
        if p.exists():
            print(f"Removing {p}")
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed += 1
    # updater_build/ is kept on purpose: it is rebuilt automatically when updater.py changes.
    inc = APP_DIR / "installer" / "version.inc"
    if inc.is_file():
        inc.unlink()
    print(f"{C['ok']}Clean done{C['x']}" if removed else "Nothing to clean, already fresh.")


def wipe_user_config() -> None:
    """Make the next launch act like a brand new user: settings, lock file, saved logins."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        cfg = Path(appdata) / "DedSecCctv"
        if cfg.exists():
            shutil.rmtree(cfg, ignore_errors=True)
            print(f"Removed {cfg}")
        else:
            print("No settings folder found (already fresh)")
    if os.name != "nt":
        return
    out = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True).stdout
    found = 0
    for line in out.splitlines():
        if "target=" in line and "dedseccctv" in line.lower():
            target = line.split("target=", 1)[1].strip()
            subprocess.run(["cmdkey", f"/delete:{target}"], capture_output=True)
            print(f"Removed saved login: {target}")
            found += 1
    if not found:
        print("No saved logins found")
    print(f"{C['ok']}User config wiped. Your account on the server is untouched.{C['x']}")


def run_source() -> None:
    subprocess.run([python_bin(), str(ENTRY)], cwd=APP_DIR)


def run_exe() -> None:
    exe = OUT_DIR / EXE_NAME
    if not exe.is_file():
        print("No built exe yet - build first.")
        return
    subprocess.Popen([str(exe)], cwd=OUT_DIR)
    print(f"Started {exe}")


def run_installer() -> None:
    setups = sorted(INSTALLER_OUT.glob("DedSecCCTV-Setup-*.exe"), key=lambda p: p.stat().st_mtime) \
        if INSTALLER_OUT.is_dir() else []
    if not setups:
        print("No installer yet - build it first.")
        return
    subprocess.Popen([str(setups[-1])])
    print(f"Started {setups[-1].name}")



def open_folder(p: Path) -> None:
    if not p.exists():
        print(f"{p} does not exist yet.")
        return
    if os.name == "nt":
        os.startfile(str(p))
    else:
        print(p)


MENU = [
    ("1", "Full build (app + installer)",        lambda: build(False, True)),
    ("2", "Build app only (no installer)",       lambda: build(False, False)),
    ("3", "Debug build (console window, logs)",  lambda: build(True, False)),
    ("4", "Build installer only",                build_installer),
    ("5", "Clean old builds",                    clean_builds),
    ("6", "Clean + full fresh build",            lambda: (clean_builds(), build(False, True))),
    ("7", "Run app from source (app.py)",        run_source),
    ("8", "Run built exe",                       run_exe),
    ("9", "Run latest installer",                run_installer),
    ("11", "Open build output folder",           lambda: open_folder(OUT_DIR)),
    ("12", "Open installer output folder",       lambda: open_folder(INSTALLER_OUT)),
    ("13", "Rebuild updater (DedSecUpdater.exe)", lambda: (build_updater(True), add_updater())),
    ("14", "Wipe user config (test as new user)", wipe_user_config),
    ("15", "Make update zip (DedSecCCTV.zip)",   make_update_zip),
]


def menu() -> None:
    if os.name == "nt":
        os.system("")  # turn on colour codes in the Windows console
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(C["t"] + BANNER + C["x"])
        print(f"  {C['g']}DedSec Lab build tool  |  {status_line()}{C['x']}\n")
        for key, label, _ in MENU:
            print(f"  {C['t']}[{key:>2}]{C['x']} {label}")
        print(f"  {C['r']}[ 0]{C['x']} Exit\n")
        choice = input(f"  {C['b']}Select > {C['x']}").strip()
        if choice == "0":
            print("Bye.")
            return
        action = next((a for k, _, a in MENU if k == choice), None)
        if not action:
            continue
        if choice in ("5", "6", "14"):
            what = ("your DedSec CCTV settings and saved logins (close the app first)" if choice == "14"
                    else "the old exe, build and installer folders")
            ok = input(f"  {C['y']}This deletes {what}. Continue? (y/n) {C['x']}")
            if ok.strip().lower() != "y":
                continue
        print()
        try:
            action()
        except subprocess.CalledProcessError as e:
            print(f"{C['r']}Step failed (exit code {e.returncode}){C['x']}")
        except SystemExit as e:
            print(f"{C['r']}{e}{C['x']}")
        except KeyboardInterrupt:
            print(f"\n{C['y']}Cancelled{C['x']}")
        input(f"\n  {C['g']}Press Enter to go back to the menu...{C['x']}")


if __name__ == "__main__":
    # No arguments = menu. Old style still works: build.py --build [--no-installer] [--debug]
    if "--build" in sys.argv:
        build(debug="--debug" in sys.argv, installer="--no-installer" not in sys.argv)
        if "--zip" in sys.argv and "--debug" not in sys.argv:
            make_update_zip()
    elif "--zip" in sys.argv:
        make_update_zip()
    else:
        menu()
