"""FW-15 app/switcher and launcher data. No Mac page UI and no device work until sync()."""
import base64
import json
from pathlib import Path
import re
import shlex
import struct
import sys
import threading
import time

import frame_spatial_launcher as launcher

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "workspace-spatial-on-frame.py"
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
KEY = re.compile(r"com\.frame-workspace\.native\.\d{1,15}\.panel-w\d{1,15}\Z")
SOURCE = re.compile(r"(?:window|separate|spatial):(\d{1,15})\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,48}\Z")
VERBS = {"open", "hide", "unhide", "quit", "show_windows"}
VERB_NAMES = {"open": "Open", "hide": "Hide", "unhide": "Show", "quit": "Quit", "show_windows": "Show"}
CATALOG_REFRESH = 60      # seconds between launcher catalogue reads while Spatial Mode runs
ICON_BATCH = 90000        # base64 bytes of icons per Frame write (one SSH command line)
ACK_KEEP = 8              # recent request answers the headset is told about
ACK_SECONDS = 60          # ... and for how long


sink = None  # tests collect log lines here; otherwise they go to stderr


def log(message):
    """One line for a hop of a headset click, in the app's server.log (grep "dock:")."""
    line = f"{time.strftime('%H:%M:%S')} dock: {message}"
    if sink is not None:
        sink(line)
        return
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass  # no stderr (a closed pipe)


def default_layout_path():
    import frame_host  # the Mac app's data folder; imported late so tests can redirect it
    return frame_host.data_dir("spatial-launcher-layout.json")


def clean(value, limit):
    return re.sub(r"[\x00-\x1f]", " ", str(value or ""))[:limit]


class SpatialApps:
    def __init__(self, macview, layout_path=None):
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
        # Counts dashboard Recenter requests made in Spatial Mode; the headset
        # brings the shelf back in front of the user when it grows.
        self.recenter_seq = 0
        # Counts Arrange Windows requests (the bar panel and the Mac page); the
        # headset re-tiles every window panel in front of the user when it grows.
        self.arrange_seq = 0
        # Launcher A: the catalogue (helper's apps and starting layout, with the
        # user's saved arrangement), the open/close sequence, the search field
        # fed by the Mac keyboard lease, and the launcher icons the Frame lacks.
        self.layout_path = Path(layout_path) if layout_path else None
        self.catalog = None
        # Catalogue numbers start from the clock, so a restarted companion never
        # mistakes the Frame's older launcher.json for its own.
        self.catalog_seq = int(time.time() * 1000)
        self._catalog_at = None
        self._catalog_key = None
        self.launcher_open = False
        self.launcher_seq = 0
        self.search = launcher.SearchField()
        self.results = []
        self._lease = False
        self._keys_after = 0
        self._published_catalog = None
        self._launcher_icons = []
        # What became of recent headset requests (by the id the headset gave them),
        # published in the state so the shelf's icon can stop hopping.
        self._acks = []

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
            # Desktop mode also ends the launcher's keyboard lease in the helper.
            with self.lock:
                self._close_launcher_locked()
            if self._configured and self.macview.agent is self._agent:
                if self._agent is not None and self._agent.poll() is None:
                    try:
                        if self._lease:
                            self.macview.call("/launcher/keys", method="POST", verb="close")
                    finally:
                        self._lease = False
                        self.macview.call("/switcher", method="POST", mode="desktop")
            self._lease = False
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
        self._refresh_catalog()
        switcher = self.macview.call("/switcher", method="POST", mode="spatial",
                                    entries=json.dumps(entries, separators=(",", ":")))
        if self.macview.agent is not self._agent:
            self._lease = False  # a restarted helper has no lease
        self._agent = self.macview.agent
        self._configured = True
        self._entries = entries
        if self.launcher_open and not self._lease:
            self._take_keyboard()
        state = {"mode": "spatial", "apps": result, "switcher": self._switcher(switcher, entries)}
        with self.lock:
            if self.recenter_seq:
                state["recenterSeq"] = self.recenter_seq
            if self.arrange_seq:
                state["arrangeSeq"] = self.arrange_seq
            if self.catalog is not None:
                state["launcher"] = self._launcher_locked()
            acks = self._acks_locked()
            if acks:
                state["acks"] = acks
        self._set_state(state)

    def _acks_locked(self):
        cutoff = time.monotonic() - ACK_SECONDS
        self._acks = [a for a in self._acks if a[0] >= cutoff][-ACK_KEEP:]
        return [a[1] for a in self._acks]

    def _ack(self, rid, ok, error=None):
        """Tell the headset how its request `rid` ended; `error` is its notice text."""
        if not rid:
            return
        ack = {"id": rid, "ok": bool(ok)}
        if error:
            ack["error"] = clean(error, 160)
        with self.lock:
            self._acks = [a for a in self._acks if a[1]["id"] != rid] + [(time.monotonic(), ack)]
            acks = self._acks_locked()
            state = {**self.state, "acks": acks} if self.state else None
        if state is not None:
            self._set_state(state)

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
            response = self.macview.call("/switcher")
            self.macview.observe_mouse_hover(response.get("mouseHoveredSrc"))
            switcher = self._switcher(response, self._entries)
            with self.lock:
                state = {**self.state, "switcher": switcher} if self.state else None
            self._set_state(state)
        self.poll_launcher()

    # Launcher A ------------------------------------------------------------

    def _layout_path(self):
        if self.layout_path is None:
            self.layout_path = Path(default_layout_path())
        return self.layout_path

    def _refresh_catalog(self, force=False):
        """Reads GET /launcher every CATALOG_REFRESH seconds (or when forced) and
        merges the saved arrangement. An older helper without it leaves the
        launcher unavailable rather than failing the shelf."""
        now = time.monotonic()
        if not force and self._catalog_at is not None and now - self._catalog_at < CATALOG_REFRESH:
            return
        self._catalog_at = now
        try:
            raw = launcher.sanitize_catalog(self.macview.call("/launcher"))
        except Exception as exc:
            self.error = str(exc)
            return
        if not raw["apps"]:
            return  # nothing to launch: the shelf shows no Applications button
        known = {a["bundleID"] for a in raw["apps"]}
        items = launcher.merge_layout(raw["items"], launcher.load_layout(self._layout_path()), known)
        with self.lock:
            self._set_catalog_locked({**raw, "items": items})

    def _set_catalog_locked(self, catalog):
        key = json.dumps([catalog["apps"], catalog["items"], catalog["recent"]], sort_keys=True)
        self.catalog = catalog
        if key != self._catalog_key:
            self._catalog_key = key
            self.catalog_seq += 1
            self._search_locked()

    def _search_locked(self):
        apps = [(a["bundleID"], a["name"]) for a in self.catalog["apps"]] if self.catalog else []
        recent = self.catalog["recent"] if self.catalog else []
        self.results = launcher.search(apps, self.search.query, recent)[:launcher.MAX_APPS]
        self.search.selected = min(self.search.selected, max(len(self.results) - 1, 0))

    def _launcher_locked(self):
        state = {"available": True, "open": self.launcher_open, "seq": self.launcher_seq,
                 "catalog": self.catalog_seq, "query": self.search.query,
                 "selected": self.search.selected, "pageStep": self.search.page_step,
                 "keyboard": self._lease}
        if self.search.query.strip():
            state["results"] = self.results
        return state

    def _publish_launcher(self):
        with self.lock:
            if self.state is None or self.catalog is None:
                return
            state = {**self.state, "launcher": self._launcher_locked()}
        self._set_state(state)

    def _close_launcher_locked(self):
        if self.launcher_open:
            self.launcher_open = False
            self.launcher_seq += 1
        self.search.reset()
        self.results = []

    def _take_keyboard(self):
        """The Mac keyboard types into the search field while the launcher is open."""
        result = self.macview.call("/launcher/keys", method="POST", verb="open")
        self._lease = isinstance(result, dict) and result.get("active") is True
        if self._lease:
            snapshot = self.macview.call("/launcher/keys", after=0)
            last = snapshot.get("last") if isinstance(snapshot, dict) else None
            self._keys_after = last if type(last) is int and last >= 0 else 0

    def _launcher_open(self):
        with self.helper_lock:
            if not self.enabled or self.catalog is None:
                return
            with self.lock:
                if not self.launcher_open:
                    self.launcher_open = True
                    self.launcher_seq += 1
                self.search.reset()
                self._search_locked()
            self._publish_launcher()
            try:
                self._take_keyboard()
            finally:
                self._publish_launcher()
            self._refresh_catalog(force=time.monotonic() - (self._catalog_at or 0) > 10)
            self._publish_launcher()

    def _launcher_close(self):
        with self.helper_lock:
            with self.lock:
                self._close_launcher_locked()
            self._publish_launcher()
            if self._lease:
                self._lease = False
                if self._configured and self.macview.agent is self._agent:
                    self.macview.call("/launcher/keys", method="POST", verb="close")
            self._publish_launcher()

    def poll_launcher(self):
        """Typed keys from the helper's lease (polling also renews it)."""
        submit = None
        with self.helper_lock:
            if (not self.enabled or not self._configured or not self.launcher_open or not self._lease or
                    self.macview.agent is not self._agent):
                return
            snapshot = self.macview.call("/launcher/keys", after=self._keys_after)
            if not isinstance(snapshot, dict):
                return
            if snapshot.get("active") is not True:
                self._lease = False  # the next refresh asks again
            close = False
            with self.lock:
                for event in snapshot.get("events") or []:
                    n = event.get("n") if isinstance(event, dict) else None
                    if type(n) is not int or n <= self._keys_after:
                        continue
                    self._keys_after = n
                    query = self.search.query
                    action = self.search.apply(event, len(self.results))
                    if self.search.query != query:
                        self._search_locked()
                    if action == "close":
                        close = True
                        break
                    if action == "submit" and self.search.query.strip() and self.results:
                        submit = self.results[min(self.search.selected, len(self.results) - 1)]
                        close = True
                        break
            if close:
                self._launcher_close()
            else:
                self._publish_launcher()
        if submit:
            self._lane(self._app_action, submit, "open")

    def _launcher_move(self, moved, before, folder):
        with self.helper_lock:
            with self.lock:
                if self.catalog is None:
                    return
                items = launcher.move(self.catalog["items"], moved, before, folder)
                if items is None:
                    return
                launcher.save_layout(self._layout_path(), items)
                self._set_catalog_locked({**self.catalog, "items": items})
            self._publish_launcher()

    def _catalog_json(self):
        catalog = self.catalog
        return json.dumps({"catalog": self.catalog_seq,
                           "apps": [{"id": a["bundleID"], "name": a["name"], "v": a["version"]} for a in catalog["apps"]],
                           "items": catalog["items"], "recent": catalog["recent"]},
                          ensure_ascii=True, separators=(",", ":"))

    def _sync_launcher(self, reported):
        """Publish the catalogue when the Frame's copy differs, then send a batch
        of the icons it lacks: recent apps first, then the grid's order."""
        with self.lock:
            catalog = self.catalog
            raw = self._catalog_json() if catalog is not None else None
            seq = self.catalog_seq
        if raw is None:
            return
        if reported != seq:
            if len(raw.encode()) > 100000:
                raise ValueError("Launcher catalogue is too large")
            out = self._run("launcher", raw)
            for line in out.splitlines():
                if line.startswith("LAUNCHER_ICONS_MISSING "):
                    missing = set(json.loads(line.split(" ", 1)[1]))
                    order = list(dict.fromkeys(catalog["recent"] + list(launcher.flatten(catalog["items"]))))
                    self._launcher_icons = [b for b in order if b in missing]
            self._published_catalog = seq
        versions = {a["bundleID"]: a["version"] for a in catalog["apps"]}
        batch, size = [], 0
        while self._launcher_icons and len(batch) < 3 * 8:
            bundle = self._launcher_icons[0]
            try:
                encoded, version = self._icon(bundle)
            except Exception as exc:
                self.error = str(exc)
                self._launcher_icons.pop(0)  # retried when the catalogue is next published
                continue
            if batch and size + len(encoded) > ICON_BATCH:
                break
            self._launcher_icons.pop(0)
            batch += [bundle, version or versions.get(bundle, ""), encoded]
            size += len(encoded)
        if batch:
            self._run("icons", *batch)
            self._sent_icons.update(batch[0::3])

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
        reported = None
        for line in out.splitlines():
            if line.startswith("SPATIAL_ICONS_MISSING "):
                missing = json.loads(line.split(" ", 1)[1])
                if isinstance(missing, list):
                    self._sent_icons.difference_update(b for b in missing if isinstance(b, str))
            elif line.startswith("LAUNCHER_CATALOG "):
                value = json.loads(line.split(" ", 1)[1])
                reported = value if type(value) is int else None
        if self.enabled and state.get("launcher"):
            try:
                self._sync_launcher(reported)
            except Exception as exc:
                self.error = str(exc)
        # Limit initial icon work so large Docks do not delay subsequent switcher updates.
        pending = [a["bundleID"] for a in state["apps"] if a["bundleID"] not in self._sent_icons]
        for bundle in pending[:4]:
            try:
                self._run("icon", bundle, self._icon(bundle)[0])
                self._sent_icons.add(bundle)
            except Exception as exc:
                self.error = str(exc)  # retry failed icons, without blocking the state

    def _icon(self, bundle):
        """A checked 128 px PNG (base64) and its version, cached by both."""
        cached = self._icons.get(bundle)
        version = None
        if self.catalog is not None:
            version = next((a["version"] for a in self.catalog["apps"] if a["bundleID"] == bundle), None)
        if cached is None or (version is not None and cached[1] not in (None, version)):
            reply = self.macview.call("/apps/icon", bundleID=bundle)
            encoded = reply["png"]
            if not isinstance(encoded, str) or len(encoded) > 100000:
                raise ValueError("Invalid app icon")
            data = base64.b64decode(encoded, validate=True)
            if (len(data) < 33 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or
                    struct.unpack(">II", data[16:24]) != (128, 128)):
                raise ValueError("Expected a 128x128 PNG")
            served = reply.get("version")
            cached = (encoded, launcher.clean(served, 96) if isinstance(served, str) else version)
            self._icons[bundle] = cached
        return cached

    def recenter(self):
        """Dashboard Recenter in Spatial Mode: publish a newer counter."""
        with self.lock:
            self.recenter_seq += 1
            state = {**self.state, "recenterSeq": self.recenter_seq} if self.state else None
        if state is not None:
            self._set_state(state)

    def arrange(self):
        """Arrange Windows in Spatial Mode: publish a newer counter."""
        with self.lock:
            self.arrange_seq += 1
            state = {**self.state, "arrangeSeq": self.arrange_seq} if self.state else None
        if state is not None:
            self._set_state(state)

    def _reject(self, rid, why, shown=None):
        """A request the companion will not carry out: say why in the log, answer the
        headset and, for one a person should hear of, show the amber notice."""
        log(f"companion rejected id={rid or '-'}: {why}")
        self._ack(rid, False, shown or why)
        record = getattr(self.macview, "_record_error", None)
        if shown and record:
            record(shown)

    def handle_request(self, request):
        action = request.get("action")
        if action == "arrange_windows":
            # Only Spatial Mode has a wall; elsewhere the request does nothing.
            if self.enabled:
                self.arrange()
            return True
        if action == "recenter":
            # Desktop Mode keeps its Workspace recenter (the caller's own path).
            if not self.enabled:
                return False
            self.recenter()
            return True
        if action not in ("app", "switch", "switcher", "launcher"):
            return False
        rid = request.get("id") if isinstance(request.get("id"), str) and REQUEST_ID.fullmatch(request["id"]) else None
        if not self.enabled:
            workspace = getattr(self.macview, "spatial_workspace", {})
            self._reject(rid, f"{action} request dropped: Spatial Mode is not running (mode "
                              f"{getattr(self.macview, 'mode', '?')}, workspace {workspace.get('state')}, "
                              f"stopping {bool(getattr(self.macview, 'exit_pending', False))})")
            return True
        if action == "app":
            bundle, verb = request.get("bundleID"), request.get("verb")
            selected = request.get("selected") if isinstance(request.get("selected"), str) else None
            log(f"companion received id={rid or '-'} verb={verb!r} bundle={bundle!r} selected={selected or '-'}")
            with self.lock:
                known = {a["bundleID"] for a in self.state["apps"]} if self.state else set()
                if self.catalog is not None and verb == "open":
                    known |= {a["bundleID"] for a in self.catalog["apps"]}
            if not isinstance(bundle, str) or not BUNDLE.fullmatch(bundle):
                self._reject(rid, "the bundle ID is not valid")
            elif not isinstance(verb, str) or verb not in VERBS:
                self._reject(rid, f"the verb is not one of {sorted(VERBS)}")
            elif bundle not in known:
                self._reject(rid, f"{bundle} is not on the shelf" + (" or in the catalogue" if verb == "open" else ""),
                             f"Couldn't {VERB_NAMES[verb]} {clean(self._app_name(bundle), 64)}: it isn't on the shelf")
            else:
                if selected is not None and not KEY.fullmatch(selected):
                    selected = None
                log(f"companion queued id={rid or '-'} on the action lane")
                self._lane(self._app_action, bundle, verb, rid, selected)
        elif action == "launcher":
            verb = request.get("verb")
            if verb == "open":
                self._lane(self._launcher_open)
            elif verb == "close":
                self._lane(self._launcher_close)
            elif verb == "move":
                moved, before, folder = request.get("id"), request.get("before"), request.get("folder")
                ids = re.compile(r"(?:folder:[A-Za-z0-9_-]{1,40}|[A-Za-z0-9][A-Za-z0-9._-]{0,199})\Z")
                if (isinstance(moved, str) and ids.fullmatch(moved) and
                        (before is None or (isinstance(before, str) and ids.fullmatch(before))) and
                        (folder is None or (isinstance(folder, str) and launcher.FOLDER.fullmatch(folder)))):
                    self._lane(self._launcher_move, moved, before, folder)
        elif action == "switch":
            key = request.get("key")
            if isinstance(key, str) and KEY.fullmatch(key) and self._source(key):
                log(f"companion received switch key={key} (the headset selected this panel)")
                self._lane(self._switch, key)
            else:
                log(f"companion rejected switch key={key!r}: not one of this Workspace's panels")
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
            apps = (self.state["apps"] if self.state else []) + (self.catalog["apps"] if self.catalog else [])
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

    def _app_action(self, bundle, verb, rid=None, selected=None):
        """Carry out a shelf click on the helper. `rid` is the headset's request id; `selected`
        is the panel it already brought forward, which then needs no second selection."""
        if not self.enabled:
            log(f"companion dropped id={rid or '-'} {verb} {bundle}: Spatial Mode stopped first")
            self._ack(rid, False, "Spatial Mode Stopped")
            return
        name = self._app_name(bundle)
        log(f"companion asking the helper id={rid or '-'} {verb} {bundle}")
        try:
            query = {"bundleID": bundle, "verb": verb}
            if rid:
                query["id"] = rid
            result = self.macview.call("/apps/action", method="POST", **query)
            if isinstance(result, dict) and result.get("ok") is False:
                raise ValueError(result.get("error") or "macOS refused")
        except Exception as exc:
            reason = str(exc).removeprefix("The Mac Streaming Helper: ")
            message = f"Couldn't {VERB_NAMES[verb]} {clean(name, 64)}: {clean(reason, 120)}"
            log(f"companion helper refused id={rid or '-'}: {message}")
            self._ack(rid, False, message)
            raise RuntimeError(message) from exc
        log(f"companion helper answered id={rid or '-'}: {result}")
        # Opening an app that already has a panel also brings that panel forward, unless
        # the headset has (it says so in `selected`).
        if verb in ("open", "show_windows"):
            keys = self._app_windows(bundle)
            if selected and selected in keys:
                log(f"companion id={rid or '-'}: the headset already brought {selected} forward")
            elif keys and self.enabled:
                log(f"companion id={rid or '-'}: choosing {keys[0]} for the headset")
                self.macview.call("/switcher", method="POST", mode="spatial", verb="choose", key=keys[0])
            else:
                log(f"companion id={rid or '-'}: {bundle} has no panel yet; the headset waits for one")
        self._ack(rid, True)
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
