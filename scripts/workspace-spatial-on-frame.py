#!/usr/bin/env python3
"""Validated, atomic FW-15 runtime state/icon writes. Sent over SSH on stdin."""
import base64
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile

BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
KEY = re.compile(r"com\.frame-workspace\.native\.\d{1,15}\.panel-w\d{1,15}\Z")
FOLDER = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
MAX_LAUNCHER_APPS = 600


def require(ok):
    if not ok:
        raise ValueError("Invalid spatial state or icon")


def text(value, limit):
    return isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def validate_state(raw):
    require(len(raw.encode()) <= 65536)
    state = json.loads(raw)
    require(isinstance(state, dict) and state.get("mode") == "spatial")
    apps, switcher = state.get("apps"), state.get("switcher")
    require(isinstance(apps, list) and len(apps) <= 128 and isinstance(switcher, dict))
    bundles, keys = set(), set()
    for app in apps:
        require(isinstance(app, dict))
        bundle = app.get("bundleID")
        require(isinstance(bundle, str) and BUNDLE.fullmatch(bundle) and bundle not in bundles)
        bundles.add(bundle)
        require(text(app.get("name"), 256) and app.get("icon") == bundle + ".png")
        require(all(type(app.get(k)) is bool for k in ("running", "pinned", "hidden", "active")))
        windows = app.get("windows")
        require(isinstance(windows, list) and len(windows) <= 256)
        for window in windows:
            require(isinstance(window, dict) and text(window.get("title"), 512))
            key = window.get("key")
            require(isinstance(key, str) and KEY.fullmatch(key) and key not in keys)
            keys.add(key)
    require(len(keys) <= 256)
    order = switcher.get("windows")
    require(isinstance(order, list) and all(isinstance(k, str) for k in order))
    require(len(order) == len(set(order)) and set(order) == keys)
    require(type(switcher.get("open")) is bool)
    require(type(switcher.get("index")) is int and 0 <= switcher["index"] < max(1, len(order)))
    require(type(switcher.get("seq")) is int and 0 <= switcher["seq"] < 2**63)
    require(switcher.get("chosen") is None or switcher["chosen"] in keys)
    require("tapActive" not in switcher or type(switcher["tapActive"]) is bool)
    require("error" not in switcher or text(switcher["error"], 512))
    if "launcher" in state:
        validate_launcher_state(state["launcher"])
    return state


def counter(value):
    return type(value) is int and 0 <= value < 2**63


def validate_launcher_state(launcher):
    """The launcher's open state and search field (Launcher A)."""
    require(isinstance(launcher, dict) and launcher.get("available") is True)
    require(all(type(launcher.get(k)) is bool for k in ("open", "keyboard")))
    require(all(counter(launcher.get(k)) for k in ("seq", "catalog", "selected")))
    require(type(launcher.get("pageStep")) is int and abs(launcher["pageStep"]) < 2**31)
    require(text(launcher.get("query"), 256))
    results = launcher.get("results", [])
    require(isinstance(results, list) and len(results) <= MAX_LAUNCHER_APPS and len(set(results)) == len(results))
    require(all(isinstance(b, str) and BUNDLE.fullmatch(b) for b in results))


def validate_catalog(raw):
    """The launcher's apps, layout and recent apps, published only when they change."""
    require(len(raw.encode()) <= 100000)
    catalog = json.loads(raw)
    require(isinstance(catalog, dict) and counter(catalog.get("catalog")))
    apps, items, recent = catalog.get("apps"), catalog.get("items"), catalog.get("recent")
    require(isinstance(apps, list) and len(apps) <= MAX_LAUNCHER_APPS)
    require(isinstance(items, list) and isinstance(recent, list) and len(recent) <= 16)
    ids = set()
    for app in apps:
        require(isinstance(app, dict) and isinstance(app.get("id"), str) and BUNDLE.fullmatch(app["id"]))
        require(app["id"] not in ids and text(app.get("name"), 128) and text(app.get("v"), 96))
        ids.add(app["id"])
    placed, folders = set(), set()
    for item in items:
        require(isinstance(item, dict))
        if "folder" in item:
            members = item.get("apps")
            require(isinstance(item["folder"], str) and FOLDER.fullmatch(item["folder"]) and item["folder"] not in folders)
            require(text(item.get("name"), 64) and isinstance(members, list) and members)
            folders.add(item["folder"])
        else:
            members = [item.get("app")]
        for bundle in members:
            require(isinstance(bundle, str) and bundle in ids and bundle not in placed)
            placed.add(bundle)
    require(all(isinstance(b, str) and b in ids for b in recent))
    return catalog


def load_versions(icons):
    try:
        versions = json.loads((icons / "versions.json").read_text())
    except (OSError, ValueError):
        return {}
    return versions if isinstance(versions, dict) else {}


def png_bytes(encoded):
    require(isinstance(encoded, str) and len(encoded) <= 100000)
    data = base64.b64decode(encoded, validate=True)
    require(33 <= len(data) <= 75000 and data[:8] == b"\x89PNG\r\n\x1a\n")
    require(data[8:16] == b"\x00\x00\x00\rIHDR" and struct.unpack(">II", data[16:24]) == (128, 128))
    return data


def atomic(path, data):
    fd, temp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main(args):
    # Validate everything before creating directories or changing files.
    require(bool(args))
    action = args[0]
    if action == "sync":
        require(len(args) == 2)
        state = validate_state(args[1])
    elif action == "icon":
        require(len(args) == 3 and BUNDLE.fullmatch(args[1]))
        data = png_bytes(args[2])
    elif action == "launcher":
        require(len(args) == 2)
        catalog = validate_catalog(args[1])
    elif action == "icons":
        # Launcher icons in one write: bundle, version, PNG; the version is
        # recorded so each icon is sent once until the app changes.
        require(len(args) >= 4 and (len(args) - 1) % 3 == 0 and len(args) <= 1 + 3 * 16)
        batch = []
        for i in range(1, len(args), 3):
            require(BUNDLE.fullmatch(args[i]) and text(args[i + 1], 96))
            batch.append((args[i], args[i + 1], png_bytes(args[i + 2])))
    else:
        require(action == "clear" and len(args) == 1)
    os.umask(0o077)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/" + str(os.getuid()))) / "frame-workspace"
    require(not runtime.is_symlink())
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    icons = runtime / "icons"
    require(not icons.is_symlink())
    path = runtime / "spatial-ui.json"
    if action == "clear":
        path.unlink(missing_ok=True)
        (runtime / "launcher.json").unlink(missing_ok=True)
    elif action == "sync":
        atomic(path, (json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
        missing = [a["bundleID"] for a in state["apps"]
                   if (icons / a["icon"]).is_symlink() or not (icons / a["icon"]).is_file()]
        print("SPATIAL_ICONS_MISSING " + json.dumps(missing))
        try:
            published = json.loads((runtime / "launcher.json").read_text()).get("catalog")
        except (OSError, ValueError, AttributeError):
            published = None
        print("LAUNCHER_CATALOG " + json.dumps(published if counter(published) else None))
    elif action == "launcher":
        atomic(runtime / "launcher.json", (json.dumps(catalog, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
        versions = load_versions(icons)
        missing = [a["id"] for a in catalog["apps"]
                   if (icons / (a["id"] + ".png")).is_symlink() or not (icons / (a["id"] + ".png")).is_file() or
                   versions.get(a["id"]) != a["v"]]
        print("LAUNCHER_ICONS_MISSING " + json.dumps(missing))
    elif action == "icons":
        icons.mkdir(mode=0o700, exist_ok=True)
        versions = load_versions(icons)
        for bundle, version, data in batch:
            atomic(icons / (bundle + ".png"), data)
            versions[bundle] = version
        if len(versions) > 4 * MAX_LAUNCHER_APPS:
            versions = dict(list(versions.items())[-2 * MAX_LAUNCHER_APPS:])
        atomic(icons / "versions.json", json.dumps(versions, separators=(",", ":")).encode())
    else:
        icons.mkdir(mode=0o700, exist_ok=True)
        atomic(icons / (args[1] + ".png"), data)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
