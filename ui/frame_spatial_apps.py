"""FW-15 app/switcher data. No Mac page UI and no device work until sync()."""
import base64
import json
from pathlib import Path
import re
import shlex
import struct
import threading
import time

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "workspace-spatial-on-frame.py"
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
KEY = re.compile(r"com\.frame-workspace\.native\.\d{1,15}\.panel-w\d{1,15}\Z")
SOURCE = re.compile(r"(?:window|separate|spatial):(\d{1,15})\Z")
VERBS = {"open", "hide", "unhide", "quit", "show_windows"}
VERB_NAMES = {"open": "Open", "hide": "Hide", "unhide": "Show", "quit": "Quit", "show_windows": "Show"}


def clean(value, limit):
    return re.sub(r"[\x00-\x1f]", " ", str(value or ""))[:limit]


class SpatialApps:
    def __init__(self, macview):
        self.macview = macview
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.helper_lock = threading.RLock()
        self.state = None
        self.error = None
        self.thread = None
        self._agent = None
        self._configured = False
        self._entries = []
        self._window_apps = {}
        self._icons = {}
        self._sent_icons = set()
        self._published = False

    @property
    def enabled(self):
        workspace = getattr(self.macview, "spatial_workspace", {})
        return (getattr(self.macview, "mode", "desktop") == "spatial" and
                workspace.get("state") == "running" and not self.macview.closing and
                not getattr(self.macview, "exit_pending", False))

    def start(self, connected=lambda: True):
        if self.thread and self.thread.is_alive():
            return

        def monitor():
            next_refresh = 0
            previous_mode = None
            while not self.macview.closing:
                active = self.enabled and connected()
                try:
                    if active != previous_mode or time.monotonic() >= next_refresh:
                        self.refresh(active)
                        next_refresh = time.monotonic() + 1
                        previous_mode = active
                    elif active:
                        self.poll_switcher()
                except Exception as exc:  # helper restarts/permission failures retry on the next tick
                    self.error = str(exc)
                    next_refresh = time.monotonic() + 1
                    previous_mode = active
                time.sleep(.1)
            # The helper also expires the spatial lease if the companion crashes.
            try:
                self.refresh(False)
            except Exception:
                pass

        self.thread = threading.Thread(target=monitor, daemon=True)
        self.thread.start()

    def _set_state(self, state):
        with self.lock:
            if state != self.state:
                self.state = state
                self.wake.set()

    def refresh(self, active=None):
        with self.helper_lock:
            self._refresh(active)

    def _refresh(self, active=None):
        active = self.enabled if active is None else active
        if not active:
            # Never start a helper just to turn off a tap it never installed.
            if self._configured and self.macview.agent is self._agent:
                if self._agent is not None and self._agent.poll() is None:
                    self.macview.call("/switcher", method="POST", mode="desktop")
            self._configured = False
            self._set_state(None)
            return
        apps = self.macview.call("/apps").get("apps", [])
        windows = self.macview.call("/windows", all="1").get("windows", [])
        panels = [(s, dict(p)) for s, p in list(self.macview.native_panels.items()) if SOURCE.fullmatch(s)]
        live_sources = {SOURCE.fullmatch(s).group(1) for s, _ in panels}
        self._window_apps = {s: b for s, b in self._window_apps.items() if s in live_sources}
        for window in windows:
            match = SOURCE.fullmatch(str(window.get("src", "")))
            bundle = window.get("bundleID")
            if match and isinstance(bundle, str) and BUNDLE.fullmatch(bundle):
                self._window_apps[match.group(1)] = bundle
        by_app = {}
        for source, panel in sorted(panels, key=lambda p: p[1].get("slot", 0)):
            bundle = panel.get("bundleID") or self._window_apps.get(SOURCE.fullmatch(source).group(1))
            key = panel.get("key")
            if isinstance(bundle, str) and BUNDLE.fullmatch(bundle) and isinstance(key, str) and KEY.fullmatch(key):
                by_app.setdefault(bundle, []).append({"key": key, "title": clean(panel.get("title"), 512)})
        result, seen = [], set()
        for app in apps:
            if not isinstance(app, dict):
                continue
            bundle = app.get("bundleID")
            if not isinstance(bundle, str) or not BUNDLE.fullmatch(bundle) or bundle in seen:
                continue
            seen.add(bundle)
            result.append({"bundleID": bundle, "name": clean(app.get("name"), 256), "icon": bundle + ".png",
                           **{k: app.get(k) is True for k in ("running", "pinned", "hidden", "active")},
                           "windows": by_app.get(bundle, [])})
        # Bounds match the Frame-side writer; never publish an incomplete switcher order.
        if len(result) > 128 or sum(len(a["windows"]) for a in result) > 256:
            raise ValueError("Spatial app/window limit exceeded")
        entries = [{"key": w["key"], "bundleID": a["bundleID"]} for a in result for w in a["windows"]]
        switcher = self.macview.call("/switcher", method="POST", mode="spatial",
                                    entries=json.dumps(entries, separators=(",", ":")))
        self._agent = self.macview.agent
        self._configured = True
        self._entries = entries
        self._set_state({"mode": "spatial", "apps": result, "switcher": self._switcher(switcher, entries)})

    @staticmethod
    def _switcher(raw, entries):
        if not isinstance(raw, dict):
            raise ValueError("Invalid helper switcher response")
        keys = [e["key"] for e in entries]
        order = raw.get("windows", keys)
        if not isinstance(order, list) or any(not isinstance(k, str) for k in order):
            order = keys
        order = list(dict.fromkeys(k for k in order if k in keys))
        order += [k for k in keys if k not in order]
        index, seq = raw.get("index", 0), raw.get("seq", 0)
        state = {"open": raw.get("open") is True and bool(order),
                 "index": index if type(index) is int and 0 <= index < max(1, len(order)) else 0,
                 "seq": seq if type(seq) is int and 0 <= seq < 2**63 else 0,
                 "chosen": raw.get("chosen") if raw.get("chosen") in keys else None,
                 "windows": order}
        if type(raw.get("tapActive")) is bool:
            state["tapActive"] = raw["tapActive"]
        if raw.get("error"):
            state["error"] = clean(raw["error"], 512)
        return state

    def poll_switcher(self):
        with self.helper_lock:
            if not self.enabled or not self._configured or self.macview.agent is not self._agent:
                return
            switcher = self._switcher(self.macview.call("/switcher"), self._entries)
            with self.lock:
                state = {**self.state, "switcher": switcher} if self.state else None
            self._set_state(state)

    def _run(self, *args):
        return self.macview.run("python3 - " + " ".join(shlex.quote(a) for a in args),
                                stdin=SCRIPT.read_text(encoding="utf-8"), timeout=8)

    def sync(self):
        """Called by the bar's one-second sync and when the monitor wakes it."""
        with self.lock:
            state = self.state
        if not self.enabled or state is None:
            if self._published:
                self._run("clear")
                self._published = False
            return
        raw = json.dumps(state, ensure_ascii=True, separators=(",", ":"))
        if len(raw.encode()) > 65536:
            raise ValueError("Spatial state is too large")
        # A dropped SSH reply can follow a successful write; still clear it on mode exit.
        self._published = True
        out = self._run("sync", raw)
        # Stop may be requested while the helper call is in flight. Clear its
        # just-published chrome before returning so a stale reply cannot make
        # the apps panel visible again.
        if not self.enabled and self._published:
            self._run("clear")
            self._published = False
        for line in out.splitlines():
            if line.startswith("SPATIAL_ICONS_MISSING "):
                missing = json.loads(line.split(" ", 1)[1])
                if isinstance(missing, list):
                    self._sent_icons.difference_update(b for b in missing if isinstance(b, str))
        # Limit initial icon work so large Docks do not delay subsequent switcher updates.
        pending = [a["bundleID"] for a in state["apps"] if a["bundleID"] not in self._sent_icons]
        for bundle in pending[:4]:
            try:
                if bundle not in self._icons:
                    encoded = self.macview.call("/apps/icon", bundleID=bundle)["png"]
                    if not isinstance(encoded, str) or len(encoded) > 100000:
                        raise ValueError("Invalid app icon")
                    data = base64.b64decode(encoded, validate=True)
                    if (len(data) < 33 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or
                            struct.unpack(">II", data[16:24]) != (128, 128)):
                        raise ValueError("Expected a 128x128 PNG")
                    self._icons[bundle] = encoded
                self._run("icon", bundle, self._icons[bundle])
                self._sent_icons.add(bundle)
            except Exception as exc:
                self.error = str(exc)  # retry failed icons, without blocking the state

    def handle_request(self, request):
        action = request.get("action")
        if action not in ("app", "switch", "switcher"):
            return False
        if not self.enabled:
            return True
        if action == "app":
            bundle, verb = request.get("bundleID"), request.get("verb")
            with self.lock:
                known = {a["bundleID"] for a in self.state["apps"]} if self.state else set()
            if isinstance(bundle, str) and bundle in known and isinstance(verb, str) and verb in VERBS:
                self._lane(self._app_action, bundle, verb)
        elif action == "switch":
            key = request.get("key")
            if isinstance(key, str) and KEY.fullmatch(key) and self._source(key):
                self._lane(self._switch, key)
        elif request.get("verb") in ("open", "close"):
            self._lane(self._switcher_action, request["verb"])
        return True

    def _lane(self, fn, *args):
        """Headset clicks run on the companion's action lane, never behind
        Spatial startup or a panel launch on the Workspace queue."""
        run = getattr(self.macview, "_action_run", None)
        if run:
            run(fn, *args)
        else:
            self.macview._dash_run(None, fn, *args)

    def _app_name(self, bundle):
        with self.lock:
            apps = self.state["apps"] if self.state else []
        return next((a["name"] for a in apps if a["bundleID"] == bundle and a.get("name")), bundle)

    def _app_windows(self, bundle):
        """This app's panel keys, most recently used first (the switcher's order)."""
        with self.lock:
            state = self.state
        if not state:
            return []
        keys = [w["key"] for a in state["apps"] if a["bundleID"] == bundle for w in a["windows"]]
        order = state.get("switcher", {}).get("windows", [])
        return [k for k in order if k in keys] + [k for k in keys if k not in order]

    def _source(self, key):
        return next((s for s, p in list(self.macview.native_panels.items())
                     if p.get("key") == key and SOURCE.fullmatch(s)), None)

    def _app_action(self, bundle, verb):
        if not self.enabled:
            return
        name = self._app_name(bundle)
        try:
            result = self.macview.call("/apps/action", method="POST", bundleID=bundle, verb=verb)
            if isinstance(result, dict) and result.get("ok") is False:
                raise ValueError(result.get("error") or "macOS refused")
        except Exception as exc:
            reason = str(exc).removeprefix("The Mac Streaming Helper: ")
            raise RuntimeError(f"Couldn't {VERB_NAMES[verb]} {clean(name, 64)}: {clean(reason, 120)}") from exc
        # Opening an app that already has a panel also brings that panel forward.
        if verb in ("open", "show_windows"):
            keys = self._app_windows(bundle)
            if keys and self.enabled:
                self.macview.call("/switcher", method="POST", mode="spatial", verb="choose", key=keys[0])
        self.refresh()

    def _switch(self, key):
        source = self._source(key)
        if self.enabled and source:
            self.macview.call("/focus", method="POST", src=source, u=.5, v=.5)
            with self.macview._spatial_lock:
                self.macview._spatial_focused = source
            self.macview.call("/switcher", method="POST", mode="spatial", verb="close")
            self.refresh()

    def _switcher_action(self, verb):
        if self.enabled:
            self.macview.call("/switcher", method="POST", mode="spatial", verb=verb)
            self.poll_switcher()
