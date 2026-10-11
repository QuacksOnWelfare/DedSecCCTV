#!/usr/bin/env python3
"""DedSec CCTV updater helper.

Started by the app after it downloaded and verified an update zip. Waits for the app to exit,
extracts the zip over the install folder and starts the app again.

    python updater.py --pid 1234 --zip %TEMP%\\DedSecCCTV-1.0.1.zip --target C:\\DedSecCCTV ^
                      --launch C:\\DedSecCCTV\\DedSecCCTV.exe [--sha256 <hex>] [--python <python.exe>]

Build for releases as DedSecUpdater.exe (single file, e.g. ``nuitka --onefile updater.py``) and ship
it next to DedSecCCTV.exe. The app copies it to %TEMP% before running it, so the updater can
itself be replaced by an update.

Safety:
* never elevates (runs as the same user as the app);
* refuses zip entries with absolute paths, drive letters or ``..`` and never writes outside --target;
* never deletes anything: files not in the zip stay; existing files under ``user_streams/`` (the
  user's camera lists) are kept as they are, only new files are added there;
* every replaced file is backed up first and restored if anything fails, then the old app is
  started again;
* only starts --launch if it lies inside --target.
Log: %TEMP%\\DedSecUpdater.log
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

PRESERVE_DIRS = ("user_streams", "streams",)          # existing files here are never overwritten
APP_MARKERS = ("DedSecCCTV.exe", "app.py")
LOG_PATH = Path(tempfile.gettempdir()) / "DedSecUpdater.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:  # pythonw / windowed exe has no stdout
        pass


# ---------------------------------------------------------------------------
# waiting for the app
# ---------------------------------------------------------------------------

def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie (exited, not yet reaped) counts as gone
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def wait_for_exit(pid: int, timeout: float) -> bool:
    """Wait until ``pid`` exits; terminate it after ``timeout`` s. True if it is gone."""
    if pid <= 0 or pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        SYNCHRONIZE, PROCESS_TERMINATE = 0x00100000, 0x0001
        h = k32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid)
        if not h:
            return True  # already gone
        try:
            if k32.WaitForSingleObject(h, int(timeout * 1000)) == 0:
                return True
            log(f"app (pid {pid}) still running after {timeout:.0f}s; terminating it")
            k32.TerminateProcess(h, 1)
            return k32.WaitForSingleObject(h, 10_000) == 0
        finally:
            k32.CloseHandle(h)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive_posix(pid):
            return True
        time.sleep(0.2)
    log(f"app (pid {pid}) still running after {timeout:.0f}s; terminating it")
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if not _pid_alive_posix(pid):
                return True
            time.sleep(0.2)
    return not _pid_alive_posix(pid)


# ---------------------------------------------------------------------------
# zip handling
# ---------------------------------------------------------------------------

class UpdateFailed(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_parts(name: str) -> tuple[str, ...]:
    """Validate a zip member name; return its path parts."""
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    parts = tuple(x for x in p.parts if x not in ("", "."))
    if (name.startswith("/") or not parts or any(x == ".." for x in parts)
            or ":" in parts[0] or any(x.endswith(" ") or x.endswith(".") and x != "." for x in parts)):
        raise UpdateFailed(f"unsafe path in zip: {name!r}")
    return parts


def plan_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    """(member, relative parts) for every file, with a single wrapping folder stripped."""
    files = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:  # symlink
            raise UpdateFailed(f"symlink in zip: {info.filename!r}")
        files.append((info, _safe_parts(info.filename)))
    if not files:
        raise UpdateFailed("the update zip is empty")
    tops = {parts[0] for _, parts in files}
    if len(tops) == 1 and all(len(parts) > 1 for _, parts in files):
        top = next(iter(tops))
        if any(parts[1:] in ((m,) for m in APP_MARKERS) for _, parts in files):
            files = [(i, parts[1:]) for i, parts in files]  # DedSecCCTV/... -> ...
    return files


def _retry(fn, attempts: int = 20, delay: float = 0.5):
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:  # Windows: file still locked by a dying process / AV scanner
            if i == attempts - 1:
                raise
            time.sleep(delay)


def apply_update(zip_path: Path, target: Path) -> dict:
    """Extract over ``target`` with backup/rollback. Returns counters."""
    target = target.resolve()
    if not target.is_dir():
        raise UpdateFailed(f"install folder not found: {target}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target / ".dedsec-update-backup" / stamp
    replaced: list[tuple[Path, Path]] = []   # (installed file, backup copy)
    created: list[Path] = []
    stats = {"written": 0, "kept": 0, "new": 0}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise UpdateFailed(f"corrupt file in zip: {bad}")
            for info, parts in plan_members(zf):
                dest = target.joinpath(*parts).resolve()
                if target not in dest.parents:
                    raise UpdateFailed(f"zip entry escapes the install folder: {info.filename!r}")
                if parts[0].lower() in PRESERVE_DIRS and dest.exists():
                    stats["kept"] += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    bk = backup.joinpath(*parts)
                    bk.parent.mkdir(parents=True, exist_ok=True)
                    _retry(lambda: shutil.copy2(dest, bk))
                    replaced.append((dest, bk))
                else:
                    created.append(dest)
                    stats["new"] += 1
                tmp = dest.with_name(dest.name + ".dedsec-new")

                def write(info=info, tmp=tmp, dest=dest):
                    with zf.open(info) as src, open(tmp, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    os.replace(tmp, dest)
                _retry(write)
                stats["written"] += 1
    except BaseException:
        log("update failed, rolling back")
        for dest, bk in reversed(replaced):
            try:
                _retry(lambda: shutil.copy2(bk, dest))
            except Exception as exc:  # pragma: no cover
                log(f"rollback failed for {dest}: {exc}")
        for dest in created:
            try:
                dest.unlink()
            except OSError:
                pass
        for leftover in target.rglob("*.dedsec-new"):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    shutil.rmtree(backup.parent, ignore_errors=True)
    return stats


# ---------------------------------------------------------------------------
# relaunch
# ---------------------------------------------------------------------------

def launch_app(launch: Path, target: Path, python: str | None) -> None:
    launch = launch.resolve()
    if target.resolve() not in launch.parents:
        raise UpdateFailed(f"refusing to start {launch}: not inside {target}")
    if not launch.is_file():
        raise UpdateFailed(f"app not found after update: {launch}")
    if launch.suffix.lower() in (".py", ".pyw"):
        interp = python or sys.executable
        if getattr(sys, "frozen", False) and not python:
            raise UpdateFailed("no Python interpreter given for a source install (--python)")
        cmd = [interp, str(launch)]
    else:
        cmd = [str(launch)]
    kw: dict = {"cwd": str(target), "close_fds": True,
                "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    log(f"started {cmd}")


def _message(text: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "DedSec CCTV update", 0x10)  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DedSec CCTV updater")
    ap.add_argument("--pid", type=int, required=True, help="process id of the running app")
    ap.add_argument("--zip", required=True, help="verified update zip")
    ap.add_argument("--target", required=True, help="install folder")
    ap.add_argument("--launch", required=True, help="exe (or app.py) to start afterwards, inside --target")
    ap.add_argument("--sha256", default="", help="expected SHA-256 of the zip (checked again)")
    ap.add_argument("--python", default="", help="interpreter for a .py --launch")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds to wait before terminating the app")
    ap.add_argument("--no-launch", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    zip_path, target, launch = Path(a.zip), Path(a.target), Path(a.launch)
    log(f"updater start: pid={a.pid} zip={zip_path} target={target}")
    ok = False
    try:
        if not wait_for_exit(a.pid, a.timeout):
            raise UpdateFailed("the app did not exit")
        if not zip_path.is_file():
            raise UpdateFailed(f"zip not found: {zip_path}")
        if a.sha256 and sha256_file(zip_path) != a.sha256.strip().lower():
            raise UpdateFailed("zip SHA-256 mismatch")
        stats = apply_update(zip_path, target)
        log(f"update applied: {stats}")
        ok = True
        try:
            zip_path.unlink()
        except OSError:
            pass
    except Exception as exc:
        log(f"ERROR: {exc}")
        _message(f"The update could not be installed:\n{exc}\n\nThe previous version will be started.")
    if not a.no_launch:
        try:
            launch_app(launch, target, a.python or None)
        except Exception as exc:
            log(f"ERROR starting the app: {exc}")
            _message(f"DedSec CCTV could not be restarted:\n{exc}")
            return 3
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
