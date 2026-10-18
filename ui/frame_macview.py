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
  cmd=("$HOME/chromium-xr/chrome" "--user-data-dir=$HOME/.local/share/frame-control/mac-view" "${common[@]}")
elif flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  cmd=(flatpak run org.chromium.Chromium
    "--user-data-dir=$HOME/.var/app/org.chromium.Chromium/data/frame-mac-view" "${common[@]}")
elif flatpak --user info com.google.Chrome >/dev/null 2>&1; then
  cmd=(flatpak run --user com.google.Chrome
    "--user-data-dir=$HOME/.var/app/com.google.Chrome/data/frame-mac-view" "${common[@]}")
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


class MacViewError(Exception):
    pass


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
            return self._show(src, quality, width, height)
        finally:
            with self.viewer_lock:
                self.launching -= 1

    def _show(self, src, quality, width, height):
        if src != "test" and not src.startswith(("window:", "display:", "separate:")):
            raise MacViewError("Pick a window or display to show.")
        self.shows += 1
        q = QUALITY.get(quality) or QUALITY["balanced"]
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
        appid = panel_id(src)
        # Unique per launch, so a new window is never confused with an old one.
        tag = "fc" + secrets.token_hex(4)
        # A single-use ticket for this source, not Frame Control's key: the URL
        # is visible in the Frame's process list.
        ticket = self.call("/ticket", method="POST", src=src)["ticket"]
        params = {"src": src, "t": ticket, "tag": tag, "codec": q["codec"], "max": q["max"], "fps": q["fps"],
                  "bpp": q["bpp"]}
        url = f"http://127.0.0.1:{self.remote_port}/view?{urlencode(params)}"
        w, h = fit(width or 1280, height or 720)
        args = " ".join(_quote(str(a)) for a in (appid, url, w, h, tag, *self.browser_flags))
        try:
            out = self.run("bash -s -- " + args, stdin=LAUNCH, timeout=60)
        except Exception as e:  # noqa: BLE001 - the server's Failure carries the Frame's words
            if "NO_BROWSER" in (getattr(e, "stdout", "") or ""):
                raise MacViewError("The Frame needs Chromium, Chromium XR, or the Google Chrome Flatpak "
                                   "(com.google.Chrome) for this.")
            raise MacViewError(str(e))
        self.shown.add(src)
        return {"panel": f"valve.steam.desktopgame.{appid}", "src": src, "detail": out.strip()}

    def stop(self, src=None):
        if src:
            self.shown.discard(src)
        else:
            self.shown.clear()
        if not (self.agent and self.agent.poll() is None):
            return {"closed": 0}
        out = self.call("/close", method="POST", **({"src": src} if src else {}))
        if not self.shown:
            threading.Thread(target=self._end_viewer_browser, args=(self.shows,), daemon=True).start()
        return out

    def _end_viewer_browser(self, shows):
        """Chromium on the Frame outlives its last viewer window (verified
        2026-09-28), so once nothing is shown, end it. It runs with a profile
        of its own, so nothing else is touched."""
        time.sleep(2)  # the viewers close their windows first
        with self.viewer_lock:
            if self.shown or self.shows != shows or self.launching:
                return
            try:
                self.run("pkill -f '[f]rame-control/mac-view|[d]ata/frame-mac-view' || true", timeout=10)
            except Exception:
                pass

    def state(self):
        reason = self.unavailable()
        if reason:
            return {"available": False, "reason": reason}
        status = self.call("/status")
        windows = self.call("/windows").get("windows", []) if status.get("screen") else []
        displays = self.call("/displays").get("displays", [])
        return {"available": True, "screen": status.get("screen", False),
                "accessibility": status.get("accessibility", False), "streams": status.get("streams", []),
                "windows": windows, "displays": displays, "tunnel": self.tunnel_up(),
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
