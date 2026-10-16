#!/usr/bin/env python3
"""DedSec CCTV updater helper.

Started by the app after it downloaded and verified an update zip. Waits for the app to exit,
extracts the zip over the install folder and starts the app again.

    python updater.py --pid 1234 --zip %TEMP%\\DedSecCCTV-1.0.1.zip --target C:\\DedSecCCTV ^
                      --launch C:\\DedSecCCTV\\DedSecCCTV.exe [--sha256 <hex>] [--python <python.exe>]

Build for releases as DedSecUpdater.exe (single file, e.g. ``nuitka --onefile updater.py``). Since 1.0.3
build.py ships it inside the app as ``assets/updater/DedSecUpdater.bin`` (nothing extra in the install
root); the app copies it to %TEMP% as DedSecUpdater.exe before running it, so the updater can itself be
replaced by an update. 1.0.2 and older kept it at <install>/DedSecUpdater.exe.

Safety:
* never elevates (runs as the same user as the app);
* refuses zip entries with absolute paths, drive letters or ``..`` and never writes outside --target;
* never deletes anything (the one exception: the legacy root DedSecUpdater.exe once the bundled
  copy is installed): files not in the zip stay; existing files under ``streams/`` (the user's
  camera lists) are kept as they are, only new files are added there;
* every replaced file is backed up first and restored if anything fails, then the old app is
  started again;
* only starts --launch if it lies inside --target.
Log: %TEMP%\\DedSecUpdater.log

1.0.3: shows a small always-on-top progress window (tkinter, no console) with the current step,
a progress bar and, on failure, the error, the log path and Retry / Close. If tkinter or a display is
not available it runs headless exactly as before. The SHA-256 re-check and every zip safety check are
unchanged; the GUI only reports progress.
"""
from __future__ import annotations

import argparse
import hashlib
import queue
import threading
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

PRESERVE_DIRS = ("streams", "crashlogs")  # existing files here are never overwritten (crashlogs: app crash logs)
LEGACY_UPDATER = "DedSecUpdater.exe"   # install-root updater of 1.0.2 and older
BUNDLED_UPDATER = ("assets", "updater", "DedSecUpdater.bin")
# Set by a Nuitka onefile bootstrap (this updater); must not leak into the app we start.
_CHILD_ENV_DROP = ("NUITKA_ONEFILE_PARENT", "NUITKA_ONEFILE_START", "NUITKA_ONEFILE_TIME_US",
                   "NUITKA_ONEFILE_RANDOM", "NUITKA_ONEFILE_BINARY", "NUITKA_ONEFILE_DIRECTORY",
                   "NUITKA_PACKAGE_HOME")
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


def sha256_file(path: Path, progress=None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size if progress else 0
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            if progress:
                done += len(chunk)
                progress(done, total)
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


def apply_update(zip_path: Path, target: Path, progress=None) -> dict:
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
            members = plan_members(zf)
            total_bytes = sum(i.file_size for i, _ in members) or 1
            done_bytes = 0
            for info, parts in members:
                if progress:
                    progress(done_bytes, total_bytes)
                done_bytes += info.file_size
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
            if progress:
                progress(total_bytes, total_bytes)
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
    remove_legacy_updater(target)
    return stats


def remove_legacy_updater(target: Path) -> None:
    """Drop <install>/DedSecUpdater.exe once the bundled copy exists (we run from a %TEMP% copy)."""
    legacy = target / LEGACY_UPDATER
    if legacy.is_file() and target.joinpath(*BUNDLED_UPDATER).is_file():
        try:
            _retry(legacy.unlink, attempts=5)
            log(f"removed legacy {legacy.name}")
        except OSError as exc:
            log(f"could not remove legacy {legacy.name}: {exc}")


# ---------------------------------------------------------------------------
# relaunch
# ---------------------------------------------------------------------------

def launch_app(launch: Path, target: Path, python: str | None) -> None:
    launch = launch.resolve()
    if not launch.is_file():  # old builds passed python.exe; fall back to the app exe
        alt = target.resolve() / "DedSecCCTV.exe"
        if alt.is_file():
            launch = alt
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
    env = {k: v for k, v in os.environ.items() if k.upper() not in _CHILD_ENV_DROP}
    kw: dict = {"cwd": str(target), "close_fds": True, "env": env,
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


# ---------------------------------------------------------------------------
# progress reporting (headless or GUI)
# ---------------------------------------------------------------------------

STEPS = ("Waiting for app to close", "Verifying", "Installing files", "Restarting")


class Reporter:
    """Thread-safe progress sink. The GUI drains ``q``; headless mode just logs."""

    def __init__(self) -> None:
        self.q: "queue.Queue[tuple]" = queue.Queue()

    def step(self, idx: int, detail: str = "") -> None:
        log(f"step: {STEPS[idx]} {detail}".rstrip())
        self.q.put(("step", idx, detail))

    def progress(self, done: int, total: int) -> None:
        self.q.put(("progress", done, total))

    def done(self, ok: bool, err: str = "") -> None:
        self.q.put(("done", ok, err))


def run_install(a, reporter: Reporter, wait: bool = True) -> None:
    """Wait for the app, re-check the SHA-256 and apply the zip. Raises on any failure.

    Same checks, same order and same strictness as 1.0.3's main()."""
    zip_path, target = Path(a.zip), Path(a.target)
    if wait:
        reporter.step(0)
        if not wait_for_exit(a.pid, a.timeout):
            raise UpdateFailed("the app did not exit")
    if not zip_path.is_file():
        raise UpdateFailed(f"zip not found: {zip_path}")
    reporter.step(1)
    if a.sha256 and sha256_file(zip_path, reporter.progress) != a.sha256.strip().lower():
        raise UpdateFailed("zip SHA-256 mismatch")
    reporter.step(2)
    stats = apply_update(zip_path, target, reporter.progress)
    log(f"update applied: {stats}")
    try:
        zip_path.unlink()
    except OSError:
        pass


def _start_app(a) -> str:
    """Start the app; returns an error text ('' on success)."""
    if a.no_launch:
        return ""
    try:
        launch_app(Path(a.launch), Path(a.target), a.python or None)
    except Exception as exc:
        log(f"ERROR starting the app: {exc}")
        return str(exc)
    return ""


def _zip_version(zip_path) -> str:
    """Version from the app's download name (DedSecCCTV-<version>.zip); '' if it doesn't match."""
    import re
    m = re.fullmatch(r"DedSecCCTV-([0-9A-Za-z.+-]{1,32})\.zip", Path(str(zip_path)).name)
    return m.group(1) if m else ""


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# Standard Windows installer look (like the Inno Setup wizard): white header with the logo, light
# body, Segoe UI, native ttk progress bar and buttons ("vista" theme on Windows).
C_BODY, C_HEAD, C_LINE = "#f0f0f0", "#ffffff", "#d9d9d9"
C_TEXT, C_MUTED, C_ERR, C_OK = "#1b1b1b", "#555555", "#c42b1c", "#0f7b0f"


class ProgressWindow:
    """Small, centred, always-on-top window with the normal Windows title bar (and taskbar button)."""

    W, H = 460, 260

    def __init__(self, a, tk, ttk, font_mod) -> None:
        self.a, self.tk = a, tk
        self.reporter = Reporter()
        self.result = 2
        self._running = True
        self.root = root = tk.Tk()
        root.title("DedSec CCTV Update")
        root.configure(bg=C_BODY)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{(sh - self.H) // 3}")
        # Title-bar close: ignored while installing; on an error screen it acts like Close.
        root.protocol("WM_DELETE_WINDOW", self._wm_close)
        self._icon = self._load_logo(Path(a.target), 32)
        if self._icon is not None:
            try:
                root.iconphoto(True, self._icon)
            except Exception:
                pass
        fam = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
        self.f_head = font_mod.Font(family=fam, size=11, weight="bold")
        self.f_body = font_mod.Font(family=fam, size=9)
        self.f_small = font_mod.Font(family=fam, size=8)
        style = ttk.Style(root)
        for theme in (("vista", "winnative") if sys.platform == "win32" else ()) + ("clam",):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        if style.theme_use() == "clam":  # non-Windows fallback: approximate the Windows look
            style.configure("Horizontal.TProgressbar", troughcolor="#e6e6e6", background="#06b025",
                            bordercolor="#bcbcbc", lightcolor="#06b025", darkcolor="#06b025")
            style.configure("TButton", background="#fdfdfd", bordercolor="#adadad", font=self.f_body, padding=(10, 2))
            style.map("TButton", background=[("active", "#e5f1fb")], bordercolor=[("active", "#0078d4")])
        style.configure("TButton", font=self.f_body, width=10)

        head = tk.Frame(root, bg=C_HEAD, height=58)
        head.pack(fill="x")
        head.pack_propagate(False)
        self._logo = self._load_logo(Path(a.target), 44)
        if self._logo is not None:
            tk.Label(head, image=self._logo, bg=C_HEAD).pack(side="right", padx=(0, 14))
        htxt = tk.Frame(head, bg=C_HEAD)
        htxt.pack(side="left", fill="both", expand=True, padx=(18, 0), pady=(9, 0))
        self.head = tk.Label(htxt, text="Installing update", fg=C_TEXT, bg=C_HEAD, font=self.f_head, anchor="w")
        self.head.pack(fill="x")
        self.subhead = tk.Label(htxt, text="Please wait while DedSec CCTV is updated.", fg=C_MUTED, bg=C_HEAD,
                                font=self.f_body, anchor="w")
        self.subhead.pack(fill="x")
        tk.Frame(root, bg=C_LINE, height=1).pack(fill="x")

        self.btns = tk.Frame(root, bg=C_BODY)
        self.btns.pack(side="bottom", fill="x", padx=12, pady=(0, 12))
        tk.Frame(root, bg=C_LINE, height=1).pack(side="bottom", fill="x", pady=(0, 10))
        self.retry_btn = ttk.Button(self.btns, text="Retry", command=self._retry, default="active")
        self.close_btn = ttk.Button(self.btns, text="Close", command=self._close)
        self.cancel_ph = ttk.Button(self.btns, text="Close", state="disabled")  # greyed while installing
        self.cancel_ph.pack(side="right")

        body = tk.Frame(root, bg=C_BODY)
        body.pack(fill="both", expand=True, padx=22, pady=(14, 0))
        self.step_lbl = tk.Label(body, text=STEPS[0] + "\u2026", fg=C_TEXT, bg=C_BODY, font=self.f_body, anchor="w")
        self.step_lbl.pack(fill="x", pady=(0, 6))
        self.pbar = ttk.Progressbar(body, mode="indeterminate", maximum=1000)
        self.pbar.pack(fill="x", ipady=1)
        self.pbar.start(12)
        self.detail = tk.Label(body, text="", fg=C_MUTED, bg=C_BODY, font=self.f_small, anchor="w")
        self.detail.pack(fill="x", pady=(4, 0))
        self.err = tk.Label(body, text="", fg=C_ERR, bg=C_BODY, font=self.f_body, anchor="nw", justify="left",
                            wraplength=self.W - 50)
        self.logl = tk.Label(body, text="", fg=C_MUTED, bg=C_BODY, font=self.f_small, anchor="w", justify="left",
                             wraplength=self.W - 50)
        self._indeterminate = True

    def _load_logo(self, target: Path, px: int):
        for name in ("dedsec_logo.png", "applogo.png"):
            p = target / "assets" / name
            try:
                if p.is_file():
                    img = self.tk.PhotoImage(file=str(p))
                    f = max(1, -(-max(img.width(), img.height()) // px))
                    return img.subsample(f, f)
            except Exception:
                pass
        return None

    def _wm_close(self) -> None:
        if not self._running:
            self._close()

    # -- worker -------------------------------------------------------------------
    def start(self, wait: bool = True) -> None:
        self._running = True
        def work():
            try:
                run_install(self.a, self.reporter, wait=wait)
            except Exception as exc:
                log(f"ERROR: {exc}")
                self.reporter.done(False, str(exc))
                return
            self.reporter.done(True)
        threading.Thread(target=work, daemon=True).start()

    def poll(self) -> None:
        try:
            while True:
                self._handle(self.reporter.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def _set_determinate(self) -> None:
        if self._indeterminate:
            self.pbar.stop()
            self.pbar.configure(mode="determinate")
            self._indeterminate = False

    def _handle(self, msg) -> None:
        kind = msg[0]
        if kind == "step":
            idx = msg[1]
            self.step_lbl.configure(text=f"{STEPS[idx]}\u2026", fg=C_TEXT)
            self.detail.configure(text=f"Step {idx + 1} of {len(STEPS)}")
            if idx == 0:
                self.pbar.configure(mode="indeterminate")
                self.pbar.start(12)
                self._indeterminate = True
            else:
                self._set_determinate()
                self.pbar["value"] = 0
        elif kind == "progress":
            done, total = msg[1], msg[2]
            if total:
                self._set_determinate()
                self.pbar["value"] = int(done * 1000 / total)
                pct = min(100, done * 100 // total)
                self.detail.configure(text=f"{pct}%  \u00b7  {_fmt_bytes(done)} / {_fmt_bytes(total)}")
        elif kind == "done":
            self._running = False
            if msg[1]:
                self._finish_ok()
            else:
                self._show_error(msg[2])

    SUCCESS_MS = 2000  # how long "Update complete" stays up before the app is restarted

    def _finish_ok(self) -> None:
        """Success state: green tick, 'Update complete', full bar; relaunch after ~2 s."""
        self.result = 0
        self._running = True  # title-bar close stays ignored until the app is started
        self._set_determinate()
        self.pbar["value"] = 1000
        ver = _zip_version(self.a.zip)
        self.head.configure(text="\u2714  Update complete", fg=C_OK)
        self.subhead.configure(text="The update was installed successfully.")
        self.step_lbl.configure(text=f"DedSec CCTV {ver + ' ' if ver else ''}installed. Restarting\u2026", fg=C_TEXT)
        self.detail.configure(text="100%")
        log(f"step: {STEPS[3]}")
        self.root.after(self.SUCCESS_MS, self._relaunch)

    def _relaunch(self) -> None:
        err = _start_app(self.a)
        if err:
            self.result = 3
            self._running = False
            self.head.configure(fg=C_TEXT)
            self._show_error(f"DedSec CCTV could not be restarted: {err}", can_retry=False, launch_on_close=False)
            return
        self.root.after(300, self.root.destroy)

    def _show_error(self, text: str, can_retry: bool = True, launch_on_close: bool = True) -> None:
        self._launch_on_close = launch_on_close
        self.pbar.stop()
        self.pbar.pack_forget()
        self.detail.pack_forget()
        self.head.configure(text="Update failed")
        self.subhead.configure(text="Nothing was changed: your files were restored." if can_retry
                               else "The update itself was installed.")
        self.step_lbl.pack_forget()
        self.err.configure(text=text)
        self.err.pack(fill="x")
        self.logl.configure(text=f"Log: {LOG_PATH}")
        self.logl.pack(fill="x", pady=(6, 0))
        self.cancel_ph.pack_forget()
        self.close_btn.pack(side="right")
        if can_retry:
            self.retry_btn.pack(side="right", padx=(0, 8))
            self.retry_btn.focus_set()
        else:
            self.retry_btn.pack_forget()

    def _retry(self) -> None:
        for w in (self.err, self.logl, self.retry_btn, self.close_btn):
            w.pack_forget()
        self.cancel_ph.pack(side="right")
        self.head.configure(text="Installing update")
        self.subhead.configure(text="Please wait while DedSec CCTV is updated.")
        self.step_lbl.pack(fill="x", pady=(0, 6))
        self.pbar.pack(fill="x", ipady=1)
        self.detail.pack(fill="x", pady=(6, 0))
        log("retry requested")
        self.start(wait=False)  # the app is already gone

    def _close(self) -> None:
        if getattr(self, "_launch_on_close", True):
            # same as 1.0.3: after a failed update the previous version is started again
            self.head.configure(text="Starting previous version")
            self._running = True  # ignore further title-bar clicks
            if _start_app(self.a):
                self.result = 3
        self.root.destroy()

    def run(self) -> int:
        self.start()
        self.root.after(50, self.poll)
        self.root.mainloop()
        return self.result


def _make_gui(a):
    if os.environ.get("DEDSEC_UPDATER_HEADLESS", "").strip() == "1":
        return None
    try:
        import tkinter as tk
        from tkinter import font as font_mod, ttk
        return ProgressWindow(a, tk, ttk, font_mod)
    except Exception as exc:  # no tkinter / no display: fall back to the 1.0.3 behaviour
        log(f"progress window unavailable ({exc.__class__.__name__}: {exc}); running headless")
        return None


def main_headless(a) -> int:
    ok = False
    try:
        run_install(a, Reporter())
        ok = True
    except Exception as exc:
        log(f"ERROR: {exc}")
        _message(f"The update could not be installed:\n{exc}\n\nThe previous version will be started.\n\n"
                 f"Log: {LOG_PATH}")
    err = _start_app(a)
    if err:
        _message(f"DedSec CCTV could not be restarted:\n{err}")
        return 3
    return 0 if ok else 2


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
    log(f"updater start: pid={a.pid} zip={a.zip} target={a.target}")
    gui = _make_gui(a)
    if gui is None:
        return main_headless(a)
    return gui.run()


if __name__ == "__main__":
    raise SystemExit(main())
