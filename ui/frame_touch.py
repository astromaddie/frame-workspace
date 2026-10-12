"""Touch and direct input for the Steam Frame's panels. Frame Control's server runs this ON the Frame.

gamescope, the Frame's compositor, serves Valve's own input injection: an EIS
socket (libei's server side), which Steam uses to feed it Remote Play input.
This connects to it with libei, which is on the SteamOS image, and points,
clicks, scrolls and types into the panel that has focus in the headset: the
one the wearer last used. No install, and it reaches every panel, on either
of gamescope's X displays (see docs/streaming.md).

  python3 frame_touch.py focus   print the focused panel as JSON
  python3 frame_touch.py panels  print every app panel as JSON, and which has focus
  python3 frame_touch.py         read events on stdin, one JSON object (or list) per line:
    {"fx": 0.5, "fy": 0.2, "window": 123, "display": ":1"}
                                           pointer to that fraction of that panel; any event can
                                           name its panel, and goes nowhere if another has focus
    {"dx": 4, "dy": -2}                    pointer by that much
    {"button": "left", "down": true}       left, right or middle; "down" false releases
    {"scroll": [0, 120]}                   by pixels; positive y scrolls down
    {"key": 30, "down": true}              a Linux (evdev) key code, as the page maps KeyboardEvent.code
    {"text": "hello"}                      printable ASCII, typed on a US layout

Status goes to stdout, one JSON object per line: {"state": "ready" | "error", ...}.
Standard library only (ctypes for libei), like the rest of what runs on the Frame.
"""
import ctypes
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time

SOCKET = "/run/user/{uid}/gamescope-0-ei"  # filled in on the Frame (Windows has no getuid; the tests import this)
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}  # BTN_LEFT, BTN_RIGHT, BTN_MIDDLE
SHIFT = 42  # KEY_LEFTSHIFT
# Printable ASCII on a US layout: character -> (evdev key code, shifted).
ROWS = [("1234567890-=", "!@#$%^&*()_+", 2), ("qwertyuiop[]", "QWERTYUIOP{}", 16),
        ("asdfghjkl;'`", 'ASDFGHJKL:"~', 30), ("\\zxcvbnm,./", "|ZXCVBNM<>?", 43)]
ASCII = {" ": (57, False), "\n": (28, False), "\t": (15, False)}
for plain, shifted, first in ROWS:
    for i, (a, b) in enumerate(zip(plain, shifted)):
        ASCII[a], ASCII[b] = (first + i, False), (first + i, True)

# libei's event types and device capabilities (libei.h, libei 1.4).
EV_CONNECT, EV_DISCONNECT, EV_SEAT_ADDED, EV_DEVICE_ADDED, EV_DEVICE_REMOVED = 1, 2, 3, 5, 6
EV_DEVICE_PAUSED, EV_DEVICE_RESUMED = 7, 8
CAP_POINTER, CAP_ABSOLUTE, CAP_KEYBOARD, CAP_SCROLL, CAP_BUTTON = 1, 2, 4, 16, 32


def say(state, **more):
    print(json.dumps({"state": state, **more}), flush=True)


# ---- which panel has focus -----------------------------------------------------

PROPS = ("GAMESCOPE_FOCUSABLE_WINDOWS", "GAMESCOPE_FOCUSED_WINDOW", "GAMESCOPE_FOCUS_DISPLAY")
TOOL_TIMEOUT = 5.0  # seconds for any one xprop or xwininfo
PANELS_DEADLINE = 12.0  # seconds for a whole `panels` run: well inside the server's 20 s for /api/panels
DEADLINE = [None]  # when the current `panels` run must stop asking X tools (monotonic), if it has one


class OutOfTime(Exception):
    """The current run's deadline passed: stop and report what was found."""


def budget(limit=TOOL_TIMEOUT):
    """Seconds an X tool may take now: its own limit, cut to what's left of the run's deadline."""
    if DEADLINE[0] is None:
        return limit
    left = DEADLINE[0] - time.monotonic()
    if left <= 0:
        raise OutOfTime()
    return min(limit, left)


def read_props(display, limit=TOOL_TIMEOUT):
    """gamescope's root properties on one display, in one xprop call: {name: [values]} for
    those the display has, {} if none (or xprop didn't answer)."""
    try:
        out = subprocess.run(["xprop", "-root", *PROPS], env=dict(os.environ, DISPLAY=display),
                             capture_output=True, text=True, timeout=budget(limit)).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    props = {}
    for line in out.splitlines():
        name, sep, values = line.partition("=")
        name = name.split("(", 1)[0].strip()
        if sep and name in PROPS:
            props[name] = [int(v) for v in values.replace(",", " ").split() if v.isdigit()]
    return props


def window_info(display, window):
    """Name and geometry of a window on one X display, or None if it isn't there."""
    try:
        which = ["-root"] if window == "root" else ["-id", str(window)]
        out = subprocess.run(["xwininfo", *which], env=dict(os.environ, DISPLAY=display),
                             capture_output=True, text=True, timeout=budget()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "IsViewable" not in out:
        return None
    info = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("xwininfo: Window id:"):
            info["name"] = line.split('"', 1)[1].rsplit('"', 1)[0] if '"' in line else ""
        for key, field in (("Absolute upper-left X:", "x"), ("Absolute upper-left Y:", "y"),
                           ("Width:", "width"), ("Height:", "height")):
            if line.startswith(key):
                info[field] = int(line.split(":", 1)[1])
    return info if "width" in info else None


def displays():
    """Every X display on the Frame: the sockets in /tmp/.X11-unix, and the abstract ones
    X servers on Linux usually listen on as well (in /proc/net/unix), in case a SteamOS
    build stops sharing /tmp/.X11-unix with SSH sessions."""
    found = set()
    try:
        found.update(int(n[1:]) for n in os.listdir("/tmp/.X11-unix") if n[:1] == "X" and n[1:].isdigit())
    except OSError:
        pass
    try:
        with open("/proc/net/unix") as f:
            found.update(int(n) for n in re.findall(r"@/tmp/\.X11-unix/X(\d+)", f.read()))
    except OSError:
        pass
    return [f":{n}" for n in sorted(found)]


# The input helper (`frame_touch.py` reading events) lives for a whole Control session and
# reads focus often, so it remembers which display has gamescope's properties: found, for
# good; not found, for ROOT_RETRY. A `panels` run is a fresh process each time, so this
# saves it nothing; it costs nothing either while :0 has the properties.
_ROOT = []  # [display, when it was given up on, or None if found]
ROOT_PROBE_TIMEOUT = 2.0  # seconds for each display other than :0, so a stalled one can't use the run
ROOT_RETRY = 30.0


def gamescope():
    """(display, properties): the X display whose root window carries gamescope's panel
    properties (GAMESCOPE_FOCUSABLE_WINDOWS and friends), and what they say now.

    That's :0 on every build seen so far (SteamOS 0.3.0 to 0.4.1), so :0 is read first and,
    when it has them, used with no other probe. Only if it hasn't are the other displays
    asked, briefly each; with none, :0 and no properties.
    """
    if _ROOT and (_ROOT[1] is None or time.monotonic() - _ROOT[1] < ROOT_RETRY):
        return _ROOT[0], read_props(_ROOT[0])
    props, found = read_props(":0"), ":0"
    if not props:
        found = None
        for display in displays():
            if display != ":0":
                props = read_props(display, ROOT_PROBE_TIMEOUT)
                if props:
                    found = display
                    break
    _ROOT[:] = [found, None] if found else [":0", time.monotonic()]
    return _ROOT[0], props


def window_pid(display, window):
    try:
        out = subprocess.run(["xprop", "-id", str(window), "_NET_WM_PID"], env=dict(os.environ, DISPLAY=display),
                             capture_output=True, text=True, timeout=budget()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.rsplit("=", 1)[-1].strip() if "=" in out else ""
    return int(value) if value.isdigit() else None


def locate(window, pid):
    """The display a focusable window is on, with its name and geometry.

    Window ids are per X server, so :0 and :1 can both have one; the pid gamescope
    lists with it (GAMESCOPE_FOCUSABLE_WINDOWS) tells them apart.
    """
    found = []
    for display in displays():
        info = window_info(display, window)
        if info:
            found.append((display, info))
    if len(found) > 1 and pid:
        found = [f for f in found if window_pid(f[0], window) == pid] or found
    if not found:
        return None
    display, info = found[0]
    root = window_info(display, "root") or {}
    return {"window": window, "display": display, **info,
            "root": [root.get("width", info["width"]), root.get("height", info["height"])]}


def focusable(props=None):
    """gamescope's focusable windows as (window, app id, pid), from props if already read."""
    t = (gamescope()[1] if props is None else props).get("GAMESCOPE_FOCUSABLE_WINDOWS", [])
    return [tuple(t[i:i + 3]) for i in range(0, len(t) - 2, 3)]


def focus_display(props=None):
    """The display of the focused window, from GAMESCOPE_FOCUS_DISPLAY on gamescope's root.

    gamescope writes the name (":1") as 32-bit items, so its first four bytes land,
    little-endian, in the first value: 12602 is 0x313A, ":1" (steamcompmgr.cpp;
    seen 2026-09-29).
    """
    values = (gamescope()[1] if props is None else props).get("GAMESCOPE_FOCUS_DISPLAY")
    if not values:
        return None
    name = (values[0] & 0xFFFFFFFF).to_bytes(4, "little").split(b"\0", 1)[0].decode("ascii", "replace")
    return name if name[:1] == ":" and name[1:].isdigit() else None


def focus_now():
    """Just which window and display have focus: one property read, for checking a press."""
    props = gamescope()[1]
    window = (props.get("GAMESCOPE_FOCUSED_WINDOW") or [0])[0]
    return (window or None, focus_display(props) if window else None)


def focus(props=None):
    """The panel that has focus in the headset: window, display, name and sizes (gamescope
    publishes the window and its display on its root display, :0 so far). props: gamescope's
    properties if already read."""
    if props is None:
        props = gamescope()[1]
    window = (props.get("GAMESCOPE_FOCUSED_WINDOW") or [0])[0]
    if not window:
        return {"window": None}
    app, pid = next(((a, p) for w, a, p in focusable(props) if w == window), (None, None))
    display = focus_display(props)
    info = window_info(display, window) if display else None
    if info:
        root = window_info(display, "root") or {}
        panel = {"window": window, "display": display, **info,
                 "root": [root.get("width", info["width"]), root.get("height", info["height"])]}
    else:
        panel = locate(window, pid)  # no display published: tell them apart by pid
    return {**panel, "app": app} if panel else {"window": None}


def panels(limit=None):
    """Every app panel (gamescope's focusable windows), for watching one that hasn't focus.

    One deadline (PANELS_DEADLINE) covers the whole run, inside the server's timeout: past
    it, the panels found so far come back with a "why" saying the headset was too slow.
    """
    DEADLINE[0] = time.monotonic() + (PANELS_DEADLINE if limit is None else limit)
    now, found, unfound, late = {}, [], 0, False
    try:
        props = gamescope()[1]
        now = focus(props)
        for window, app, pid in focusable(props):
            panel = locate(window, pid)
            unfound += panel is None
            if panel and panel["width"] > 1 and panel["height"] > 1 and \
                    not any(f["window"] == window and f["display"] == panel["display"] for f in found):
                panel.pop("root", None)
                found.append({**panel, "app": app, "focused": (window, panel["display"]) ==
                              (now.get("window"), now.get("display"))})
        late = time.monotonic() >= DEADLINE[0]  # the last read may have been cut short
    except OutOfTime:
        late = True
    finally:
        DEADLINE[0] = None
    out = {"focus": now.get("window"), "focus_display": now.get("display"), "panels": found}
    if late:
        out["why"] = ("The headset took too long to list its panels (its X tools didn't answer in time). "
                      + ("Some may be missing. " if found else "") + "Try again, or use the Headset view.")
    elif not found:
        out["why"] = why_none(unfound)
    return out


# The headset's Linux desktop, by process name (docs/how-the-frame-works.md).
DESKTOP = ("plasmashell", "kwin_wayland", "kwin_wayland_wrapper", "steamos-nested-desktop", "startplasma-wayland")


def desktop_running():
    """Whether the headset's Linux desktop (the nested Plasma session) is running."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() in DESKTOP:
                    return True
        except OSError:
            continue
    return False


def why_none(listed):
    """Why panels() found nothing, in words the person at the computer can act on.
    listed: how many of the windows gamescope lists aren't X11 windows on any display."""
    missing = [tool for tool in ("xprop", "xwininfo") if not shutil.which(tool)]
    if missing:
        return (f"This SteamOS build has no {' or '.join(missing)}, which Frame Control uses to find the "
                "headset's panels. Use the Headset view instead, and please report your SteamOS version.")
    if listed:
        # gamescope lists windows, but none is an X11 window on any display: a Wayland
        # window (a desktop drawn straight to gamescope, say) can't be found or captured this way.
        return (f"The headset has {listed} panel{'s' if listed > 1 else ''} open, but Frame Control can't "
                f"find {'its' if listed == 1 else 'their'} window. This SteamOS build may draw "
                f"{'it' if listed == 1 else 'them'} in a way the Desktop view can't capture yet. "
                "Use the Headset view instead, and please report your SteamOS version.")
    if desktop_running():
        return ("The headset's desktop is running, but SteamOS isn't listing it as a panel. Look at it in "
                "the headset (or close and reopen it), then try again, or use the Headset view.")
    return "No app panel is open in the headset. Open the desktop or an app in the headset, then try again."


def to_root(panel, fx, fy):
    """A point given as a fraction of the panel, in the root coordinates gamescope's pointer uses.

    gamescope fits each panel's window to its display, so a 1920x1080 window on a
    1280x720 display takes pointer positions at two thirds scale (verified 2026-09-29).
    """
    rw, rh = panel["root"]
    w, h = panel["width"], panel["height"]
    s = min(rw / w, rh / h)
    ox, oy = (rw - w * s) / 2, (rh - h * s) / 2
    fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
    return ox + fx * (w * s - 1), oy + fy * (h * s - 1)


# ---- gamescope's input socket ----------------------------------------------------

def libei():
    L = ctypes.CDLL("libei.so.1")
    vp, c = ctypes.c_void_p, ctypes
    sig = {
        "ei_new_sender": (vp, [vp]), "ei_configure_name": (None, [vp, c.c_char_p]),
        "ei_setup_backend_socket": (c.c_int, [vp, c.c_char_p]), "ei_get_fd": (c.c_int, [vp]),
        "ei_dispatch": (None, [vp]), "ei_get_event": (vp, [vp]), "ei_event_get_type": (c.c_int, [vp]),
        "ei_event_unref": (vp, [vp]), "ei_event_get_seat": (vp, [vp]), "ei_event_get_device": (vp, [vp]),
        "ei_device_has_capability": (c.c_bool, [vp, c.c_int]), "ei_now": (c.c_uint64, [vp]),
        "ei_device_start_emulating": (None, [vp, c.c_uint32]), "ei_device_stop_emulating": (None, [vp]),
        "ei_device_frame": (None, [vp, c.c_uint64]),
        "ei_device_pointer_motion": (None, [vp, c.c_double, c.c_double]),
        "ei_device_pointer_motion_absolute": (None, [vp, c.c_double, c.c_double]),
        "ei_device_button_button": (None, [vp, c.c_uint32, c.c_bool]),
        "ei_device_scroll_delta": (None, [vp, c.c_double, c.c_double]),
        "ei_device_keyboard_key": (None, [vp, c.c_uint32, c.c_bool]),
        "ei_unref": (vp, [vp]),
    }
    for name, (res, args) in sig.items():
        f = getattr(L, name)
        f.restype, f.argtypes = res, args
    return L


class Gamescope:
    """One connection to gamescope's EIS socket and its virtual input device."""

    def __init__(self):
        self.L = L = libei()
        self.ei = L.ei_new_sender(None)
        L.ei_configure_name(self.ei, b"Frame Control")
        if L.ei_setup_backend_socket(self.ei, SOCKET.format(uid=os.getuid()).encode()) != 0:
            raise RuntimeError("Couldn't reach gamescope's input socket. Is the headset on?")
        self.fd = L.ei_get_fd(self.ei)
        self.device, self.sequence, self.held, self.keys, self.alive = None, 0, set(), set(), True

    def pump(self, wait=0.0):
        """Handle gamescope's events; False once it has disconnected."""
        select.select([self.fd], [], [], wait)
        self.L.ei_dispatch(self.ei)
        alive = True
        while True:
            ev = self.L.ei_get_event(self.ei)
            if not ev:
                return alive
            kind = self.L.ei_event_get_type(ev)
            if kind == EV_SEAT_ADDED:
                seat = self.L.ei_event_get_seat(ev)
                # Variadic, ending in 0 (NULL): ask for everything we send.
                self.L.ei_seat_bind_capabilities(ctypes.c_void_p(seat), *map(ctypes.c_int, (
                    CAP_POINTER, CAP_ABSOLUTE, CAP_BUTTON, CAP_SCROLL, CAP_KEYBOARD, 0)))
            elif kind == EV_DEVICE_RESUMED:
                device = self.L.ei_event_get_device(ev)
                if self.L.ei_device_has_capability(device, CAP_ABSOLUTE):
                    self.sequence += 1
                    self.L.ei_device_start_emulating(device, self.sequence)
                    self.device = device
                    # Releases that arrived while it was paused were dropped: let go of
                    # everything now, so the headset and this agent agree nothing is held.
                    if self.held or self.keys:
                        self.release_all()
            elif kind in (EV_DEVICE_PAUSED, EV_DEVICE_REMOVED):
                if self.L.ei_event_get_device(ev) == self.device:
                    self.device = None
            elif kind == EV_DISCONNECT:
                self.device, alive, self.alive = None, False, False
            self.L.ei_event_unref(ev)

    def wait_ready(self, timeout=5):
        end = time.time() + timeout
        while self.device is None and time.time() < end:
            if not self.pump(0.1):
                break
        if self.device is None:
            raise RuntimeError("gamescope closed its input socket" if not self.alive
                               else "gamescope didn't offer an input device")

    def frame(self):
        self.L.ei_device_frame(self.device, self.L.ei_now(self.ei))
        self.L.ei_dispatch(self.ei)

    def move_to(self, x, y):
        self.L.ei_device_pointer_motion_absolute(self.device, x, y)
        self.frame()

    def move_by(self, dx, dy):
        self.L.ei_device_pointer_motion(self.device, dx, dy)
        self.frame()

    def button(self, name, down):
        code = BUTTONS[name]
        if down == (code in self.held):
            return  # already in that state
        self.L.ei_device_button_button(self.device, code, down)
        self.frame()
        (self.held.add if down else self.held.discard)(code)

    def scroll(self, dx, dy):
        self.L.ei_device_scroll_delta(self.device, dx, dy)
        self.frame()

    def key(self, code, down):
        self.L.ei_device_keyboard_key(self.device, code, down)
        self.frame()
        (self.keys.add if down else self.keys.discard)(code)
        # Paced: a burst of keys can reach the app out of order (seen 2026-09-29).
        time.sleep(0.008)

    def text(self, text):
        for ch in text:
            if ch not in ASCII:
                continue
            code, shifted = ASCII[ch]
            if shifted:
                self.key(SHIFT, True)
            self.key(code, True)
            self.key(code, False)
            if shifted:
                self.key(SHIFT, False)

    def release_all(self):
        """Let go of every button and key still down, so nothing stays held in the headset."""
        for code in list(self.held):
            name = next(n for n, c in BUTTONS.items() if c == code)
            self.button(name, False)
        for code in list(self.keys):
            self.key(code, False)


# ---- events from the server --------------------------------------------------------

def events(line):
    try:
        data = json.loads(line)
    except ValueError:
        return []
    return [e for e in (data if isinstance(data, list) else [data]) if isinstance(e, dict)]


def number(value, limit=100000.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError("not a number")
    return max(-limit, min(limit, float(value)))


STALE = [False]  # whether the last status said a tap went nowhere


def aimed_elsewhere(event, panel):
    """Whether an event names a panel that isn't the one with focus now."""
    if "window" not in event:
        return False
    return (panel.get("window"), panel.get("display")) != (event.get("window"), event.get("display"))


def apply(gs, event, panel):
    """Send one event; returns the focused panel it checked against (looked up at most once a second).

    Positions and presses name the panel they were meant for. If focus has moved to
    another panel since, they go nowhere, so a tap can't land on the wrong one;
    releases always go, so nothing stays held.
    """
    # Moves may use a focus reading up to a second old; anything that acts (a press, key,
    # text or scroll) reads it afresh, so it can't land on a panel that took focus since.
    acts = any(k in event for k in ("button", "key", "text", "scroll")) and event.get("down") is not False
    if "window" in event:
        if panel and acts and focus_now() == (panel.get("window"), panel.get("display")):
            pass  # still the same panel (its geometry is re-read on the usual one-second schedule)
        elif acts or not panel or time.time() - panel.get("_at", 0) > 1 or aimed_elsewhere(event, panel):
            panel = {**focus(), "_at": time.time()}
    stale = aimed_elsewhere(event, panel) if "window" in event else False
    if stale and not (event.get("down") is False and ("button" in event or "key" in event)):
        say("ready", focus=panel.get("window"), display=panel.get("display"), stale=True)  # the page re-syncs
        STALE[0] = True
        return panel
    if STALE[0] and not stale:
        # Anything that goes through (a trackpad move names no panel) means caught up: stop re-syncing.
        STALE[0] = False
        say("ready", focus=(panel or {}).get("window"), display=(panel or {}).get("display"))
    if "fx" in event and panel and panel.get("window"):
        gs.move_to(*to_root(panel, number(event["fx"], 1), number(event["fy"], 1)))
    if "dx" in event or "dy" in event:
        gs.move_by(number(event.get("dx", 0), 2000), number(event.get("dy", 0), 2000))
    if event.get("button") in BUTTONS:
        gs.button(event["button"], event.get("down") is not False)
    if isinstance(event.get("scroll"), list) and len(event["scroll"]) == 2:
        gs.scroll(number(event["scroll"][0], 5000), number(event["scroll"][1], 5000))
    if isinstance(event.get("key"), int) and not isinstance(event["key"], bool) and 0 < event["key"] < 768:
        gs.key(event["key"], event.get("down") is not False)
    if isinstance(event.get("text"), str):
        gs.text(event["text"][:500])
    return panel


def main():
    if sys.argv[1:] == ["focus"]:
        print(json.dumps(focus()))
        return 0
    if sys.argv[1:] == ["panels"]:
        print(json.dumps(panels()))
        return 0
    try:
        gs = Gamescope()
        gs.wait_ready()
    except (OSError, RuntimeError) as e:
        say("error", message=str(e))
        return 1
    say("ready", focus=focus().get("window"))
    stdin, pending, panel = sys.stdin.fileno(), b"", None
    try:
        while True:
            ready, _, _ = select.select([stdin, gs.fd], [], [], 30)
            if gs.fd in ready and not gs.pump():
                say("error", message="gamescope closed its input socket")
                return 1
            if stdin not in ready:
                continue
            chunk = os.read(stdin, 65536)
            if not chunk:
                return 0  # the server went away
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                waited = False
                for event in events(line):
                    if gs.device is None and waited:
                        continue  # still paused: don't wait again for each event of this batch
                    if gs.device is None:
                        waited = True
                        # Paused (gamescope can pause the device): wait a moment; drop this
                        # event if it doesn't come back. Only a disconnect ends the session.
                        try:
                            gs.wait_ready(2)
                        except RuntimeError:
                            if not gs.alive:
                                raise
                            continue
                    try:
                        panel = apply(gs, event, panel)
                    except (ValueError, KeyError, TypeError, OSError):
                        continue  # the server checks events; skip anything odd
    except RuntimeError as e:
        say("error", message=str(e))
        return 1
    finally:
        if gs.device is not None:
            gs.release_all()  # never leave a button held down in the headset


if __name__ == "__main__":
    sys.exit(main())
