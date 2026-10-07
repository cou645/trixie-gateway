# Copyright (C) 2026 Marcos M Contant aka stemsee <cou645@gmail.com>
# All rights reserved.
#
# This software is proprietary. No licence is granted to use, copy, modify,
# distribute, sublicense or sell it, in whole or in part, without the prior
# written permission of the copyright holder. Licensing: cou645@gmail.com
#
# Copyright is held by the author personally. CHAMELEON-AI-AGENT LTD has not
# paid for this software or for the hours spent writing it, and holds no
# rights in it.
# Donations via PayPal: cou645@gmail.com
"""Phone app updates served by the gateway (TrXi-Ctrl, Chameleon Companion…).

Publish a build on the PC:
    python3 capabilities/app_updates.py publish --flutter <project dir> \\
        --package com.trxi.ctrl --name TrXi-Ctrl [--notes "…"]
(reads the version from pubspec.yaml and takes every
build/app/outputs/flutter-apk/app-<abi>-release.apk).

Phones (paired, token) list /yay/updates, download /yay/updates/<pkg>/<abi> and
compare its sha256 with the APK they have installed — so a rebuild counts
as an update even when the version number didn't change. A share link
(/dl/<token>/<file>) works without a token for 24 h, for a browser or a
phone that isn't paired yet; it is posted to the shared clipboard.

    ~/.config/trixie-gateway/apps/apps.json      manifest
    ~/.config/trixie-gateway/apps/<pkg>/<code>/  the APKs (last 2 builds kept)
    ~/.config/trixie-gateway/apps/shares.json    share links
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

APPS_DIR = Path.home() / ".config" / "trixie-gateway" / "apps"
ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")
KEEP_BUILDS = 2
SHARE_TTL = 24 * 3600


def _dir():
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    return APPS_DIR


def _read(name, default):
    try:
        return json.loads((_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name, data):
    p = _dir() / name
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest():
    return _read("apps.json", {"apps": {}})


def list_apps():
    """For phones: what can be installed (no server paths)."""
    out = []
    for pkg, a in sorted(manifest()["apps"].items()):
        out.append({"package": pkg, "name": a["name"], "version": a["version"],
                    "code": a["code"], "notes": a.get("notes", ""),
                    "published": a["published"],
                    "files": {abi: {"sha256": f["sha256"], "size": f["size"]}
                              for abi, f in a["files"].items()}})
    return out


def publish(package, name, version, code, apks, notes=""):
    """apks: {abi: path}. Copies them in, updates the manifest, keeps the
    last KEEP_BUILDS builds of the package."""
    if not re.fullmatch(r"[A-Za-z][\w.]*", package):
        raise ValueError("bad package name")
    if not apks:
        raise ValueError("no APKs to publish")
    dest = _dir() / package / str(code)
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    for abi, src in apks.items():
        if abi not in ABIS:
            raise ValueError("unknown ABI %s" % abi)
        target = dest / ("%s-%s.apk" % (re.sub(r"\W", "", name), abi))
        shutil.copyfile(src, target)
        files[abi] = {"file": str(target.relative_to(APPS_DIR)),
                      "sha256": _sha256(target), "size": target.stat().st_size}
    m = manifest()
    m["apps"][package] = {"name": name, "version": version, "code": int(code),
                          "notes": notes, "files": files,
                          "published": time.strftime("%Y-%m-%d %H:%M")}
    _write("apps.json", m)
    builds = sorted((p for p in (_dir() / package).iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime)
    for old in builds[:-KEEP_BUILDS]:
        shutil.rmtree(old, ignore_errors=True)
    return m["apps"][package]


def from_flutter(project, package, name, notes=""):
    """Publish a Flutter project's release APKs (flutter build apk
    --release --split-per-abi), version from pubspec.yaml."""
    project = Path(project)
    m = re.search(r"^version:\s*([\w.\-]+)(?:\+(\d+))?",
                  (project / "pubspec.yaml").read_text(encoding="utf-8"), re.M)
    version, code = (m.group(1), int(m.group(2) or 1)) if m else ("0", 1)
    out = project / "build" / "app" / "outputs" / "flutter-apk"
    apks = {abi: out / ("app-%s-release.apk" % abi) for abi in ABIS
            if (out / ("app-%s-release.apk" % abi)).exists()}
    return publish(package, name, version, code, apks, notes)


def apk_path(package, abi):
    a = manifest()["apps"].get(package)
    if not a:
        return None
    f = a["files"].get(abi) or a["files"].get("arm64-v8a") or \
        next(iter(a["files"].values()), None)
    p = APPS_DIR / f["file"] if f else None
    return p if p and p.exists() else None


def share(package, abi="arm64-v8a", ttl=SHARE_TTL):
    """A token link valid for ttl seconds -> (token, filename, expires)."""
    a = manifest()["apps"].get(package)
    if not a or not apk_path(package, abi):
        return None
    shares = {t: s for t, s in _read("shares.json", {}).items()
              if s["expires"] > time.time()}            # drop expired ones
    token = secrets.token_urlsafe(16)
    fname = "%s-%s-%s.apk" % (re.sub(r"\W", "", a["name"]), a["version"], abi)
    shares[token] = {"package": package, "abi": abi, "file": fname,
                     "expires": time.time() + ttl}
    _write("shares.json", shares)
    return token, fname, shares[token]["expires"]


def resolve_share(token):
    s = _read("shares.json", {}).get(token)
    if not s or s["expires"] < time.time():
        return None, None
    return apk_path(s["package"], s["abi"]), s["file"]


def _selftest():
    import tempfile
    global APPS_DIR
    d = Path(tempfile.mkdtemp())
    APPS_DIR = d / "apps"
    proj = d / "proj"
    out = proj / "build" / "app" / "outputs" / "flutter-apk"
    out.mkdir(parents=True)
    (proj / "pubspec.yaml").write_text("name: x\nversion: 1.2.3+7\n")
    for abi in ("arm64-v8a", "armeabi-v7a"):
        (out / ("app-%s-release.apk" % abi)).write_bytes(b"PK" + abi.encode())
    a = from_flutter(proj, "com.example.app", "Example App")
    assert a["version"] == "1.2.3" and a["code"] == 7
    assert set(a["files"]) == {"arm64-v8a", "armeabi-v7a"}
    lst = list_apps()
    assert lst[0]["files"]["arm64-v8a"]["sha256"] == \
        hashlib.sha256(b"PKarm64-v8a").hexdigest()
    assert "file" not in lst[0]["files"]["arm64-v8a"]     # no server paths
    assert apk_path("com.example.app", "x86_64").read_bytes() == b"PKarm64-v8a"
    tok, fname, exp = share("com.example.app", "armeabi-v7a")
    p, f = resolve_share(tok)
    assert p.read_bytes() == b"PKarmeabi-v7a" and f == fname
    assert resolve_share("nope") == (None, None)
    tok2, _, _ = share("com.example.app", ttl=-1)          # already expired
    assert resolve_share(tok2) == (None, None)
    for code in (8, 9):                                    # keep the last 2
        (proj / "pubspec.yaml").write_text("name: x\nversion: 1.2.3+%d\n" % code)
        time.sleep(0.01)
        from_flutter(proj, "com.example.app", "Example App")
    assert sorted(p.name for p in (APPS_DIR / "com.example.app").iterdir()) == ["8", "9"]
    print("app_updates selftest OK")


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    pub = sub.add_parser("publish", help="publish a Flutter project's release APKs")
    pub.add_argument("--flutter", required=True, help="Flutter project directory")
    pub.add_argument("--package", required=True)
    pub.add_argument("--name", required=True)
    pub.add_argument("--notes", default="")
    sub.add_parser("list")
    sub.add_parser("selftest")
    args = ap.parse_args()
    if args.cmd == "publish":
        print(json.dumps(from_flutter(args.flutter, args.package, args.name,
                                      args.notes), indent=1))
    elif args.cmd == "list":
        print(json.dumps(list_apps(), indent=1))
    elif args.cmd == "selftest":
        _selftest()
    else:
        ap.print_help()
        sys.exit(1)
