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
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import quote, urlencode  # %20, not +: the agent's URLComponents keeps +

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PAGE = HERE / "mac-view.html"
SOURCES = ROOT / "mac" / "frame-mac-view"
WORKSPACE_VIEWER_SCRIPT = ROOT / "scripts" / "workspace-viewer-on-frame.sh"
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
    if workspace and name == "sharp":
        fps = os.environ.get("FRAME_WORKSPACE_FPS", "30")
        if fps not in ("30", "60"):
            raise MacViewError("FRAME_WORKSPACE_FPS must be 30 or 60.")
        return {**settings, "fps": int(fps)}
    return settings
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
setsid nohup "${cmd[@]}" >>"$log" 2>&1 </dev/null &
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
            try:
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
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


def fit(w, h, box=PANEL_BOX):
    s = min(box[0] / max(w, 1), box[1] / max(h, 1))
    return max(320, round(w * s)), max(200, round(h * s))


class MacView:
    def __init__(self, tunnel_ssh, run, frame, track=None):
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
        self.shown = set()  # sources with a viewer out there, connected or retrying
        self.browser_flags = list(BROWSER_FLAGS)
        self.browser_lock = threading.Lock()
        self.browser_cleanup_error = None
        self.workspace_panel_key = None

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
            threading.Thread(target=self.agent.stdout.read, daemon=True).start()  # drain

    def call(self, path, method="GET", **query):
        """A request to the agent; starts it if needed."""
        self.ensure_agent()
        url = f"http://127.0.0.1:{self.port}{path}?{urlencode({**query, 'k': self.token}, quote_via=quote)}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
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
            proc = subprocess.Popen([*self.tunnel_ssh, "-o", "ControlPath=none", *via, *target[1],
                                     "-o", "ExitOnForwardFailure=yes",
                                     "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3", "-N",
                                     "-R", f"127.0.0.1:{port}:127.0.0.1:{self.port}", target[0]],
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

    def _supervise(self):
        """Reopen the tunnel on the same port after the headset sleeps or the
        network drops, so viewers that are retrying find the Mac again."""
        if self.supervisor and self.supervisor.is_alive():
            return

        def loop():
            while not self.closing:
                time.sleep(5)
                if self.closing or self.tunnel_up() or not (self.agent and self.agent.poll() is None):
                    continue
                try:
                    self.ensure_tunnel()
                except MacViewError:
                    pass  # still unreachable; try again shortly

        self.supervisor = threading.Thread(target=loop, daemon=True)
        self.supervisor.start()

    def _probe(self, port):
        """Whether the Frame reaches the agent through the tunnel on `port`."""
        # /ping needs no key, so none appears on the Frame's command lines.
        cmd = f"curl -s -m 2 'http://127.0.0.1:{port}/ping'"
        try:
            return self.run(cmd, timeout=8).strip() == "frame-mac-view"
        except Exception:  # noqa: BLE001 - the server's Failure, timeouts: all mean "not yet"
            return False

    # ---- viewers on the Frame ----

    def show(self, src, quality="balanced", width=None, height=None):
        with self.viewer_lock:
            self.launching += 1
        try:
            if src == WorkspaceDisplayManager.KEY:
                workspace = self.workspace_manager.state()
                if workspace.get("state") != "running":
                    raise MacViewError("Start the Workspace display before showing it.")
                return self._show(workspace["captureSrc"], quality, width, height,
                                  panel_key=WorkspaceDisplayManager.KEY, public_src=WorkspaceDisplayManager.KEY)
            return self._show(src, quality, width, height)
        finally:
            with self.viewer_lock:
                self.launching -= 1

    def _show(self, src, quality, width, height, panel_key=None, public_src=None):
        if src != "test" and not src.startswith(("window:", "display:", "separate:")):
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
        url = f"http://127.0.0.1:{self.remote_port}/view?{urlencode(params)}"
        if workspace_viewer:
            workspace = self.workspace_manager.state()
            stream = workspace.get("stream", {})
            w, h = int(width or stream.get("width") or 1280), int(height or stream.get("height") or 720)
            script_args = " ".join(_quote(str(a)) for a in ("start", url, w, h, tag, *self.browser_flags))
            out = self.run("bash -s -- " + script_args, stdin=WORKSPACE_VIEWER_SCRIPT.read_text(encoding="utf-8"),
                           timeout=60)
            match = re.search(r"registered OpenVR key (com\.frame-control\.workspace\.\d+\.fc[0-9a-f]{8})\b", out)
            if not match:
                raise MacViewError("The dedicated Workspace launcher did not report its registered OpenVR panel key.")
            self.workspace_panel_key = match.group(1)
        else:
            w, h = fit(width or 1280, height or 720)
            args = " ".join(_quote(str(a)) for a in (appid, url, w, h, tag, *self.browser_flags))
            with self.browser_lock:
                if self.browser_cleanup_error:
                    raise MacViewError("The previous Frame Workspace browser did not confirm shutdown. Stop all viewers and retry.")
                try:
                    out = self.run("bash -s -- " + args, stdin=LAUNCH, timeout=60)
                except Exception as e:  # noqa: BLE001 - the server's Failure carries the Frame's words
                    if "NO_BROWSER" in (getattr(e, "stdout", "") or ""):
                        raise MacViewError("The Frame needs Chromium, Chromium XR, or the Google Chrome Flatpak "
                                           "(com.google.Chrome) for this.")
                    raise MacViewError(str(e))
        self.shown.add(src)
        panel = self.workspace_panel_key if workspace_viewer else f"valve.steam.desktopgame.{appid}"
        return {"panel": panel, "src": public_src, "captureSrc": src, "detail": out.strip()}

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
            out = self.run(f"{_quote(FRAME_VRCMD)} --showdashboard {_quote(key)}", timeout=10)
        except Exception as e:  # noqa: BLE001 - preserve the headset tool's diagnostic
            raise MacViewError(f"Could not focus the Workspace OpenVR panel: {e}") from e
        return out.strip()

    def start_workspace(self, preset="5120x1440", scale=1, quality="sharp"):
        """Create/reuse the owned display and show its stable workspace panel.

        A size or scale change replaces the old display only after its viewer,
        stream, and macOS virtual display have all stopped successfully.
        """
        request = WorkspaceDisplayManager._validate_request(preset, scale, quality)
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
            capture_src = workspace["captureSrc"]
            if not created and prior_quality == quality and capture_src in self.shown and self.workspace_panel_key:
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
                                  width=workspace["stream"]["width"], height=workspace["stream"]["height"])
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
        if src is None and (self.workspace_panel_key or self.workspace_manager.state().get("state") != "stopped"):
            try:
                self.stop_workspace()
            except MacViewError as e:
                workspace_error = e
        if src:
            self.shown.discard(src)
        else:
            self.shown.clear()
        if not (self.agent and self.agent.poll() is None):
            if workspace_error:
                raise workspace_error
            return {"closed": 0}
        out = self.call("/close", method="POST", **({"src": src} if src else {}))
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
        workspace = self.workspace_manager.state()
        if workspace.get("state") == "stopped":
            registered = next((d for d in displays if d.get("name") == "Frame Workspace"), None)
            if registered:
                workspace = {"id": WorkspaceDisplayManager.KEY, "state": "owner_unknown",
                             "displayID": registered.get("id"), "captureSrc": registered.get("src"),
                             "display": {"logicalWidth": registered.get("w"), "logicalHeight": registered.get("h"),
                                         "bufferWidth": registered.get("pixelW"), "bufferHeight": registered.get("pixelH")},
                             "error": "A Frame Workspace display is registered, but this companion does not own its process. Stop checks the live-owner lease before recovery."}
        return {"available": True, "screen": status.get("screen", False),
                "accessibility": status.get("accessibility", False), "streams": status.get("streams", []),
                "windows": windows, "displays": displays, "workspace": workspace, "tunnel": self.tunnel_up(),
                "route": self.route}

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
                self.agent.wait(3)
            except subprocess.TimeoutExpired:
                self.agent.kill()


def _quote(s):
    return "'" + s.replace("'", "'\\''") + "'"
