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
    return state


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
    elif action == "sync":
        atomic(path, (json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
        missing = [a["bundleID"] for a in state["apps"]
                   if (icons / a["icon"]).is_symlink() or not (icons / a["icon"]).is_file()]
        print("SPATIAL_ICONS_MISSING " + json.dumps(missing))
    else:
        icons.mkdir(mode=0o700, exist_ok=True)
        atomic(icons / (args[1] + ".png"), data)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
