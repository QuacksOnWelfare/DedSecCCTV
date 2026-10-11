#!/usr/bin/env python3
"""DedSec CCTV offline update signer (Ed25519). Run ONLY on your own PC. Never copy the private key anywhere.

  python sign_update.py keygen  [--out DIR]          # once: writes update_signing_key.pem (PRIVATE) + prints public key
  python sign_update.py sign    --key KEY.pem --zip DedSecCCTV.zip --version 1.0.1 [--notes "..."] [--optional]
        -> writes latest.json + latest.json.sig next to the zip (upload zip, then .sig, then latest.json)
  python sign_update.py verify  --pub BASE64 --dir DIR  # check a release folder before publishing

Requires: pip install cryptography
"""
import argparse, base64, getpass, hashlib, json, os, re, sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _pub_b64(pub):
    return base64.b64encode(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def keygen(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    path = out / "update_signing_key.pem"
    if path.exists():
        sys.exit(f"{path} already exists; refusing to overwrite (losing it means re-shipping the app).")
    pw = getpass.getpass("Passphrase for the private key (recommended, empty = none): ").encode()
    if pw and pw != getpass.getpass("Repeat passphrase: ").encode():
        sys.exit("Passphrases differ.")
    key = Ed25519PrivateKey.generate()
    enc = serialization.BestAvailableEncryption(pw) if pw else serialization.NoEncryption()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc))
    try: os.chmod(path, 0o600)
    except OSError: pass
    print(f"PRIVATE key: {path}  (keep offline + make a backup; never upload it)")
    print("PUBLIC key - paste into app.py UPDATE_PUBLIC_KEY_B64:")
    print(_pub_b64(key.public_key()))


def _load_key(path):
    data = Path(path).read_bytes()
    try:
        return serialization.load_pem_private_key(data, password=None)
    except TypeError:
        return serialization.load_pem_private_key(data, password=getpass.getpass("Key passphrase: ").encode())


def sign(a):
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", a.version):
        sys.exit("version must be semver, e.g. 1.0.1")
    z = Path(a.zip)
    h = hashlib.sha256()
    with open(z, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    manifest = {"version": a.version, "url": z.name, "sha256": h.hexdigest(),
                "mandatory": not a.optional, "notes": a.notes or ""}
    raw = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    key = _load_key(a.key)
    sig = base64.b64encode(key.sign(raw)).decode()
    (z.parent / "latest.json").write_bytes(raw)
    (z.parent / "latest.json.sig").write_text(sig + "\n", encoding="ascii")
    print(raw.decode()); print("signed with public key", _pub_b64(key.public_key()))
    print("Wrote latest.json + latest.json.sig. Do not edit latest.json after signing.")


def verify(a):
    d = Path(a.dir)
    raw = (d / "latest.json").read_bytes()
    sig = base64.b64decode((d / "latest.json.sig").read_text().strip())
    Ed25519PublicKey.from_public_bytes(base64.b64decode(a.pub)).verify(sig, raw)
    m = json.loads(raw)
    h = hashlib.sha256((d / m["url"]).read_bytes()).hexdigest()
    if h != m["sha256"]: sys.exit("zip SHA-256 does NOT match latest.json")
    print("OK: signature valid and zip hash matches", m["version"])


p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sp = p.add_subparsers(dest="cmd", required=True)
k = sp.add_parser("keygen"); k.add_argument("--out", default="."); k.set_defaults(f=keygen)
s = sp.add_parser("sign"); s.add_argument("--key", required=True); s.add_argument("--zip", required=True)
s.add_argument("--version", required=True); s.add_argument("--notes", default="")
s.add_argument("--optional", action="store_true", help="not a mandatory update"); s.set_defaults(f=sign)
v = sp.add_parser("verify"); v.add_argument("--pub", required=True); v.add_argument("--dir", default="."); v.set_defaults(f=verify)
a = p.parse_args(); a.f(a)
