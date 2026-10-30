"""FW-15 contract tests: helper and Frame transport are fakes, writer runs locally."""
import base64
import copy
import fnmatch
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
from frame_spatial_apps import SpatialApps, SCRIPT
import frame_macview
import frame_spatial_apps
import frame_spatial_launcher as launcher

TERMINAL = "com.apple.Terminal"
NOTES = "com.apple.Notes"
PINNED = "com.example.Pinned"
KEY1 = "com.frame-workspace.native.1000.panel-w1"
KEY2 = "com.frame-workspace.native.1000.panel-w2"


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\x10\x20\x30\xff" * 128) * 128)) + chunk(b"IEND", b""))


class FakeAgent:
    def poll(self):
        return None


class FakeMacView:
    def __init__(self):
        self.mode, self.closing = "spatial", False
        self.spatial_workspace, self.exit_pending = {"state": "running"}, False
        self.agent = FakeAgent()
        self._spatial_lock = threading.Lock()
        self._spatial_focused = None
        self.hovered = None
        self.calls, self.remote = [], []
        self.native_panels = {
            "spatial:1": {"slot": 1, "key": KEY1, "title": "zsh"},
            "window:2": {"slot": 0, "key": KEY2, "title": "Note"},
        }
        self.apps = [{"bundleID": b, "name": n, "running": r, "pinned": p, "hidden": h, "active": a}
                     for b, n, r, p, h, a in [(PINNED, "Pinned", False, True, False, False),
                                            (TERMINAL, "Terminal", True, True, False, True),
                                            (NOTES, "Notes", True, False, True, False)]]
        self.windows = [{"src": "window:1", "bundleID": TERMINAL}, {"src": "window:2", "bundleID": NOTES}]
        self.switcher = {"open": False, "index": 0, "seq": 0, "chosen": None,
                         "windows": [KEY1, KEY2], "tapActive": True}
        self.missing, self.icon_fail = [], False
        self.lane, self.action_error = [], None
        # Launcher A: the helper's catalogue (None: an older helper without
        # one) and its keyboard lease.
        self.catalog = None
        self.keys = {"active": False, "events": [], "last": 0, "tap": True}

    def observe_mouse_hover(self, source):
        self.hovered = source

    def call(self, path, method="GET", **query):
        self.calls.append((path, method, query))
        if path == "/apps":
            return {"apps": copy.deepcopy(self.apps)}
        if path == "/windows":
            return {"windows": copy.deepcopy(self.windows)}
        if path == "/switcher":
            return copy.deepcopy(self.switcher)
        if path == "/launcher":
            if self.catalog is None:
                raise frame_macview.MacViewError("The Mac Streaming Helper: not found")
            return copy.deepcopy(self.catalog)
        if path == "/launcher/keys":
            keys = self.keys
            if method == "POST":
                keys["active"] = query["verb"] == "open" and keys["tap"]
                if keys["active"]:
                    keys["events"] = []
                return {"active": keys["active"], "tapActive": keys["tap"]}
            after = int(query.get("after", 0))
            return {"active": keys["active"], "last": keys["last"],
                    "events": [e for e in keys["events"] if e["n"] > after]}
        if path == "/apps/action" and self.action_error:
            raise frame_macview.MacViewError("The Mac Streaming Helper: " + self.action_error)
        if path == "/apps/icon":
            if self.icon_fail:
                raise ValueError("icon failed")
            return {"png": base64.b64encode(png()).decode(), "version": "v-" + query["bundleID"]}
        return {}

    def type_keys(self, *events):
        """The helper's key queue, as the event tap fills it."""
        for event in events:
            self.keys["last"] += 1
            self.keys["events"].append({**event, "n": self.keys["last"]})

    def run(self, command, **kwargs):
        args = shlex.split(command)
        assert args[:2] == ["python3", "-"]
        assert kwargs["stdin"] == SCRIPT.read_text()
        self.remote.append(args[2:])
        return "SPATIAL_ICONS_MISSING " + json.dumps(self.missing) if args[2] == "sync" else ""

    def _dash_run(self, busy, fn, *args):
        fn(*args)

    def _action_run(self, fn, *args):
        self.lane.append(fn.__name__)
        fn(*args)


class Companion(unittest.TestCase):
    def setUp(self):
        self.mv = FakeMacView()
        self.apps = SpatialApps(self.mv)
        self.log = []
        frame_spatial_apps.sink = self.log.append
        self.addCleanup(setattr, frame_spatial_apps, "sink", None)

    def test_dock_order_and_window_panel_mapping(self):
        self.apps.refresh()
        state = self.apps.state
        self.assertEqual(state["mode"], "spatial")
        self.assertEqual([a["bundleID"] for a in state["apps"]], [PINNED, TERMINAL, NOTES])
        self.assertEqual(state["apps"][0]["windows"], [])
        self.assertEqual(state["apps"][1]["windows"], [{"key": KEY1, "title": "zsh"}])
        self.assertTrue(state["apps"][2]["hidden"])
        self.assertEqual(state["switcher"]["windows"], [KEY1, KEY2])
        self.assertIn(("/windows", "GET", {"all": "1"}), self.mv.calls)
        sent = json.loads(self.mv.calls[-1][2]["entries"])
        self.assertEqual(sent, [{"key": KEY1, "bundleID": TERMINAL}, {"key": KEY2, "bundleID": NOTES}])
        self.apps.wake.clear()
        self.apps.refresh()
        self.assertFalse(self.apps.wake.is_set())

    def test_auxiliary_panels_never_enter_dock_switcher_or_switch_requests(self):
        for source in frame_macview.SPATIAL_AUX_ROLES:
            self.mv.native_panels[source] = {"slot": 0, "key": KEY1.replace("w1", source.replace(":", "")),
                                             "title": "Auxiliary", "bundleID": TERMINAL}
        self.apps.refresh()
        self.apps.sync()
        state = json.loads(self.mv.remote[0][1])
        self.assertEqual([w["key"] for a in state["apps"] for w in a["windows"]], [KEY1, KEY2])
        self.assertEqual(state["switcher"]["windows"], [KEY1, KEY2])
        count = len(self.mv.calls)
        for source in frame_macview.SPATIAL_AUX_ROLES:
            self.apps.handle_request({"action": "switch", "key": self.mv.native_panels[source]["key"]})
        self.assertEqual(len(self.mv.calls), count)

    def test_hidden_windows_retain_mapping_but_removed_panels_do_not(self):
        self.apps.refresh()
        self.mv.windows.clear()
        self.apps.refresh()
        self.assertEqual(self.apps.state["apps"][1]["windows"][0]["key"], KEY1)
        del self.mv.native_panels["spatial:1"]
        self.apps.refresh()
        self.assertEqual(self.apps.state["apps"][1]["windows"], [])
        self.assertNotIn("1", self.apps._window_apps)

    def test_panel_metadata_maps_already_hidden_window(self):
        self.mv.windows.clear()
        self.mv.native_panels["spatial:1"]["bundleID"] = TERMINAL
        self.apps.refresh()
        self.assertEqual(self.apps.state["apps"][1]["windows"][0]["key"], KEY1)

    def test_switcher_poll_wakes_sync_and_carries_cmd_release_choice(self):
        self.apps.refresh()
        self.apps.wake.clear()
        self.mv.switcher.update(open=True, index=1, seq=1, mouseHoveredSrc="spatial:1")
        self.apps.poll_switcher()
        self.assertEqual(self.mv.hovered, "spatial:1")
        self.assertTrue(self.apps.wake.is_set())
        self.apps.sync()
        sent = json.loads(self.mv.remote[0][1])
        self.assertEqual(sent["switcher"]["index"], 1)
        self.mv.switcher.update(open=False, chosen=KEY2, seq=2)
        self.apps.poll_switcher()
        self.assertEqual(self.apps.state["switcher"]["chosen"], KEY2)
        self.assertFalse(self.apps.state["switcher"]["open"])

    def test_dashboard_recenter_in_spatial_mode_publishes_a_newer_counter(self):
        self.apps.refresh()
        self.assertNotIn("recenterSeq", self.apps.state)
        self.assertTrue(self.apps.handle_request({"action": "recenter"}))
        self.assertEqual(self.apps.state["recenterSeq"], 1)
        self.apps.handle_request({"action": "recenter"})
        self.apps.refresh()  # a refresh keeps the counter
        self.assertEqual(self.apps.state["recenterSeq"], 2)
        self.apps.sync()
        self.assertEqual(json.loads([r for r in self.mv.remote if r[0] == "sync"][-1][1])["recenterSeq"], 2)
        self.mv.mode = "desktop"
        self.assertFalse(self.apps.handle_request({"action": "recenter"}))

    def test_arrange_windows_publishes_a_newer_counter_and_is_spatial_only(self):
        self.apps.refresh()
        self.assertNotIn("arrangeSeq", self.apps.state)
        self.assertTrue(self.apps.handle_request({"action": "arrange_windows"}))
        self.assertEqual(self.apps.state["arrangeSeq"], 1)
        self.apps.refresh()  # a refresh keeps the counter
        self.assertEqual(self.apps.state["arrangeSeq"], 1)
        self.apps.sync()
        self.assertEqual(json.loads([r for r in self.mv.remote if r[0] == "sync"][-1][1])["arrangeSeq"], 1)
        self.mv.mode = "desktop"
        self.assertTrue(self.apps.handle_request({"action": "arrange_windows"}), "consumed, with nothing to arrange")
        self.assertEqual(self.apps.arrange_seq, 1)

    def test_desktop_never_starts_helper_or_writes_state(self):
        del self.mv.mode  # companion mode worker may not yet be present
        self.apps.refresh()
        self.apps.sync()
        self.assertEqual(self.mv.calls, [])
        self.assertEqual(self.mv.remote, [])

    def test_leaving_spatial_disables_tap_and_removes_published_state(self):
        self.apps.refresh()
        self.apps.sync()
        self.mv.mode = "desktop"
        self.apps.refresh()
        self.apps.sync()
        self.assertEqual(self.mv.calls[-1], ("/switcher", "POST", {"mode": "desktop"}))
        self.assertEqual(self.mv.remote[-1], ["clear"])
        self.assertIsNone(self.apps.state)
        self.apps.sync()
        self.assertEqual(sum(r == ["clear"] for r in self.mv.remote), 1)

    def test_pending_stop_disables_spatial_apps_and_clears_published_state(self):
        self.apps.refresh()
        self.apps.sync()
        self.mv.exit_pending = True
        self.assertFalse(self.apps.enabled)
        self.apps.sync()
        self.assertEqual(self.mv.remote[-1], ["clear"])
        self.assertFalse(self.apps._published)

    def test_workspace_must_be_running_to_publish_spatial_apps(self):
        self.mv.spatial_workspace["state"] = "stopped"
        self.apps.refresh()
        self.apps.sync()
        self.assertEqual(self.mv.remote, [])

    def test_lost_sync_reply_still_clears_state_on_mode_exit(self):
        self.apps.refresh()
        original_run = self.mv.run
        self.mv.run = lambda *a, **k: (_ for _ in ()).throw(OSError("lost reply"))
        with self.assertRaises(OSError):
            self.apps.sync()
        self.mv.run = original_run
        self.mv.mode = "desktop"
        self.apps.refresh()
        self.apps.sync()
        self.assertEqual(self.mv.remote, [["clear"]])

    def test_icons_sent_once_retry_failures_and_recover_frame_runtime_loss(self):
        self.apps.refresh()
        self.mv.icon_fail = True
        self.apps.sync()
        self.assertEqual(len(self.mv.remote), 1)  # state survives icon errors
        self.mv.icon_fail = False
        self.apps.sync()
        self.apps.sync()
        self.assertEqual(sum(r[0] == "icon" for r in self.mv.remote), 3)
        self.assertEqual(sum(c[0] == "/apps/icon" for c in self.mv.calls), 6)  # three failed, three successful
        self.mv.missing = [TERMINAL]
        self.apps.sync()
        self.assertEqual(sum(r[0] == "icon" for r in self.mv.remote), 4)
        self.assertEqual(sum(c[0] == "/apps/icon" for c in self.mv.calls), 6)  # reuse the PNG

    def test_requests_allow_known_apps_owned_panels_and_fixed_verbs(self):
        self.apps.refresh()
        for verb in ("open", "hide", "unhide", "quit", "show_windows"):
            self.assertTrue(self.apps.handle_request({"action": "app", "verb": verb, "bundleID": TERMINAL}))
        self.assertEqual(sum(c[0] == "/apps/action" for c in self.mv.calls), 5)
        self.apps.handle_request({"action": "switch", "key": KEY2})
        self.assertIn(("/focus", "POST", {"src": "window:2", "u": .5, "v": .5}), self.mv.calls)
        self.assertEqual(self.mv._spatial_focused, "window:2")
        self.assertIn(("/switcher", "POST", {"mode": "spatial", "verb": "close"}), self.mv.calls)
        for verb in ("open", "close"):
            self.apps.handle_request({"action": "switcher", "verb": verb})
            self.assertIn(("/switcher", "POST", {"mode": "spatial", "verb": verb}), self.mv.calls)
        count = len(self.mv.calls)
        for request in ({"action": "app", "bundleID": "../../bad", "verb": "open"},
                        {"action": "app", "bundleID": TERMINAL, "verb": "force_quit"},
                        {"action": "app", "bundleID": TERMINAL, "verb": []},
                        {"action": "switch", "key": KEY1.replace("w1", "w99")},
                        {"action": "switcher", "verb": "shell"}):
            self.apps.handle_request(request)
        self.assertEqual(len(self.mv.calls), count)
        self.mv.mode = "desktop"
        self.apps.handle_request({"action": "app", "bundleID": TERMINAL, "verb": "quit"})
        self.assertEqual(len(self.mv.calls), count)

    def test_headset_clicks_use_the_action_lane(self):
        self.apps.refresh()
        self.apps.handle_request({"action": "app", "verb": "hide", "bundleID": NOTES})
        self.apps.handle_request({"action": "switch", "key": KEY2})
        self.apps.handle_request({"action": "switcher", "verb": "open"})
        self.assertEqual(self.mv.lane, ["_app_action", "_switch", "_switcher_action"])

    def test_opening_an_app_with_a_panel_brings_that_panel_forward(self):
        self.mv.switcher["windows"] = [KEY2, KEY1]
        self.apps.refresh()
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": TERMINAL})
        self.assertIn(("/switcher", "POST", {"mode": "spatial", "verb": "choose", "key": KEY1}), self.mv.calls)
        self.mv.calls.clear()
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": PINNED})  # no panel yet
        self.apps.handle_request({"action": "app", "verb": "hide", "bundleID": TERMINAL})
        self.assertFalse([c for c in self.mv.calls if c[2].get("verb") == "choose"])

    def test_failed_app_action_names_the_app_and_reason(self):
        self.apps.refresh()
        self.mv.action_error = "application not found"
        with self.assertRaisesRegex(RuntimeError, r"^Couldn't Open Pinned: application not found$"):
            self.apps._app_action(PINNED, "open")
        self.assertFalse([c for c in self.mv.calls if c[2].get("verb") == "choose"])

    def logged(self, text):
        return [line for line in self.log if text in line]

    def test_a_shelf_click_is_logged_at_every_hop_and_acknowledged_to_the_headset(self):
        self.apps.refresh()
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": PINNED, "id": "d42-7"})
        # The helper gets the request id too, so its own log lines can be matched.
        self.assertIn(("/apps/action", "POST", {"bundleID": PINNED, "verb": "open", "id": "d42-7"}), self.mv.calls)
        for hop in ("companion received id=d42-7 verb='open' bundle='com.example.Pinned'",
                    "companion queued id=d42-7 on the action lane",
                    "companion asking the helper id=d42-7 open com.example.Pinned",
                    "companion helper answered id=d42-7",
                    "com.example.Pinned has no panel yet"):
            self.assertTrue(self.logged(hop), (hop, self.log))
        self.assertTrue(all(" dock: " in line for line in self.log))
        self.assertEqual(self.apps.state["acks"], [{"id": "d42-7", "ok": True}])
        self.assertEqual(self.mv.lane, ["_app_action"])

    def test_acknowledgements_pass_the_frame_writer_and_are_bounded(self):
        self.apps.refresh()
        for n in range(12):
            self.apps.handle_request({"action": "app", "verb": "open", "bundleID": PINNED, "id": f"d1-{n}"})
        acks = self.apps.state["acks"]
        self.assertEqual([a["id"] for a in acks], [f"d1-{n}" for n in range(4, 12)])
        self.mv.action_error = "application not found"
        with self.assertRaises(RuntimeError):
            self.apps._app_action(PINNED, "open", "d1-99")
        self.assertEqual(self.apps.state["acks"][-1],
                         {"id": "d1-99", "ok": False, "error": "Couldn't Open Pinned: application not found"})
        self.assertTrue(self.logged("companion helper refused id=d1-99: Couldn't Open Pinned: application not found"))
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_RUNTIME_DIR": tmp}
            def write(state):
                return subprocess.run([sys.executable, str(SCRIPT), "sync", json.dumps(state)], env=env,
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(write(self.apps.state).returncode, 0)
            for bad in ([{"id": "../x", "ok": True}], [{"id": "d1-1"}], [{"id": "d1-1", "ok": 1}],
                        [{"id": "d1-1", "ok": False, "error": "x" * 200}], "no", [{"id": "d1-1", "ok": True}] * 17):
                self.assertEqual(write({**self.apps.state, "acks": bad}).returncode, 2, bad)

    def test_rejected_requests_say_why_and_answer_the_headset(self):
        self.apps.refresh()
        errors = []
        self.mv._record_error = errors.append
        count = len(self.mv.calls)
        for request, why in (({"action": "app", "bundleID": "../../bad", "verb": "open", "id": "r1"}, "the bundle ID is not valid"),
                             ({"action": "app", "bundleID": TERMINAL, "verb": "force_quit", "id": "r2"}, "the verb is not one of"),
                             ({"action": "app", "bundleID": "com.unknown.App", "verb": "open", "id": "r3"},
                              "com.unknown.App is not on the shelf or in the catalogue"),
                             ({"action": "app", "bundleID": "com.unknown.App", "verb": "hide", "id": "r4"},
                              "com.unknown.App is not on the shelf")):
            self.assertTrue(self.apps.handle_request(request))
            self.assertTrue(self.logged(f"companion rejected id={request['id']}: {why}"), (why, self.log))
        self.assertEqual(len(self.mv.calls), count, "a rejected request never reaches the helper")
        acks = {a["id"]: a for a in self.apps.state["acks"]}
        self.assertEqual(set(acks), {"r1", "r2", "r3", "r4"})
        self.assertFalse(any(a["ok"] for a in acks.values()))
        self.assertEqual(errors, ["Couldn't Open com.unknown.App: it isn't on the shelf",
                                  "Couldn't Hide com.unknown.App: it isn't on the shelf"])
        self.apps.handle_request({"action": "switch", "key": KEY1.replace("w1", "w99")})
        self.assertTrue(self.logged("companion rejected switch key="))
        # Not in Spatial Mode: dropped, and the reason says which condition failed.
        self.mv.spatial_workspace["state"] = "starting"
        self.assertTrue(self.apps.handle_request({"action": "app", "bundleID": TERMINAL, "verb": "open", "id": "r5"}))
        self.assertTrue(self.logged("companion rejected id=r5: app request dropped: Spatial Mode is not running "
                                    "(mode spatial, workspace starting, stopping False)"), self.log)
        self.assertEqual(len(self.mv.calls), count)

    def test_opening_an_app_chooses_its_panel_unless_the_headset_already_did(self):
        self.mv.switcher["windows"] = [KEY2, KEY1]
        self.apps.refresh()
        choose = lambda: [c for c in self.mv.calls if c[2].get("verb") == "choose"]
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": TERMINAL, "id": "c1"})
        self.assertEqual(choose(), [("/switcher", "POST", {"mode": "spatial", "verb": "choose", "key": KEY1})])
        self.assertTrue(self.logged(f"companion id=c1: choosing {KEY1} for the headset"))
        self.mv.calls.clear()
        # The headset brought the panel forward itself: no second selection to jump it again.
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": TERMINAL, "id": "c2", "selected": KEY1})
        self.assertEqual(choose(), [])
        self.assertTrue(self.logged(f"companion id=c2: the headset already brought {KEY1} forward"))
        # A panel it names that is not this app's is not trusted.
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": TERMINAL, "id": "c3", "selected": KEY2})
        self.assertEqual(len(choose()), 1)
        # And one that is not a panel key at all is dropped before it reaches anything.
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": TERMINAL, "id": "c4", "selected": "../x"})
        self.assertEqual(len(choose()), 2)
        self.assertEqual([a["id"] for a in self.apps.state["acks"]], ["c1", "c2", "c3", "c4"])

    def test_real_macview_action_lane_skips_a_blocked_workspace_queue_and_records_failures(self):
        mv = frame_macview.MacView([], lambda *a, **k: "", "fake")
        mv.mode = "spatial"
        mv.spatial_workspace["state"] = "running"
        mv.native_panels = self.mv.native_panels
        mv.call = self.mv.call
        mv.agent = self.mv.agent
        mv.spatial_apps.refresh()
        started, release = threading.Event(), threading.Event()
        mv._dash_run(None, lambda: (started.set(), release.wait(5)))  # a long panel launch
        self.assertTrue(started.wait(2))
        self.mv.action_error = "application not found"
        mv._handle_frame_requests(json.dumps({"action": "app", "verb": "open", "bundleID": PINNED}))
        mv._handle_frame_requests(json.dumps({"action": "switch", "key": KEY1}))
        mv.action_jobs.join()
        self.assertEqual(mv._spatial_focused, "spatial:1")
        self.assertEqual([e["message"] for e in mv.errors_state()], ["Couldn't Open Pinned: application not found"])
        self.assertEqual(mv.dash_state()["workspace"]["notice"], "Couldn't Open Pinned: application not found")
        self.assertEqual(mv.dash_state()["errors"], ["Couldn't Open Pinned: application not found"])
        release.set()
        mv.dash_jobs.join()

    def test_real_macview_logs_a_truncated_request_and_a_failed_poll_once(self):
        mv = frame_macview.MacView([], lambda *a, **k: "", "fake")
        mv._handle_frame_requests('{"action":"app","verb":"open","bundleID":"com.apple.Termi')
        self.assertTrue(self.logged("companion could not read a request line from the headset"))
        for _ in range(3):
            mv._log_poll_failure("request poll", RuntimeError("ssh timed out"))
        self.assertEqual(len(self.logged("companion request poll failed: ssh timed out")), 1)
        mv._log_poll_failure("request poll", RuntimeError("no route"))
        self.assertEqual(len(self.logged("companion request poll failed")), 2)

    def test_real_macview_shelf_click_runs_the_whole_companion_path_with_its_id(self):
        mv = frame_macview.MacView([], lambda *a, **k: "", "fake")
        mv.mode = "spatial"
        mv.spatial_workspace["state"] = "running"
        mv.native_panels = self.mv.native_panels
        mv.call = self.mv.call
        mv.agent = self.mv.agent
        mv.spatial_apps.refresh()
        mv._handle_frame_requests(json.dumps({"action": "app", "verb": "open", "bundleID": TERMINAL, "id": "d9-1"}))
        mv.action_jobs.join()
        self.assertIn(("/apps/action", "POST", {"bundleID": TERMINAL, "verb": "open", "id": "d9-1"}), self.mv.calls)
        self.assertEqual(mv.spatial_apps.state["acks"], [{"id": "d9-1", "ok": True}])
        self.assertEqual(mv.errors_state(), [])
        # A refusal comes back as the amber notice and as a failed acknowledgement.
        self.mv.action_error = "application not found"
        mv._handle_frame_requests(json.dumps({"action": "app", "verb": "open", "bundleID": PINNED, "id": "d9-2"}))
        mv.action_jobs.join()
        self.assertEqual([e["message"] for e in mv.errors_state()], ["Couldn't Open Pinned: application not found"])
        self.assertEqual(mv.spatial_apps.state["acks"][-1]["ok"], False)

    def test_real_macview_dispatch_hook_uses_the_action_queue(self):
        mv = frame_macview.MacView([], lambda *a, **k: "", "fake")
        mv.mode = "spatial"
        mv.spatial_workspace["state"] = "running"
        mv.native_panels = self.mv.native_panels
        mv.call = self.mv.call
        mv.agent = self.mv.agent
        mv.spatial_apps.refresh()
        mv._handle_frame_requests(json.dumps({"action": "switch", "key": KEY1}))
        mv.action_jobs.join()
        self.assertEqual(mv._spatial_focused, "spatial:1")
        self.assertIn(("/focus", "POST", {"src": "spatial:1", "u": .5, "v": .5}), self.mv.calls)

    def test_companion_transport_executes_writer_on_stdin_and_recovers_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            def local_run(command, **kwargs):
                result = subprocess.run(shlex.split(command), input=kwargs["stdin"],
                                        env={**os.environ, "XDG_RUNTIME_DIR": tmp},
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout
            self.mv.run = local_run
            self.apps.refresh()
            self.apps.sync()
            runtime = Path(tmp) / "frame-workspace"
            state = runtime / "spatial-ui.json"
            self.assertEqual(json.loads(state.read_text()), self.apps.state)
            icon = runtime / "icons" / (TERMINAL + ".png")
            self.assertEqual(icon.read_bytes(), png())
            icon.unlink()  # lost Frame runtime contents must not require a new helper request
            icon_calls = sum(c[0] == "/apps/icon" for c in self.mv.calls)
            self.mv.switcher.update(open=True, seq=1, index=1)
            self.apps.poll_switcher()
            self.apps.sync()
            self.assertTrue(json.loads(state.read_text())["switcher"]["open"])
            self.assertEqual(icon.read_bytes(), png())
            self.assertEqual(sum(c[0] == "/apps/icon" for c in self.mv.calls), icon_calls)


class RuntimeWriter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "frame-workspace"
        self.env = {**os.environ, "XDG_RUNTIME_DIR": self.temp.name}
        apps = SpatialApps(FakeMacView())
        apps.refresh()
        self.state = apps.state

    def run_writer(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_desktop_app_packages_the_runtime_writer(self):
        resources = json.loads((ROOT / "app/package.json").read_text())["build"]["extraResources"]
        scripts = next(r for r in resources if r["from"] == "../scripts")
        self.assertTrue(any(fnmatch.fnmatch(SCRIPT.name, pattern) for pattern in scripts["filter"]))

    def test_atomic_private_state_icons_and_cleanup(self):
        first = self.run_writer("sync", json.dumps(self.state))
        self.assertEqual(first.returncode, 0, first.stderr)
        path = self.runtime / "spatial-ui.json"
        self.assertEqual(json.loads(path.read_text()), self.state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn(TERMINAL, first.stdout)
        encoded = base64.b64encode(png()).decode()
        icon = self.run_writer("icon", TERMINAL, encoded)
        self.assertEqual(icon.returncode, 0, icon.stderr)
        self.assertEqual((self.runtime / "icons" / (TERMINAL + ".png")).read_bytes(), png())
        second = self.run_writer("sync", json.dumps(self.state))
        self.assertNotIn(TERMINAL, second.stdout)
        self.assertFalse(list(self.runtime.glob(".*")))
        self.assertEqual(self.run_writer("clear").returncode, 0)
        self.assertFalse(path.exists())

    def test_invalid_inputs_cannot_replace_state_or_escape_runtime(self):
        self.assertEqual(self.run_writer("sync", json.dumps(self.state)).returncode, 0)
        before = (self.runtime / "spatial-ui.json").read_bytes()
        bad_states = [{**self.state, "mode": "desktop"}, {**self.state, "apps": "bad"}]
        for field, value in (("index", -1), ("index", True), ("seq", -2), ("chosen", "foreign"),
                             ("windows", [KEY1, KEY1])):
            bad_states.append({**self.state, "switcher": {**self.state["switcher"], field: value}})
        malformed = copy.deepcopy(self.state)
        malformed["apps"][0]["icon"] = "../evil.png"
        bad_states.append(malformed)
        for state in bad_states:
            with self.subTest(state=state):
                self.assertEqual(self.run_writer("sync", json.dumps(state)).returncode, 2)
                self.assertEqual((self.runtime / "spatial-ui.json").read_bytes(), before)
        for args in (("icon", "../../evil", base64.b64encode(png()).decode()),
                     ("icon", TERMINAL, "not png"), ("clear", "extra"), ("shell",)):
            self.assertEqual(self.run_writer(*args).returncode, 2)

    def test_symlink_icon_directory_is_rejected(self):
        self.runtime.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.runtime / "icons").symlink_to(outside)
        self.assertEqual(self.run_writer("icon", TERMINAL, base64.b64encode(png()).decode()).returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])


SAFARI, MAIL, CHESS, ECRAN, PHOTOS, BOOTH = ("com.apple.Safari", "com.apple.mail", "com.apple.Chess",
                                             "com.example.ecran", "com.apple.Photos", "com.apple.PhotoBooth")


def catalog():
    """A helper catalogue: Launchpad order with a Games folder, the rest by name."""
    names = {SAFARI: "Safari", MAIL: "Mail", CHESS: "Chess", ECRAN: "Écran Partagé", PHOTOS: "Photos",
             BOOTH: "Photo Booth", TERMINAL: "Terminal", NOTES: "Notes"}
    return {"apps": [{"bundleID": b, "name": n, "version": "1-" + b} for b, n in names.items()],
            "items": [{"app": SAFARI}, {"app": MAIL}, {"folder": "folder-1", "name": "Games", "apps": [CHESS]},
                      {"app": ECRAN}, {"app": NOTES}, {"app": BOOTH}, {"app": PHOTOS}, {"app": TERMINAL}],
            "recent": [PHOTOS, TERMINAL], "launchpad": "imported"}


class LauncherData(unittest.TestCase):
    APPS = [(PHOTOS, "Photos"), (BOOTH, "Photo Booth"), (ECRAN, "Écran Partagé"), (SAFARI, "Safari"),
            ("com.example.iphoto", "iPhoto Library Upgrader"), ("com.example.graph", "Graphotype"),
            ("com.example.shop", "Adobe Photoshop 2026")]

    def test_search_is_case_and_diacritic_insensitive_with_word_prefixes_first(self):
        self.assertEqual(launcher.search(self.APPS, "pho"), [BOOTH, PHOTOS, "com.example.shop", "com.example.graph",
                                                             "com.example.iphoto"])
        self.assertEqual(launcher.search(self.APPS, "PHO", recent=[PHOTOS]), [PHOTOS, BOOTH, "com.example.shop",
                                                                             "com.example.graph", "com.example.iphoto"])
        self.assertEqual(launcher.search(self.APPS, "ecran"), [ECRAN])
        self.assertEqual(launcher.search(self.APPS, "PARTAGE"), [ECRAN])
        self.assertEqual(launcher.search(self.APPS, "photo b"), [BOOTH])
        self.assertEqual(launcher.search(self.APPS, "  "), [])
        self.assertEqual(launcher.search(self.APPS, "zz"), [])

    def test_saved_layout_drops_gone_apps_and_appends_new_ones(self):
        base = catalog()["items"]
        known = {b for b, _ in self.APPS} | {MAIL, CHESS, NOTES, TERMINAL}
        self.assertEqual(launcher.merge_layout(base, None, known), [i for i in base if i.get("app") != "x"])
        saved = [{"app": TERMINAL}, {"app": "com.gone"}, {"folder": "folder-1", "name": "Games", "apps": [CHESS, "x"]},
                 {"folder": "empty", "name": "Gone", "apps": ["com.gone"]}, {"app": TERMINAL}, "junk",
                 {"folder": "../bad", "name": "Bad", "apps": [SAFARI]}]
        merged = launcher.merge_layout(base, saved, known)
        self.assertEqual(merged[:2], [{"app": TERMINAL}, {"folder": "folder-1", "name": "Games", "apps": [CHESS]}])
        self.assertEqual([i["app"] for i in merged[2:]], [SAFARI, MAIL, ECRAN, NOTES, BOOTH, PHOTOS,
                                                          "com.example.graph", "com.example.iphoto", "com.example.shop"])
        self.assertEqual(len(list(launcher.flatten(merged))), len(known), "every app exactly once")

    def test_move_reorders_without_making_folders(self):
        items = catalog()["items"]
        moved = launcher.move(items, TERMINAL, SAFARI)
        self.assertEqual([launcher.item_id(i) for i in moved][:3], [TERMINAL, SAFARI, MAIL])
        self.assertEqual([launcher.item_id(i) for i in launcher.move(items, SAFARI, None)][-1], SAFARI)
        self.assertEqual([launcher.item_id(i) for i in launcher.move(items, "folder:folder-1", SAFARI)][0],
                         "folder:folder-1")
        two = [{"folder": "f", "name": "F", "apps": [CHESS, MAIL, SAFARI]}]
        self.assertEqual(launcher.move(two, SAFARI, CHESS, "f")[0]["apps"], [SAFARI, CHESS, MAIL])
        self.assertEqual(two[0]["apps"], [CHESS, MAIL, SAFARI], "the original is untouched")
        for args in ((SAFARI, SAFARI), ("com.gone", None), (SAFARI, "com.gone"), (CHESS, None),
                     (SAFARI, None, "folder-1"), (CHESS, "com.gone", "folder-1"), (CHESS, None, "nope")):
            self.assertIsNone(launcher.move(items, *args), args)

    def test_search_field_keys(self):
        field = launcher.SearchField()
        for event in ({"k": "text", "s": "Ph"}, {"k": "text", "s": "o"}, {"k": "back"}, {"k": "text", "s": "o"}):
            field.apply(event, 0)
        self.assertEqual(field.query, "Pho")
        for key, expected in (("right", 1), ("down", 9), ("down", 11), ("left", 10), ("up", 2), ("up", 0)):
            field.apply({"k": key}, 12)
            self.assertEqual(field.selected, expected, key)
        field.apply({"k": "right"}, 12)
        field.apply({"k": "text", "s": "t"}, 12)
        self.assertEqual(field.selected, 0, "typing selects the top match again")
        self.assertEqual(field.apply({"k": "return"}, 3), "submit")
        self.assertEqual(field.apply({"k": "esc"}, 3), "close")
        field.apply({"k": "text", "s": "x" * 100}, 0)
        self.assertEqual(len(field.query), launcher.MAX_QUERY)
        field.reset()
        for key in ("right", "right", "left", "down"):
            field.apply({"k": key}, 0)
        self.assertEqual((field.page_step, field.selected), (1, 0), "with an empty field the arrows turn pages")
        field.apply({"k": "text", "s": "\x00\x1b"}, 0)
        self.assertEqual(field.query, "  ")

    def test_layout_file_round_trip_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "layout.json"
            self.assertIsNone(launcher.load_layout(path))
            launcher.save_layout(path, catalog()["items"])
            self.assertEqual(launcher.load_layout(path), catalog()["items"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("{")
            self.assertIsNone(launcher.load_layout(path))
            path.write_text('{"version": 2, "items": []}')
            self.assertIsNone(launcher.load_layout(path))


class Launcher(unittest.TestCase):
    def setUp(self):
        self.log = []
        frame_spatial_apps.sink = self.log.append
        self.addCleanup(setattr, frame_spatial_apps, "sink", None)
        self.mv = FakeMacView()
        self.mv.catalog = catalog()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Path(self.temp.name) / "layout.json"
        self.apps = SpatialApps(self.mv, layout_path=self.layout)
        self.apps.refresh()

    def launcher_state(self):
        return self.apps.state["launcher"]

    def open(self):
        self.assertTrue(self.apps.handle_request({"action": "launcher", "verb": "open"}))

    def test_publication_and_helper_without_a_catalogue(self):
        state = self.launcher_state()
        self.assertEqual({k: state[k] for k in ("available", "open", "query", "selected", "pageStep", "keyboard")},
                         {"available": True, "open": False, "query": "", "selected": 0, "pageStep": 0,
                          "keyboard": False})
        self.assertNotIn("results", state)
        self.mv.missing = []
        self.apps.sync()
        commands = [r[0] for r in self.mv.remote]
        self.assertEqual(commands[:2], ["sync", "launcher"])
        published = json.loads(self.mv.remote[1][1])
        self.assertEqual(published["catalog"], state["catalog"])
        self.assertEqual(published["apps"][3], {"id": ECRAN, "name": "Écran Partagé", "v": "1-" + ECRAN})
        self.assertEqual(published["items"][2]["apps"], [CHESS])
        self.assertEqual(published["recent"], [PHOTOS, TERMINAL])
        older = FakeMacView()
        apps = SpatialApps(older, layout_path=self.layout)
        apps.refresh()
        self.assertNotIn("launcher", apps.state, "an older helper shows no Applications button")
        self.assertEqual(len(apps.state["apps"]), 3, "and the shelf still works")

    def test_catalogue_is_published_when_the_frame_lacks_it_and_icons_once_by_version(self):
        def run(command, **kwargs):
            args = shlex.split(command)[2:]
            self.mv.remote.append(args)
            if args[0] == "sync":
                return "SPATIAL_ICONS_MISSING []\nLAUNCHER_CATALOG " + json.dumps(frame_catalog)
            if args[0] == "launcher":
                return "LAUNCHER_ICONS_MISSING " + json.dumps(missing)
            return ""
        self.mv.run = run
        frame_catalog, missing = None, [TERMINAL, PHOTOS, SAFARI, CHESS]
        self.apps.sync()
        launches = [r for r in self.mv.remote if r[0] == "launcher"]
        self.assertEqual(len(launches), 1)
        icons = [r for r in self.mv.remote if r[0] == "icons"]
        self.assertEqual(icons[0][1::3], [PHOTOS, TERMINAL, SAFARI, CHESS], "recent apps first, then the grid's order")
        self.assertEqual(icons[0][2::3], ["v-" + b for b in (PHOTOS, TERMINAL, SAFARI, CHESS)])
        frame_catalog = self.launcher_state()["catalog"]
        self.mv.remote.clear()
        self.apps.sync()
        self.assertEqual([r[0] for r in self.mv.remote], ["sync"], "nothing resent while the Frame is current")
        frame_catalog = None  # the Frame's runtime folder was emptied
        self.apps.sync()
        self.assertIn("launcher", [r[0] for r in self.mv.remote])

    def test_icon_batches_fit_one_command_line(self):
        big = base64.b64encode(png() + b"\0" * 30000).decode()
        self.mv.catalog = catalog()
        self.apps._icons = {b: (big, "1-" + b) for b in (SAFARI, MAIL, CHESS, ECRAN, PHOTOS, BOOTH, TERMINAL, NOTES)}
        self.apps._launcher_icons = list(self.apps._icons)
        self.apps.sync()
        batches = [r for r in self.mv.remote if r[0] == "icons"]
        self.assertEqual(len(batches), 1)
        self.assertLessEqual(sum(len(x) for x in batches[0][3::3]), 90000)
        self.assertEqual(len(self.apps._launcher_icons), 8 - len(batches[0][1::3]), "the rest follow on later syncs")

    def test_open_takes_the_keyboard_and_typing_filters_and_return_opens(self):
        self.open()
        state = self.launcher_state()
        self.assertTrue(state["open"] and state["keyboard"])
        self.assertIn(("/launcher/keys", "POST", {"verb": "open"}), self.mv.calls)
        seq = state["seq"]
        self.mv.type_keys({"k": "text", "s": "PH"}, {"k": "text", "s": "o"})
        self.apps.poll_launcher()
        state = self.launcher_state()
        self.assertEqual((state["query"], state["results"]), ("PHo", [PHOTOS, BOOTH]))
        self.mv.type_keys({"k": "right"})
        self.apps.poll_launcher()
        self.assertEqual(self.launcher_state()["selected"], 1)
        self.mv.type_keys({"k": "return"}, {"k": "text", "s": "ignored"})
        self.apps.poll_launcher()
        self.assertIn(("/apps/action", "POST", {"bundleID": BOOTH, "verb": "open"}), self.mv.calls)
        state = self.launcher_state()
        self.assertFalse(state["open"])
        self.assertGreater(state["seq"], seq)
        self.assertEqual(state["query"], "")
        self.assertIn(("/launcher/keys", "POST", {"verb": "close"}), self.mv.calls)
        self.assertFalse(self.mv.keys["active"], "the keyboard goes back to the Mac")

    def test_esc_and_headset_close_release_the_keyboard(self):
        self.open()
        self.mv.type_keys({"k": "text", "s": "x"}, {"k": "esc"})
        self.apps.poll_launcher()
        self.assertFalse(self.launcher_state()["open"])
        self.assertFalse(self.mv.keys["active"])
        self.open()
        self.assertTrue(self.mv.keys["active"])
        self.assertEqual(self.launcher_state()["query"], "", "a reopened field is empty")
        self.apps.handle_request({"action": "launcher", "verb": "close"})
        self.assertFalse(self.mv.keys["active"])
        self.assertFalse(self.launcher_state()["open"])

    def test_stop_desktop_and_disconnect_release_the_keyboard(self):
        for leave in ("desktop", "stop", "restart"):
            with self.subTest(leave=leave):
                self.mv.mode, self.mv.exit_pending = "spatial", False
                self.apps.refresh()
                self.open()
                self.assertTrue(self.mv.keys["active"])
                if leave == "desktop":
                    self.mv.mode = "desktop"
                    self.apps.refresh()
                    self.assertFalse(self.mv.keys["active"])
                elif leave == "stop":
                    self.mv.exit_pending = True
                    self.apps.refresh(False)
                    self.assertFalse(self.mv.keys["active"])
                else:
                    # A restarted helper has no lease; the companion asks again.
                    self.mv.agent = FakeAgent()
                    self.mv.keys["active"] = False
                    self.apps.refresh()
                    self.assertTrue(self.mv.keys["active"] and self.apps._lease)
                    self.apps.handle_request({"action": "launcher", "verb": "close"})
                self.assertFalse(self.apps.launcher_open)
        # A helper whose lease lapsed (the companion stalled) is asked again.
        self.apps.refresh()
        self.open()
        self.mv.keys["active"] = False
        self.apps.poll_launcher()
        self.assertFalse(self.apps._lease)
        self.apps.refresh()
        self.assertTrue(self.apps._lease)

    def test_without_a_tap_the_launcher_still_opens(self):
        self.mv.keys["tap"] = False
        self.open()
        self.assertTrue(self.launcher_state()["open"])
        self.assertFalse(self.launcher_state()["keyboard"])

    def test_requests_open_catalogue_apps_and_save_moves(self):
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": CHESS})
        self.assertIn(("/apps/action", "POST", {"bundleID": CHESS, "verb": "open"}), self.mv.calls)
        count = len(self.mv.calls)
        self.apps.handle_request({"action": "app", "verb": "quit", "bundleID": CHESS})
        self.apps.handle_request({"action": "app", "verb": "open", "bundleID": "com.unknown"})
        self.assertEqual(len(self.mv.calls), count, "only opening reaches apps that are not on the shelf")
        seq = self.launcher_state()["catalog"]
        self.apps.handle_request({"action": "launcher", "verb": "move", "id": TERMINAL, "before": SAFARI})
        self.assertEqual(self.apps.catalog["items"][0], {"app": TERMINAL})
        self.assertGreater(self.launcher_state()["catalog"], seq)
        self.assertEqual(launcher.load_layout(self.layout)[0], {"app": TERMINAL})
        self.apps.handle_request({"action": "launcher", "verb": "move", "id": CHESS, "before": None, "folder": "folder-1"})
        for bad in ({"id": "../x"}, {"id": TERMINAL, "before": 3}, {"id": TERMINAL, "folder": "../f"},
                    {"id": TERMINAL, "before": TERMINAL}, {"id": "com.gone"}):
            before = self.layout.read_text()
            self.apps.handle_request({"action": "launcher", "verb": "move", **bad})
            self.assertEqual(self.layout.read_text(), before, bad)
        # The saved arrangement wins over the helper's order on the next read.
        self.apps._catalog_at = None
        self.apps.refresh()
        self.assertEqual(self.apps.catalog["items"][0], {"app": TERMINAL})
        self.mv.catalog["apps"].append({"bundleID": "com.example.new", "name": "Aardvark", "version": "1"})
        self.mv.catalog["items"].append({"app": "com.example.new"})
        self.apps._catalog_at = None
        self.apps.refresh()
        self.assertEqual(self.apps.catalog["items"][-1], {"app": "com.example.new"}, "new apps join at the end")

    def test_launcher_state_passes_the_frame_writer(self):
        self.open()
        self.mv.type_keys({"k": "text", "s": "é"})
        self.apps.poll_launcher()
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_RUNTIME_DIR": tmp}
            def write(*args):
                return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True,
                                      text=True, timeout=10)
            result = write("sync", json.dumps(self.apps.state))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LAUNCHER_CATALOG null", result.stdout)
            result = write("launcher", self.apps._catalog_json())
            self.assertEqual(result.returncode, 0, result.stderr)
            missing = json.loads(result.stdout.split("LAUNCHER_ICONS_MISSING ", 1)[1])
            self.assertEqual(set(missing), {a["bundleID"] for a in self.apps.catalog["apps"]})
            runtime = Path(tmp) / "frame-workspace"
            self.assertEqual(json.loads((runtime / "launcher.json").read_text())["catalog"], self.apps.catalog_seq)
            self.assertIn("LAUNCHER_CATALOG %d" % self.apps.catalog_seq, write("sync", json.dumps(self.apps.state)).stdout)
            encoded = base64.b64encode(png()).decode()
            result = write("icons", SAFARI, "1-" + SAFARI, encoded, CHESS, "old", encoded)
            self.assertEqual(result.returncode, 0, result.stderr)
            missing = json.loads(write("launcher", self.apps._catalog_json()).stdout.split(" ", 1)[1])
            self.assertNotIn(SAFARI, missing)
            self.assertIn(CHESS, missing, "an icon from an older version is sent again")
            self.assertEqual((runtime / "icons" / (SAFARI + ".png")).read_bytes(), png())
            bad = copy.deepcopy(self.apps.state)
            for field, value in (("open", 1), ("seq", -1), ("query", "x" * 300), ("results", ["../x"]),
                                 ("results", [SAFARI, SAFARI]), ("available", False)):
                broken = copy.deepcopy(bad)
                broken["launcher"][field] = value
                self.assertEqual(write("sync", json.dumps(broken)).returncode, 2, field)
            catalogue = json.loads(self.apps._catalog_json())
            for change in (lambda c: c["items"].append({"app": SAFARI}),
                           lambda c: c["items"].append({"app": "com.unknown"}),
                           lambda c: c["items"].append({"folder": "../x", "name": "X", "apps": []}),
                           lambda c: c["apps"].append({"id": "../x", "name": "X", "v": ""}),
                           lambda c: c.update(catalog=-1)):
                broken = copy.deepcopy(catalogue)
                change(broken)
                self.assertEqual(write("launcher", json.dumps(broken)).returncode, 2)
            self.assertEqual(write("icons", "../x", "1", encoded).returncode, 2)
            self.assertEqual(write("icons", SAFARI, "1").returncode, 2)
            self.assertEqual(write("clear").returncode, 0)
            self.assertFalse((runtime / "launcher.json").exists())


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Swift contract check needs macOS")
class SwiftContract(unittest.TestCase):
    def test_companion_entries_pass_helper_parser_and_release_contract(self):
        mv = FakeMacView()
        apps = SpatialApps(mv)
        apps.refresh()
        entries = mv.calls[-1][2]["entries"]
        with tempfile.TemporaryDirectory(prefix="spatial-contract-") as tmp:
            source = Path(tmp) / "Contract.swift"
            binary = Path(tmp) / "contract"
            source.write_text('''import Foundation
@main struct Contract {
    static func main() throws {
        let panels = try SwitcherPanel.parseEntries(CommandLine.arguments[1])
        let state = SwitcherState()
        state.seedMRU(panels.map(\\.bundleID))
        state.enableSpatial(entries: panels, at: 1)
        _ = state.cycle(reverse: false)
        print(String(data: try JSONSerialization.data(withJSONObject: state.snapshot().json), encoding: .utf8)!)
        state.commandReleased()
        print(String(data: try JSONSerialization.data(withJSONObject: state.snapshot().json), encoding: .utf8)!)
    }
}
''')
            build = subprocess.run(["xcrun", "swiftc", "-swift-version", "5",
                                    str(ROOT / "mac/frame-mac-view/Sources/AppsState.swift"), str(source),
                                    "-o", str(binary)], capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary), entries], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            opened, released = map(json.loads, result.stdout.splitlines())
            self.assertEqual(opened["windows"], [KEY1, KEY2])
            self.assertTrue(opened["open"])
            self.assertIsNone(opened["chosen"])
            self.assertEqual(opened["index"], 1)
            self.assertFalse(released["open"])
            self.assertEqual(released["chosen"], KEY2)
            self.assertGreater(released["seq"], opened["seq"])

    def test_helper_catalogue_and_key_events_pass_the_companion(self):
        with tempfile.TemporaryDirectory(prefix="launcher-contract-") as tmp:
            source = Path(tmp) / "Contract.swift"
            binary = Path(tmp) / "contract"
            source.write_text('''import Foundation
@main struct Contract {
    static func main() throws {
        let apps = [LauncherApp(bundleID: "com.apple.Safari", name: "Safari", path: "/A/Safari.app", version: "1-2"),
                    LauncherApp(bundleID: "com.apple.Chess", name: "Échecs", path: "/A/Chess.app", version: "3")]
        let items = LauncherCatalog.merge(launchpad: [.folder(name: "Games", apps: ["com.apple.Chess"])], apps: apps)
        let catalog = LauncherCatalog.json(apps: apps, items: items, recent: ["com.apple.Chess"], launchpad: "imported")
        print(String(data: try JSONSerialization.data(withJSONObject: catalog), encoding: .utf8)!)
        let lease = LauncherKeyLease()
        lease.open(at: 0)
        for (code, text) in [(UInt16(0x0E), "é"), (0x08, "c"), (0x33, ""), (0x7C, ""), (0x24, "\\r"), (0x35, "")] {
            _ = lease.handle(.down, code: code, command: false, control: false, text: text)
        }
        print(String(data: try JSONSerialization.data(withJSONObject: lease.snapshot(after: 0).json), encoding: .utf8)!)
    }
}
''')
            sources = ROOT / "mac/frame-mac-view/Sources"
            build = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", str(sources / "AppsState.swift"),
                                    str(sources / "LauncherCatalog.swift"), str(sources / "LauncherKeys.swift"),
                                    str(source), "-lsqlite3", "-o", str(binary)], capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            raw_catalog, raw_keys = map(json.loads, result.stdout.splitlines())
            catalog = launcher.sanitize_catalog(raw_catalog)
            self.assertEqual(catalog["items"], [{"folder": "folder-1", "name": "Games", "apps": ["com.apple.Chess"]},
                                                {"app": "com.apple.Safari"}])
            self.assertEqual(catalog["recent"], ["com.apple.Chess"])
            self.assertEqual(catalog["apps"][1], {"bundleID": "com.apple.Chess", "name": "Échecs", "version": "3"})
            field, actions = launcher.SearchField(), []
            for event in raw_keys["events"]:
                actions.append(field.apply(event, 1))
            self.assertEqual(field.query, "é")
            self.assertEqual(actions[-2:], ["submit", "close"])
            self.assertEqual(raw_keys["last"], 6)
            self.assertTrue(raw_keys["active"])


if __name__ == "__main__":
    unittest.main()
