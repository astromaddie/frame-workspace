#!/usr/bin/env python3
"""One hash for the sources the native Workspace viewer is built from.

The build script compiles it into the viewer (``--capabilities`` reports it)
and the companion computes it from its own bundled copy, so the two can tell
whether the Frame runs the viewer this Mac app ships. The Mac app bundles
exactly these files (app/package.json) and the headset update sends them.

    python3 scripts/native_source_identity.py [ROOT]          # the identity
    python3 scripts/native_source_identity.py --list [ROOT]   # its files
"""
import hashlib
import pathlib
import sys

SCHEMA = "frame-workspace-native-source-1"
# Relative to the repository (or bundle resources) root.
PATTERNS = (
    "native/*.cpp",
    "native/*.hpp",
    "native/input/*.json",
    "native/vendor/**/*",
    "scripts/build-workspace-native.sh",
    "scripts/native_source_identity.py",
)


def files(root):
    root = pathlib.Path(root)
    found = set()
    for pattern in PATTERNS:
        for path in root.glob(pattern):
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and
                    path.suffix not in (".pyc", ".pyo")):
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def identity(root):
    root = pathlib.Path(root)
    names = files(root)
    if not any(name == "native/main.cpp" for name in names):
        raise FileNotFoundError(f"no native viewer sources under {root}")
    digest = hashlib.sha256((SCHEMA + "\n").encode())
    for name in names:
        digest.update(name.encode() + b"\0" + hashlib.sha256((root / name).read_bytes()).hexdigest().encode() + b"\n")
    return digest.hexdigest()


if __name__ == "__main__":
    args = sys.argv[1:]
    listing = "--list" in args
    args = [a for a in args if a != "--list"]
    base = args[0] if args else pathlib.Path(__file__).resolve().parent.parent
    print("\n".join(files(base)) if listing else identity(base))
