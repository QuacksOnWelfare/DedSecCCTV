"""DedSec CCTV auth client -- drop-in module for the PyQt6 desktop app.

Talks to the DedSec auth backend over HTTPS, keeps the short-lived access token in
memory only, and stores the long-lived (rotating) refresh token in the OS keyring
(Windows Credential Manager / macOS Keychain / Secret Service). If no keyring
backend is available it falls back to an encrypted file under %APPDATA%/DedSecCctv
(DPAPI on Windows, Fernet + 0600 key file elsewhere).

Basic use
---------
    from dedsec_auth_client import AuthClient, TotpRequired, AuthError

    auth = AuthClient("https://auth.example.com")
    if auth.try_resume():                    # silent login with the stored refresh token
        user = auth.current_user()
    else:
        try:
            auth.login(email, password)
        except TotpRequired:
            auth.submit_totp(code_from_user)  # 6-digit code from the authenticator app
    auth.logout()

Replacing the local LockScreen in app.py
----------------------------------------
Today app.py shows a ``LockScreen`` that checks a TOTP code against the secret in
``%APPDATA%/DedSecCctv/lock.json``. Swap it for a login dialog backed by this client::

    from PyQt6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton,
                                 QLabel, QInputDialog, QMessageBox)
    from dedsec_auth_client import AuthClient, AuthError, TotpRequired

    AUTH = AuthClient("https://auth.example.com")

    class LoginDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("DedSec CCTV -- Sign in")
            self.email = QLineEdit(); self.pw = QLineEdit()
            self.pw.setEchoMode(QLineEdit.EchoMode.Password)
            self.err = QLabel(); btn = QPushButton("Sign in")
            form = QFormLayout(self)
            form.addRow("Email", self.email); form.addRow("Password", self.pw)
            form.addRow(self.err); form.addRow(btn)
            btn.clicked.connect(self._submit)

        def _submit(self):
            try:
                AUTH.login(self.email.text(), self.pw.text())
            except TotpRequired:
                code, ok = QInputDialog.getText(self, "Two-factor", "6-digit code:")
                if not ok:
                    return
                try:
                    AUTH.submit_totp(code)
                except AuthError as e:
                    self.err.setText(str(e)); return
            except AuthError as e:
                self.err.setText(str(e)); return
            finally:
                self.pw.clear()
            self.accept()

    # in main(), where LockScreen used to be shown:
    if not AUTH.try_resume():
        if LoginDialog().exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
    window = MainWindow(); window.show()

Network calls block, so for a snappy UI run ``login``/``submit_totp`` in a
``QThread``/``QThreadPool`` worker; the client is thread-safe. To migrate an existing
authenticator entry, read the old secret from lock.json after login and pass it to
``setup_2fa(existing_secret=...)`` followed by ``enable_2fa(code)``, then delete lock.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

__all__ = ["AuthClient", "AuthError", "TotpRequired", "DeviceLocked", "UpdateRequiredError", "hardware_id", "TokenStore", "User", "SavedLogin", "SavedCredentials",
           "hardware_id", "DEVICE_LOCKED_STATUS"]

APP_DIR_NAME = "DedSecCctv"
KEYRING_SERVICE = "DedSecCCTV-auth"
LOGIN_KEYRING_SERVICE = "DedSecCCTV-login"  # "Remember me" credentials (OS store only)

# Sent on every request so the server can refuse versions older than its MIN_APP_VERSION.
APP_VERSION_HEADER = "X-DedSec-App-Version"
APP_VERSION = "1.0.2"  # default only; app.py passes its own APP_VERSION to AuthClient(app_version=...)
UPDATE_REQUIRED_STATUS = 426
UPDATE_REQUIRED_MSG = "This version of DedSec CCTV is out of date. Please update to keep using the app."
_VERSION_HEADER_RE = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")


class AuthError(Exception):
    """Any authentication failure. ``status`` is the HTTP status (0 = network error)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class UpdateRequiredError(AuthError):
    """The server refuses this app version (HTTP 426 ``update_required``); the user must update.

    ``min_version`` is the oldest version the server still accepts ("" if it didn't say).
    """

    def __init__(self, message: str = "", status: int = 426, min_version: str = "") -> None:
        super().__init__(message or UPDATE_REQUIRED_MSG, status)
        self.min_version = min_version


class TotpRequired(AuthError):
    """Raised by ``login()`` when the account has 2FA; call ``submit_totp(code)`` next."""


class DeviceLocked(AuthError):
    """The account is bound to another PC (HTTP 403 ``X-Error-Code: device_mismatch``).

    Offer ``request_device_reset(email, password)`` -> emailed code -> ``confirm_device_reset``.
    """


@dataclass(frozen=True)
class User:
    id: str
    email: str
    email_verified: bool
    totp_enabled: bool
    created_at: str
    device_bound: bool = False


# ---------------------------------------------------------------------------
# hardware id (one PC per account)
# ---------------------------------------------------------------------------

DEVICE_HEADER = "X-Device-Id"
DEVICE_LOCKED_STATUS = 403  # with X-Error-Code: device_mismatch
DEVICE_MISMATCH_MSG = "This account is locked to another PC. Request a device reset."
HWID_LENGTH = 32
_JUNK_IDS = {"", "none", "default string", "to be filled by o.e.m.", "not applicable",
             "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"}
_HWID_CACHE: str | None = None
_HWID_LOCK = threading.Lock()


def _clean_id(value: Any) -> str:
    v = str(value or "").strip().strip("{}").lower()
    return "" if v in _JUNK_IDS or len(set(v.replace("-", ""))) <= 1 else v


def _win_machine_guid() -> str:
    """HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (64-bit view). Never sent anywhere."""
    try:
        import winreg
        flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, flags) as k:
            return _clean_id(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:
        return ""


def _win_volume_serial() -> str:
    """Serial number of the system drive's volume (e.g. C:\\), as 8 hex digits."""
    try:
        import ctypes
        serial = ctypes.c_uint32(0)
        root = (os.environ.get("SystemDrive") or "C:") + "\\"
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), None, 0,  # type: ignore[attr-defined]
                                                          ctypes.byref(serial), None, None, None, 0)
        return f"{serial.value:08x}" if ok and serial.value else ""
    except Exception:
        return ""


def _is_wine() -> bool:
    try:
        import ctypes
        return hasattr(ctypes.windll.ntdll, "wine_get_version")  # type: ignore[attr-defined]
    except Exception:
        return False


def compute_hwid(machine_guid: str, volume_serial: str) -> str:
    """SHA-256 hex of (MachineGuid + system volume serial), truncated to 32 chars."""
    raw = f"dedsec-hwid-v1|{machine_guid.strip().lower()}|{volume_serial.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:HWID_LENGTH]


def fallback_hwid(hostname: str, username: str) -> str:
    """Non-Windows / Wine / unreadable registry: hash of hostname + user name."""
    raw = f"dedsec-hwid-fallback-v1|{hostname.strip().lower()}|{username.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:HWID_LENGTH]


def _username() -> str:
    for key in ("USERNAME", "USER", "LOGNAME"):
        if os.environ.get(key):
            return os.environ[key]
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return ""


def hardware_id() -> str:
    """Stable per-PC device id (32 hex chars). Only this hash is ever sent (``X-Device-Id``).

    Windows: SHA-256 of MachineGuid + system-volume serial. Fallback (non-Windows, Wine, or
    either value unreadable): SHA-256 of hostname + user name.
    """
    global _HWID_CACHE
    with _HWID_LOCK:
        if _HWID_CACHE is None:
            guid = serial = ""
            if sys.platform == "win32" and not _is_wine():
                guid, serial = _win_machine_guid(), _win_volume_serial()
            if guid and serial:
                _HWID_CACHE = compute_hwid(guid, serial)
            else:
                import socket
                _HWID_CACHE = fallback_hwid(socket.gethostname(), _username())
        return _HWID_CACHE


# ---------------------------------------------------------------------------
# secure refresh-token storage
# ---------------------------------------------------------------------------

def _app_dir() -> Path:
    base = os.environ.get("APPDATA") if sys.platform == "win32" else os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / (".config" if sys.platform != "win32" else "AppData/Roaming")
    d = root / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class _Dpapi:
    """Windows DPAPI via ctypes: data is bound to the current Windows user account."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._ct, self._BLOB = ctypes, BLOB
        self._crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._entropy = b"DedSecCCTV-refresh-token-v1"

    def _blob(self, data: bytes) -> Any:
        buf = self._ct.create_string_buffer(data, len(data))
        return self._BLOB(len(data), self._ct.cast(buf, self._ct.POINTER(self._ct.c_char))), buf

    def _call(self, fn: Any, data: bytes) -> bytes:
        inp, _b1 = self._blob(data)
        ent, _b2 = self._blob(self._entropy)
        out = self._BLOB()
        if not fn(self._ct.byref(inp), None, self._ct.byref(ent), None, None, 0x01, self._ct.byref(out)):
            raise OSError("DPAPI call failed")
        try:
            return self._ct.string_at(out.pbData, out.cbData)
        finally:
            self._kernel32.LocalFree(out.pbData)

    def protect(self, data: bytes) -> bytes:
        return self._call(self._crypt32.CryptProtectData, data)

    def unprotect(self, data: bytes) -> bytes:
        # CryptUnprotectData has an extra ppszDataDescr out-param in position 2; passing None is fine.
        return self._call(self._crypt32.CryptUnprotectData, data)


class _EncryptedFileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._dpapi: _Dpapi | None = None
        if sys.platform == "win32":
            try:
                self._dpapi = _Dpapi()
            except Exception:
                self._dpapi = None

    def _fernet(self) -> Any:
        from cryptography.fernet import Fernet
        key_path = self.path.with_suffix(".key")
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return Fernet(key_path.read_bytes().strip())

    def _encrypt(self, data: bytes) -> bytes:
        return b"D1" + self._dpapi.protect(data) if self._dpapi else b"F1" + self._fernet().encrypt(data)

    def _decrypt(self, blob: bytes) -> bytes:
        tag, body = blob[:2], blob[2:]
        if tag == b"D1" and self._dpapi:
            return self._dpapi.unprotect(body)
        if tag == b"F1":
            return self._fernet().decrypt(body)
        raise ValueError("unknown token file format")

    def get(self, account: str) -> str | None:
        try:
            data = json.loads(self._decrypt(self.path.read_bytes()))
            return data.get(account)
        except Exception:
            return None

    def set(self, account: str, value: str | None) -> None:
        try:
            data = json.loads(self._decrypt(self.path.read_bytes()))
        except Exception:
            data = {}
        if value is None:
            data.pop(account, None)
        else:
            data[account] = value
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._encrypt(json.dumps(data).encode()))
        os.replace(tmp, self.path)


def _os_keyring() -> Any:
    """Return the ``keyring`` module if a real OS credential store is usable, else ``None``.

    Frozen builds (Nuitka/PyInstaller) can lose keyring's entry-point metadata, so backend
    auto-detection falls back to the "fail" backend; on Windows select Credential Manager
    (WinVaultKeyring) explicitly in that case. Never raises.
    """
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring) and sys.platform == "win32":
            from keyring.backends import Windows
            keyring.set_keyring(Windows.WinVaultKeyring())
        if not isinstance(keyring.get_keyring(), fail.Keyring):
            return keyring
    except Exception:
        pass
    return None


class TokenStore:
    """Refresh-token storage: OS keyring first, encrypted file as fallback."""

    def __init__(self, account: str, use_keyring: bool = True, file_path: Path | None = None) -> None:
        self.account = account
        self._keyring: Any = _os_keyring() if use_keyring else None
        self._file = _EncryptedFileStore(file_path or _app_dir() / "session.bin")

    @property
    def backend(self) -> str:
        return "keyring" if self._keyring else "encrypted-file"

    def load(self) -> str | None:
        if self._keyring:
            try:
                return self._keyring.get_password(KEYRING_SERVICE, self.account)
            except Exception:
                pass
        return self._file.get(self.account)

    def save(self, token: str) -> None:
        if self._keyring:
            try:
                self._keyring.set_password(KEYRING_SERVICE, self.account, token)
                return
            except Exception:
                pass
        self._file.set(self.account, token)

    def clear(self) -> None:
        if self._keyring:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, self.account)
            except Exception:
                pass
        self._file.set(self.account, None)


# ---------------------------------------------------------------------------
# "Remember me": saved sign-in credentials
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SavedCredentials:
    email: str
    password: str | None  # None when only the email could be remembered
    remember: bool = True

    def __repr__(self) -> str:  # never leak the password into logs/tracebacks
        return f"SavedCredentials(email={self.email!r}, password={'***' if self.password else None}, " \
               f"remember={self.remember})"


class SavedLogin:
    """Remembered email + password for the sign-in form.

    * The password is stored **only** in the OS credential store (Windows Credential
      Manager / macOS Keychain / Secret Service) as one entry under
      ``LOGIN_KEYRING_SERVICE``. If no OS keyring is usable it is simply not saved;
      there is deliberately no file fallback for the password.
    * The email (non-secret) also goes into ``remember.json`` in the app data folder
      (``%APPDATA%/DedSecCctv`` on Windows) as ``{"email": ..., "remember": true}`` so it
      can be prefilled even when the keyring is unavailable.

    Every method swallows errors: a broken keyring must never crash the app.
    """

    ACCOUNT = "remembered-login"

    def __init__(self, json_path: Path | None = None, use_keyring: bool = True) -> None:
        self._json_path = json_path
        self._keyring: Any = _os_keyring() if use_keyring else None

    @property
    def can_store_password(self) -> bool:
        return self._keyring is not None

    @property
    def json_path(self) -> Path:
        return self._json_path or _app_dir() / "remember.json"

    # -- keyring ---------------------------------------------------------------
    def _kr_get(self) -> dict[str, Any] | None:
        if not self._keyring:
            return None
        try:
            raw = self._keyring.get_password(LOGIN_KEYRING_SERVICE, self.ACCOUNT)
            data = json.loads(raw) if raw else None
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _kr_set(self, email: str, password: str) -> bool:
        if not self._keyring:
            return False
        try:
            self._keyring.set_password(LOGIN_KEYRING_SERVICE, self.ACCOUNT,
                                       json.dumps({"email": email, "password": password}))
            return True
        except Exception:
            return False

    def _kr_delete(self) -> None:
        if not self._keyring:
            return
        try:
            self._keyring.delete_password(LOGIN_KEYRING_SERVICE, self.ACCOUNT)
        except Exception:
            pass

    # -- remember.json (email only, never the password) -----------------------
    def _json_get(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _json_set(self, email: str) -> None:
        try:
            path = self.json_path
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"email": email, "remember": True}), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass

    def _json_delete(self) -> None:
        try:
            self.json_path.unlink(missing_ok=True)
        except Exception:
            pass

    # -- public API ------------------------------------------------------------
    def load(self) -> SavedCredentials | None:
        """Return the remembered login, or ``None`` if nothing is remembered."""
        meta = self._json_get() or {}
        email = str(meta.get("email") or "") if meta.get("remember") else ""
        secret = self._kr_get() or {}
        kr_email = str(secret.get("email") or "")
        password = secret.get("password") if isinstance(secret.get("password"), str) else None
        if not email:
            email = kr_email
        if kr_email and kr_email.lower() != email.lower():
            password = None  # stale keyring entry for another account: don't pair it
        if not email:
            return None
        return SavedCredentials(email=email, password=password or None, remember=True)

    def save(self, email: str, password: str) -> bool:
        """Remember ``email`` (and ``password`` in the OS keyring only).

        Returns True if the password was stored, False if only the email could be
        remembered (no usable OS keyring). Never raises.
        """
        email = (email or "").strip()
        if not email:
            return False
        self._json_set(email)
        if password and self._kr_set(email, password):
            return True
        self._kr_delete()  # don't leave an older password behind
        return False

    def forget_password(self) -> None:
        self._kr_delete()

    def clear(self) -> None:
        """Forget the remembered email and password. Never raises."""
        self._kr_delete()
        self._json_delete()


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class AuthClient:
    """Thread-safe client for the DedSec auth API.

    :param base_url: e.g. ``https://auth.example.com``. Plain ``http://`` is refused
        unless the host is localhost *and* ``allow_insecure_localhost=True`` (dev only).
    :param verify: TLS verification (``True`` or a CA-bundle path for a pinned/self-hosted CA).
    """

    REFRESH_MARGIN = 60  # refresh the access token this many seconds before expiry

    def __init__(
        self,
        base_url: str,
        *,
        profile: str = "default",
        timeout: float = 10.0,
        verify: bool | str = True,
        allow_insecure_localhost: bool = False,
        token_store: TokenStore | None = None,
        device_id: Any = None,
        app_version: str | None = None,
    ) -> None:
        u = urlparse(base_url)
        local = u.hostname in {"localhost", "127.0.0.1", "::1"}
        if u.scheme != "https" and not (u.scheme == "http" and local and allow_insecure_localhost):
            raise ValueError("base_url must use https:// (http only allowed for localhost in dev mode)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._s = requests.Session()
        self._s.verify = verify
        self._s.headers.update({"User-Agent": "DedSecCCTV-Desktop/1.0", "Accept": "application/json"})
        version = str(app_version or APP_VERSION).strip()
        if not _VERSION_HEADER_RE.match(version):  # never put junk/CRLF into a header
            raise ValueError(f"invalid app_version {version!r}")
        self.app_version = version
        self._s.headers[APP_VERSION_HEADER] = version
        self._s.trust_env = False  # ignore proxy env vars that could intercept credentials
        self._store = token_store or TokenStore(account=f"{profile}@{u.hostname}")
        self._lock = threading.RLock()
        self._access: str | None = None
        self._access_exp: float = 0.0
        self._refresh: str | None = None
        self._challenge: str | None = None
        # str, or a callable returning it (computed lazily on the worker thread; default hardware_id)
        self._device_id = device_id if device_id is not None else hardware_id

    @property
    def device_id(self) -> str:
        dev = self._device_id() if callable(self._device_id) else self._device_id
        return str(dev or "")

    def _device_header(self) -> dict[str, str]:
        dev = self.device_id
        return {DEVICE_HEADER: dev} if dev else {}

    def _hwid_body(self) -> dict[str, str]:
        dev = self.device_id
        return {"hwid": dev} if dev else {}

    # -- low level ---------------------------------------------------------
    def _post(self, path: str, body: dict[str, Any] | None = None, auth: bool = False) -> requests.Response:
        return self._request("POST", path, body, auth)

    def _request(self, method: str, path: str, body: dict[str, Any] | None, auth: bool) -> requests.Response:
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = f"Bearer {self._valid_access_token()}"
        r = self._send(method, path, body, headers)
        if auth and r.status_code == 401 and self._refresh:
            # access token rejected (revoked/expired early): refresh once and retry
            self._do_refresh()
            headers["Authorization"] = f"Bearer {self._access}"
            r = self._send(method, path, body, headers)
        return r

    def _send(self, method: str, path: str, body: dict[str, Any] | None,
              headers: dict[str, str] | None = None) -> requests.Response:
        """Single HTTP round-trip; network failures become ``AuthError(status=0)``."""
        headers = {**self._device_header(), **(headers or {})}
        try:
            return self._s.request(method, self.base_url + path, json=body, headers=headers,
                                   timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise AuthError(f"Cannot reach the authentication server: {exc.__class__.__name__}") from exc

    @staticmethod
    def _raise_for(r: requests.Response) -> None:
        if r.ok:
            return
        try:
            detail = r.json().get("detail", r.reason)
        except (ValueError, AttributeError):  # not JSON, or JSON that isn't an object
            detail = r.reason
        if r.status_code == UPDATE_REQUIRED_STATUS or r.headers.get("X-Error-Code") == "update_required" or (
                isinstance(detail, dict) and detail.get("code") == "update_required"):
            min_v = str(detail.get("min_version") or "")[:32] if isinstance(detail, dict) else ""
            raise UpdateRequiredError(UPDATE_REQUIRED_MSG, r.status_code, min_v)
        if isinstance(detail, dict) and "password" in detail:
            detail = "Password " + "; ".join(detail["password"])
        elif isinstance(detail, list):
            detail = "Invalid input"
        if r.status_code == DEVICE_LOCKED_STATUS and (
                r.headers.get("X-Error-Code") == "device_mismatch" or "another pc" in str(detail).lower()):
            raise DeviceLocked(str(detail) or DEVICE_MISMATCH_MSG, r.status_code)
        raise AuthError(str(detail), r.status_code)

    def _accept_tokens(self, data: dict[str, Any]) -> None:
        self._access = data["access_token"]
        self._access_exp = time.time() + int(data["expires_in"])
        self._refresh = data["refresh_token"]
        self._challenge = None
        self._store.save(self._refresh)

    def _do_refresh(self) -> None:
        with self._lock:
            if not self._refresh:
                raise AuthError("Not logged in", 401)
            r = self._send("POST", "/auth/refresh", {"refresh_token": self._refresh})
            if r.status_code in (401, DEVICE_LOCKED_STATUS):  # 403: session belongs to another PC
                self._clear_session()
            self._raise_for(r)
            self._accept_tokens(r.json())

    def _valid_access_token(self) -> str:
        with self._lock:
            if self._access is None or time.time() > self._access_exp - self.REFRESH_MARGIN:
                self._do_refresh()
            assert self._access is not None
            return self._access

    def _clear_session(self) -> None:
        self._access, self._access_exp, self._refresh, self._challenge = None, 0.0, None, None
        self._store.clear()

    # -- public API --------------------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return self._refresh is not None

    def register(self, email: str, password: str, email_updates: bool = False, terms_accepted: bool = False) -> str:
        """Create an account. The server always answers generically; check your inbox.

        ``email_updates`` is the sign-up opt-in for product-update emails (default off, UK PECR/GDPR)."""
        r = self._post("/auth/register", {"email": email, "password": password, "email_updates": bool(email_updates),
                                            "terms_accepted": bool(terms_accepted)})
        self._raise_for(r)
        return str(r.json()["message"])

    def get_email_updates(self) -> bool:
        """Is this account opted in to product-update emails?"""
        r = self._request("GET", "/account/email-preferences", None, auth=True)
        self._raise_for(r)
        return bool(r.json().get("email_updates"))

    def set_email_updates(self, enabled: bool) -> bool:
        """Opt in/out of product-update emails. Returns the stored value."""
        r = self._request("PUT", "/account/email-preferences", {"email_updates": bool(enabled)}, auth=True)
        self._raise_for(r)
        return bool(r.json().get("email_updates"))

    def verify_email(self, token: str, email: str | None = None) -> None:
        """Verify with the 6-digit emailed code. Sending the email enables the per-account attempt limit."""
        code = "".join(ch for ch in token if ch.isdigit()) if len(token.strip()) <= 12 else token.strip()
        body: dict[str, Any] = {"token": code}
        if email:
            body["email"] = email.strip()
        self._raise_for(self._post("/auth/verify-email", body))

    def login(self, email: str, password: str) -> User | None:
        """Log in. Returns the user, or raises ``TotpRequired`` if a 2FA code is needed."""
        with self._lock:
            r = self._post("/auth/login", {"email": email, "password": password, **self._hwid_body()})
            self._raise_for(r)
            data = r.json()
            if data.get("status") == "totp_required":
                self._challenge = data["challenge_token"]
                raise TotpRequired("Enter the 6-digit code from your authenticator app.", 200)
            self._accept_tokens(data)
        return self.current_user()

    def submit_totp(self, code: str) -> User:
        with self._lock:
            if not self._challenge:
                raise AuthError("No pending two-factor challenge; log in again.")
            r = self._post("/auth/login/totp", {"challenge_token": self._challenge, "code": code.strip(),
                                                **self._hwid_body()})
            self._raise_for(r)
            self._accept_tokens(r.json())
        return self.current_user()

    def try_resume(self, raise_network_errors: bool = False) -> bool:
        """Silently restore a session from the stored refresh token. Returns True on success.

        A rejected token (401) is wiped. Network/server errors keep the stored token so a later
        retry can still succeed; pass ``raise_network_errors=True`` to get those as ``AuthError``
        (``status`` 0 = unreachable, 429/5xx = server side) instead of ``False``.
        """
        with self._lock:
            self._refresh = self._store.load()
            if not self._refresh:
                return False
            try:
                self._do_refresh()
                return True
            except UpdateRequiredError:
                raise  # keep the stored session: it works again once the app is updated
            except AuthError as e:
                if e.status in (401, DEVICE_LOCKED_STATUS):
                    self._clear_session()
                    return False
                if raise_network_errors:
                    raise
                return False

    def current_user(self) -> User:
        r = self._request("GET", "/auth/me", None, auth=True)
        self._raise_for(r)
        d = r.json()
        return User(d["id"], d["email"], d["email_verified"], d["totp_enabled"], d["created_at"],
                    bool(d.get("device_bound", False)))

    # -- hardware lock: move the account to this PC ------------------------
    def request_device_reset(self, email: str, password: str) -> str:
        """Ask the server to email a 6-digit reset code that moves the account to THIS PC."""
        r = self._post("/auth/device/reset-request",
                       {"email": email.strip(), "password": password, "new_hwid": self.device_id})
        self._raise_for(r)
        return str(r.json().get("message", ""))

    def confirm_device_reset(self, email: str, code: str) -> str:
        """Confirm with the emailed code. Every other session ends; sign in again afterwards."""
        code = "".join(ch for ch in code if ch.isdigit())
        r = self._post("/auth/device/reset-confirm",
                       {"email": email.strip(), "code": code, "new_hwid": self.device_id})
        self._raise_for(r)
        return str(r.json().get("message", ""))

    def setup_2fa(self, existing_secret: str | None = None) -> dict[str, Any]:
        """Start 2FA enrolment. Returns ``{"secret", "otpauth_uri", ...}`` -- render the URI as a QR."""
        body = {"existing_secret": existing_secret} if existing_secret else {}
        r = self._post("/auth/2fa/setup", body, auth=True)
        self._raise_for(r)
        return dict(r.json())

    def enable_2fa(self, code: str) -> None:
        """Confirm enrolment with a first code. The server then ends all sessions: log in again."""
        r = self._post("/auth/2fa/enable", {"code": code.strip()}, auth=True)
        self._raise_for(r)
        self._clear_session()

    def logout(self, all_sessions: bool = False) -> None:
        """Revoke the session server-side (best effort) and wipe local tokens."""
        with self._lock:
            try:
                if self._refresh:
                    self._post("/auth/logout", {"refresh_token": self._refresh, "all_sessions": all_sessions},
                               auth=True)
            except AuthError:
                pass
            finally:
                self._clear_session()
