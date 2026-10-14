#!/usr/bin/env python3
"""Pack what Frame Control's server needs to run on the Frame itself (the files the
desktop app ships, plus ui/local-bin) into one reproducible .tar.gz. That includes
KDE Connect for the keyboard and trackpad (frame/kdeconnect), downloaded first and
checked against its pinned SHA-256s.

The iPhone app copies it to ~/.cache/frame-control/<version> on the Frame and
starts ui/server.py there. <version> is the SHA-256 of the archive, so a new
build replaces an old one and an unchanged one isn't copied again.

Usage: make_frame_bundle.py OUT.tar.gz   (prints the version)
"""
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERNS = ["ui/*.py", "ui/*.html", "ui/*.js", "ui/*.css", "ui/local-bin/*", "scripts/*.sh", "frame/android/*.sh", "frame/android/*.py",
            "frame/devkit-utils/**/*", "apk-catalog/*.py", "apk-catalog/pins.json", "apk-catalog/site/apps.js",
            "frame/kdeconnect/packages.json", "frame/kdeconnect/NOTICE.md", "frame/kdeconnect/LICENSES/*/*",
            "frame/kdeconnect/packages/*.pkg.tar.zst", "LICENSE", "THIRD_PARTY_NOTICES.md"]


def files():
    found = set()
    for pattern in PATTERNS:
        for p in ROOT.glob(pattern):
            if p.is_file() and "__pycache__" not in p.parts:
                found.add(p)
    return sorted(found)


def build():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for p in files():
            info = tarfile.TarInfo(str(p.relative_to(ROOT)))
            data = p.read_bytes()
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0, 0, "", ""
            info.mode = 0o755 if p.stat().st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.path.insert(0, str(ROOT / "frame" / "kdeconnect"))
    import fetch
    fetch.fetch()
    data = build()
    Path(sys.argv[1]).write_bytes(data)
    print(hashlib.sha256(data).hexdigest()[:16])
