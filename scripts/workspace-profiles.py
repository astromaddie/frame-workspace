#!/usr/bin/env python3
"""Manage local MacView workspace profiles through the authenticated companion API.

Examples:
  python3 scripts/workspace-profiles.py list
  python3 scripts/workspace-profiles.py save Coding --preset 5120x1440 \
      --window com.google.Chrome::Project --window com.apple.Terminal
  python3 scripts/workspace-profiles.py restore Coding
  python3 scripts/workspace-profiles.py delete Coding

The companion API listens on loopback. Set FRAME_CONTROL_URL and FRAME_UI_KEY
when it is running on a non-default local port or uses a custom UI key.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
import frame_mcp  # noqa: E402


PRESETS = ("5120x1440", "5120x2160", "3440x1440", "2560x2880", "2880x2560", "3840x2160")
QUALITIES = ("sharp", "balanced", "light", "compatible")


def parse_rule(value):
    bundle_id, separator, hint = value.partition("::")
    if separator and not hint:
        raise argparse.ArgumentTypeError("a :: title hint cannot be empty")
    rule = {"bundleID": bundle_id}
    if separator:
        rule["titleHint"] = hint
    return rule


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default=os.environ.get("FRAME_CONTROL_URL", "http://127.0.0.1:47810"),
                        help="authenticated loopback companion URL")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list saved profiles")
    show = commands.add_parser("show", help="print one saved profile")
    show.add_argument("name")
    save = commands.add_parser("save", help="create or replace a profile")
    save.add_argument("name")
    save.add_argument("--preset", choices=PRESETS, default="5120x1440")
    save.add_argument("--scale", choices=(1, 2), type=int, default=1)
    save.add_argument("--quality", choices=QUALITIES, default="sharp")
    save.add_argument("--window", action="append", type=parse_rule, default=[], metavar="BUNDLE_ID[::TITLE_HINT]",
                      help="require exactly one visible matching window; repeat as needed")
    restore = commands.add_parser("restore", help="start the Workspace and show uniquely matched windows")
    restore.add_argument("name")
    delete = commands.add_parser("delete", help="delete one saved profile")
    delete.add_argument("name")
    reset = commands.add_parser("reset", help="clear all saved profiles without changing open windows")
    reset.add_argument("--yes", action="store_true", help="confirm the reset without an interactive prompt")
    return result


def run(args, client, input_fn=input):
    if args.command == "list":
        result = client.request("/api/macview", {"action": "profile_list"})
    elif args.command == "show":
        profiles = client.request("/api/macview", {"action": "profile_list"})["profiles"]
        result = next((profile for profile in profiles if profile["name"].casefold() == args.name.casefold()), None)
        if result is None:
            raise ValueError(f"workspace profile {args.name!r} was not found")
    elif args.command == "save":
        profile = {"name": args.name,
                   "workspace": {"preset": args.preset, "scale": args.scale, "quality": args.quality},
                   "windows": args.window}
        result = client.request("/api/macview", {"action": "profile_save", "profile": profile})
    elif args.command == "restore":
        result = client.request("/api/macview", {"action": "profile_restore", "name": args.name})
    elif args.command == "delete":
        result = client.request("/api/macview", {"action": "profile_delete", "name": args.name})
    else:
        if not args.yes:
            answer = input_fn("Clear every saved workspace profile? Type RESET to continue: ")
            if answer != "RESET":
                raise ValueError("reset cancelled")
        result = client.request("/api/macview", {"action": "profile_reset", "confirmation": "RESET"})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args, frame_mcp.Client(args.url, os.environ.get("FRAME_UI_KEY", "1")))
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
