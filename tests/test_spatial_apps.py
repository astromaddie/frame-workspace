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

    def call(self, path, method="GET", **query):
        self.calls.append((path, method, query))
        if path == "/apps":
            return {"apps": copy.deepcopy(self.apps)}
        if path == "/windows":
            return {"windows": copy.deepcopy(self.windows)}
        if path == "/switcher":
            return copy.deepcopy(self.switcher)
        if path == "/apps/action" and self.action_error:
            raise frame_macview.MacViewError("The Mac Streaming Helper: " + self.action_error)
        if path == "/apps/icon":
            if self.icon_fail:
                raise ValueError("icon failed")
            return {"png": base64.b64encode(png()).decode()}
        return {}

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
        self.mv.switcher.update(open=True, index=1, seq=1)
        self.apps.poll_switcher()
        self.assertTrue(self.apps.wake.is_set())
        self.apps.sync()
        sent = json.loads(self.mv.remote[0][1])
        self.assertEqual(sent["switcher"]["index"], 1)
        self.mv.switcher.update(open=False, chosen=KEY2, seq=2)
        self.apps.poll_switcher()
        self.assertEqual(self.apps.state["switcher"]["chosen"], KEY2)
        self.assertFalse(self.apps.state["switcher"]["open"])

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


if __name__ == "__main__":
    unittest.main()
