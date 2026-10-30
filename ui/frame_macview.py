"""Mac in the headset: stream Mac windows or displays into the Steam Frame as
panels you can place anywhere, with the laser, wheel and keys driving the Mac.

Runs on the Mac, inside Frame Control's server. The pieces:

- mac/bin/frame-mac-view (Swift, built from mac/frame-mac-view): captures with
  ScreenCaptureKit, encodes with VideoToolbox, serves ui/mac-view.html and one
  WebSocket per stream on 127.0.0.1, and plays input back with CGEvent.
- An `ssh -R` tunnel, so the Frame reaches the agent on its own 127.0.0.1.
  Every request carries a random token, so other programs on the Frame
  (Android apps included) can't watch or drive the Mac.
- A Chromium app window on the Frame per stream, on gamescope's X display
  with its own STEAM_GAME id, which makes it its own SteamVR panel (see
  docs/panels.md). Chromium XR (~/chromium-xr) is preferred because it's
  built with H.264; Flathub Chromium is the fallback.

Stdlib only. MacView gets a plain ssh argv for the tunnel (its own
connection) and the server's `run(remote, stdin=, timeout=)` for commands.
"""
import collections
import json
import math
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import quote, urlencode  # %20, not +: the agent's URLComponents keeps +

from frame_native_update import HeadsetViewer, ViewerError
import frame_spatial_apps
from frame_spatial_apps import SpatialApps

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PAGE = HERE / "mac-view.html"
SOURCES = ROOT / "mac" / "frame-mac-view"
WORKSPACE_VIEWER_SCRIPT = ROOT / "scripts" / "workspace-viewer-on-frame.sh"
WORKSPACE_NATIVE_SCRIPT = ROOT / "scripts" / "workspace-native-on-frame.sh"
SPATIAL_AUX_ROLES = {"menubar": "menubar", "status": "status", **{f"menu:{i}": "menu" for i in range(4)}}
WORKSPACE_DASH_SCRIPT = ROOT / "scripts" / "workspace-dash-on-frame.sh"
WORKSPACE_EXIT_HELPER = ROOT / "scripts" / "workspace-exit-on-frame.py"
FRAME_VRCMD = "/opt/steamvr/bin/linuxarm64/vrcmd"

AGENT = Path(os.environ.get("FRAME_MAC_VIEW") or ROOT / "mac" / "bin" / "frame-mac-view")
REMOTE_PORTS = range(47900, 47920)
SUPPORTED = sys.platform == "darwin"

# Stream settings by name: long side in pixels, frames per second, H.264 bits
# per pixel per frame, codec. JPEG is for a Frame browser without H.264.
QUALITY = {
    "sharp": {"max": 2560, "fps": 60, "bpp": 0.14, "codec": "h264"},
    "balanced": {"max": 1920, "fps": 60, "bpp": 0.1, "codec": "h264"},
    "light": {"max": 1280, "fps": 30, "bpp": 0.08, "codec": "h264"},
    "compatible": {"max": 1280, "fps": 20, "bpp": 0.1, "codec": "jpeg"},
}
# Workspace has its own long-edge target because its dedicated OpenVR overlay
# can render the full ultrawide image. Ordinary viewers keep Sharp at 2560/60.
# VideoToolbox on the tested Mac fails asynchronously at 5120-wide H.264;
# 4096-wide encoding is verified. The logical desktop stays 5120 pixels wide.
WORKSPACE_QUALITY = {"sharp": {"max": 4096, "fps": 30, "bpp": 0.14, "codec": "h264"}}


def quality_settings(name, workspace=False):
    settings = (WORKSPACE_QUALITY.get(name) if workspace else None) or QUALITY.get(name) or QUALITY["balanced"]
    if workspace:
        backend = workspace_viewer_backend()
        if backend == "native":
            workspace_decoder()  # fail before creating the owned display
            workspace_conversion()  # keep backend selection explicit
            workspace_foveation()
            workspace_gaze()
            workspace_diagnostic_gaze(gaze=workspace_foveation() != "1" and workspace_gaze() == "1")
        if backend == "native" and settings["codec"] != "h264":
            raise MacViewError("The native Workspace viewer needs H.264. Choose Sharp, Balanced or Light.")
    if workspace and name == "sharp":
        fps = os.environ.get("FRAME_WORKSPACE_FPS", "30")
        if fps not in ("30", "60"):
            raise MacViewError("FRAME_WORKSPACE_FPS must be 30 or 60.")
        return {**settings, "fps": int(fps)}
    return settings


def workspace_viewer_backend():
    backend = os.environ.get("FRAME_WORKSPACE_VIEWER", "browser")
    if backend not in ("browser", "native"):
        raise MacViewError("FRAME_WORKSPACE_VIEWER must be browser or native.")
    return backend


def workspace_decoder():
    decoder = os.environ.get("FRAME_WORKSPACE_DECODER", "software")
    if decoder not in ("software", "v4l2"):
        raise MacViewError("FRAME_WORKSPACE_DECODER must be software or v4l2.")
    return decoder


def native_window_decoder():
    """Decoder for independent native window and Spatial auxiliary panels."""
    decoder = os.environ.get("FRAME_WINDOW_DECODER", "software")
    if decoder not in ("software", "v4l2"):
        raise MacViewError("FRAME_WINDOW_DECODER must be software or v4l2.")
    return decoder


def workspace_foveation():
    value = os.environ.get("FRAME_WORKSPACE_FOVEATION", "0")
    if value not in ("0", "1"):
        raise MacViewError("FRAME_WORKSPACE_FOVEATION must be 0 or 1.")
    return value


def workspace_gaze():
    """Gaze-directed detail for the native Workspace (on by default)."""
    value = os.environ.get("FRAME_WORKSPACE_GAZE", "1")
    if value not in ("0", "1"):
        raise MacViewError("FRAME_WORKSPACE_GAZE must be 0 or 1.")
    return value


def workspace_diagnostic_gaze(gaze=False):
    value = os.environ.get("FRAME_WORKSPACE_DIAGNOSTIC_GAZE", "")
    if not value:
        return None
    if (workspace_foveation() != "1" and not gaze) or os.environ.get("FRAME_MAC_VIEW_DIAGNOSTIC_GAZE") != "1":
        raise MacViewError("Diagnostic gaze requires FRAME_WORKSPACE_FOVEATION=1 or FRAME_WORKSPACE_GAZE=1, "
                           "and FRAME_MAC_VIEW_DIAGNOSTIC_GAZE=1.")
    if value == "scan":
        return value
    try:
        uv = [float(part) for part in value.split(",")]
        if len(uv) != 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in uv):
            raise ValueError()
    except ValueError:
        raise MacViewError("FRAME_WORKSPACE_DIAGNOSTIC_GAZE must be scan or finite U,V in [0,1].") from None
    return f"{uv[0]:.17g},{uv[1]:.17g}"


def window_displays():
    """FRAME_WINDOW_DISPLAYS=1: Desktop's native panels give each window a display
    of its own (Separate.swift), so its menus, sheets and menu bar come along and
    nothing overlaps it. Off: the window alone, at its size."""
    return os.environ.get("FRAME_WINDOW_DISPLAYS", "0") == "1"


def spatial_decode_pixels():
    """Total decoder capacity reserved for Spatial Mode, in pixels."""
    try:
        mp = float(os.environ.get("FRAME_SPATIAL_DECODE_MP", "18"))
    except ValueError:
        mp = 0
    pixels = mp * 1_000_000
    if not math.isfinite(pixels) or pixels < 1:
        raise MacViewError("FRAME_SPATIAL_DECODE_MP Must Be A Positive, Finite Number.")
    return int(pixels)


SPATIAL_FOCUSED_MAX_LONG = QUALITY["light"]["max"]
SPATIAL_FOCUSED_MAX_FPS = QUALITY["light"]["fps"]
SPATIAL_BACKGROUND_MAX_FPS = 10
SPATIAL_RESTORE_SECONDS = 2.0


def validate_spatial_selection(windows):
    """Only freshly enumerated individual identities may enter the workspace."""
    if not isinstance(windows, list) or not 1 <= len(windows) <= 32:
        raise MacViewError("Choose At Least One Individual Window Before Start.")
    selected = []
    seen = set()
    for window in windows:
        if (not isinstance(window, dict) or type(window.get("id")) is not int or
                not 0 < window["id"] <= 0xffffffff or type(window.get("pid")) is not int or
                window["pid"] <= 0 or not isinstance(window.get("stamp"), str) or
                not 1 <= len(window["stamp"]) <= 128 or window["id"] in seen):
            raise MacViewError("Window Selection Is Invalid. Choose The Window Again.")
        seen.add(window["id"])
        selected.append({key: window[key] for key in ("id", "pid", "stamp")})
    return selected


def spatial_launch_parallel():
    """How many Spatial window panels launch at once (FRAME_SPATIAL_LAUNCH_PARALLEL,
    1-8, default 4). The focused window goes first; the rest show placeholders."""
    try:
        value = int(os.environ.get("FRAME_SPATIAL_LAUNCH_PARALLEL", "4"))
    except ValueError:
        value = 0
    if not 1 <= value <= 8:
        raise MacViewError("FRAME_SPATIAL_LAUNCH_PARALLEL Must Be A Whole Number From 1 To 8.")
    return value


# What a saved window pose records besides the placement itself (FW-15): set
# by the user, for which window. Only userSet survives into Last Session.
POSE_PROVENANCE = ("userSet", "bundleID", "title", "session", "savedAt")
PLACEHOLDER_ERROR = "Couldn't open this window. Retrying…"


def wifi_mbps():
    """Bitrate ceiling on the network route, in Mbit/s (0 = none)."""
    try:
        value = float(os.environ.get("FRAME_MACVIEW_WIFI_MBPS", "12"))
    except ValueError:
        value = -1
    if not 0 <= value <= 1000:
        raise MacViewError("FRAME_MACVIEW_WIFI_MBPS must be 0 (no cap) to 1000.")
    return value


def workspace_refresh_hz():
    """The Workspace display's refresh rate, for the Swift helper.

    At 60 Hz a 60 fps capture needs every vsync, so each late app frame costs
    a streamed frame (about 58/s measured); at 120 Hz it can take the next one.
    FRAME_MAC_VIEW_VD_HZ=60 restores the previous display mode.
    """
    value = os.environ.get("FRAME_MAC_VIEW_VD_HZ", "120")
    try:
        hz = float(value)
    except ValueError:
        hz = 0
    if not 30 <= hz <= 240:
        raise MacViewError("FRAME_MAC_VIEW_VD_HZ must be a refresh rate from 30 to 240 Hz.")
    return value


def workspace_conversion():
    conversion = os.environ.get("FRAME_WORKSPACE_CONVERSION", "cpu")
    if conversion not in ("cpu", "gpu"):
        raise MacViewError("FRAME_WORKSPACE_CONVERSION must be cpu or gpu.")
    return conversion


# Extra Chromium flags for viewers (see docs/mac-in-headset.md, "Measuring").
BROWSER_FLAGS = []
# Viewer windows are fitted inside a panel's size. gamescope made a 1280x720
# request 1920x1080 anyway (verified 2026-09-28, build 20260925.6191901).
PANEL_BOX = (1920, 1080)

# Opens one viewer on gamescope's X display and gives its window its own panel
# id. Args: appid url width height tag [browser flags...]. The page puts "[tag]" in its title at
# once, which is how its X window is found (Chromium may hand the URL to an
# instance that's already running, so there's no process to follow).
LAUNCH = r"""set -u
appid=$1 url=$2 w=$3 h=$4 tag=$5
shift 5
session=${FRAME_WORKSPACE_SESSION:-}
exit_helper="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/workspace-exit-on-frame.py"
session_command() {
  [ -n "$session" ] || return 0
  [ -x "$exit_helper" ] || { echo 'The local Workspace Stop helper is missing.' >&2; exit 3; }
  python3 "$exit_helper" "$1" --session "$session"
}
if [ -n "$session" ]; then session_command begin; fi
export DISPLAY=:0 LC_ALL=C.UTF-8
unset WAYLAND_DISPLAY
if ! xprop -root GAMESCOPE_FOCUSABLE_WINDOWS >/dev/null 2>&1; then
  echo "The headset isn't showing anything (gamescope's display :0 isn't up). Wake it and try again." >&2
  exit 2
fi
common=(--ozone-platform=x11 --force-device-scale-factor=1 --no-first-run --no-default-browser-check
  --password-store=basic --disable-session-crashed-bubble --noerrdialogs --disable-infobars
  --disable-features=Translate,MediaRouter --autoplay-policy=no-user-gesture-required
  "--window-size=$w,$h" "$@" "--app=$url")
if [ -x "$HOME/chromium-xr/chrome" ]; then
  cmd=("$HOME/chromium-xr/chrome" "--user-data-dir=$HOME/.local/share/frame-workspace/mac-view" "${common[@]}")
elif flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  cmd=(flatpak run org.chromium.Chromium
    "--user-data-dir=$HOME/.var/app/org.chromium.Chromium/data/frame-workspace/mac-view" "${common[@]}")
elif flatpak --user info com.google.Chrome >/dev/null 2>&1; then
  cmd=(flatpak run --user com.google.Chrome
    "--user-data-dir=$HOME/.var/app/com.google.Chrome/data/frame-workspace/mac-view" "${common[@]}")
else
  echo "NO_BROWSER"
  exit 3
fi
log=/tmp/frame-mac-view.log
if [ -n "$session" ]; then
  python3 "$exit_helper" guarded-run --session "$session" -- \
    /bin/bash -c 'setsid nohup "$@" >>/tmp/frame-mac-view.log 2>&1 </dev/null &' frame-workspace-launch "${cmd[@]}"
else
  setsid nohup "${cmd[@]}" >>"$log" 2>&1 </dev/null &
fi
for _ in $(seq 1 60); do
  sleep 0.5
  # xprop, not xwininfo: in a C locale xwininfo can't print a non-ASCII title
  # at all, while xprop escapes those bytes and leaves the ASCII tag readable.
  for win in $(xwininfo -root -children 2>/dev/null | awk '/^ +0x/ {print $1}'); do
    xprop -id "$win" _NET_WM_NAME WM_NAME 2>/dev/null | grep -qF "[$tag]" || continue
    if xprop -id "$win" -f STEAM_GAME 32c -set STEAM_GAME "$appid" 2>/dev/null; then
      echo "panel valve.steam.desktopgame.$appid window $win"
      exit 0
    fi
  done
done
echo "The viewer started, but its window didn't appear within 30 s. Chromium's log:" >&2
tail -n 15 "$log" >&2
exit 1
"""

# Only the fork's browser profile is terminated when its last viewer closes.
# Wait for Chromium's profile lock to be released before another launch.
CLOSE_BROWSER = r"""set -eu
pattern='[f]rame-workspace/mac-view|[d]ata/frame-workspace/mac-view'
pkill -TERM -f "$pattern" >/dev/null 2>&1 || true
for _ in $(seq 1 15); do
  if ! pgrep -f "$pattern" >/dev/null 2>&1; then exit 0; fi
  sleep 0.2
done
pkill -KILL -f "$pattern" >/dev/null 2>&1 || true
for _ in $(seq 1 50); do
  if ! pgrep -f "$pattern" >/dev/null 2>&1; then exit 0; fi
  sleep 0.2
done
echo "Frame Workspace viewer browser did not exit" >&2
exit 1
"""


class MacViewError(Exception):
    pass

def validate_pose(value):
    """Strict pose-file format; limits and row tolerance follow placement.hpp.
    The bend (each half, side to side or top to bottom) is optional."""
    base = {"version", "width", "distance", "curvature", "matrix"}
    if not isinstance(value, dict) or set(value) - {"userSet"} not in (base, base | {"bend"}):
        raise MacViewError("Pose must contain exactly version, width, distance, curvature and matrix, "
                           "with an optional bend.")
    if "userSet" in value and value["userSet"] is not True:
        raise MacViewError("A pose's userSet marker can only be true.")
    if type(value["version"]) is not int or value["version"] != 1:
        raise MacViewError("Pose version must be 1.")

    def number(v, low, high):
        if type(v) not in (int, float) or not low <= v <= high or not math.isfinite(v):
            raise MacViewError("Pose numbers must be finite and within native placement limits.")
        return v

    width = number(value["width"], .01, 8)
    distance = number(value["distance"], .4, 10)
    curvature = number(value["curvature"], 0, 1)
    matrix = value["matrix"]
    if not isinstance(matrix, list) or len(matrix) != 12:
        raise MacViewError("Pose matrix must contain 12 numbers.")
    matrix = [number(v, -100, 100) for v in matrix]
    rows = [matrix[i:i + 3] for i in (0, 4, 8)]
    for r in range(3):
        for s in range(3):
            if abs(sum(a * b for a, b in zip(rows[r], rows[s])) - (1 if r == s else 0)) > .02:
                raise MacViewError("Pose rotation must be orthonormal within 0.02.")
    a, b, c = rows
    determinant = (a[0] * (b[1] * c[2] - b[2] * c[1]) -
                   a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0]))
    if determinant <= 0:
        raise MacViewError("Pose rotation must be right handed.")
    pose = {"version": 1, "width": width, "distance": distance, "curvature": curvature, "matrix": matrix}
    if "bend" in value:
        bend = value["bend"]
        if (not isinstance(bend, dict) or set(bend) != {"vertical", "a", "b"} or
                type(bend["vertical"]) is not bool):
            raise MacViewError("Pose bend must contain exactly vertical (true or false), a and b.")
        pose["bend"] = {"vertical": bend["vertical"], "a": number(bend["a"], 0, 1), "b": number(bend["b"], 0, 1)}
    if value.get("userSet") is True:
        pose["userSet"] = True
    return pose


def launcher_pose(pose):
    """The pose as the Frame launcher takes it: placement only (it records the
    provenance itself)."""
    return json.dumps({k: v for k, v in pose.items() if k != "userSet"}, allow_nan=False)


def _pose_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MacViewError("Pose JSON contains duplicate keys.")
        result[key] = value
    return result


class WorkspaceDisplayManager:
    """Own the foreground Swift workspace process for one MacView server.

    `workspace` is the stable API identity. The display ID in the helper's
    ready record is kept only in memory and refreshed each time it starts.
    """

    KEY = "workspace"
    PRESETS = {
        "5120x1440": (5120, 1440),
        "5120x2160": (5120, 2160),
        "3440x1440": (3440, 1440),
        "2560x2880": (2560, 2880),
        "2880x2560": (2880, 2560),
        "3840x2160": (3840, 2160),
    }

    def __init__(self, executable=AGENT, track=None, on_unexpected_stop=None):
        self.executable = Path(executable)
        self.track = track or (lambda proc: None)
        self.on_unexpected_stop = on_unexpected_stop
        self._operation_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._owner = None

    @staticmethod
    def _validate_request(preset, scale, quality):
        if not isinstance(preset, str) or preset not in WorkspaceDisplayManager.PRESETS:
            raise MacViewError("Choose a supported Workspace size: " + ", ".join(WorkspaceDisplayManager.PRESETS) + ".")
        if type(scale) is not int or scale not in (1, 2):
            raise MacViewError("Workspace scale must be 1 or 2.")
        if not isinstance(quality, str) or quality not in QUALITY:
            raise MacViewError("Choose a supported Mac stream quality.")
        quality_settings(quality, workspace=True)  # reject invalid mode before creating a display
        width, height = WorkspaceDisplayManager.PRESETS[preset]
        if width * height * scale * scale > 67_108_864:
            raise MacViewError("That Workspace buffer exceeds the safe size limit.")
        return {"preset": preset, "width": width, "height": height, "scale": scale, "quality": quality}

    @staticmethod
    def _validate_ready(record, config):
        source = record.get("src")
        display_id = record.get("displayID")
        if (record.get("state") != "ready" or not isinstance(display_id, int) or isinstance(display_id, bool)
                or source != f"display:{display_id}"):
            raise MacViewError("The Workspace helper returned an invalid display identity.")
        actual = record.get("actual") or {}
        expected = (config["width"], config["height"], config["width"] * config["scale"], config["height"] * config["scale"])
        got = tuple(actual.get(k) for k in ("logicalWidth", "logicalHeight", "bufferWidth", "bufferHeight"))
        if got != expected:
            raise MacViewError(f"macOS created {got[0]}×{got[1]} logical / {got[2]}×{got[3]} pixels, "
                               f"not the requested {expected[0]}×{expected[1]} @{config['scale']}x mode.")
        if record.get("mirrored") is not False or record.get("main") is not False or record.get("physicalLayoutUnchanged") is not True:
            raise MacViewError("macOS changed the physical display layout or mirrored Workspace; the helper is rolling it back.")

    def _read_output(self, owner):
        try:
            for line in owner["proc"].stdout:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    owner["diagnostic"] = raw[-500:]
                    continue
                with self._state_lock:
                    if owner["record"] is None and event.get("state") == "ready":
                        try:
                            self._validate_ready(event, owner["config"])
                            owner["record"] = event
                        except MacViewError as e:
                            owner["error"] = str(e)
                        owner["ready"].set()
                    elif owner["record"] is None and event.get("state") == "error":
                        owner["error"] = event.get("error") or event.get("startupError") or "Workspace creation failed."
                        owner["terminal"] = event
                        owner["ready"].set()
                    elif event.get("state") in ("stopped", "error"):
                        owner["terminal"] = event
                        if event.get("state") == "error":
                            owner["error"] = event.get("error") or event.get("startupError") or owner["error"]
        except (OSError, ValueError) as e:
            with self._state_lock:
                owner["diagnostic"] = str(e)
        finally:
            try:
                owner["proc"].stdout.close()
            except (AttributeError, OSError):
                pass
            with self._state_lock:
                if not owner["ready"].is_set():
                    owner["error"] = owner["diagnostic"] or "The Workspace helper exited before creating a display."
                    owner["ready"].set()
                if owner["terminal"] is None and owner["proc"].poll() is not None:
                    owner["terminal"] = {"state": "error", "removed": False,
                                          "error": owner["diagnostic"] or "The Workspace helper exited unexpectedly."}
                    owner["error"] = owner["terminal"]["error"]
                unexpected = bool(owner["record"] and owner["terminal"] and not owner.get("stopping")
                                  and not owner.get("on_end_called"))
                if unexpected:
                    owner["on_end_called"] = True
                    ended_src = owner["record"]["src"]
                owner["output_done"].set()
            if unexpected and self.on_unexpected_stop:
                try:
                    self.on_unexpected_stop(ended_src)
                except Exception:
                    pass

    def start(self, preset="5120x1440", scale=1, quality="sharp"):
        config = self._validate_request(preset, scale, quality)
        with self._operation_lock:
            with self._state_lock:
                owner = self._owner
                if owner and owner["proc"].poll() is None and owner["record"]:
                    if owner["terminal"] is not None:
                        raise MacViewError("The Workspace helper is stopping; wait for it to finish before starting again.")
                    display_keys = ("preset", "width", "height", "scale")
                    if any(owner["config"][key] != config[key] for key in display_keys):
                        raise MacViewError("A Workspace is already running with different settings; stop it before changing size or scale.")
                    owner["config"] = config  # stream quality can change without replacing the display
                    return self._public_state(owner), False
                if owner and owner["proc"].poll() is None:
                    raise MacViewError("The Workspace helper is still starting; try again shortly.")
            if not self.executable.exists():
                raise MacViewError("The Mac streaming helper is missing; rebuild Frame Control first.")
            width, height = config["width"], config["height"]
            args = [str(self.executable), "workspace", "--width", str(width), "--height", str(height),
                    "--scale", str(scale), "--exit-on-stdin-eof"]
            env = {**os.environ, "FRAME_MAC_VIEW_VD_HZ": workspace_refresh_hz()}
            try:
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
            except OSError as e:
                raise MacViewError(f"Couldn't start the Workspace helper: {e}")
            owner = {"proc": proc, "config": config, "record": None, "error": None,
                     "terminal": None, "diagnostic": "", "ready": threading.Event(),
                     "output_done": threading.Event(), "stopping": False, "on_end_called": False}
            with self._state_lock:
                self._owner = owner
            self.track(proc)
            threading.Thread(target=self._read_output, args=(owner,), daemon=True,
                             name="frame-workspace-output").start()
            if not owner["ready"].wait(25):
                self._stop_owner(owner)
                raise MacViewError("macOS did not report a ready Workspace display within 25 seconds.")
            with self._state_lock:
                error, record = owner["error"], owner["record"]
            if error or not record:
                cleanup = self._stop_owner(owner)
                detail = error or "The Workspace helper did not report a display."
                if cleanup.get("removed") is False:
                    detail += " Display removal is still pending; retry Stop after the Mac wakes."
                raise MacViewError(detail)
            return self._public_state(owner), True

    def _public_state(self, owner):
        record, config = owner["record"], owner["config"]
        actual = record["actual"]
        q = quality_settings(config["quality"], workspace=True)
        factor = min(1.0, q["max"] / max(actual["bufferWidth"], actual["bufferHeight"], 1))
        stream_w = max(16, round(actual["bufferWidth"] * factor / 2) * 2)
        stream_h = max(16, round(actual["bufferHeight"] * factor / 2) * 2)
        return {
            "id": self.KEY, "state": "running", "name": record["name"],
            "displayID": record["displayID"], "captureSrc": record["src"],
            "preset": config["preset"], "scale": config["scale"], "quality": config["quality"],
            "display": {"logicalWidth": actual["logicalWidth"], "logicalHeight": actual["logicalHeight"],
                        "bufferWidth": actual["bufferWidth"], "bufferHeight": actual["bufferHeight"]},
            "stream": {"width": stream_w, "height": stream_h, "maxLong": q["max"],
                       "fps": q["fps"], "codec": q["codec"]},
            "bounds": record.get("bounds"), "mirrored": False, "main": False,
            "physicalLayoutUnchanged": True, "windowRestoreAvailable": record.get("windowRestoreAvailable", False),
            "snapshottedWindows": record.get("snapshottedWindows", 0),
        }

    def state(self):
        with self._state_lock:
            owner = self._owner
            if owner is None:
                return {"id": self.KEY, "state": "stopped"}
            if owner["terminal"] is not None:
                terminal = owner["terminal"]
                if terminal.get("state") == "stopped" and terminal.get("removed") is True:
                    return {"id": self.KEY, "state": "stopped", "removed": True}
                state = {"id": self.KEY, "state": "error", "error": owner["error"] or terminal.get("error") or
                         "The Workspace helper exited unexpectedly.", "removed": terminal.get("removed", False)}
                if owner["record"]:
                    state.update({"displayID": owner["record"]["displayID"], "captureSrc": owner["record"]["src"]})
                return state
            if owner["proc"].poll() is not None:
                terminal = owner["terminal"] or {}
                if terminal.get("state") == "stopped" and terminal.get("removed") is True:
                    return {"id": self.KEY, "state": "stopped", "removed": True}
                state = {"id": self.KEY, "state": "error", "error": owner["error"] or terminal.get("error") or
                         "The Workspace helper exited unexpectedly.", "removed": terminal.get("removed", False)}
                if owner["record"]:
                    state.update({"displayID": owner["record"]["displayID"], "captureSrc": owner["record"]["src"]})
                return state
            if not owner["record"]:
                return {"id": self.KEY, "state": "starting"}
            return self._public_state(owner)

    def _stop_owner(self, owner):
        with self._state_lock:
            owner["stopping"] = True
        proc = owner["proc"]
        if proc.poll() is None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()  # the Swift owner treats EOF as graceful Stop
                proc.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
        owner["output_done"].wait(1)
        with self._state_lock:
            terminal = owner["terminal"]
            if terminal is None:
                result = {"id": self.KEY, "state": "error", "removed": False,
                          "error": "The Workspace helper exited without confirming display removal."}
            else:
                result = {**terminal, "id": self.KEY}
            if self._owner is owner:
                self._owner = None
        return result

    def stop(self):
        with self._operation_lock:
            with self._state_lock:
                owner = self._owner
            if owner is None:
                return {"id": self.KEY, "state": "stopped", "removed": True, "owned": False}
            return self._stop_owner(owner)

    def recover_orphan(self):
        """Ask a short-lived Swift owner to wake and wait for stale removal.

        The Swift command takes the same advisory lease as a normal owner. If
        another live Workspace owns the display, it reports that fact without
        changing or releasing that display.
        """
        with self._operation_lock:
            with self._state_lock:
                owner = self._owner
                if owner and owner["proc"].poll() is None:
                    raise MacViewError("A live Workspace process is already owned by this companion.")
            try:
                proc = subprocess.Popen([str(self.executable), "workspace", "--recover-orphan"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                self.track(proc)
                out, _ = proc.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
                raise MacViewError("macOS did not finish Workspace orphan recovery within 12 seconds.")
            except OSError as e:
                raise MacViewError(f"Couldn't start Workspace recovery: {e}")
            lines = [line for line in (out or "").splitlines() if line.strip()]
            try:
                event = json.loads(lines[-1]) if lines else {}
            except ValueError:
                event = {}
            if proc.returncode != 0 or event.get("state") != "stopped" or event.get("removed") is not True:
                raise MacViewError(event.get("error") or "Workspace is still registered; macOS did not confirm removal.")
            return {"id": self.KEY, "owned": False, **event}


def panel_id(src):
    """A stable panel id per source, in the range panel-on-frame.sh uses."""
    return 2_001_000_000 + zlib.crc32(f"mac:{src}".encode()) % 1_000_000


def beside_workspace_pose(matrix, width, distance, logical_width, window_width, slot):
    """A native window panel's first pose, beside the Workspace.

    `matrix` is the Workspace's 3x4 row-major transform (columns: right, up,
    normal toward the viewer, centre). The panel sits on the circle around
    the viewing point at the Workspace's distance, just past its edge (slot 0
    right, 1 left, 2 further right, ...), turned to face the viewer, and as
    wide as the window would be at the Workspace's scale (same text size)."""
    m = [float(v) for v in matrix]
    right, up, normal = [m[0], m[4], m[8]], [m[1], m[5], m[9]], [m[2], m[6], m[10]]
    centre = [m[3], m[7], m[11]]
    d = min(max(float(distance), 0.4), 10.0)
    panel = min(max(float(window_width) / max(float(logical_width), 1.0) * float(width), 0.3), 3.0)
    gap = 0.1
    arc = float(width) / 2 + gap + panel / 2 + (slot // 2) * (panel + gap)
    angle = (1 if slot % 2 == 0 else -1) * min(arc / d, 1.4)
    viewer = [c + n * d for c, n in zip(centre, normal)]
    facing = [n * math.cos(angle) - r * math.sin(angle) for n, r in zip(normal, right)]
    position = [v - f * d for v, f in zip(viewer, facing)]
    side = [up[1] * facing[2] - up[2] * facing[1], up[2] * facing[0] - up[0] * facing[2],
            up[0] * facing[1] - up[1] * facing[0]]
    rows = [[side[i], up[i], facing[i], position[i]] for i in range(3)]
    return json.dumps({"version": 1, "width": round(panel, 4), "distance": round(d, 4), "curvature": 0,
                       "matrix": [round(v, 6) for row in rows for v in row]})


def fit(w, h, box=PANEL_BOX):
    s = min(box[0] / max(w, 1), box[1] / max(h, 1))
    return max(320, round(w * s)), max(200, round(h * s))


class MacView:
    def __init__(self, tunnel_ssh, run, frame, track=None, last_workspace_file=None):
        self.tunnel_ssh = list(tunnel_ssh)
        self.run = run
        self.frame = frame
        self.host_opts = []  # the headset in use and how to reach it (see retarget)
        # Short, never held across ssh: retarget() and publishing a new tunnel check and
        # change the headset together (self.lock is held while a tunnel is being opened).
        self.route_lock = threading.Lock()
        self.track = track or (lambda proc: None)  # the server ends these on exit
        self.workspace_manager = WorkspaceDisplayManager(track=self.track, on_unexpected_stop=self._workspace_ended)
        self.workspace_lock = threading.RLock()
        self.workspace_removal_pending = False
        self.mode = "desktop"
        self.spatial_workspace = {"state": "stopped", "removed": True}
        self._mode_generation = 0
        self.session_id = secrets.token_hex(16)
        self.exit_pending = False
        self.exit_error = None
        self._exit_helper_frame = None
        self._frame_stop_available = False
        self._spatial_agent = None
        self._spatial_owned = set()
        self._spatial_suppressed = set()
        self._spatial_retry = {}
        self._spatial_resume_poses = {}
        self._spatial_reconcile_queued = False
        # Parallel window launches (FW-15): sources in flight -> their mode
        # generation, wall slots reserved before a window has its panel, and
        # the placeholder list the Frame last confirmed.
        self._spatial_launching = {}
        self._spatial_admitted = {}
        self._mouse_selection_required = False
        self._mouse_selection_pending = []
        self._spatial_mouse_ticket = ""
        self._spatial_legacy_all = False
        self._spatial_slots = {}
        self._placeholders_synced = None
        self._placeholders_on_frame = False
        self._placeholder_retry_after = 0
        self._launch_run = self._launch_thread
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(24)
        self.agent = None
        self.port = None
        self.tunnel = None
        self.remote_port = None
        self.supervisor = None
        self.closing = False
        self.shows = 0  # counts Show presses, so a late cleanup can't close a new viewer
        self.launching = 0  # Shows in progress (one may be replacing its own stream)
        # Held by a Show while it counts itself in, and by the cleanup for its
        # check and its pkill together, so a Show can't start in between.
        self.viewer_lock = threading.Lock()
        # Use the Frame's USB-C network when it's plugged into this Mac (see
        # _usb_route); FRAME_MACVIEW_USB=0 turns that off.
        self.prefer_usb = os.environ.get("FRAME_MACVIEW_USB") != "0"
        self.route = "network"
        self.usb_ip, self._usb_misses = None, 0
        self.native_panels = {}  # src -> {"panel", "key", "slot"}
        self._link_applied = None
        self._pointer_applied = None
        self._spatial_applied = None
        self._spatial_candidates = {}
        self._spatial_live_since = {}
        self._spatial_focused = None
        self._spatial_hovered = None
        self._spatial_budget_wake = threading.Event()
        self._spatial_requested_sizes = {}
        self._spatial_policy_targets = {}
        self._spatial_policy_agent = None
        self._spatial_restore_since = {}
        self._spatial_lock = threading.Lock()
        self._pending_focus = None
        self._focus_queued = False
        self._last_frame_focus = -math.inf
        self.shown = set()  # sources with a viewer out there, connected or retrying
        self.browser_flags = list(BROWSER_FLAGS)
        self.browser_lock = threading.Lock()
        self.browser_cleanup_error = None
        self.workspace_panel_key = None
        # The SteamVR bar panel (FW-08), see start_dash.
        self.dash_thread = None
        self.dash_action = None  # "starting"/"stopping" while a headset press is carried out
        self.dash_jobs = queue.Queue()
        self.dash_lock = threading.Lock()
        self.dash_worker = None
        # Headset app actions, switcher and focus requests: their own lane, so
        # a click never waits behind Spatial startup or panel launches.
        self.action_jobs = queue.Queue(maxsize=32)
        self.action_lock = threading.Lock()
        self.action_worker = None
        # Recent failed headset actions and panel launches, for the bar panel
        # and the Mac page (newest last).
        self.recent_errors = collections.deque(maxlen=8)
        self._stop_requested = 0  # Mac-page Stops waiting for the Workspace lock
        self._spatial_cover = False  # the helper's laptop cover is showing
        # The Frame's native viewer: checked on connect and before each start.
        self.headset_viewer = HeadsetViewer(lambda *args, **kwargs: self.run(*args, **kwargs))
        self.spatial_apps = SpatialApps(self)
        self.dash_wake = self.spatial_apps.wake  # one waker for the bar and the spatial state
        self.last_workspace_file = last_workspace_file
        self.last_workspace = ("5120x1440", 1, "sharp")
        self._load_last_workspace()
        # Session auto-save (frame_workspace_profiles.SessionKeeper) hooks.
        self.before_workspace_stop = None
        self.after_workspace_start = None
        self.resume_workspace_pose = None

    # ---- the agent on this Mac ----

    def unavailable(self):
        """Why this can't work here, or None."""
        if not SUPPORTED:
            return "Streaming your computer into the headset needs macOS."
        if not AGENT.exists() and not (SOURCES.exists() and shutil.which("xcrun")):
            return "The Mac streaming helper is missing from this copy of Frame Control."
        return None

    def build(self):
        if AGENT.exists() and not self._stale():
            return
        if not (SOURCES / "build.sh").exists():
            if AGENT.exists():
                return
            raise MacViewError("The Mac streaming helper is missing.")
        r = subprocess.run(["/bin/sh", str(SOURCES / "build.sh"), str(AGENT)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=600)
        if r.returncode != 0:
            raise MacViewError("Couldn't build the Mac streaming helper: " + (r.stderr or r.stdout).strip()[-400:])

    def _stale(self):
        try:
            built = AGENT.stat().st_mtime
            return any(p.stat().st_mtime > built for p in (SOURCES / "Sources").glob("*.swift"))
        except OSError:
            return False

    def ensure_agent(self):
        with self.lock:
            if self.agent and self.agent.poll() is None:
                return
            reason = self.unavailable()
            if reason:
                raise MacViewError(reason)
            self.build()
            env = {**os.environ, "FRAME_MAC_VIEW_TOKEN": self.token}
            # The same port as before when restarting, so a running tunnel still
            # fits; otherwise (or if it's gone) whatever the system gives.
            for port in dict.fromkeys([self.port or 0, 0]):
                self.agent = subprocess.Popen([str(AGENT), "serve", "--port", str(port), "--page", str(PAGE),
                                               "--exit-on-eof"], env=env, stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                line = self.agent.stdout.readline()
                m = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
                if m:
                    break
                self.agent.kill()
            else:
                raise MacViewError(f"The Mac streaming helper didn't start: {line.strip() or 'no output'}")
            if self.port != int(m.group(1)):
                self._drop_tunnel()
            self.port = int(m.group(1))
            self.track(self.agent)
            threading.Thread(target=self._keep_helper_log, args=(self.agent,), daemon=True).start()

    @staticmethod
    def helper_log_path():
        """The helper's log lives beside the app's server.log: ~/Library/Logs/Frame Workspace/
        in the Workspace app, ~/Library/Logs/Frame Control/ in Frame Control. The app tells
        the server where its log is (FRAME_CONTROL_LOG); a bare run uses Frame Control's folder."""
        server_log = os.environ.get("FRAME_CONTROL_LOG")
        folder = Path(server_log).parent if server_log else Path.home() / "Library" / "Logs" / "Frame Control"
        return folder / "mac-helper.log"

    HELPER_LOG_LIMIT = 4 << 20

    def _keep_helper_log(self, agent):
        """Keeps the helper's own messages (its errors explain refused streams, failed
        Spatial steps and every shelf click it was asked to carry out) in mac-helper.log
        beside server.log. Each helper adds to the file under a header line, so a restart
        does not wipe what an earlier one wrote; a file past 4 MB is moved to
        mac-helper.log.1 first."""
        path = self.helper_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > self.HELPER_LOG_LIMIT:
                os.replace(path, path.with_name(path.name + ".1"))
            log = open(path, "a", encoding="utf-8")
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---- helper pid {agent.pid} started ----\n")
            log.flush()
        except OSError:
            log = None
        written = 0
        for line in agent.stdout:
            if log and written < 4 << 20:
                stamp = time.strftime("%H:%M:%S")
                log.write(f"{stamp} {line}")
                log.flush()
                written += len(line)
        if log:
            log.close()

    def call(self, path, method="GET", **query):
        """A request to the agent; starts it if needed."""
        self.ensure_agent()
        url = f"http://127.0.0.1:{self.port}{path}?{urlencode({**query, 'k': self.token}, quote_via=quote)}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=120 if path.startswith("/spatial/") else 10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(65536).decode("utf-8", errors="replace")
                try:
                    detail = json.loads(body).get("error")
                except (ValueError, AttributeError):
                    detail = body.strip()
                message = detail if isinstance(detail, str) and detail else str(e)
            except OSError:
                message = str(e)
            finally:
                e.close()
            raise MacViewError(f"The Mac Streaming Helper: {message}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise MacViewError(f"The Mac streaming helper didn't answer: {e}")

    # ---- the tunnel from the Frame ----

    def _drop_tunnel(self):
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        self.tunnel = None

    def tunnel_up(self):
        return self.tunnel is not None and self.tunnel.poll() is None

    def ensure_tunnel(self, allow_new_port=False):
        """Open the tunnel, on the port viewers already use if there is one.
        A new port only when nobody is watching, since viewers can't move."""
        with self.lock:
            if self.tunnel_up():
                return
            last = ""
            ports = [self.remote_port] if self.remote_port else list(REMOTE_PORTS)
            if self.remote_port and allow_new_port:
                ports += [p for p in REMOTE_PORTS if p != self.remote_port]
            usb = self._usb_route()
            # USB-C first when it's there; if that fails (unplugged just now,
            # something else at that address), the normal path.
            for via in ([usb, []] if usb else [[]]):
                self.route = "usb" if via else "network"
                if self._open_tunnel(via, ports):
                    return
                last = self._last_tunnel_error
            raise MacViewError(f"Couldn't open a tunnel from {self.frame} to this Mac: {last or 'no answer through it'}")

    def retarget(self, alias, host_opts):
        """The server now reaches the headset as `alias` with `host_opts` (another address,
        or another headset). Only the short route_lock, never self.lock: this runs while
        the server routes, which a tunnel being opened may be waiting on."""
        # host_opts is "-o", "Name=value" pairs; the tunnel keeps its own connection,
        # not the shared master.
        opts = [x for flag, value in zip(host_opts[::2], host_opts[1::2])
                if not value.startswith("ControlPath=") for x in (flag, value)]
        with self.route_lock:
            moved = alias != self.frame
            self.frame, self.host_opts = alias, opts
            if moved:
                self._exit_helper_frame = None
            tunnel = self.tunnel
            if moved and tunnel is not None:
                # Another headset: its viewers can't be the old one's. The supervisor
                # reopens a tunnel to the new one if anything is being shown.
                self.tunnel, self.remote_port = None, None
        if moved and tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()

    def _open_tunnel(self, via, ports):
        """Tries the ports on one route; True once the tunnel answers. With self.lock held."""
        last = ""
        for port in ports:
            with self.route_lock:
                target = (self.frame, self.host_opts)  # retarget() may change these meanwhile
            # `via` first: ssh keeps the first value of an option, so USB-C's HostName wins
            # while the headset's pinned identity (in host_opts) still checks it.
            # A tunnel-only (-N) connection keeps Nagle on and uses SSH's bulk
            # traffic class: verified 2026-10-19, ssh -vv never logs "setting
            # TCP_NODELAY" for it. With Nagle each frame's last packet waited for
            # the Frame's delayed ACK, adding up to tens of ms of irregular delay.
            # A do-nothing terminal session makes both ends interactive
            # (TCP_NODELAY, low-delay QoS); IPQoS af41 asks Wi-Fi for its video
            # queue. "-e none" so no byte in the stream is an escape.
            proc = subprocess.Popen([*self.tunnel_ssh, "-o", "ControlPath=none", *via, *target[1],
                                     "-o", "ExitOnForwardFailure=yes", "-o", "IPQoS=af41",
                                     "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                                     "-tt", "-e", "none",
                                     "-R", f"127.0.0.1:{port}:127.0.0.1:{self.port}", target[0],
                                     "exec sleep 2147483647"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
            # A taken port makes ssh exit once it's connected; a working
            # tunnel answers the agent's status from the Frame's side.
            ok = False
            for _ in range(15):
                time.sleep(0.4)
                if proc.poll() is not None:
                    break
                if self._probe(port):
                    ok = True
                    break
            if ok:
                with self.route_lock:  # checked and published together, so a switch can't slip between
                    ok = target[0] == self.frame  # else the app switched headset while this connected
                    if ok:
                        self.tunnel, self.remote_port = proc, port
            if ok:
                self.track(proc)
                self._supervise()
                return True
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
            last = (proc.stderr.read() or "").strip()
            if "forward" not in last.lower():
                break  # not a port clash: the Frame is unreachable this way
        self._last_tunnel_error = last
        return False

    def _usb_route(self):
        """ssh options to reach the Frame over its USB-C network, or [].

        Plugged into a Mac, the Frame is a USB network device (macOS lists it
        as "Steam Frame"): the Frame's usb0 answers at about 1 ms, with none
        of Wi-Fi's stalls. Measured 2026-09-28: content latency 7 ms instead
        of 10, click to screen 17 ms instead of 27 (docs/mac-in-headset.md).
        The host key is the same, so it's checked against the usual name."""
        if not self.prefer_usb:
            return []
        try:
            out = self.run("ip -4 -o addr show usb0 2>/dev/null", timeout=8)
        except Exception:
            return []
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out or "")
        if not m:
            return []
        ip = m.group(1)
        try:
            socket.create_connection((ip, 22), timeout=1).close()
        except OSError:
            return []  # not plugged into this Mac
        self.usb_ip = ip
        if any(o.startswith("HostKeyAlias=") for o in self.host_opts):
            return ["-o", f"HostName={ip}"]  # checked against the headset's own pinned key
        alias = self.frame
        try:
            cfg = subprocess.run(["ssh", "-G", self.frame], capture_output=True, text=True, timeout=5).stdout
            opts = dict(line.split(None, 1) for line in cfg.splitlines() if " " in line)
            alias = opts.get("hostkeyalias") or opts.get("hostname") or alias  # a configured alias wins
        except (OSError, subprocess.SubprocessError):
            pass
        return ["-o", f"HostName={ip}", "-o", f"HostKeyAlias={alias}"]

    def observe_mouse_hover(self, source):
        """Coalesce the existing 10 Hz switcher monitor into a budget wake.

        This does not commit focus or enable a stream; the supervisor reads the
        helper's leased status and applies the same cap as its periodic pass.
        """
        source = source if isinstance(source, str) and re.fullmatch(r"spatial:[0-9]+", source) else None
        with self._spatial_lock:
            if source == self._spatial_hovered:
                return
            self._spatial_hovered = source
            self._spatial_budget_wake.set()

    def _supervise(self):
        """Reopen the tunnel on the same port after the headset sleeps or the
        network drops, so viewers that are retrying find the Mac again."""
        if self.supervisor and self.supervisor.is_alive():
            return

        def loop():
            tick = 0
            next_periodic = time.monotonic() + 1
            while not self.closing:
                self._spatial_budget_wake.wait(max(0, next_periodic - time.monotonic()))
                self._spatial_budget_wake.clear()
                periodic = time.monotonic() >= next_periodic
                if periodic:
                    next_periodic = time.monotonic() + 1
                    tick += 1
                if self.closing:
                    continue
                if not (self.agent and self.agent.poll() is None):
                    self._spatial_mode_tick()  # report a lost home owner and retire its Frame panels
                    continue
                try:
                    self._spatial_tick()
                except MacViewError:
                    pass  # retry the local helper next second, even without a tunnel
                if not periodic:
                    continue  # hover only wakes budget policy, not network maintenance
                if self.tunnel_up():
                    self._follow_best_route(tick)
                    self._apply_link_cap()
                    continue
                if tick % 5:
                    continue
                try:
                    self.ensure_tunnel()
                except MacViewError:
                    pass  # still unreachable; try again shortly

        self.supervisor = threading.Thread(target=loop, daemon=True)
        self.supervisor.start()

    def _spatial_tick(self):
        """Budget window decoders on the supervisor tick and coalesced hover wakes.

        A changed window stays eligible for five seconds. Optional additions
        need one second of observation; admitted windows get three seconds
        before a newer contender can replace them. Focus and the pixel cap
        override this residency. Focus is the helper's (`focusedSrc`): the
        window the Mac actually has active, which the headset's eyes only
        highlight and never move (FW-15), so it is always first and the rest
        fit around it. No remote calls or panel launches here.
        """
        self._spatial_mode_tick()
        enabled = self._window_displays()
        if self._spatial_applied is not None and self._spatial_applied[0] != id(self.agent):
            self._spatial_candidates.clear()
            self._spatial_live_since.clear()
            self._spatial_requested_sizes.clear()
            self._spatial_policy_targets.clear()
            self._spatial_restore_since.clear()
            self._spatial_policy_agent = id(self.agent)
        pointer_key = (id(self.agent), enabled)
        if self._pointer_applied != pointer_key:
            self.call("/pointer", method="POST", shared="1" if enabled else "0")
            self._pointer_applied = pointer_key
        if not enabled and self._spatial_applied is None:
            return
        self._flush_frame_focus()
        status = self.call("/status")
        streams = status.get("streams", [])
        windows = {}
        auxiliaries = {}
        reserved = 0
        unknown_auxiliary = False
        for stream in streams:
            src = stream.get("src", "")
            if not isinstance(src, str):
                continue
            w, h = stream.get("width"), stream.get("height")
            pixels = w * h if type(w) is int and type(h) is int and w > 0 and h > 0 else None
            budget = stream.get("budget") or (stream.get("stats") or {}).get("budget") or {}
            requested_w = stream.get("requestedWidth", budget.get("requestedWidth", w))
            requested_h = stream.get("requestedHeight", budget.get("requestedHeight", h))
            requested_pixels = (requested_w * requested_h if type(requested_w) is int and type(requested_h) is int
                                and requested_w > 0 and requested_h > 0 else None)
            has_requested_size = ((type(stream.get("requestedWidth")) is int and type(stream.get("requestedHeight")) is int)
                                  or (type(budget.get("requestedWidth")) is int and type(budget.get("requestedHeight")) is int))
            if not src.startswith(("window:", "separate:", "spatial:")):
                if src in SPATIAL_AUX_ROLES:
                    if stream.get("visible") is not True:
                        continue  # hidden menu slots do not enter or consume the capture budget
                    entry = auxiliaries.setdefault(src, {"pixels": 0, "ids": [], "changed": 0, "live": True})
                    entry["pixels"] = None if pixels is None or entry["pixels"] is None else entry["pixels"] + pixels
                    entry["ids"].append(stream.get("id"))
                    entry["live"] = entry["live"] and stream.get("live") is True
                    changed = stream.get("lastChanged", 0)
                    if type(changed) in (int, float) and math.isfinite(changed):
                        entry["changed"] = max(entry["changed"], changed)
                    continue
                if pixels is None:
                    unknown_auxiliary = True
                else:
                    reserved += pixels
                continue
            entry = windows.setdefault(src, {"pixels": 0, "requestedPixels": 0, "requestedLong": 0,
                                             "explicitRequested": True, "changed": 0, "ids": [], "live": True})
            entry["pixels"] = None if pixels is None or entry["pixels"] is None else entry["pixels"] + pixels
            entry["requestedPixels"] = (None if requested_pixels is None or entry["requestedPixels"] is None
                                        else entry["requestedPixels"] + requested_pixels)
            entry["explicitRequested"] = entry["explicitRequested"] and has_requested_size
            if type(requested_w) is int and type(requested_h) is int and requested_w > 0 and requested_h > 0:
                entry["requestedLong"] = max(entry["requestedLong"], requested_w, requested_h)
            changed = stream.get("lastChanged", 0)
            if type(changed) in (int, float) and math.isfinite(changed):
                entry["changed"] = max(entry["changed"], changed)
            entry["ids"].append(stream.get("id"))
            entry["live"] = entry["live"] and stream.get("live") is True

        if not enabled:
            if self._spatial_policy_targets:
                try:
                    self.call("/streams/policy", method="POST", all="1")
                except MacViewError:
                    pass  # Older helpers still restore live streams below.
            self.call("/streams/live", method="POST", all="1")
            self._spatial_applied = None
            self._spatial_candidates.clear()
            self._spatial_live_since.clear()
            self._spatial_requested_sizes.clear()
            self._spatial_policy_targets.clear()
            self._spatial_restore_since.clear()
            with self._spatial_lock:
                self._pending_focus = None
                self._spatial_focused = None
            return

        now, wall = time.monotonic(), time.time()
        with self._spatial_lock:
            focus = status.get("focusedSrc") or self._spatial_focused
        # Session identities stop a reconnect inheriting a departed viewer's lease.
        identities = {s: tuple(sorted(e["ids"], key=str)) for s, e in windows.items()}
        auxiliary_identities = {s: tuple(sorted(e["ids"], key=str)) for s, e in auxiliaries.items()}
        requested_sizes = {}
        for src, entry in windows.items():
            identity = identities[src]
            cached = self._spatial_requested_sizes.get(src)
            if cached and cached[0] == identity and not entry["explicitRequested"]:
                # Legacy helpers don't publish requested dimensions. Preserve
                # their last unbudgeted size while still tracking growth.
                if entry["requestedPixels"] is None:
                    if cached[1] is not None:
                        entry["requestedPixels"] = cached[1]
                        entry["requestedLong"] = cached[2]
                    elif entry["pixels"] is not None:
                        entry["requestedPixels"] = entry["pixels"]
                        dimensions = [value for stream in streams if stream.get("src") == src
                                      for value in (stream.get("width"), stream.get("height"))
                                      if type(value) is int and value > 0]
                        entry["requestedLong"] = max(dimensions, default=0)
                elif (entry["requestedPixels"] is not None and cached[1] is not None
                      and entry["requestedPixels"] < cached[1] and entry["pixels"] is not None):
                    entry["requestedPixels"] = max(entry["requestedPixels"], entry["pixels"])
            elif entry["requestedPixels"] is None:
                entry["requestedPixels"] = entry["pixels"]
                entry["requestedLong"] = max(entry["requestedLong"], 0)
            requested_sizes[src] = (identity, entry["requestedPixels"], entry["requestedLong"])
        self._spatial_requested_sizes = requested_sizes
        self._spatial_restore_since = {src: since for src, since in self._spatial_restore_since.items()
                                       if src == focus and src in windows}
        recent = {s for s, e in windows.items() if e["changed"] > 0 and 0 <= wall - e["changed"] <= 5}
        self._spatial_candidates = {
            s: self._spatial_candidates[s] if self._spatial_candidates.get(s, (None,))[0] == identities[s]
            else (identities[s], now) for s in recent}
        self._spatial_live_since = {s: lease for s, lease in self._spatial_live_since.items()
                                    if s in windows and lease[0] == identities[s] and windows[s]["live"]}
        eligible = {s for s, (_, since) in self._spatial_candidates.items() if now - since >= 1}
        pinned = {s for s, (_, since) in self._spatial_live_since.items() if s in recent and now - since < 3}
        newest = lambda s: (-windows[s]["changed"], s)
        order = ([focus] if focus in windows else [])
        hovered = status.get("hoveredSrc")
        if hovered in windows and hovered not in order:
            order.append(hovered)  # wake immediately, without candidate/residency delay
        order += sorted(pinned - set(order), key=newest)
        order += sorted((eligible | (recent & self._spatial_live_since.keys())) - set(order), key=newest)
        capacity = spatial_decode_pixels()
        focus_pixels = windows[focus]["requestedPixels"] if focus in windows else 0
        focus_long = windows[focus]["requestedLong"] if focus in windows else 0
        focus_max_long = 0
        focus_max_fps = 0
        focus_cost = 0
        focus_target_pixels = 0
        focus_reason = "focused-window"
        limited_focus = False
        previous_focus_policy = self._spatial_policy_targets.get(focus) if focus in windows else None
        had_focus_limit = bool(previous_focus_policy and previous_focus_policy[0] == identities[focus]
                               and previous_focus_policy[3] > 0)
        # These are already running screen streams outside the window policy;
        # unlike menubar/status/menu surfaces, /streams/live cannot suspend them.
        fixed_auxiliary = min(capacity, reserved)

        if focus in windows:
            if focus_pixels is None:
                # Dimensions are not known yet. Keep focus live and reserve its
                # full service before allowing unknown auxiliary streams in.
                focus_cost = capacity
                focus_reason = "focused-size-unknown"
            elif focus_pixels + fixed_auxiliary > capacity:
                focus_max_long = min(focus_long, SPATIAL_FOCUSED_MAX_LONG) if focus_long else 0
                if focus_long and focus_max_long:
                    projected = int(focus_pixels * (focus_max_long / focus_long) ** 2)
                    focus_target_pixels = projected
                    limited_focus = projected + fixed_auxiliary > capacity
                    focus_cost = min(capacity, projected)
                else:
                    limited_focus = True
                    focus_cost = capacity
                    focus_target_pixels = capacity
                focus_max_fps = SPATIAL_FOCUSED_MAX_FPS
                if limited_focus:
                    focus_reason = "focused-decode-limited"
                else:
                    focus_reason = "focused-decode-budget"
            elif had_focus_limit:
                # A smaller policy remains in place until full requested quality
                # has had two seconds of spare capacity after unmanageable streams.
                spare_for_focus = capacity - fixed_auxiliary
                if not unknown_auxiliary and focus_pixels <= spare_for_focus:
                    since = self._spatial_restore_since.setdefault(focus, now)
                    if now - since >= SPATIAL_RESTORE_SECONDS:
                        self._spatial_restore_since.pop(focus, None)
                        focus_cost = focus_pixels
                    else:
                        focus_max_long = previous_focus_policy[3]
                        focus_max_fps = previous_focus_policy[4]
                        focus_cost = min(focus_pixels, capacity)
                        focus_reason = previous_focus_policy[2]
                else:
                    self._spatial_restore_since.pop(focus, None)
                    focus_max_long = previous_focus_policy[3]
                    focus_max_fps = previous_focus_policy[4]
                    focus_cost = min(focus_pixels, capacity)
                    focus_reason = previous_focus_policy[2]
            else:
                self._spatial_restore_since.pop(focus, None)
                focus_cost = focus_pixels

        selected = {focus} if focus in windows else set()
        remaining = max(0, capacity - min(capacity, focus_cost))
        remaining = max(0, remaining - min(remaining, fixed_auxiliary))
        if unknown_auxiliary:
            remaining = 0
        if focus in windows and focus_max_long > 0:
            current_focus_pixels = windows[focus]["pixels"]
            if current_focus_pixels is None or current_focus_pixels > focus_target_pixels:
                # The in-place configuration has been requested, but the
                # decoder has not reported the smaller capture yet.
                remaining = 0
        selected_auxiliaries = set()
        if focus in windows:
            # Keep the focused window's minimum first. Menu/status captures that
            # do not fit after it go still before any background is admitted.
            auxiliary_order = sorted(auxiliaries, key=lambda src: (0 if src in ("menubar", "status") else 1,
                                                                    -auxiliaries[src]["changed"], src))
            for src in auxiliary_order:
                pixels = auxiliaries[src]["pixels"]
                if pixels is not None and pixels <= remaining:
                    selected_auxiliaries.add(src)
                    remaining -= pixels
        else:
            # Preserve the existing system/menu surfaces while no window is
            # focused; their measured cost still limits optional windows.
            selected_auxiliaries = set(auxiliaries)
            remaining = max(0, remaining - sum(e["pixels"] or 0 for e in auxiliaries.values()))
        for src in order:
            if src == focus:
                continue
            pixels = windows[src]["pixels"]
            if pixels is not None and pixels <= remaining:
                selected.add(src)
                remaining -= pixels

        policies = {}
        for src in windows:
            if src == focus:
                policies[src] = (identities[src], "focused", focus_reason, focus_max_long, focus_max_fps)
            elif focus in windows:
                policies[src] = (identities[src], "background", "background-window", 0,
                                 SPATIAL_BACKGROUND_MAX_FPS)
            else:
                policies[src] = (identities[src], "normal", "no-focused-window", 0, 0)
        policy_failed = False
        if self._spatial_policy_agent != id(self.agent):
            self._spatial_policy_targets.clear()
            self._spatial_policy_agent = id(self.agent)
        next_policies = dict(self._spatial_policy_targets)
        for src, policy in policies.items():
            if self._spatial_policy_targets.get(src) == policy:
                next_policies[src] = policy
                continue
            _, role, reason, max_long, max_fps = policy
            try:
                self.call("/streams/policy", method="POST", src=src, role=role, reason=reason,
                          maxLong=str(max_long), maxFps=str(max_fps))
                next_policies[src] = policy
            except MacViewError:
                policy_failed = True
                break
        if not policy_failed:
            next_policies = {src: policy for src, policy in next_policies.items()
                             if src in policies and policies[src] == policy}
        self._spatial_policy_targets = next_policies
        selected.update(selected_auxiliaries)
        all_identities = {**identities, **auxiliary_identities}
        key = (id(self.agent), tuple(sorted((s, all_identities[s]) for s in selected)))
        managed_sources = set(windows) | set(auxiliaries)
        mismatched = any((s.get("live") is True) != (s.get("src") in selected) for s in streams
                         if s.get("src") in managed_sources)
        if key != self._spatial_applied or mismatched:
            self.call("/streams/live", method="POST", src=",".join(sorted(selected)))
            self._spatial_applied = key
        self._spatial_live_since = {s: self._spatial_live_since.get(s, (identities[s], now))
                                    for s in selected if s in windows}

    def _apply_link_cap(self):
        """Tell the agent the bitrate ceiling for the route in use, whenever the
        route or the agent process changes: none on USB-C, and
        FRAME_MACVIEW_WIFI_MBPS (default 12) on the network. Worn Wi-Fi carried
        only ~20 Mbit/s Mac -> Frame (2026-10-19), and the quality presets allow
        up to 60, so video and animations queued frames for 50-300 ms."""
        key = (self.route, id(self.agent))
        if key == self._link_applied:
            return
        mbps = 0 if self.route == "usb" else wifi_mbps()
        try:
            self.call("/link", method="POST", maxbps=str(int(mbps * 1_000_000)))
            self._link_applied = key
        except MacViewError:
            pass  # an older agent without /link, or not answering yet; retried next second

    def set_gaze_sharpen(self, mode):
        """Diagnostic: auto (Wi-Fi only), on or off, live on the agent."""
        if mode not in ("auto", "on", "off"):
            raise MacViewError("Gaze sharpening must be auto, on or off.")
        return self.call("/link", method="POST", sharpen=mode)

    # ---- the SteamVR bar panel (FW-08) ----

    ASPECTS = {"5120x1440": "32:9", "5120x2160": "21:9", "3440x1440": "21:9", "2560x2880": "8:9",
               "2880x2560": "9:8", "3840x2160": "16:9"}

    def start_dash(self, connected=lambda: True, last_workspace_file=None):
        """Keep the "Frame Workspace" entry on the SteamVR bar while the Frame
        is connected: once a second, send it the Workspace and window list and
        collect what was pressed there. It stays on the bar after the companion
        quits, showing that the Mac is not connected. FRAME_WORKSPACE_DASH=0
        turns it off; its binary is installed with the native viewer."""
        if (not SUPPORTED or os.environ.get("FRAME_WORKSPACE_DASH", "1") == "0" or
                (self.dash_thread and self.dash_thread.is_alive())):
            return
        if last_workspace_file is not None:
            self.last_workspace_file = last_workspace_file
            self._load_last_workspace()
        self.spatial_apps.start(connected)

        def loop():
            tick, failures = 0, 0
            while not self.closing:
                self.dash_wake.wait(1)
                self.dash_wake.clear()
                tick += 1
                if not connected():
                    continue
                try:
                    self._install_frame_exit_helper()
                    self.headset_viewer.check(self.frame)  # once per connected Frame
                    self._frame_stop_available = self._probe_frame_stop_available()
                    self.spatial_apps.sync()
                except Exception as exc:  # retry the spatial state independently of bar availability
                    self.spatial_apps.error = str(exc)
                    self._log_poll_failure("spatial state sync", exc)
                    failures += 1
                    continue
                # The script bounds bar restarts; unreachable links retry every 5 s.
                if failures and tick % 5:
                    continue
                try:
                    out = self.run("bash -s -- sync " + _quote(self.dash_state_text()),
                                   stdin=WORKSPACE_DASH_SCRIPT.read_text(encoding="utf-8"), timeout=8)
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - asleep or out of reach; try again shortly
                    self._log_poll_failure("request poll", exc)
                    failures += 1
                    continue
                self._handle_frame_requests(out)

        self.dash_thread = threading.Thread(target=loop, daemon=True)
        self.dash_thread.start()

    def _log_poll_failure(self, what, exc):
        """A failed poll of the headset's requests (a click made meanwhile may wait or
        be lost): in server.log, each distinct failure at most every 30 seconds."""
        message = f"{what} failed: {str(exc)[:200]}"
        now = time.monotonic()
        last = getattr(self, "_poll_logged", (None, 0))
        if last[0] == message and now - last[1] < 30:
            return
        self._poll_logged = (message, now)
        frame_spatial_apps.log(f"companion {message}")

    def _load_last_workspace(self):
        try:
            data = json.loads(Path(self.last_workspace_file).read_text(encoding="utf-8"))
            if data.get("mode") in ("desktop", "spatial"):
                self.mode = data["mode"]
            request = WorkspaceDisplayManager._validate_request(data["preset"], data["scale"], data["quality"])
            self.last_workspace = (request["preset"], request["scale"], request["quality"])
        except Exception:  # noqa: BLE001 - none saved yet, or unreadable: the defaults
            pass

    def _remember_workspace(self, preset, scale, quality):
        self.last_workspace = (preset, scale, quality)
        if not self.last_workspace_file:
            return
        try:
            path = Path(self.last_workspace_file)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".last-start-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"preset": preset, "scale": scale, "quality": quality, "mode": self.mode}, stream)
                    stream.write("\n")
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except OSError:
            pass  # remembered for this run only

    def dash_state(self):
        """What the bar panel shows: the Workspace, and each floating window
        with the key its Close button sends back."""
        workspace = self.workspace_state()
        running = workspace.get("state") == "running" and (self.mode == "spatial" or bool(self.workspace_panel_key))
        busy = self.dash_action or ("starting" if workspace.get("state") == "starting" else None)
        preset = workspace.get("preset") if running else self.last_workspace[0]
        w, h = WorkspaceDisplayManager.PRESETS.get(preset, (0, 0))
        label = f"{self.ASPECTS.get(preset, '')} · {w} × {h}".strip(" ·") if w else ""
        if self.mode == "spatial":
            label = "Spatial Mode"
        windows = [{"title": p.get("title") or "Mac Window", "key": p["key"]}
                   for p in sorted(list(self.native_panels.values()), key=lambda p: p.get("slot") if p.get("slot") is not None else 1024)
                   if p.get("role", "window") == "window"]
        stop_available = (workspace.get("state") != "stopped" or self.exit_pending or
                          bool(self.workspace_panel_key) or bool(self.native_panels) or
                          self._frame_stop_available)
        workspace = {"running": running, "busy": busy, "label": label, "mode": self.mode,
                     "stopAvailable": stop_available, "session": self.session_id}
        notice = "Choose a window in the Mac app, then Start." if self._mouse_selection_required else self._notice()
        if notice:
            workspace["notice"] = notice
        return {"connected": True, "workspace": workspace, "windows": windows[:32],
                "errors": [e["message"] for e in list(self.recent_errors)[-4:]]}

    DASH_STATE_LIMIT = 24000  # bytes; the Frame's sync script refuses more than 32768 characters

    def dash_state_text(self):
        """dash_state() as the Frame's sync script takes it. One that is too big makes the
        script refuse the whole call, and with it the poll for headset requests (a shelf
        click would wait for nothing), so the window list is cut to fit. Titles written
        out as \\u escapes are six bytes a character, which 32 long Japanese titles fill."""
        state = self.dash_state()
        text = json.dumps(state, separators=(",", ":"), allow_nan=False)
        while len(text) > self.DASH_STATE_LIMIT and state["windows"]:
            state["windows"] = state["windows"][:-1]
            text = json.dumps(state, separators=(",", ":"), allow_nan=False)
        return text

    def _install_frame_exit_helper(self):
        """Install the bounded local Stop helper before the dashboard is shown."""
        if self._exit_helper_frame == self.frame:
            return
        target_frame = self.frame
        script = WORKSPACE_EXIT_HELPER.read_text(encoding="utf-8")
        command = ("umask 077; target=\"${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/workspace-exit-on-frame.py\"; "
                   "mkdir -p \"$(dirname \"$target\")\"; tmp=\"$target.tmp.$$\"; "
                   "cat > \"$tmp\" && chmod 700 \"$tmp\" && mv -f \"$tmp\" \"$target\" && "
                   "python3 \"$target\" adopt --session " + _quote(self.session_id))
        try:
            self.run(command, stdin=script, timeout=8)
        except Exception as exc:
            raise MacViewError(f"Could not install the local Frame Stop helper: {exc}") from exc
        if self.frame != target_frame:
            raise MacViewError("The Frame connection changed while installing its Stop helper; retry after reconnecting.")
        self._exit_helper_frame = target_frame

    def _probe_frame_stop_available(self):
        """Report Frame-local orphan viewers even when the Mac state says stopped."""
        helper = '"${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/workspace-exit-on-frame.py"'
        try:
            out = self.run("python3 " + helper + " status", timeout=3)
            values = [json.loads(line) for line in (out or "").splitlines() if line.startswith("{")]
            return not values or values[-1].get("stopAvailable") is not False
        except Exception:
            # The app-scoped Stop remains available when inventory cannot be read.
            return True

    def _stop_frame_session(self):
        """Confirm the Frame-local viewer owners stopped before retiring this session."""
        if self._exit_helper_frame != self.frame:
            self._install_frame_exit_helper()
        helper = '"${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/workspace-exit-on-frame.py"'
        command = "python3 " + helper + " stop --session " + _quote(self.session_id)
        out = self.run(command, timeout=8)
        if "Frame Workspace viewer session stopped." not in (out or ""):
            raise MacViewError("The Frame did not confirm that its owned viewers stopped. Retry Stop.")
        self._frame_stop_available = False

    def _handle_frame_requests(self, out):
        """Act on requests made in the headset (FW-08): Close on a native panel
        or in the bar panel closes that window's stream or stops the Workspace;
        the bar panel can also start the Workspace (with the last settings used)
        and recenter it. The Frame can only ask for these fixed actions. Each
        line is one request, already removed on the Frame. Spatial gaze focus
        uses the same owned panel keys and only the newest valid request in a
        batch survives; execution is capped at four requests per second. A
        `direct` focus was already raised and acknowledged over the panel's
        stream (native input): it only steers the live set, and drops any older
        queued focus so a late replay cannot undo it."""
        focus, direct = None, None
        for line in (out or "").splitlines():
            if not line.startswith("{"):
                continue
            try:
                request = json.loads(line)
            except ValueError:
                frame_spatial_apps.log(f"companion could not read a request line from the headset: {line[:120]!r}")
                continue
            if not isinstance(request, dict):
                continue
            if request.get("source") == "steam-library":
                expires = request.get("expiresAt")
                now = time.time()
                if (type(expires) not in (int, float) or not math.isfinite(expires) or
                        expires <= now or expires > now + 12):
                    continue
            if self.spatial_apps.handle_request(request):
                continue
            action, key = request.get("action"), str(request.get("key") or "")
            request_session = request.get("session")
            if action == "workspace_stop" and request_session not in (None, self.session_id):
                continue
            if action == "wall_release":
                with self.workspace_lock:
                    for panel in list(self.native_panels.values()):
                        if panel.get("key") == key and panel.get("generation") == request.get("generation"):
                            panel["slot"] = None
            elif action == "close":
                if re.fullmatch(r"com\.frame-workspace\.native\.\d+\.(workspace|panel-w\d{1,15})", key):
                    self._dash_run(None, self._close_from_frame, key)
            elif action == "focus" and self._window_displays():
                u, v = request.get("u"), request.get("v")
                if (type(u) in (int, float) and type(v) in (int, float) and
                        math.isfinite(u) and math.isfinite(v) and 0 <= u <= 1 and 0 <= v <= 1 and
                        re.fullmatch(r"com\.frame-workspace\.native\.\d+\.panel-w\d{1,15}", key) and
                        any(p["key"] == key for p in list(self.native_panels.values()))):
                    if request.get("direct") is True:
                        focus, direct = None, key
                    else:
                        focus, direct = (key, u, v), None
            elif action == "workspace_start":
                if not self.exit_pending:
                    self._dash_run("starting", lambda: self.start_workspace(*self.last_workspace, resume=True))
            elif action == "workspace_stop":
                self.exit_pending = True
                self.exit_error = None
                self._mode_generation += 1
                self._pending_focus = None
                with self.dash_lock:
                    while True:
                        try:
                            self.dash_jobs.get_nowait()
                        except queue.Empty:
                            break
                        else:
                            self.dash_jobs.task_done()
                    self.dash_jobs.put(("stopping", self._stop_workspace_session, ()))
                    if not self.dash_worker or not self.dash_worker.is_alive():
                        self._dash_run_worker_locked()
                self.spatial_apps.wake.set()
            elif action in ("mode_desktop", "mode_spatial"):
                if not self.exit_pending:
                    self._dash_run("starting", self.set_mode, action[5:])
            elif action == "recenter":
                if not self.exit_pending:
                    self._dash_run(None, self._focus_workspace_viewer)
        if direct:
            src = next((s for s, p in list(self.native_panels.items()) if p["key"] == direct), None)
            with self._spatial_lock:
                self._pending_focus = None
                if src and src.startswith("spatial:"):
                    self._spatial_focused = src
        if focus:
            with self._spatial_lock:
                self._pending_focus = focus
            self._flush_frame_focus()

    def _flush_frame_focus(self):
        with self._spatial_lock:
            if (not self._window_displays() or self._pending_focus is None or self._focus_queued or
                    time.monotonic() - self._last_frame_focus < .25):
                return
            self._focus_queued = True
        self._action_run(self._focus_from_frame)

    def _focus_from_frame(self):
        with self._spatial_lock:
            focus, self._pending_focus = self._pending_focus, None
            self._focus_queued = False
            if not focus or not self._window_displays():
                return
            self._last_frame_focus = time.monotonic()
        key, u, v = focus
        src = next((s for s, p in list(self.native_panels.items()) if p["key"] == key), None)
        if src and src.startswith(("window:", "separate:", "spatial:")):
            self.call("/focus", method="POST", src=src, u=u, v=v)
            with self._spatial_lock:
                self._spatial_focused = src

    AUX_NAMES = {"menubar": "Menu Bar", "status": "Status", "menu": "Menu"}
    NOTICE_SECONDS = 20

    def _record_error(self, message, kind="action"):
        """Keep a failure the user caused (a headset click, a panel launch) visible."""
        message = re.sub(r"[\x00-\x1f]+", " ", str(message or "")).strip()[:160] or "Something Went Wrong."
        self.recent_errors.append({"at": time.time(), "kind": kind, "message": message})
        self.dash_wake.set()

    def errors_state(self):
        return [dict(e) for e in list(self.recent_errors)]

    def _notice(self):
        latest = self.recent_errors[-1] if self.recent_errors else None
        return latest["message"] if latest and time.time() - latest["at"] <= self.NOTICE_SECONDS else ""

    def _action_run(self, fn, *args):
        """Run a headset action (open, hide, quit, switch, focus) on its own lane."""
        with self.action_lock:
            try:
                self.action_jobs.put_nowait((fn, args))
            except queue.Full:
                self._record_error("Too Many Headset Actions At Once; Some Were Dropped.")
                return
            if self.action_worker and self.action_worker.is_alive():
                return

            def work():
                while True:
                    try:
                        job, job_args = self.action_jobs.get(timeout=5)
                    except queue.Empty:
                        with self.action_lock:
                            if self.action_jobs.empty():
                                self.action_worker = None
                                return
                        continue
                    try:
                        job(*job_args)
                    except Exception as exc:  # noqa: BLE001 - shown on the bar panel and the Mac page
                        self._record_error(str(exc))
                    finally:
                        self.action_jobs.task_done()

            self.action_worker = threading.Thread(target=work, daemon=True)
            self.action_worker.start()

    def _dash_run(self, busy, fn, *args):
        """Carry out headset requests in order, off the sync loop, so the
        panel keeps updating (showing `busy` meanwhile)."""
        with self.dash_lock:
            self.dash_jobs.put((busy, fn, args))
            if self.dash_worker and self.dash_worker.is_alive():
                return

            self._dash_run_worker_locked()

    def _dash_run_worker_locked(self):
        """Start the queue worker while ``dash_lock`` is held."""

        def work():
            while True:
                try:
                    job_busy, job, job_args = self.dash_jobs.get(timeout=5)
                except queue.Empty:
                    with self.dash_lock:
                        if self.dash_jobs.empty():
                            self.dash_worker = None
                            return
                    continue
                self.dash_action = job_busy
                try:
                    job(*job_args)
                except Exception as exc:  # noqa: BLE001 - the Mac page and bar panel show it
                    self._record_error(str(exc), kind="workspace")
                finally:
                    self.dash_action = None
                    self.dash_jobs.task_done()

        self.dash_worker = threading.Thread(target=work, daemon=True)
        self.dash_worker.start()

    def _close_from_frame(self, key):
        if key.endswith(".workspace"):
            if key == self.workspace_panel_key:
                self.stop_workspace()
            return
        src = next((s for s, p in list(self.native_panels.items()) if p["key"] == key), None)
        if src:
            if self.mode == "spatial":
                self._spatial_suppressed.add(src)
            self.stop(src)
        elif self.mode == "spatial":
            # A placeholder's Close: that window stops opening (and retrying).
            match = re.fullmatch(r"com\.frame-workspace\.native\.\d+\.panel-w(\d{1,15})", key)
            if match:
                self._spatial_suppressed.add("spatial:" + match.group(1))
                self._queue_spatial_reconcile()

    def _usb_reachable(self):
        """Whether the Frame's USB-C address still answers (cheap: no ssh)."""
        if not self.usb_ip:
            return False
        try:
            socket.create_connection((self.usb_ip, 22), timeout=0.5).close()
            return True
        except OSError:
            return False

    def _follow_best_route(self, tick):
        """Keep the tunnel on the best link while viewers use it: move to USB-C
        when it is plugged in, and back to the network within ~2 s when it is
        pulled, instead of waiting ~15 s for ssh keepalives. Viewers keep the
        same port and reconnect on their own, so a switch is a brief blink.
        Worn on Wi-Fi the Mac -> Frame link carried only ~20 Mbit/s, which
        video and animations saturate; USB-C removes that (2026-10-19)."""
        if self.route == "usb":
            if self._usb_reachable():
                self._usb_misses = 0
                return
            self._usb_misses += 1
            if self._usb_misses < 2:
                return
        elif not self.prefer_usb or tick % 5 or not self._usb_route():
            return
        self._usb_misses = 0
        with self.lock:
            old, self.tunnel = self.tunnel, None
        if old is not None and old.poll() is None:
            old.terminate()
            try:
                old.wait(3)
            except subprocess.TimeoutExpired:
                old.kill()
        for _ in range(3):  # the Frame may take a moment to release the forwarded port
            try:
                self.ensure_tunnel()
                return
            except MacViewError:
                time.sleep(0.5)

    def _probe(self, port):
        """Whether the Frame reaches the agent through the tunnel on `port`."""
        # /ping needs no key, so none appears on the Frame's command lines.
        cmd = f"curl -s -m 2 'http://127.0.0.1:{port}/ping'"
        try:
            return self.run(cmd, timeout=8).strip() == "frame-mac-view"
        except Exception:  # noqa: BLE001 - the server's Failure, timeouts: all mean "not yet"
            return False

    # ---- viewers on the Frame ----

    def show(self, src, quality="balanced", width=None, height=None, pose=None):
        with self.workspace_lock:
            if self.closing or self.exit_pending:
                raise MacViewError("The Companion Is Shutting Down.")
            if (self.mode == "spatial" and src.startswith(("window:", "spatial:")) and
                    self.spatial_workspace.get("state") != "running"):
                raise MacViewError("Start Spatial Mode Before Showing A Window.")
            result = self._show_source(src, quality, width, height, pose)
            capture_src = result.get("captureSrc", "")
            if self.mode == "spatial" and capture_src.startswith("spatial:"):
                self._spatial_owned.add(capture_src)
            return result

    def _show_source(self, src, quality="balanced", width=None, height=None, pose=None):
        if self.mode == "spatial" and src.startswith("window:"):
            src = "spatial:" + src.split(":", 1)[1]
        if src.startswith("spatial:") and self.mode != "spatial":
            raise MacViewError("Start Spatial Mode Before Showing A Window.")
        if self.mode == "spatial" and src.startswith("separate:"):
            raise MacViewError("Separate Window Sources Require Desktop Mode.")
        if pose is not None:
            pose = validate_pose(pose)
            if ((self.mode != "spatial" and workspace_viewer_backend() != "native") or
                    (src != WorkspaceDisplayManager.KEY and
                     (not self._native_windows() or not src.startswith(("window:", "separate:", "spatial:")) or
                      quality_settings(quality)["codec"] != "h264"))):
                raise MacViewError("Saved poses require a native Workspace or H.264 window panel.")
        if src.startswith("separate:") and self._native_windows() and not self._window_displays():
            # Native panels show the window at its own size; moving it onto a
            # display of its own (which resizes it) is not needed.
            src = "window:" + src.split(":", 1)[1]
        with self.viewer_lock:
            self.launching += 1
        try:
            if src == WorkspaceDisplayManager.KEY:
                workspace = self.workspace_manager.state()
                if workspace.get("state") != "running":
                    raise MacViewError("Start the Workspace display before showing it.")
                return self._show(workspace["captureSrc"], quality, width, height,
                                  panel_key=WorkspaceDisplayManager.KEY, public_src=WorkspaceDisplayManager.KEY, pose=pose)
            return self._show(src, quality, width, height, pose=pose)
        finally:
            with self.viewer_lock:
                self.launching -= 1

    def _show(self, src, quality, width, height, panel_key=None, public_src=None, pose=None, role="window",
              wall_slot=None, reconciled=False):
        if self.exit_pending:
            raise MacViewError("Workspace Stop is in progress; the Frame viewer was not started.")
        auxiliary = self.mode == "spatial" and SPATIAL_AUX_ROLES.get(src) == role
        if src != "test" and not src.startswith(("window:", "display:", "separate:", "spatial:")) and not auxiliary:
            raise MacViewError("Pick a window or display to show.")
        panel_key = panel_key or src
        public_src = public_src or src
        self.shows += 1
        workspace_viewer = panel_key == WorkspaceDisplayManager.KEY
        q = quality_settings(quality, workspace=workspace_viewer)
        state = self.call("/status")
        if src != "test" and not state.get("screen"):
            raise MacViewError("Frame Control needs Screen Recording permission first (Allow… under Mac in the headset).")
        if src.startswith("separate:") and not state.get("accessibility"):
            raise MacViewError("A window on its own display needs the Accessibility permission too, to move it "
                               "(Allow… under Mac in the headset).")
        self.shown -= set(state.get("finished", []))  # their Mac windows closed
        if src in self.shown or any(st.get("src") == src for st in state.get("streams", [])):
            # Showing it again replaces the old viewer, connected or not: this
            # revokes its keys so it can't come back alongside the new one.
            if reconciled:
                # The Spatial reconciler's own retry: a leftover capture, not
                # the user closing the window, so the window is not suppressed.
                self.call("/close", method="POST", src=src)
                self.shown.discard(src)
            else:
                self.stop(src)
        # Viewers that lost the tunnel keep retrying its old port, even though
        # the agent no longer counts them, so only move when none are out there.
        others = self.shown - {src}
        self.ensure_tunnel(allow_new_port=not others)
        appid = panel_id(panel_key)
        # Unique per launch, so a new window is never confused with an old one.
        tag = "fc" + secrets.token_hex(4)
        # A single-use ticket for this source, not Frame Control's key: the URL
        # is visible in the Frame's process list.
        ticket = self.call("/ticket", method="POST", src=src)["ticket"]
        params = {"src": src, "t": ticket, "tag": tag, "codec": q["codec"], "max": q["max"], "fps": q["fps"],
                  "bpp": q["bpp"]}
        if src.startswith("spatial:") and self._spatial_mouse_ticket:
            identity = self._spatial_admitted.get(int(src.split(":", 1)[1]))
            if identity is None:
                raise MacViewError("Window Is Not Admitted To The Mouse Workspace.")
            params.update(workspaceTicket=self._spatial_mouse_ticket, admissionStamp=identity[1])
        url = f"http://127.0.0.1:{self.remote_port}/view?{urlencode(params)}"
        if workspace_viewer:
            workspace = self.workspace_manager.state()
            stream = workspace.get("stream", {})
            w, h = int(width or stream.get("width") or 1280), int(height or stream.get("height") or 720)
            native = workspace_viewer_backend() == "native"
            script = WORKSPACE_NATIVE_SCRIPT if native else WORKSPACE_VIEWER_SCRIPT
            flags = [workspace_decoder(), workspace_conversion()] if native else self.browser_flags
            if native and workspace_foveation() == "1":
                flags.append("1")
                diagnostic = workspace_diagnostic_gaze()
                if diagnostic:
                    flags.append(diagnostic)
            elif native and workspace_gaze() == "1":
                # Gaze-directed detail: the agent softens away from gaze only
                # while the link is capped (Wi-Fi) unless told otherwise.
                flags.append("gaze")
                diagnostic = workspace_diagnostic_gaze(gaze=True)
                if diagnostic:
                    flags.append(diagnostic)
            pose_args = ["--pose", launcher_pose(pose)] if pose is not None else []
            session_args = ["--session", self.session_id] if self._exit_helper_frame == self.frame else []
            script_args = " ".join(_quote(str(a)) for a in (*session_args, *pose_args,
                                                               "start", url, w, h, tag, *flags))
            out = self.run("bash -s -- " + script_args, stdin=script.read_text(encoding="utf-8"),
                           timeout=60)
            match = re.search(r"registered OpenVR key (com\.(?:frame-control\.workspace\.\d+\.fc[0-9a-f]{8}|frame-workspace\.native\.\d+\.workspace))\b", out)
            if not match:
                raise MacViewError("The dedicated Workspace launcher did not report its registered OpenVR panel key.")
            self.workspace_panel_key = match.group(1)
        elif self._native_windows() and q["codec"] == "h264" and \
                (auxiliary or src.startswith(("window:", "separate:", "spatial:"))):
            out = self._show_native_window(src, url, tag, width, height, pose=pose, role=role, wall_slot=wall_slot)
        else:
            w, h = fit(width or 1280, height or 720)
            args = " ".join(_quote(str(a)) for a in (appid, url, w, h, tag, *self.browser_flags))
            with self.browser_lock:
                if self.browser_cleanup_error:
                    raise MacViewError("The previous Frame Workspace browser did not confirm shutdown. Stop all viewers and retry.")
                try:
                    session_env = ("FRAME_WORKSPACE_SESSION=" + _quote(self.session_id) + " ") \
                        if self._exit_helper_frame == self.frame else ""
                    out = self.run(session_env + "bash -s -- " + args, stdin=LAUNCH, timeout=60)
                except Exception as e:  # noqa: BLE001 - the server's Failure carries the Frame's words
                    if "NO_BROWSER" in (getattr(e, "stdout", "") or ""):
                        raise MacViewError("The Frame needs Chromium, Chromium XR, or the Google Chrome Flatpak "
                                           "(com.google.Chrome) for this.")
                    raise MacViewError(str(e))
        if self.exit_pending:
            if workspace_viewer:
                self._stop_workspace_viewer()
            elif src in self.native_panels:
                self._stop_native_panels(src)
            else:
                self.run("bash -s", stdin=CLOSE_BROWSER, timeout=15)
            raise MacViewError("Workspace Stop cancelled the Frame viewer launch.")
        self.shown.add(src)
        native = self.native_panels.get(src)
        panel = (self.workspace_panel_key if workspace_viewer else
                 native["key"] if native else f"valve.steam.desktopgame.{appid}")
        return {"panel": panel, "src": public_src, "captureSrc": src, "detail": out.strip()}

    def _native_windows(self):
        """Window panels use the native viewer when the Workspace does: their
        own OpenVR panel and hardware decoder, at the window's own size (no
        separate display), placed beside the Workspace. FRAME_WINDOW_VIEWER=
        browser keeps the older Chrome panels."""
        return self.mode == "spatial" or (workspace_viewer_backend() == "native" and
                os.environ.get("FRAME_WINDOW_VIEWER", "native") == "native")

    def _window_displays(self):
        return self.mode == "spatial" or window_displays()

    def _workspace_frame_placement(self):
        """The Workspace's live transform, width and distance on the Frame, or None."""
        try:
            state = json.loads(self.run("cat \"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/frame-workspace/state.json\"",
                                        timeout=8))
            placement = state.get("placement") if state.get("ready") else None
            if isinstance(placement, dict) and len(placement.get("matrix") or []) == 12:
                return placement
        except Exception:  # noqa: BLE001 - no Workspace: the panel recenters ahead instead
            pass
        return None

    def read_panel_poses(self, sources):
        """Read bounded saved pose files in one Frame call, keyed by current source."""
        panels = {}
        for source in sources:
            if source == WorkspaceDisplayManager.KEY:
                panels[source] = "workspace"
            elif re.fullmatch(r"(?:window|separate|spatial):[1-9][0-9]{0,14}", source):
                panels[source] = "w" + source.split(":", 1)[1]
            else:
                raise MacViewError("Invalid native panel source for pose capture.")
        script = '''import json, os, pathlib, sys
base = pathlib.Path(os.environ.get('XDG_DATA_HOME') or pathlib.Path.home() / '.local/share') / 'frame-workspace'
poses = {}
for panel in sys.argv[1:]:
    path = base / ('workspace-pose.json' if panel == 'workspace' else 'panels/' + panel + '-pose.json')
    try:
        with path.open('rb') as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        continue
    if len(raw) > 4096:
        raise SystemExit('Saved panel pose is too large.')
    poses[panel] = raw.decode('utf-8')
print(json.dumps(poses))
'''
        try:
            raw = self.run("python3 - " + " ".join(_quote(p) for p in panels.values()), stdin=script, timeout=10)
            data = json.loads(raw, object_pairs_hook=_pose_object)
            if not isinstance(data, dict) or set(data) - set(panels.values()):
                raise MacViewError("Unexpected saved panel pose response.")
            result = {}
            for source, panel in panels.items():
                if panel in data:
                    if not isinstance(data[panel], str) or len(data[panel]) > 4096:
                        raise MacViewError("Invalid saved panel pose response.")
                    pose = json.loads(data[panel], object_pairs_hook=_pose_object)
                    if panel != "workspace":
                        # A window's pose counts only once its user placed it
                        # (FW-15); the rest is where the wall or a test put it.
                        if not isinstance(pose, dict) or pose.get("userSet") is not True:
                            continue
                        pose = {k: v for k, v in pose.items() if k not in POSE_PROVENANCE[1:]}
                    result[source] = validate_pose(pose)
            return result
        except Exception as exc:
            raise MacViewError(f"Could not capture saved native panel poses: {exc}") from exc

    def _show_native_window(self, src, url, tag, width, height, pose=None, role="window", wall_slot=None):
        if pose is not None:
            pose = validate_pose(pose)
        if role == "window":
            digits = re.sub(r"\D", "", src.split(":", 1)[1])[:15]
            if not digits:
                raise MacViewError("Pick a window to show.")
            panel = "w" + digits
            window_src = "window:" + digits
            info = next((w for w in self.call("/windows").get("windows", []) if w.get("src") == window_src), {})
        elif SPATIAL_AUX_ROLES.get(src) == role and pose is None:
            panel = src.replace(":", "")
            info = {"title": {"menubar": "Menu Bar", "status": "Status"}.get(src, "Menu")}
        else:
            raise MacViewError("Invalid Spatial Panel Role.")
        title = re.sub(r"[\r\n\t]", " ", str(info.get("title") or info.get("app") or "Mac Window"))[:64]
        if wall_slot is not None:
            slot = wall_slot  # reserved by the Spatial reconciler
        else:
            used = self._used_wall_slots(src)
            previous = self.native_panels.get(src, {}).get("slot")
            slot = previous if previous is not None else next(i for i in range(len(used) + 1) if i not in used)
        if self.mode == "spatial" and (role != "window" or pose is not None):
            slot = None
        initial_pose = ""
        placement = self._workspace_frame_placement() if pose is None and self.mode == "desktop" and role == "window" else None
        if placement:
            logical = (self.workspace_manager.state().get("display") or {}).get("logicalWidth") or 2880
            initial_pose = beside_workspace_pose(placement["matrix"], placement["width"], placement["distance"],
                                                 logical, info.get("w") or width or 1280, slot)
        w, h = fit(width or info.get("w") or 1280, height or info.get("h") or 720)
        pose_args = (["--pose", launcher_pose(pose)] if pose is not None else
                     ["--initial-pose", initial_pose] if initial_pose else [])
        # The window's app and size: its placeholder icon, its size on the wall.
        identity_args = []
        if role == "window":
            bundle = info.get("bundleID")
            if isinstance(bundle, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,255}", bundle):
                identity_args += ["--bundle-id", bundle]
            pw, ph = info.get("w"), info.get("h")
            if type(pw) is int and type(ph) is int and 1 <= pw <= 100000 and 1 <= ph <= 100000:
                identity_args += ["--points", f"{pw}x{ph}"]
        host = self.mode == "spatial" or os.environ.get("FRAME_NATIVE_HOST") == "1"
        role_args = ["--role", role] if role != "window" or self.mode == "spatial" else []
        slot_args = ["--slot", slot] if self.mode == "spatial" and slot is not None else []
        session_args = ["--session", self.session_id] if self._exit_helper_frame == self.frame else []
        args = session_args + ["--panel", panel, "--name", title] + role_args + identity_args + pose_args + slot_args + \
               ["start", url, w, h, tag, native_window_decoder(), workspace_conversion()]
        remote_env = "FRAME_NATIVE_HOST=1 " if host else ""
        out = self.run(remote_env + "bash -s -- " + " ".join(_quote(str(a)) for a in args),
                       stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=60)
        match = re.search(r"registered OpenVR key (com\.frame-workspace\.native\.\d+\.panel-" + panel + r")\b", out)
        if not match:
            raise MacViewError("The native window panel did not report its registered OpenVR panel key.")
        self.native_panels[src] = {"panel": panel, "key": match.group(1), "slot": slot,
                                   "title": title, "host": host, "generation": tag,
                                   "bundleID": info.get("bundleID"), "role": role}
        return out

    def _used_wall_slots(self, src=None):
        """Wall slots held by other window panels and by Spatial windows still
        waiting for (or launching) their panel."""
        used = {p["slot"] for s, p in list(self.native_panels.items())
                if s != src and p.get("role", "window") == "window" and p.get("slot") is not None}
        return used | {slot for s, slot in list(self._spatial_slots.items()) if s != src and slot is not None}

    def _stop_native_panels(self, src=None):
        """Stop each native panel owner and remove its runtime files. Saved poses stay."""
        for s in ([src] if src else list(self.native_panels)):
            info = self.native_panels.get(s)
            if not info:
                continue
            try:
                args = ["--panel", info["panel"]]
                remote_env = ""
                if info.get("host"):
                    remote_env = "FRAME_NATIVE_HOST=1 "
                    if info.get("generation"):
                        args += ["--generation", info["generation"]]
                args += ["stop"]
                self.run(remote_env + "bash -s -- " + " ".join(_quote(a) for a in args),
                         stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"),
                         timeout=20 if info.get("host") else 10)
            except Exception:  # noqa: BLE001 - the viewer is gone either way
                if not info.get("host") and self.native_panels.get(s) is info:
                    self.native_panels.pop(s, None)
            else:
                if self.native_panels.get(s) is info:
                    self.native_panels.pop(s, None)

    def _stop_workspace_viewer(self):
        try:
            out = self.run("bash -s -- stop", stdin=WORKSPACE_VIEWER_SCRIPT.read_text(encoding="utf-8"), timeout=15)
        except Exception as e:  # noqa: BLE001 - report remote service ownership errors plainly
            raise MacViewError(f"Could not confirm Workspace viewer shutdown: {e}") from e
        return out.strip()

    def _focus_workspace_viewer(self):
        key = self.workspace_panel_key
        if not key:
            raise MacViewError("The Workspace OpenVR panel key is unknown; use Show Workspace again to replace it safely.")
        try:
            if key.startswith("com.frame-workspace.native."):
                out = self.run("bash -s -- focus", stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=10)
            else:
                out = self.run(f"{_quote(FRAME_VRCMD)} --showdashboard {_quote(key)}", timeout=10)
        except Exception as e:  # noqa: BLE001 - preserve the headset tool's diagnostic
            raise MacViewError(f"Could not focus the Workspace OpenVR panel: {e}") from e
        return out.strip()

    @staticmethod
    def _placement_number(value, name, low, high):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise MacViewError(f"Workspace {name} must be between {low:g} and {high:g} meters.")
        return float(value)

    def workspace_placement(self, workspace):
        """Read the reserved owner once per normal state refresh, never per frame."""
        key = self.workspace_panel_key or ""
        if workspace.get("state") != "running" or not re.fullmatch(r"com\.frame-workspace\.native\.\d+\.workspace", key):
            return None
        try:
            owner = json.loads(self.run("python3 - --native-owner",
                stdin=(ROOT / "ui" / "frame_panels.py").read_text(), timeout=10))
            if not isinstance(owner, dict) or owner.get("key") != key:
                return None
            placement = owner.get("placement", {})
            width = self._placement_number(placement.get("width"), "width", 0.4, 8.0)
            distance = self._placement_number(placement.get("distance"), "distance", 0.4, 10.0)
            return {"width": width, "distance": distance}
        except Exception:
            # Unreachable or unready owners must not expose guessed dimensions.
            return None

    def move_workspace(self, move=(0, 0, 0), rotate=(0, 0, 0)):
        """Move (m: right, up, toward the viewer) and turn (degrees: yaw, pitch,
        roll) the native Workspace in its own frame, live, without restarting."""
        def triple(values, limit, name):
            if not isinstance(values, (list, tuple)) or len(values) != 3:
                raise MacViewError(f"Workspace {name} takes three numbers.")
            out = []
            for v in values:
                if type(v) not in (int, float) or not math.isfinite(v) or abs(v) > limit:
                    raise MacViewError(f"Workspace {name} values must be within {limit:g}.")
                out.append(float(v))
            return out
        move, rotate = triple(move, 2, "move"), triple(rotate, 90, "rotation")
        with self.workspace_lock:
            workspace = self.workspace_manager.state()
            if workspace.get("state") != "running":
                raise MacViewError("The Workspace display is stopped; start Workspace before moving it.")
            key = self.workspace_panel_key or ""
            if not re.fullmatch(r"com\.frame-workspace\.native\.\d+\.workspace", key):
                raise MacViewError("Placement controls require the active native Workspace viewer.")
            args = " ".join(f"{v:.6f}" for v in (*move, *rotate))
            try:
                out = self.run("bash -s -- pose " + args,
                               stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=10)
                lines = [line for line in out.splitlines() if line.strip()]
                result = json.loads(lines[-1]) if lines else {}
                if result.get("ready") is not True or result.get("overlayKey") != key:
                    raise MacViewError("The native Workspace owner did not confirm the move.")
                return {"ready": True, "state": result.get("state"), "overlayKey": key,
                        "placement": result.get("placement")}
            except MacViewError:
                raise
            except Exception as e:  # noqa: BLE001 - preserve the helper's ownership/readiness error
                raise MacViewError(f"Could not move the Workspace: {e}") from e

    def adjust_workspace(self, width, distance):
        """Change placement through the active native owner without restarting it."""
        width = self._placement_number(width, "width", 0.4, 8.0)
        distance = self._placement_number(distance, "distance", 0.4, 10.0)
        with self.workspace_lock:
            workspace = self.workspace_manager.state()
            if workspace.get("state") != "running":
                raise MacViewError("The Workspace display is stopped; start Workspace before changing placement.")
            key = self.workspace_panel_key or ""
            if not re.fullmatch(r"com\.frame-workspace\.native\.\d+\.workspace", key):
                raise MacViewError("Placement controls require the active native Workspace viewer.")
            try:
                out = self.run(
                    "bash -s -- adjust " + f"{width:.6f} {distance:.6f}",
                    stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=10)
                lines = [line for line in out.splitlines() if line.strip()]
                result = json.loads(lines[-1]) if lines else {}
                placement = result.get("placement") if isinstance(result, dict) else None
                actual_width = placement.get("width") if isinstance(placement, dict) else None
                actual_distance = placement.get("distance") if isinstance(placement, dict) else None
                if (result.get("ready") is not True or result.get("overlayKey") != key or
                        not isinstance(result.get("state"), str) or
                        type(actual_width) not in (int, float) or type(actual_distance) not in (int, float) or
                        not math.isclose(actual_width, width, abs_tol=0.00001) or
                        not math.isclose(actual_distance, distance, abs_tol=0.00001)):
                    raise MacViewError("The native Workspace owner did not confirm the requested placement.")
                return {"ready": True, "state": result["state"], "overlayKey": key,
                        "placement": {"width": actual_width, "distance": actual_distance}}
            except MacViewError:
                raise
            except Exception as e:  # noqa: BLE001 - preserve the helper's ownership/readiness error
                raise MacViewError(f"Could not adjust the native Workspace placement: {e}") from e

    @staticmethod
    def _curvature_number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise MacViewError("Workspace curvature must be between 0 and 1.")
        return float(value)

    def set_workspace_curvature(self, curvature):
        """Ask the active native owner to set curvature and return its readback."""
        curvature = self._curvature_number(curvature)
        with self.workspace_lock:
            workspace = self.workspace_manager.state()
            if workspace.get("state") != "running":
                raise MacViewError("The Workspace display is stopped; start Workspace before changing curvature.")
            key = self.workspace_panel_key or ""
            if not re.fullmatch(r"com\.frame-workspace\.native\.\d+\.workspace", key):
                raise MacViewError("Curvature controls require the active native Workspace viewer.")
            try:
                out = self.run(
                    "bash -s -- curvature " + f"{curvature:.6f}",
                    stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=10)
                lines = [line for line in out.splitlines() if line.strip()]
                result = json.loads(lines[-1]) if lines else {}
                if not isinstance(result, dict):
                    raise MacViewError("The native Workspace owner returned an invalid curvature result.")
                detail = result.get("curvature")
                if not isinstance(detail, dict):
                    raise MacViewError("The native Workspace owner returned an invalid curvature result.")
                requested = detail.get("requested")
                value = detail.get("value")
                supported = detail.get("supported")
                applied = detail.get("applied")
                error_code = detail.get("errorCode")
                ok = result.get("ok")
                if (result.get("ready") is not True or result.get("overlayKey") != key or
                        not isinstance(result.get("state"), str) or
                        type(requested) not in (int, float) or
                        not math.isclose(requested, curvature, abs_tol=0.00001) or
                        type(supported) is not bool or type(applied) is not bool or
                        type(ok) is not bool or ok is not applied or type(error_code) is not int or
                        (value is not None and (type(value) not in (int, float) or
                                                not math.isfinite(value) or not 0 <= value <= 1)) or
                        (applied and (not supported or value is None or
                                      not math.isclose(value, curvature, abs_tol=0.0001)))):
                    raise MacViewError("The native Workspace owner returned an invalid curvature result.")
                return {"ready": True, "ok": ok, "state": result["state"], "overlayKey": key,
                        "curvature": {"supported": supported, "applied": applied,
                                      "requested": requested, "value": value,
                                      "errorCode": error_code}}
            except MacViewError:
                raise
            except Exception as e:  # noqa: BLE001 - preserve the helper's ownership/readiness error
                raise MacViewError(f"Could not set native Workspace curvature: {e}") from e

    def workspace_state(self):
        workspace = self.spatial_workspace if self.mode == "spatial" else self.workspace_manager.state()
        return {**workspace, "mode": self.mode, "mouse": os.environ.get("FRAME_WORKSPACE_MOUSE") == "1",
                "selectionRequired": self._mouse_selection_required, "mouseSelection": self._mouse_selection_pending}

    def set_mode(self, mode, restart=True):
        """Select a durable mode, replacing a running setup after confirmed Stop."""
        if mode not in ("desktop", "spatial"):
            raise MacViewError("Mode Must Be Desktop Or Spatial.")
        if mode == "spatial":
            native_window_decoder()  # validate before closing the current display or panels
            if (restart and os.environ.get("FRAME_WORKSPACE_MOUSE") == "1" and self.mode != mode
                    and self.workspace_state().get("state") == "running"):
                return self.start_workspace(*self.last_workspace, mode=mode, resume=True)
        with self.workspace_lock:
            if self.exit_pending:
                raise MacViewError(self.exit_error or "Workspace Stop is still being confirmed. Retry Stop before changing modes.")
            if mode == self.mode:
                return {"mode": mode, "workspace": self.workspace_state(), "changed": False}
            prior = self.workspace_state()
            was_running = prior.get("state") == "running"
            if was_running and restart and mode == "spatial" and quality_settings(self.last_workspace[2])["codec"] != "h264":
                raise MacViewError("Choose Sharp, Balanced Or Light Before Switching To Spatial Mode.")
            if prior.get("state") != "stopped" and not (
                    prior.get("state") == "error" and prior.get("removed") is True):
                self.stop_workspace()
            # Window panels belong to the mode's Last Session too. Finish the
            # old setup before the new helper snapshots any Mac window frames.
            self._close_window_panels()
            self._mode_generation += 1
            self.mode = mode
            self._remember_workspace(*self.last_workspace)
            self.dash_wake.set()
            if was_running and restart:
                return {**self.start_workspace(*self.last_workspace, resume=True), "mode": mode, "changed": True}
            return {"mode": mode, "workspace": self.workspace_state(), "changed": True}

    def _close_window_panels(self):
        sources = {s for s in self.shown if s.startswith(("window:", "separate:", "spatial:")) or
                   s in SPATIAL_AUX_ROLES} | set(self.native_panels)
        errors = []
        for source in sorted(sources):
            if self.agent and self.agent.poll() is None:
                try:
                    self.call("/close", method="POST", src=source)
                except MacViewError as exc:
                    errors.append(str(exc))
            self.shown.discard(source)
        self._stop_native_panels()
        self._clear_spatial_placeholders()
        if self.native_panels:
            raise MacViewError("Window Panel Shutdown Is Unconfirmed. Retry Stop Before Switching Modes.")
        if errors:
            raise MacViewError("Mac window capture shutdown could not be confirmed. Retry Stop. " + errors[0])

    def start_workspace(self, preset="5120x1440", scale=1, quality="sharp", pose=None, resume=False, mode=None, windows=None):
        """Starts the Workspace; with `resume` (the Mac page and the headset's
        Start), then brings back the last session's windows where they were."""
        with self.workspace_lock:
            if self.closing:
                raise MacViewError("The Companion Is Shutting Down.")
            if self.exit_pending:
                raise MacViewError(self.exit_error or "Workspace Stop is still being confirmed. Retry Stop before starting again.")
            if self.headset_viewer.updating():
                raise MacViewError("The headset viewer is updating. Start again when it finishes.")
            WorkspaceDisplayManager._validate_request(preset, scale, quality)
            if pose is not None:
                pose = validate_pose(pose)
                if (mode or self.mode) != "spatial" and workspace_viewer_backend() != "native":
                    raise MacViewError("Saved Workspace poses require the native viewer.")
            if (mode or self.mode) == "spatial":
                if quality_settings(quality)["codec"] != "h264":
                    raise MacViewError("Spatial Mode Requires Sharp, Balanced Or Light Quality.")
                native_window_decoder()  # reject invalid panel settings before a mode/display change
            if (mode or self.mode) == "spatial" and os.environ.get("FRAME_WORKSPACE_MOUSE") == "1":
                if windows is None and self.spatial_workspace.get("state") != "running":
                    choices = self.call("/spatial/windows").get("windows", [])
                    # A Steam resume can reuse one still-valid selected identity;
                    # a new helper/window identity always requires fresh selection.
                    previous = [w for w in choices if self._spatial_admitted.get(w.get("id")) == (w.get("pid"), w.get("stamp"))]
                    if resume and previous:
                        windows = [previous[0]]
                    else:
                        self._mouse_selection_pending = choices
                        self._mouse_selection_required = True
                        self.dash_wake.set()
                        return {"state": "selectionRequired", "selectionRequired": True, "mode": "spatial",
                                "windows": choices, "workspace": self.workspace_state()}
                if windows is not None:
                    validate_spatial_selection(windows)
                    if len(windows) != 1:
                        raise MacViewError("Start With One Window, Then Add More Windows.")
                # Refuse an older host before mode switching, restoring old panels,
                # installing a recovery job, or moving any selected Mac window.
                try:
                    self.headset_viewer.require_spatial(self.frame, mouse=True)
                except ViewerError as exc:
                    raise MacViewError(str(exc)) from exc
            self._mouse_selection_required = False
            self._mouse_selection_pending = []
            self._install_frame_exit_helper()
            if mode is not None:
                self.set_mode(mode, restart=False)
            if (resume and pose is None and self.mode == "desktop" and self.resume_workspace_pose and
                    self.workspace_state().get("state") != "running"):
                pose = self.resume_workspace_pose()
            result = (self._start_spatial(preset, scale, quality, windows) if self.mode == "spatial" else
                      self._start_workspace(preset, scale, quality, pose))
            if result.get("selectionRequired"):
                return result
            if self.exit_pending:
                raise MacViewError("Workspace Stop cancelled the start request.")
            self._mode_generation += 1
            if self.after_workspace_start:
                self.after_workspace_start(resume)
            if self.mode == "spatial":
                self._queue_spatial_reconcile()
            self.dash_wake.set()
            result["mode"] = self.mode
            if "workspace" in result:
                result["workspace"]["mode"] = self.mode
            return result

    def admit_spatial_windows(self, windows):
        selected = validate_spatial_selection(windows)
        with self.workspace_lock:
            if self.mode != "spatial" or self.spatial_workspace.get("state") != "running":
                raise MacViewError("Start The Workspace Before Adding Windows.")
            self.call("/spatial/admit", method="POST", windows=json.dumps(selected, separators=(",", ":")))
            self._spatial_admitted.update({w["id"]: (w["pid"], w["stamp"]) for w in selected})
            self._queue_spatial_reconcile()
            return {"admitted": [w["id"] for w in selected]}

    def _start_spatial(self, preset, scale, quality, windows=None):
        request = WorkspaceDisplayManager._validate_request(preset, scale, quality)
        if quality_settings(quality)["codec"] != "h264":
            raise MacViewError("Spatial Mode Requires Sharp, Balanced Or Light Quality.")
        native_window_decoder()
        workspace_conversion()
        spatial_decode_pixels()
        if self.spatial_workspace.get("state") == "running":
            current = self.call("/spatial")
            if (current.get("state") == "running" and self._spatial_agent is self.agent and
                    self.spatial_workspace.get("quality") == quality):
                self.spatial_workspace.update(request)
                self._remember_workspace(preset, scale, quality)
                return {"state": "running", "workspace": self.workspace_state(), "alreadyRunning": True}
        mouse = os.environ.get("FRAME_WORKSPACE_MOUSE") == "1"
        legacy_all = windows is None and not mouse
        if windows is None and mouse:
            return {"state": "selectionRequired", "selectionRequired": True,
                    "windows": self.call("/spatial/windows").get("windows", []), "workspace": self.workspace_state()}
        selected = [] if legacy_all else validate_spatial_selection(windows)
        if self.spatial_workspace.get("removed") is False:
            self._stop_spatial()
        status = self.call("/status")
        if not status.get("screen") or not status.get("accessibility"):
            raise MacViewError("Spatial Mode Needs Screen Recording And Accessibility Permissions.")
        # Check the viewer path before changing the Mac's display arrangement,
        # even for an empty desktop where no window launcher would run yet.
        self.ensure_tunnel(allow_new_port=not self.shown)
        self._check_startup()
        # The viewer must understand roles, surfaces and wall slots; an older one
        # ignores them. Refuse before the helper moves windows or covers the laptop.
        try:
            self.headset_viewer.require_spatial(self.frame, mouse=mouse)
        except ViewerError as exc:
            raise MacViewError(str(exc)) from exc
        self._check_startup()
        self._close_window_panels()
        self._spatial_cover = False
        self.spatial_workspace = {**request, "state": "starting", "removed": False, "panelsReady": False}
        self._spatial_owned.clear()
        self._spatial_suppressed.clear()
        self._spatial_retry.clear()
        self._spatial_resume_poses.clear()
        self._spatial_slots.clear()
        spatial_launch_parallel()
        try:
            self._check_startup()
            home = self.call("/spatial/start", method="POST", **({"policy": "legacy-all"} if legacy_all else
                             {"windows": json.dumps(selected, separators=(",", ":"))}))
            if home.get("state") != "running":
                raise MacViewError(home.get("error") or "The Spatial Shared Display Is Not Ready.")
            if self.exit_pending or self._stop_requested or self.closing:
                removal = self.call("/spatial/stop", method="POST")
                self.spatial_workspace.update(state="stopped", removed=removal.get("removed") is True)
                raise MacViewError("Workspace Stop cancelled Spatial Mode startup.")
            self._spatial_mouse_ticket = self.call("/workspace/control-ticket", method="POST", start="1")["ticket"] if mouse else ""
        except Exception as exc:
            try:
                cleanup = self.call("/spatial/stop", method="POST")
            except MacViewError:
                cleanup = {"removed": False}
            self.spatial_workspace.update(state="error", error=str(exc), removed=cleanup.get("removed") is True)
            raise MacViewError(f"Spatial Mode Could Not Start: {exc}") from exc
        self._spatial_legacy_all = legacy_all
        self._spatial_admitted = {w["id"]: (w["pid"], w["stamp"]) for w in selected}
        self._spatial_agent = self.agent
        self.spatial_workspace.update(state="running", home=home, removed=False)
        self._remember_workspace(preset, scale, quality)
        self._supervise()
        return {"state": "running", "workspace": self.workspace_state()}

    def _spatial_mode_tick(self):
        if not self.exit_pending and self.mode == "spatial" and self.spatial_workspace.get("state") == "running":
            self._queue_spatial_reconcile()

    def _queue_spatial_reconcile(self):
        # Never wait behind a panel launch on the one-second budget/focus tick.
        if not self.workspace_lock.acquire(blocking=False):
            return
        try:
            if (self._spatial_reconcile_queued or self.closing or self.exit_pending or
                    self.spatial_workspace.get("state") != "running"):
                return
            self._spatial_reconcile_queued = True
            self._dash_run(None, self._reconcile_spatial_windows, self._mode_generation)
        finally:
            self.workspace_lock.release()

    def _spatial_cancelled(self, generation):
        return (self.closing or self.exit_pending or self._stop_requested or self.mode != "spatial" or
                generation != self._mode_generation or self.spatial_workspace.get("state") != "running")

    def _check_startup(self):
        """Between Spatial startup stages: a Stop from the headset or the Mac page wins."""
        if self.closing or self.exit_pending or self._stop_requested:
            raise MacViewError("Workspace Stop cancelled Spatial Mode startup.")

    def _reconcile_spatial_windows(self, generation):
        # Window panels launch in parallel, a few at a time (focused first);
        # every window still waiting shows a placeholder at its wall slot.
        # AX notifications update the helper's confirmed window list. Menu
        # Bar, Status and the menu slots launch only when no window is waiting
        # or launching, so an idle or failing one never delays them.
        again = False
        with self.workspace_lock:
            try:
                if self._spatial_cancelled(generation):
                    return
                state = self.call("/spatial")
                if state.get("state") != "running" or self._spatial_agent is not self.agent:
                    returned = bool(self._spatial_mouse_ticket) and state.get("state") == "stopped" and state.get("removed") is True
                    self.spatial_workspace.update(state="stopped" if returned else "error", removed=state.get("removed") is True,
                                                  panelCleanupPending=bool(self.native_panels),
                                                  error=None if returned else state.get("error") or "The Spatial Helper Stopped. Use Stop Before Restarting.")
                    if returned:
                        self._spatial_mouse_ticket = ""
                    self._spatial_cover = False
                    self._close_window_panels()
                    self.spatial_workspace["panelCleanupPending"] = False
                    return
                if self._spatial_mouse_ticket and "admission" in state:
                    # This is the helper's committed admission set, not its
                    # eligible-window catalog. Native Add uses the same stamped
                    # selection validator as POST /spatial/admit.
                    admitted = state["admission"]
                    selected = validate_spatial_selection(admitted) if admitted else []
                    self._spatial_admitted = {w["id"]: (w["pid"], w["stamp"]) for w in selected}
                windows = {"spatial:" + str(w["id"]): w for w in state.get("windows", [])
                           if isinstance(w, dict) and type(w.get("id")) is int and
                           (self._spatial_legacy_all or self._spatial_admitted.get(w["id"]) == (w.get("pid"), w.get("stamp")))}
                self._spatial_suppressed.intersection_update(windows)
                for source in list(self._spatial_owned - windows.keys()):
                    self.call("/close", method="POST", src=source)
                    self._stop_native_panels(source)
                    if source in self.native_panels:
                        continue  # keep ownership so the next reconciliation retries the removal
                    self.shown.discard(source)
                    self._spatial_owned.discard(source)
                    self._spatial_retry.pop(source, None)
                    self._spatial_resume_poses.pop(source, None)
                for source in list(self._spatial_slots):
                    if source not in windows or source in self._spatial_suppressed:
                        self._spatial_slots.pop(source, None)
                        self._spatial_retry.pop(source, None)
                # The helper supplies front-to-back order; focused window gets first refusal.
                available = (windows.keys() - set(list(self.native_panels)) - self._spatial_suppressed -
                             self._spatial_launching.keys())
                pending = [s for s in windows if s in available]
                focused = state.get("focusedSrc")
                if focused in pending:
                    pending.remove(focused)
                    pending.insert(0, focused)
                # Each waiting window keeps one wall slot from first sight, so
                # its placeholder and its panel open in the same place.
                for source in pending:
                    if source not in self._spatial_slots:
                        used = self._used_wall_slots(source)
                        self._spatial_slots[source] = next(i for i in range(len(used) + 1) if i not in used)
                now = time.monotonic()
                inflight = sum(1 for g in self._spatial_launching.values() if g == generation)
                room = max(0, spatial_launch_parallel() - inflight)
                for candidate in [s for s in pending if self._spatial_retry.get(s, {}).get("after", 0) <= now][:room]:
                    pose = self._spatial_resume_poses.get(candidate)
                    slot = None if pose is not None else self._spatial_slots.get(candidate)
                    self._spatial_launching[candidate] = generation
                    pending.remove(candidate)
                    self._launch_run(self._launch_spatial_window, candidate, windows[candidate], pose, slot, generation)
                launching = any(g == generation for g in self._spatial_launching.values())
                if not pending and not launching and not self._spatial_cancelled(generation):
                    again = self._ensure_spatial_aux_panels()
                if not self._spatial_cancelled(generation):
                    self._sync_spatial_placeholders(windows)
                    self._update_spatial_cover()
                self.spatial_workspace.update(home=state,
                    panelsReady=not pending and not launching and SPATIAL_AUX_ROLES.keys() <= set(list(self.native_panels)),
                    windowCount=len(windows),
                    panelErrors={s: e["error"] for s, e in self._spatial_retry.items() if s in windows or s in SPATIAL_AUX_ROLES})
                self.dash_wake.set()
            finally:
                self._spatial_reconcile_queued = False
                # Keep launching while there is work, without waiting for the next tick.
                if again and not self._spatial_cancelled(generation):
                    self._queue_spatial_reconcile()

    @staticmethod
    def _launch_thread(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _launch_spatial_window(self, src, info, pose, slot, generation):
        """One window's panel, on its own thread (the reconciler bounds how
        many run at once). Its outcome is recorded under the Workspace lock."""
        error = None
        try:
            if self._spatial_cancelled(generation) or self.closing or self.exit_pending:
                raise MacViewError("Spatial Mode Stopped Before This Window Opened.")
            if not self._spatial_legacy_all and self._spatial_admitted.get(info.get("id")) != (info.get("pid"), info.get("stamp")):
                raise MacViewError("Window Was Not Admitted To This Workspace.")
            with self.viewer_lock:
                self.launching += 1
            try:
                self._show(src, self.spatial_workspace.get("quality", "balanced"), info.get("w"), info.get("h"),
                           pose=validate_pose(pose) if pose is not None else None, wall_slot=slot, reconciled=True)
            finally:
                with self.viewer_lock:
                    self.launching -= 1
        except Exception as exc:  # noqa: BLE001 - shown on the placeholder, the bar and the Mac page
            error = exc
        with self.workspace_lock:
            if self._spatial_launching.get(src) == generation:
                self._spatial_launching.pop(src, None)
            cancelled = self._spatial_cancelled(generation) or src in self._spatial_suppressed
            if error is None and src in self.native_panels:
                if cancelled:
                    # Stop, a mode change or a Close came while it launched.
                    try:
                        self.call("/close", method="POST", src=src)
                    except MacViewError:
                        pass
                    self._stop_native_panels(src)
                    self.shown.discard(src)
                else:
                    self._spatial_owned.add(src)
                    self._spatial_retry.pop(src, None)
                    self._spatial_resume_poses.pop(src, None)
                    self._spatial_slots.pop(src, None)
            elif error is not None and not cancelled:
                if src not in self.native_panels:
                    try:
                        self.call("/close", method="POST", src=src)
                    except MacViewError:
                        pass
                self._spatial_retry[src] = {"after": time.monotonic() + 5, "error": str(error)}
                self._record_error(f"Couldn't Show {info.get('app') or 'A Window'}: {error}", kind="panel")
            self.dash_wake.set()
        if not cancelled:
            self._queue_spatial_reconcile()

    def _placeholder_items(self, windows):
        items = []
        for source, info in windows.items():
            if source in self.native_panels or source in self._spatial_suppressed:
                continue
            digits = source.split(":", 1)[1]
            item = {"panel": "w" + digits,
                    "name": re.sub(r"[\x00-\x1f\x7f]", " ", str(info.get("title") or info.get("app") or "Mac Window"))[:64]}
            bundle = info.get("bundleID")
            if isinstance(bundle, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,255}", bundle):
                item["bundleID"] = bundle
            pw, ph = info.get("w"), info.get("h")
            if type(pw) is int and type(ph) is int and 1 <= pw <= 100000 and 1 <= ph <= 100000:
                item["points"] = [pw, ph]
            pose = self._spatial_resume_poses.get(source)
            if pose is not None:
                item["pose"] = json.loads(launcher_pose(pose))
            elif self._spatial_slots.get(source) is not None:
                item["slot"] = self._spatial_slots[source]
            if source in self._spatial_retry:
                item["error"] = PLACEHOLDER_ERROR
            items.append(item)
        return items[:128]

    def _sync_spatial_placeholders(self, windows):
        """Placeholder-only panels on the Frame for every window still without
        its own panel: queued, launching (its own spec replaces it) or
        retrying after a failure, with a short error. One call per change."""
        items = self._placeholder_items(windows)
        body = json.dumps({"panels": items, "session": self.session_id}, separators=(",", ":"), sort_keys=True)
        if body == self._placeholders_synced or (not items and not self._placeholders_on_frame):
            return
        if time.monotonic() < self._placeholder_retry_after:
            return
        try:
            self.run("FRAME_NATIVE_HOST=1 bash -s -- placeholders " + _quote(body),
                     stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=20)
        except Exception:  # noqa: BLE001 - the windows still open; placeholders retry shortly
            self._placeholder_retry_after = time.monotonic() + 5
            return
        self._placeholders_synced = body
        self._placeholders_on_frame = bool(items)

    def _clear_spatial_placeholders(self):
        if not self._placeholders_on_frame:
            return
        self._placeholders_synced = None
        try:
            self.run("FRAME_NATIVE_HOST=1 bash -s -- placeholders " + _quote(json.dumps({"panels": []})),
                     stdin=WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"), timeout=20)
            self._placeholders_on_frame = False
        except Exception:  # noqa: BLE001 - Stop on the Frame removes them with every other spec
            pass

    def arrange_windows(self):
        """Arrange Windows (the Mac page; the bar's request comes through the
        Spatial app state): every window panel back onto the wall in front of
        the wearer, kept there as their own layout."""
        if self.mode != "spatial" or self.spatial_workspace.get("state") != "running":
            raise MacViewError("Arrange Windows Needs Spatial Mode Running.")
        self.spatial_apps.arrange()
        return {"arranged": True, "workspace": self.workspace_state()}

    def _update_spatial_cover(self):
        """Black out the laptop only while the headset shows a window panel.

        A window panel is in native_panels only after its launcher saw a
        submitted picture, so this is the first moment the user can see the
        Mac in the headset. If every window panel goes, the laptop shows again."""
        want = any(p.get("role", "window") == "window" for p in list(self.native_panels.values()))
        if want == self._spatial_cover:
            return
        try:
            self.call("/spatial/cover", method="POST", show="1" if want else "0")
            self._spatial_cover = want
        except MacViewError as exc:
            self._record_error(f"Couldn't {'Cover' if want else 'Uncover'} The Laptop Screen: {exc}", kind="panel")

    def _ensure_spatial_aux_panels(self):
        """Launch the next missing auxiliary panel; True if one launched."""
        now = time.monotonic()
        for source, role in SPATIAL_AUX_ROLES.items():
            if source in self.native_panels or self._spatial_retry.get(source, {}).get("after", 0) > now:
                continue
            try:
                self._show(source, self.spatial_workspace["quality"], None, None, role=role)
                self._spatial_retry.pop(source, None)
                return True
            except Exception as exc:
                self.call("/close", method="POST", src=source)
                self._stop_native_panels(source)
                self._spatial_retry[source] = {"after": now + 5, "error": str(exc)}
                self._record_error(f"Couldn't Open The {self.AUX_NAMES.get(role, 'Menu')} Panel: {exc}", kind="panel")
                return True  # it waits five seconds; the next one may go now
        return False

    def _stop_spatial(self):
        with self.workspace_lock:
            if self.before_workspace_stop:
                try:
                    self.before_workspace_stop()
                except Exception:
                    pass
            self._mode_generation += 1
            self.spatial_workspace["state"] = "stopping"
            try:
                # The helper revokes reconnects, ends captures, restores the
                # original layout/windows, then confirms shared-display removal.
                removal = self.call("/spatial/stop", method="POST")
                if removal.get("removed") is not True or removal.get("state") != "stopped":
                    raise MacViewError(removal.get("error") or "Spatial Display Removal Is Still Pending. Retry Stop After The Mac Wakes.")
                self.spatial_workspace.update(state="stopped", removed=True, panelCleanupPending=bool(self.native_panels))
                self._close_window_panels()
                self.spatial_workspace["panelCleanupPending"] = False
            except MacViewError as exc:
                self.spatial_workspace.update(state="error", error=str(exc))
                raise
            self._spatial_owned.clear()
            self._spatial_resume_poses.clear()
            self._spatial_retry.clear()
            self._spatial_slots.clear()
            self._spatial_cover = False  # the helper took it down with the display
            self.dash_wake.set()
            return {**removal, "mode": "spatial", "workspace": self.workspace_state()}

    def _stop_workspace_session(self):
        """Stop the monitor and every companion-owned Frame viewer in one session."""
        errors = []

        def attempt(operation):
            try:
                return operation()
            except Exception as exc:  # keep closing the remaining owned viewers
                errors.append(str(exc))
                return None

        try:
            attempt(self._stop_frame_session)
            # Always ask the Mac owner to confirm or recover its display state.
            # A prior stream status of "stopped" does not prove that the owned
            # virtual display and moved windows finished cleanup.
            attempt(self.stop_workspace)

            # Confirm the reserved Workspace viewer even if the Mac API already
            # reported a stopped display. The Frame-side helper closes it first;
            # this is the retryable companion check for a surviving unit.
            attempt(self._stop_workspace_viewer)
            attempt(self._close_window_panels)

            # Browser-backed auxiliary viewers have no panel unit. Close their
            # Mac streams, then end only the Frame Workspace browser profile.
            if self.agent and self.agent.poll() is None:
                if attempt(lambda: self.call("/close", method="POST")) is not None:
                    self.shown.clear()
            else:
                self.shown.clear()
            attempt(lambda: self.run("bash -s", stdin=CLOSE_BROWSER, timeout=15))
            attempt(lambda: self.run(_quote(FRAME_VRCMD) + " --hidedashboard", timeout=5))
            if self.native_panels:
                errors.append("Frame Workspace panels are still registered; retry Stop.")
            final_state = self.workspace_state()
            if final_state.get("state") != "stopped" or self.workspace_removal_pending:
                errors.append("The Mac has not confirmed monitor and display cleanup; retry Stop.")
            if errors:
                raise MacViewError("Workspace cleanup is not confirmed; retry Stop. " + errors[0])
            self.browser_cleanup_error = None
            self.exit_pending = False
            self.exit_error = None
            self.session_id = secrets.token_hex(16)
            self.dash_wake.set()
            return {"state": "stopped", "removed": True}
        except Exception as exc:
            self.exit_pending = True
            self.exit_error = str(exc)
            self.dash_wake.set()
            if isinstance(exc, MacViewError):
                raise
            raise MacViewError("Workspace cleanup is not confirmed; retry Stop. " + str(exc)) from exc

    def _start_workspace(self, preset="5120x1440", scale=1, quality="sharp", pose=None):
        """Create/reuse the owned display and show its stable workspace panel.

        A size or scale change replaces the old display only after its viewer,
        stream, and macOS virtual display have all stopped successfully.
        """
        request = WorkspaceDisplayManager._validate_request(preset, scale, quality)
        if pose is not None:
            pose = validate_pose(pose)
            if workspace_viewer_backend() != "native":
                raise MacViewError("Saved Workspace poses require the native viewer.")
        self._remember_workspace(request["preset"], request["scale"], request["quality"])
        if workspace_viewer_backend() == "native":
            # Desktop Mode works with an older viewer; the Mac page offers the update.
            self.headset_viewer.check(self.frame, force=True)
        with self.workspace_lock:
            if self.workspace_removal_pending:
                # The last Stop could not confirm WindowServer removal. Retry
                # safe recovery before accepting any new display request.
                self.stop_workspace()
            prior = self.workspace_manager.state()
            prior_quality = prior.get("quality") if prior.get("state") == "running" else None
            replaced = False
            previous_preset = prior.get("preset")
            build_needed = prior.get("state") not in ("running", "starting")

            if prior.get("state") == "owner_unknown":
                raise MacViewError("A Workspace display is registered outside this companion. Stop it and wait "
                                   "for confirmed removal before starting another.")
            if prior.get("state") == "stopped":
                registered = self.call("/displays").get("displays", [])
                if any(display.get("name") == "Frame Workspace" for display in registered):
                    raise MacViewError("A Frame Workspace display is still registered without an owned helper. "
                                       "Stop Workspace and wait for confirmed removal before starting another.")

            if prior.get("state") == "running" and any(
                    prior.get(key) != request[key] for key in ("preset", "scale")):
                # Normal Stop restores eligible moved windows. The replacement
                # starts only after confirmed removal; macOS may rehome windows.
                self.stop_workspace()
                replaced = True
                build_needed = False  # the live owner already used this binary
                prior_quality = None
                prior = self.workspace_manager.state()

            if prior.get("state") == "error" and (prior.get("captureSrc") or prior.get("removed") is False):
                # Do not turn an unconfirmed prior removal into a second display.
                self.stop_workspace()
                prior = self.workspace_manager.state()
                prior_quality = None
                build_needed = True

            if build_needed:
                # Workspace uses a newer CLI command than the original helper;
                # verify/rebuild the binary before creating any display.
                self.build()
            workspace, created = self.workspace_manager.start(request["preset"], request["scale"], request["quality"])
            if self.exit_pending:
                cleanup = self.workspace_manager.stop()
                self.workspace_removal_pending = cleanup.get("removed") is False
                raise MacViewError("Workspace Stop cancelled the display start.")
            capture_src = workspace["captureSrc"]
            if pose is None and not created and prior_quality == quality and capture_src in self.shown and self.workspace_panel_key:
                status = self.call("/status")
                if any(s.get("src") == capture_src for s in status.get("streams", [])):
                    self._focus_workspace_viewer()
                    return {"id": WorkspaceDisplayManager.KEY, "state": "running",
                            "panel": self.workspace_panel_key,
                            "workspace": workspace, "alreadyRunning": True}
            if not created and prior_quality != quality and capture_src in self.shown:
                self.stop(capture_src)
            try:
                panel = self.show(WorkspaceDisplayManager.KEY, quality=quality,
                                  width=workspace["stream"]["width"], height=workspace["stream"]["height"],
                                  **({"pose": pose} if pose is not None else {}))
            except Exception as e:
                try:
                    self._stop_workspace_viewer()
                except MacViewError:
                    pass
                try:
                    self.stop(capture_src)
                except MacViewError:
                    pass
                cleanup = self.workspace_manager.stop()
                detail = str(e)
                if cleanup.get("removed") is False:
                    detail += " Workspace display removal is pending; retry Stop after the Mac wakes."
                raise MacViewError(f"Couldn't show Workspace in the Frame; its display was rolled back. {detail}") from e
            return {"id": WorkspaceDisplayManager.KEY, "state": "running", "workspace": self.workspace_manager.state(),
                    "replaced": replaced, "previousPreset": previous_preset if replaced else None, **panel}

    def _workspace_ended(self, capture_src):
        """Close an old display stream if its independent owner exits/crashes."""
        if self.closing:
            return
        try:
            self._stop_workspace_viewer()
            self.workspace_panel_key = None
            self.stop(capture_src)
        except MacViewError:
            pass  # it is already gone or the agent is restarting; no stale ID is retained

    def stop_workspace(self):
        """Close only the workspace panel/stream, then release its owned display."""
        if self.mode == "spatial":
            # Tell a running startup or panel launch to stop at its next stage,
            # before waiting for the lock it holds.
            self._stop_requested += 1
            self._mode_generation += 1
            try:
                return self._stop_spatial()
            finally:
                self._stop_requested -= 1
        if self.before_workspace_stop and self.workspace_panel_key:
            try:
                self.before_workspace_stop()  # the session's last snapshot, before anything closes
            except Exception:  # noqa: BLE001 - never block a stop
                pass
        with self.workspace_lock:
            workspace = self.workspace_manager.state()
            capture_src = workspace.get("captureSrc")
            panel_result = {"closed": 0}
            panel_error = None
            local_owner = workspace.get("state") in ("running", "starting", "error")
            if local_owner:
                # Close only the service reserved for this Workspace before its
                # source disappears; a failed stop leaves the display intact.
                self._stop_workspace_viewer()
            if capture_src:
                try:
                    panel_result = self.stop(capture_src)
                except MacViewError as e:
                    panel_error = str(e)
            if panel_error:
                raise MacViewError("Could not confirm Workspace stream shutdown; the display was left in place. "
                                   + panel_error)

            try:
                if workspace.get("state") in ("running", "starting", "error"):
                    removal = self.workspace_manager.stop()
                else:
                    # After a companion crash there is no retained display ID or
                    # process handle. Let the Swift owner query WindowServer even
                    # when the capture agent is unavailable; its lease prevents it
                    # from changing a display owned by another live process.
                    removal = self.workspace_manager.recover_orphan()
                    # Recovery succeeded, so no other live Workspace owner can
                    # own the reserved service. This also clears a service orphaned
                    # by a previous companion crash.
                    self._stop_workspace_viewer()
            except MacViewError:
                self.workspace_removal_pending = True
                raise

            if removal.get("removed") is False:
                # The owner's normal release can be deferred while the built-in
                # screen sleeps. Retry via the safe orphan path, which waits for
                # WindowServer removal and never calls CGVirtualDisplay.release
                # on an object it does not own.
                try:
                    removal = self.workspace_manager.recover_orphan()
                except MacViewError as e:
                    removal["recoveryError"] = str(e)
            if removal.get("removed") is False:
                self.workspace_removal_pending = True
                raise MacViewError(removal.get("recoveryError") or removal.get("error") or
                                   "macOS has not confirmed Workspace display removal; the display was not falsely reported as stopped.")
            self.workspace_removal_pending = False
            self.workspace_panel_key = None
            result = {"id": WorkspaceDisplayManager.KEY, "state": "stopped", "removed": True,
                      "panelClosed": panel_result.get("closed", 0), "windowsRestored": removal.get("windowsRestored", 0),
                      "recoveredOrphan": removal.get("recoveredOrphan", False), "owned": removal.get("owned", True)}
            if panel_error:
                result["panelError"] = panel_error
            return result

    def stop(self, src=None):
        workspace_error = None
        if src is None and (self.workspace_panel_key or self.workspace_state().get("state") != "stopped"):
            try:
                self.stop_workspace()
            except MacViewError as e:
                workspace_error = e
        if src:
            if self.mode == "spatial" and src.startswith(("window:", "spatial:")):
                self._spatial_suppressed.add("spatial:" + src.split(":", 1)[1])
            self.shown.discard(src)
        else:
            self.shown.clear()
        if not (self.agent and self.agent.poll() is None):
            if self.native_panels:
                # The Mac server may already be gone during shutdown. Native
                # panels still have independent Frame-side owners and need
                # their specs removed directly.
                self._stop_native_panels(src)
            if workspace_error:
                raise workspace_error
            return {"closed": 0}
        out = self.call("/close", method="POST", **({"src": src} if src else {}))
        if self.native_panels:
            threading.Thread(target=self._stop_native_panels, args=(src,), daemon=True).start()
        if not self.shown:
            threading.Thread(target=self._end_viewer_browser, args=(self.shows,), daemon=True).start()
        if workspace_error:
            raise workspace_error
        return out

    def _end_viewer_browser(self, shows):
        """Chromium on the Frame outlives its last viewer window (verified
        2026-09-28), so once nothing is shown, end it. It runs with a profile
        of its own, so nothing else is touched."""
        time.sleep(2)  # the viewers close their windows first
        with self.browser_lock:
            with self.viewer_lock:
                if self.shown or self.shows != shows or self.launching:
                    return
            try:
                self.run("bash -s", stdin=CLOSE_BROWSER, timeout=15)
                self.browser_cleanup_error = None
            except Exception as e:
                self.browser_cleanup_error = str(e)

    def finish_browser_cleanup(self):
        """Wait for last-viewer cleanup before a short-lived parent exits."""
        self._end_viewer_browser(self.shows)
        if self.browser_cleanup_error:
            raise MacViewError("Frame Workspace browser cleanup was not confirmed: " + self.browser_cleanup_error)

    def state(self):
        reason = self.unavailable()
        if reason:
            return {"available": False, "reason": reason}
        status = self.call("/status")
        windows = self.call("/windows").get("windows", []) if status.get("screen") else []
        displays = self.call("/displays").get("displays", [])
        workspace = self.workspace_state()
        if self.mode == "desktop" and workspace.get("state") == "stopped":
            registered = next((d for d in displays if d.get("name") == "Frame Workspace"), None)
            if registered:
                workspace = {"id": WorkspaceDisplayManager.KEY, "state": "owner_unknown",
                             "displayID": registered.get("id"), "captureSrc": registered.get("src"),
                             "display": {"logicalWidth": registered.get("w"), "logicalHeight": registered.get("h"),
                                         "bufferWidth": registered.get("pixelW"), "bufferHeight": registered.get("pixelH")},
                             "error": "A Frame Workspace display is registered, but this companion does not own its process. Stop checks the live-owner lease before recovery."}
        workspace = {**workspace, "mode": self.mode,
                     "placement": self.workspace_placement(workspace) if self.mode == "desktop" else None}
        return {"available": True, "screen": status.get("screen", False),
                "accessibility": status.get("accessibility", False), "streams": status.get("streams", []),
                "windows": windows, "displays": displays, "workspace": workspace, "tunnel": self.tunnel_up(),
                "route": self.route, "recentErrors": self.errors_state(), "viewer": self.headset_viewer.state()}

    # ---- the Frame's native viewer (frame_native_update.py) ----

    def check_headset_viewer(self):
        if self.headset_viewer.check(self.frame, force=True) is None:
            raise MacViewError("Could not check the headset viewer. Is the Frame connected?")
        return self.headset_viewer.state()

    def update_headset_viewer(self):
        """Update Headset Viewer: rebuild it on the Frame from this app's sources."""
        with self.workspace_lock:
            idle = (self.workspace_state().get("state") == "stopped" and not self.native_panels and
                    not self.workspace_panel_key and not self.exit_pending and not self.closing)
            try:
                return self.headset_viewer.start_update(self.frame, idle)
            except ViewerError as exc:
                raise MacViewError(str(exc)) from exc

    def restart_agent(self):
        """Only if it's missing a permission: a running stream would stop."""
        with self.lock:
            if not (self.agent and self.agent.poll() is None):
                return
        status = self.call("/status")
        if status.get("screen") and status.get("accessibility"):
            return
        with self.lock:
            self.agent.terminate()
            self.agent.wait(5)

    def request_permissions(self):
        return self.call("/permissions", method="POST")

    def shutdown(self):
        self.closing = True
        try:
            self.stop()
        except MacViewError:
            # The Swift owner also exits on stdin EOF. Keep shutdown bounded
            # and never claim removal if WindowServer deferred it.
            self.workspace_manager.stop()
        try:
            self.finish_browser_cleanup()
        except MacViewError:
            pass
        for proc in (self.tunnel, self.agent):
            if proc and proc.poll() is None:
                proc.terminate()
        # The helper puts separated windows back before it exits (about 1 s).
        if self.agent:
            try:
                self.agent.wait(15)
            except subprocess.TimeoutExpired:
                self.agent.kill()


def _quote(s):
    return "'" + s.replace("'", "'\\''") + "'"
