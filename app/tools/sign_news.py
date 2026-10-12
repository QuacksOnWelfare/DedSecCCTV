#!/usr/bin/env python3
"""DedSec CCTV news signer (Ed25519). Run ONLY on your own PC, with the SAME key as sign_update.py.

  python sign_news.py check  --src news_source.json
  python sign_news.py sign   --key update_signing_key.pem --src news_source.json [--out DIR]
        -> writes news.json + news.json.sig (upload both to your update folder)
  python sign_news.py verify --pub BASE64 --dir DIR

news_source.json:
  {"items": [{"id": "release-1.0.2", "type": "changelog", "title": "DedSec CCTV 1.0.2",
              "date": "2026-10-12", "version": "1.0.2", "body": "- line one\\n- line two"}]}
type is "announcement" or "changelog". body is plain text with a tiny markdown subset:
"# heading", "- bullet", **bold**, `code`. HTML is never rendered by the app (it is shown escaped).

Requires: pip install cryptography
"""
import argparse, base64, datetime, getpass, json, re, sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TYPES = ("announcement", "changelog")
MAX_BYTES = 256 * 1024
MAX_ITEMS = 100


def _pub_b64(pub):
    return base64.b64encode(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _load_key(path):
    data = Path(path).read_bytes()
    try:
        return serialization.load_pem_private_key(data, password=None)
    except TypeError:
        return serialization.load_pem_private_key(data, password=getpass.getpass("Key passphrase: ").encode())


def build(src):
    data = json.loads(Path(src).read_text(encoding="utf-8-sig"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        sys.exit("source must be an object with a non-empty 'items' list")
    if len(items) > MAX_ITEMS:
        sys.exit("too many items (max %d)" % MAX_ITEMS)
    seen, out = set(), []
    for i, it in enumerate(items, 1):
        where = "item %d" % i
        if not isinstance(it, dict):
            sys.exit(where + ": not an object")
        kind = str(it.get("type", "")).strip().lower()
        if kind not in TYPES:
            sys.exit(where + ": type must be announcement or changelog")
        title = str(it.get("title", "")).strip()
        if not title or len(title) > 200:
            sys.exit(where + ": title required, max 200 chars")
        date = str(it.get("date", "")).strip()
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            sys.exit(where + ": date must be YYYY-MM-DD")
        version = str(it.get("version", "") or "").strip()
        if version and not re.fullmatch(r"\d{1,4}(\.\d{1,4}){0,3}", version):
            sys.exit(where + ": bad version")
        body = str(it.get("body", "") or "")
        if len(body) > 8000:
            sys.exit(where + ": body max 8000 chars")
        if re.search(r"\{[A-Z_]+\}", title + body):
            sys.exit(where + ": unfilled placeholder like {FEED_COUNT} left in the text")
        item_id = str(it.get("id") or ("%s-%s-%d" % (kind, date, i)))[:80]
        if item_id in seen:
            sys.exit(where + ": duplicate id " + item_id)
        seen.add(item_id)
        out.append({"id": item_id, "type": kind, "title": title, "date": date, "version": version, "body": body})
    feed = {"schema": 1,
            "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "items": out}
    raw = (json.dumps(feed, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        sys.exit("feed too big (%d bytes, max %d)" % (len(raw), MAX_BYTES))
    return raw, out


def check(a):
    _raw, items = build(a.src)
    print("OK: %d items" % len(items))
    for it in items:
        print("  %s  %-12s %s" % (it["date"], it["type"], it["title"]))


def sign(a):
    raw, items = build(a.src)
    key = _load_key(a.key)
    sig = base64.b64encode(key.sign(raw)).decode()
    out = Path(a.out or Path(a.src).parent)
    out.mkdir(parents=True, exist_ok=True)
    (out / "news.json").write_bytes(raw)
    (out / "news.json.sig").write_text(sig + "\n", encoding="ascii")
    print("Wrote %s and news.json.sig (%d items)" % (out / "news.json", len(items)))
    print("signed with public key", _pub_b64(key.public_key()))
    print("Do not edit news.json after signing.")


def verify(a):
    d = Path(a.dir)
    raw = (d / "news.json").read_bytes()
    sig = base64.b64decode((d / "news.json.sig").read_text(encoding="ascii").strip())
    Ed25519PublicKey.from_public_bytes(base64.b64decode(a.pub)).verify(sig, raw)
    feed = json.loads(raw)
    print("OK: signature valid, %d items, generated %s" % (len(feed["items"]), feed["generated"]))


p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sp = p.add_subparsers(dest="cmd", required=True)
c = sp.add_parser("check"); c.add_argument("--src", required=True); c.set_defaults(f=check)
s = sp.add_parser("sign"); s.add_argument("--key", required=True); s.add_argument("--src", required=True)
s.add_argument("--out", default=""); s.set_defaults(f=sign)
v = sp.add_parser("verify"); v.add_argument("--pub", required=True); v.add_argument("--dir", default=".")
v.set_defaults(f=verify)
a = p.parse_args(); a.f(a)
