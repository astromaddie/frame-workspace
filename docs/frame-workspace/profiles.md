# Logical workspace profiles

The companion can save named MacView profiles through its authenticated loopback API and the `scripts/workspace-profiles.py` CLI. Profiles store a Workspace preset, scale, stream quality, and optional window rules. They never store current Mac window/display IDs, one-use stream tickets, or reconnect keys.

```sh
python3 scripts/workspace-profiles.py list
python3 scripts/workspace-profiles.py save Coding --preset 5120x1440 --scale 1 --quality sharp \
  --window com.google.Chrome::Project --window com.apple.Terminal
python3 scripts/workspace-profiles.py restore Coding
python3 scripts/workspace-profiles.py show Coding
python3 scripts/workspace-profiles.py delete Coding
python3 scripts/workspace-profiles.py reset
```

Each `--window` takes a stable macOS bundle identifier and an optional title hint after `::`. For example, `com.google.Chrome::Project` matches a visible Chrome window whose title contains “Project”. Restore freshly enumerates the Mac before matching. It shows a window only when exactly one current window matches that rule. Missing apps, renamed titles, ambiguous matches, and duplicate selections are skipped with a reason; open the app, make the intended window visible, or refine the hint and retry. Restore does not launch apps or move Mac windows.

Restore starts the saved virtual Workspace through the normal MacView lifecycle and opens uniquely matched windows as their own Frame panels. Panel placement remains manual. The current native overlay evidence does not establish a room-safe transform across runtime or room changes, so profiles report spatial restore as unavailable and persist no poses.

The local schema is version 1. Unknown fields and unsupported versions are rejected; corrupt files are reported without rewriting them. The file is stored under the companion's app data directory at `workspace-profiles/profiles.json`, in a private directory with a mode `0600` file on macOS and POSIX systems. A restore needs the existing Screen Recording permission and a visible current window for every rule. The profile is saved on the computer running the MacView companion.

The CLI uses `http://127.0.0.1:47810` and the default UI key `1`. Set `FRAME_CONTROL_URL` or pass `--url` for another local port, and set `FRAME_UI_KEY` when the companion uses a custom key. Requests continue through the companion's authenticated loopback API.

`reset` prompts for the literal `RESET`; `--yes` supplies that confirmation. It clears only saved profile definitions. It does not stop the Workspace, close viewers, move windows, or alter macOS settings.
