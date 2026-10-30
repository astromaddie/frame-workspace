"""Mac in the headset (ui/frame_macview.py and the frame-mac-view agent).

The Python checks run anywhere. On macOS the agent is built and driven over
HTTP and WebSocket with its test pattern, which needs no Screen Recording
permission: status, the token, the viewer page, H.264 keyframes, pointer
input reaching the source, and closing.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import base64
import http.client
import json
import math
import io
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_macview  # noqa: E402


class Helpers(unittest.TestCase):
    def test_helper_cleanup_error_reaches_the_companion(self):
        mv = frame_macview.MacView([], mock.Mock(), "fake")
        mv.ensure_agent = mock.Mock()
        mv.port = 12345
        failure = frame_macview.urllib.error.HTTPError("http://127.0.0.1", 500, "Failure", {},
            io.BytesIO(b'{"state":"error","removed":false,"error":"Display Removal Is Pending"}'))
        with mock.patch.object(frame_macview.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaisesRegex(frame_macview.MacViewError, "Display Removal Is Pending"):
                mv.call("/spatial/stop", method="POST")

    def test_diagnostic_gaze_is_canonical_before_launch(self):
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_FOVEATION": "1", "FRAME_MAC_VIEW_DIAGNOSTIC_GAZE": "1", "FRAME_WORKSPACE_DIAGNOSTIC_GAZE": " 0.5 , 1e-1 "}):
            value = frame_macview.workspace_diagnostic_gaze()
            self.assertNotIn(" ", value)
            self.assertEqual(list(map(float, value.split(","))), [0.5, 0.1])

    def test_tunnel_follows_usb_in_and_falls_back_when_pulled(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")

        class Tunnel:
            def __init__(self): self.alive = True
            def poll(self): return None if self.alive else 0
            def terminate(self): self.alive = False
            def wait(self, timeout=None): return 0

        opened = []

        def ensure(allow_new_port=False):
            mv.tunnel = Tunnel()
            mv.route = "usb" if mv._usb_route() else "network"
            opened.append(mv.route)

        usb = {"present": True}
        mv.ensure_tunnel = ensure
        mv._usb_route = lambda: ["-o", "HostName=10.86.200.233"] if usb["present"] else []
        mv._usb_reachable = lambda: usb["present"]
        mv.tunnel, mv.route = Tunnel(), "network"
        mv._follow_best_route(tick=3)  # USB is only looked for every 5 s
        self.assertEqual(opened, [])
        mv._follow_best_route(tick=5)
        self.assertEqual(opened, ["usb"])
        usb["present"] = False
        mv._follow_best_route(tick=6)  # one miss is tolerated
        self.assertEqual(opened, ["usb"])
        mv._follow_best_route(tick=7)
        self.assertEqual(opened, ["usb", "network"])
        self.assertEqual(mv.route, "network")

    def test_link_cap_follows_route_and_agent(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        calls = []
        mv.call = lambda path, method="GET", **q: calls.append((path, q["maxbps"]))
        mv.agent, mv.route = object(), "network"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRAME_MACVIEW_WIFI_MBPS", None)
            mv._apply_link_cap()
            mv._apply_link_cap()  # unchanged: not resent
            mv.route = "usb"
            mv._apply_link_cap()
            mv.agent = object()  # a restarted agent starts uncapped
            mv.route = "network"
            mv._apply_link_cap()
        self.assertEqual(calls, [("/link", "12000000"), ("/link", "0"), ("/link", "12000000")])

    def test_frame_exit_helper_is_installed_once_with_private_executable_mode(self):
        calls = []
        mv = frame_macview.MacView(["ssh"], lambda remote, **kwargs: calls.append((remote, kwargs)) or "", "frame")

        mv._install_frame_exit_helper()
        mv._install_frame_exit_helper()

        self.assertEqual(len(calls), 1)
        self.assertIn("chmod 700", calls[0][0])
        self.assertIn("adopt --session", calls[0][0])
        self.assertIn("workspace-exit-on-frame.py", calls[0][0])
        self.assertIn('def retire_and_stop', calls[0][1]["stdin"])
        self.assertEqual(mv._exit_helper_frame, mv.frame)

    def test_frame_exit_helper_install_does_not_cache_a_retargeted_frame(self):
        mv = frame_macview.MacView(["ssh"], lambda remote, **kwargs: setattr(mv, "frame", "other") or "", "frame")

        with self.assertRaisesRegex(frame_macview.MacViewError, "connection changed"):
            mv._install_frame_exit_helper()

        self.assertIsNone(mv._exit_helper_frame)

    def test_frame_owned_orphan_enables_existing_stop_button(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager.state = lambda: {"state": "stopped"}
        mv._exit_helper_frame = mv.frame
        mv.run = mock.Mock(return_value='{"stopAvailable":true}')

        mv._frame_stop_available = mv._probe_frame_stop_available()

        self.assertTrue(mv.dash_state()["workspace"]["stopAvailable"])
        self.assertIn(" status", mv.run.call_args.args[0])

    def test_window_panel_opens_beside_the_workspace_facing_the_viewer(self):
        workspace = [1, 0, 0, 0, 0, 1, 0, 1.3, 0, 0, 1, -2]  # 2 m ahead of a viewer at (0, 1.3, 0)
        viewer = [0, 1.3, 0]
        sides = []
        for slot in range(3):
            pose = json.loads(frame_macview.beside_workspace_pose(workspace, 2.4, 2.0, 2880, 1200, slot))
            m = pose["matrix"]
            centre, normal = [m[3], m[7], m[11]], [m[2], m[6], m[10]]
            self.assertAlmostEqual(pose["width"], 1.0, places=3)  # same scale as the Workspace
            self.assertAlmostEqual(math.dist(centre, viewer), 2.0, places=3)
            to_viewer = [v - c for v, c in zip(viewer, centre)]
            self.assertAlmostEqual(sum(a * b for a, b in zip(normal, to_viewer)) / 2.0, 1.0, places=4)
            for a in range(3):  # orthonormal rotation
                for b in range(3):
                    dot = sum(m[r * 4 + a] * m[r * 4 + b] for r in range(3))
                    self.assertAlmostEqual(dot, 1.0 if a == b else 0.0, places=4)
            # Seen from the viewer, the panel's inner edge is past the
            # Workspace's edge (no overlap).
            side = [m[0], m[4], m[8]]
            sign = 1 if slot % 2 == 0 else -1
            inner = [c - sign * s * pose["width"] / 2 for c, s in zip(centre, side)]
            sides.append(sign * math.atan2(inner[0], -inner[2]))
        edge = math.atan2(1.2, 2.0)
        self.assertGreater(sides[0], edge)  # right of the Workspace
        self.assertGreater(sides[1], edge)  # left of it
        self.assertGreater(sides[2], sides[0])  # further right

    def test_native_window_show_uses_own_panel_at_window_size(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        runs = []

        def run(cmd, **kwargs):
            runs.append(cmd)
            if cmd.startswith("cat "):
                return json.dumps({"ready": True, "placement": {"width": 2.4, "distance": 2.0,
                                                                "matrix": [1, 0, 0, 0, 0, 1, 0, 1.3, 0, 0, 1, -2]}})
            return "Native Workspace ready: registered OpenVR key com.frame-workspace.native.1000.panel-w42 in unit"
        mv.run = run
        mv.call = lambda path, **kw: {"windows": [{"src": "window:42", "title": "Terminal\tzsh", "w": 1200, "h": 800}]}
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_DECODER": "v4l2",
                                          "FRAME_WINDOW_DECODER": "v4l2",
                                          "FRAME_WORKSPACE_CONVERSION": "gpu"}):
            mv._show_native_window("window:42", "http://127.0.0.1:1/view?x", "fc12345678", 1200, 800)
        self.assertTrue(runs[-1].startswith("bash -s -- "))
        args = shlex.split(runs[-1])[3:]
        self.assertEqual(args[:4], ["--panel", "w42", "--name", "Terminal zsh"])
        # Its size in points (no bundle ID in this listing) sizes the panel on the Frame.
        self.assertEqual(args[4:6], ["--points", "1200x800"])
        self.assertEqual(args[6], "--initial-pose")
        self.assertEqual(json.loads(args[7])["width"], 1.0)
        self.assertEqual(args[8:10], ["start", "http://127.0.0.1:1/view?x"])
        self.assertEqual(args[-2:], ["v4l2", "gpu"])
        self.assertEqual(mv.native_panels["window:42"]["key"], "com.frame-workspace.native.1000.panel-w42")
        self.assertIs(mv.native_panels["window:42"]["host"], False)

    def test_native_window_defaults_software_even_when_workspace_uses_v4l2(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.run = mock.Mock(return_value="registered OpenVR key com.frame-workspace.native.1000.panel-w42")
        mv.call = mock.Mock(return_value={"windows": [{"src": "window:42", "title": "Terminal", "w": 1200, "h": 800}]})
        mv._workspace_frame_placement = mock.Mock(return_value=None)
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_DECODER": "v4l2",
                                          "FRAME_WORKSPACE_CONVERSION": "gpu"}):
            os.environ.pop("FRAME_WINDOW_DECODER", None)
            mv._show_native_window("window:42", "http://127.0.0.1:1/view?x", "fc12345678", 1200, 800)
        args = shlex.split(mv.run.call_args.args[0])
        self.assertEqual(args[-2:], ["software", "gpu"])

    def test_native_host_is_opt_in_and_stop_reuses_recorded_host_selection(self):
        mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        commands = []
        mv.run = lambda command, **kwargs: commands.append(command) or \
            "Native Workspace ready: registered OpenVR key com.frame-workspace.native.1000.panel-w42"
        mv.call = lambda path, **kwargs: {"windows": [{"src": "window:42", "title": "Terminal", "w": 1200, "h": 800}]}
        mv._workspace_frame_placement = lambda: None
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native", "FRAME_NATIVE_HOST": "1"}):
            mv._show_native_window("window:42", "http://127.0.0.1:1/view?x", "fc12345678", 1200, 800)
        self.assertTrue(commands[0].startswith("FRAME_NATIVE_HOST=1 bash -s -- "))
        self.assertIs(mv.native_panels["window:42"]["host"], True)
        self.assertEqual(mv.native_panels["window:42"]["generation"], "fc12345678")

        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}, clear=False):
            os.environ.pop("FRAME_NATIVE_HOST", None)
            mv._stop_native_panels("window:42")
        self.assertTrue(commands[1].startswith("FRAME_NATIVE_HOST=1 bash -s -- "))
        stop_args = shlex.split(commands[1])[4:]
        self.assertEqual(stop_args, ["--panel", "w42", "--generation", "fc12345678", "stop"])
        self.assertNotIn("window:42", mv.native_panels)

    def test_stop_closes_companion_stream_before_removing_host_panel_spec(self):
        mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        order = []
        finished = threading.Event()

        class Agent:
            def poll(self): return None

        mv.agent = Agent()
        mv.shown = {"window:42", "window:99"}
        mv.native_panels["window:42"] = {"panel": "w42", "key": "com.frame-workspace.native.1000.panel-w42",
                                         "host": True, "generation": "fc12345678"}

        def close(path, method="GET", **kwargs):
            order.append(("server", path, method))
            return {"closed": 1}

        def remove_spec(command, **kwargs):
            order.append(("frame", command))
            finished.set()
            return "Stopped native panel"

        mv.call = close
        mv.run = remove_spec
        self.assertEqual(mv.stop("window:42"), {"closed": 1})
        self.assertTrue(finished.wait(2), "native host spec removal should follow the server close response")
        self.assertEqual(order[0], ("server", "/close", "POST"))
        self.assertTrue(order[1][1].startswith("FRAME_NATIVE_HOST=1 bash -s -- "))
        self.assertEqual(shlex.split(order[1][1])[4:],
                         ["--panel", "w42", "--generation", "fc12345678", "stop"])
        self.assertNotIn("window:42", mv.native_panels)

    def test_stop_removes_host_spec_when_companion_server_is_already_closed(self):
        mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        commands = []

        class ExitedAgent:
            def poll(self): return 1

        mv.agent = ExitedAgent()
        mv.shown = {"window:42", "window:99"}
        mv.native_panels["window:42"] = {"panel": "w42", "key": "com.frame-workspace.native.1000.panel-w42",
                                         "host": True, "generation": "fc12345678"}
        mv.call = mock.Mock(side_effect=AssertionError("a closed companion has no HTTP server"))
        mv.run = lambda command, **kwargs: commands.append(command) or "Stopped native panel"
        self.assertEqual(mv.stop("window:42"), {"closed": 0})
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("FRAME_NATIVE_HOST=1 bash -s -- "))
        self.assertEqual(shlex.split(commands[0])[4:],
                         ["--panel", "w42", "--generation", "fc12345678", "stop"])
        mv.call.assert_not_called()
        self.assertNotIn("window:42", mv.native_panels)

    def test_slow_host_stop_does_not_remove_a_newer_panel_record(self):
        mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        old = {"panel": "w42", "host": True, "generation": "fc12345678"}
        replacement = {"panel": "w42", "host": True, "generation": "fc87654321"}
        mv.native_panels["window:42"] = old

        def slow_stop(command, **kwargs):
            mv.native_panels["window:42"] = replacement
            return "Stopped native panel"

        mv.run = slow_stop
        mv._stop_native_panels("window:42")
        self.assertIs(mv.native_panels["window:42"], replacement)

    def test_native_window_saved_pose_overrides_beside_workspace_placement(self):
        mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        pose = {"version": 1, "width": 1.5, "distance": 3, "curvature": .7,
                "matrix": [0, 0, 1, 3, 0, 1, 0, 1, -1, 0, 0, -2]}
        mv.run = mock.Mock(return_value="registered OpenVR key com.frame-workspace.native.1000.panel-w42")
        mv.call = mock.Mock(return_value={"windows": [{"src": "window:42", "title": "Terminal"}]})
        mv._workspace_frame_placement = mock.Mock()
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv._show_native_window("window:42", "http://127.0.0.1:1/view?x", "fc12345678", 1200, 800, pose=pose)
        args = shlex.split(mv.run.call_args.args[0])[3:]
        self.assertEqual(args[4], "--pose")
        self.assertEqual(json.loads(args[5]), pose)
        self.assertNotIn("--initial-pose", args)
        mv._workspace_frame_placement.assert_not_called()

    def test_invalid_or_unsupported_saved_pose_fails_before_viewer_side_effects(self):
        mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        mv.call = mock.Mock()
        mv.build = mock.Mock()
        mv._remember_workspace = mock.Mock()
        with self.assertRaises(frame_macview.MacViewError):
            mv.show("window:42", pose={})
        with self.assertRaises(frame_macview.MacViewError):
            mv.start_workspace(pose={})
        pose = {"version": 1, "width": 1.5, "distance": 3, "curvature": .7,
                "matrix": [1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, -2]}
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "browser"}):
            with self.assertRaisesRegex(frame_macview.MacViewError, "native"):
                mv.show("window:42", pose=pose)
            with self.assertRaisesRegex(frame_macview.MacViewError, "native"):
                mv.start_workspace(pose=pose)
        mv.call.assert_not_called()
        mv.run.assert_not_called()
        mv.build.assert_not_called()
        mv._remember_workspace.assert_not_called()

    def test_pose_validation_keeps_an_optional_bend(self):
        pose = {"version": 1, "width": 2.4, "distance": 2.0, "curvature": 0.0,
                "matrix": [1, 0, 0, 0, 0, 1, 0, 1.3, 0, 0, 1, -2]}
        self.assertNotIn("bend", frame_macview.validate_pose(pose))
        bent = {**pose, "bend": {"vertical": True, "a": 0.1, "b": 0.4}}
        self.assertEqual(frame_macview.validate_pose(bent)["bend"], {"vertical": True, "a": 0.1, "b": 0.4})
        for bad in ({"vertical": "yes", "a": 0, "b": 0}, {"vertical": False, "a": 2, "b": 0},
                    {"vertical": False, "a": 0}, [0, 0]):
            with self.assertRaises(frame_macview.MacViewError):
                frame_macview.validate_pose({**pose, "bend": bad})

    def test_pose_capture_reader_uses_xdg_files_and_bounds_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "frame-workspace"
            (base / "panels").mkdir(parents=True)
            pose = {"version": 1, "width": 1.5, "distance": 3, "curvature": .7,
                    "matrix": [1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, -2]}
            path = base / "panels" / "w42-pose.json"
            # A window's pose as the Frame writes it once its user placed it.
            path.write_text(json.dumps({**pose, "userSet": True, "bundleID": "com.apple.Terminal", "title": "zsh",
                                        "session": "s" * 32, "savedAt": 1}))
            (base / "panels" / "w7-pose.json").write_text(json.dumps(pose))  # no provenance: not the user's
            (base / "workspace-pose.json").write_text(json.dumps(pose))
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")

            def local_reader(cmd, stdin, timeout):
                result = subprocess.run([sys.executable, *shlex.split(cmd)[1:]], input=stdin,
                                        env={**os.environ, "XDG_DATA_HOME": str(root)},
                                        text=True, capture_output=True, timeout=timeout, check=True)
                return result.stdout

            mv.run.side_effect = local_reader
            user = {**pose, "userSet": True}
            self.assertEqual(mv.read_panel_poses(["workspace", "window:42", "window:7"]),
                             {"workspace": pose, "window:42": user})
            self.assertEqual(mv.read_panel_poses(["spatial:42"]), {"spatial:42": user})
            path.write_text(" " * 4097)
            with self.assertRaisesRegex(frame_macview.MacViewError, "capture saved native panel poses"):
                mv.read_panel_poses(["window:42"])
            mv.run.reset_mock()
            for source in ("window:42;touch bad", *frame_macview.SPATIAL_AUX_ROLES):
                with self.assertRaisesRegex(frame_macview.MacViewError, "Invalid native panel source"):
                    mv.read_panel_poses([source])
            mv.run.assert_not_called()

    def test_headset_close_requests_close_only_known_panels(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        mv.native_panels = {"window:42": {"panel": "w42", "key": "com.frame-workspace.native.1000.panel-w42", "slot": 0}}
        stopped, workspace = [], []
        mv.stop = lambda src=None: stopped.append(src)
        mv.stop_workspace = lambda: workspace.append(True)
        lines = [
            '{"action":"close","key":"com.frame-workspace.native.1000.panel-w42"}',
            '{"action":"close","key":"com.frame-workspace.native.1000.panel-w7"}',  # not ours
            '{"action":"rm -rf","key":"com.frame-workspace.native.1000.panel-w42"}',  # unknown action
            'not json',
            '{"action":"close","key":"com.frame-workspace.native.1000.workspace"}',
        ]
        mv._handle_frame_requests("\n".join(lines))
        mv.dash_jobs.join()
        self.assertEqual(stopped, ["window:42"])
        self.assertEqual(workspace, [True])

    def test_bar_panel_requests_start_stop_and_recenter_the_workspace(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        started, release = threading.Event(), threading.Event()
        calls = []
        mv._dash_run("starting", lambda: (started.set(), release.wait(2)))
        self.assertTrue(started.wait(1))
        mv._dash_run("starting", lambda: calls.append(("start",)))
        mv._dash_run(None, lambda: calls.append(("recenter",)))
        mv._stop_workspace_session = lambda: calls.append(("stop",))
        mv._handle_frame_requests(json.dumps({"action": "workspace_stop", "session": mv.session_id}))
        self.assertTrue(mv.exit_pending)
        release.set()
        mv.dash_jobs.join()
        self.assertEqual(calls, [("stop",)])
        self.assertTrue(mv.exit_pending)

    def test_steam_library_start_request_is_bounded_and_expired_requests_are_ignored(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.last_workspace = ("2880x2560", 1, "sharp")
        mv.mode = "spatial"
        calls = []
        mv.start_workspace = lambda *a, **k: calls.append((mv.mode, a, k))
        now = time.time()
        request = {"action": "workspace_start", "source": "steam-library", "expiresAt": now + 10}
        expired = {**request, "expiresAt": now - 1}
        far_future = {**request, "expiresAt": now + 60}
        with mock.patch.object(frame_macview.time, "time", return_value=now):
            mv._handle_frame_requests("\n".join(map(json.dumps, (expired, far_future, request))))
        mv.dash_jobs.join()
        self.assertEqual(calls, [("spatial", ("2880x2560", 1, "sharp"), {"resume": True})])

    def test_bar_panel_state_lists_the_workspace_and_windows(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager.state = lambda: {"state": "running", "preset": "2880x2560"}
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        mv.native_panels = {
            "window:9": {"panel": "w9", "key": "com.frame-workspace.native.1000.panel-w9", "slot": 1, "title": "Notes"},
            "window:4": {"panel": "w4", "key": "com.frame-workspace.native.1000.panel-w4", "slot": 0, "title": "Terminal"},
        }
        state = mv.dash_state()
        self.assertEqual(state["workspace"], {"running": True, "busy": None, "label": "9:8 · 2880 × 2560",
                                               "mode": "desktop", "stopAvailable": True,
                                               "session": mv.session_id})
        self.assertEqual([w["title"] for w in state["windows"]], ["Terminal", "Notes"])
        mv.workspace_manager.state = lambda: {"state": "stopped"}
        mv.workspace_panel_key = None
        self.assertFalse(mv.dash_state()["workspace"]["running"])

    def test_completed_stop_rotates_the_session_for_a_later_start(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager.state = lambda: {"state": "stopped"}
        mv._exit_helper_frame = mv.frame
        mv.run = lambda *a, **k: "Frame Workspace viewer session stopped."
        mv.stop_workspace = mock.Mock(return_value={"state": "stopped", "removed": True})
        mv._stop_workspace_viewer = lambda: "already stopped"
        mv._close_window_panels = lambda: None
        old_session = mv.session_id
        mv.exit_pending = True

        result = mv._stop_workspace_session()

        self.assertEqual(result, {"state": "stopped", "removed": True})
        self.assertFalse(mv.exit_pending)
        self.assertNotEqual(mv.session_id, old_session)
        self.assertTrue(mv.dash_state()["workspace"]["session"] == mv.session_id)

    def test_exit_pending_rejects_direct_mode_change_before_cleanup_or_mutation(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.mode = "spatial"
        mv.exit_pending = True
        mv.workspace_manager.state = mock.Mock(side_effect=AssertionError("mode change must not inspect/restart"))
        mv._close_window_panels = mock.Mock()

        with self.assertRaisesRegex(frame_macview.MacViewError, "Stop is still being confirmed"):
            mv.set_mode("desktop")

        self.assertEqual(mv.mode, "spatial")
        mv.workspace_manager.state.assert_not_called()
        mv._close_window_panels.assert_not_called()

    def test_session_stop_closes_frame_viewers_and_retries_unconfirmed_mac_restore(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        states = iter(({"state": "error", "removed": False}, {"state": "stopped", "removed": True}))
        mv.workspace_manager.state = lambda: next(states)
        mv.stop_workspace = mock.Mock(side_effect=[frame_macview.MacViewError("Mac restore pending"),
                                                   {"state": "stopped"}])
        mv._stop_workspace_viewer = mock.Mock()
        mv._close_window_panels = mock.Mock()
        mv._exit_helper_frame = mv.frame
        mv.agent = mock.Mock(poll=lambda: None)
        mv.call = mock.Mock(return_value={"closed": 0})
        mv.run = mock.Mock(return_value="Frame Workspace viewer session stopped.")
        mv.exit_pending = True
        old_session = mv.session_id

        with self.assertRaisesRegex(frame_macview.MacViewError, "Mac restore pending"):
            mv._stop_workspace_session()

        self.assertTrue(mv.exit_pending)
        self.assertIn("Mac restore pending", mv.exit_error)
        mv._stop_workspace_viewer.assert_called_once()
        mv._close_window_panels.assert_called_once()
        self.assertTrue(any(call.args and "--hidedashboard" in call.args[0] for call in mv.run.call_args_list))

        result = mv._stop_workspace_session()

        self.assertEqual(result, {"state": "stopped", "removed": True})
        self.assertFalse(mv.exit_pending)
        self.assertNotEqual(mv.session_id, old_session)

    def test_failed_frame_local_stop_does_not_rotate_mac_session(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager.state = lambda: {"state": "stopped", "removed": True}
        mv.stop_workspace = mock.Mock(return_value={"state": "stopped", "removed": True})
        mv._stop_workspace_viewer = mock.Mock()
        mv._close_window_panels = mock.Mock()
        mv._exit_helper_frame = mv.frame
        mv.run = mock.Mock(side_effect=lambda command, **kwargs:
                           "local helper failed" if "workspace-exit-on-frame.py" in command else "")
        mv.exit_pending = True
        old_session = mv.session_id

        with self.assertRaisesRegex(frame_macview.MacViewError, "Frame did not confirm"):
            mv._stop_workspace_session()

        self.assertTrue(mv.exit_pending)
        self.assertEqual(mv.session_id, old_session)

    def test_headset_start_uses_the_last_workspace_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "last-start.json")
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv.last_workspace_file = path
            mv._remember_workspace("3840x2160", 2, "balanced")
            again = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            again.last_workspace_file = path
            again._load_last_workspace()
            self.assertEqual(again.last_workspace, ("3840x2160", 2, "balanced"))

    def test_workspace_display_defaults_to_120_hz(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRAME_MAC_VIEW_VD_HZ", None)
            self.assertEqual(frame_macview.workspace_refresh_hz(), "120")
        with mock.patch.dict(os.environ, {"FRAME_MAC_VIEW_VD_HZ": "60"}):
            self.assertEqual(frame_macview.workspace_refresh_hz(), "60")
        for bad in ("0", "fast", "1000"):
            with mock.patch.dict(os.environ, {"FRAME_MAC_VIEW_VD_HZ": bad}), \
                    self.assertRaisesRegex(frame_macview.MacViewError, "30 to 240 Hz"):
                frame_macview.workspace_refresh_hz()

    def test_panel_id_is_stable_and_in_range(self):
        a = frame_macview.panel_id("window:123")
        self.assertEqual(a, frame_macview.panel_id("window:123"))
        self.assertNotEqual(a, frame_macview.panel_id("window:124"))
        self.assertTrue(2_001_000_000 <= a < 2_002_000_000)

    def test_fit_keeps_aspect_inside_the_panel(self):
        self.assertEqual(frame_macview.fit(2560, 1440), (1920, 1080))
        w, h = frame_macview.fit(800, 1600)
        self.assertEqual(h, 1080)
        self.assertAlmostEqual(w / h, 0.5, places=2)

    def test_the_tunnel_follows_the_headset(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.retarget("frame", ["-o", "ControlPath=/tmp/x-%C", "-o", "HostName=192.0.2.1", "-o", "HostKeyAlias=frame-control-a"])
        self.assertEqual(mv.host_opts, ["-o", "HostName=192.0.2.1", "-o", "HostKeyAlias=frame-control-a"])

        class Tunnel:
            ended = False
            def poll(self): return None
            def terminate(self): Tunnel.ended = True
        mv.tunnel, mv.remote_port = Tunnel(), 47999
        mv.retarget("frame", ["-o", "HostName=192.0.2.9"])  # another address, same headset: keep it
        self.assertFalse(Tunnel.ended)
        mv.retarget("frame-2", ["-o", "HostName=192.0.2.2"])  # another headset: never the old one's tunnel
        self.assertTrue(Tunnel.ended)
        self.assertIsNone(mv.tunnel)
        self.assertEqual(mv.frame, "frame-2")

    def test_a_switch_just_before_publishing_drops_the_old_headsets_tunnel(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.port = 47000
        ended = []

        class Proc:
            def poll(self): return None
            def terminate(self): ended.append(self)
            def wait(self): return 0
            stderr = io.StringIO("")

        def probe(port):
            mv.retarget("frame-2", ["-o", "HostName=192.0.2.2"])  # the app switches right now
            return True
        with mock.patch.object(frame_macview.subprocess, "Popen", return_value=Proc()), \
                mock.patch.object(frame_macview.time, "sleep"), mock.patch.object(mv, "_probe", probe):
            self.assertFalse(mv._open_tunnel([], [47001]))
        self.assertIsNone(mv.tunnel)
        self.assertEqual(len(ended), 1)  # the tunnel to the old headset was closed

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_launch_script_parses(self):
        for script in (frame_macview.LAUNCH, frame_macview.WORKSPACE_VIEWER_SCRIPT.read_text(encoding="utf-8"),
                       frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8")):
            with self.subTest(script=script[:40]):
                r = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_native_adjust_helper_waits_for_owner_readback_and_keeps_control_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            folder = runtime / "frame-workspace"
            folder.mkdir()
            bindir = root / "bin"
            bindir.mkdir()
            calls = root / "systemctl-calls"
            systemctl = bindir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$TEST_SYSTEMCTL_CALLS\"\n"
                "case \"$4\" in\n"
                "  --property=ActiveState) printf '%s\\n' \"$TEST_ACTIVE\" ;;\n"
                "  --property=MainPID) printf '%s\\n' \"$TEST_PID\" ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n", encoding="utf-8")
            systemctl.chmod(0o755)
            key = f"com.frame-workspace.native.{os.getuid()}.workspace"
            state_file = folder / "state.json"
            (folder / "control.json").write_text(json.dumps({"revision": 10**18}), encoding="utf-8")
            state_file.write_text(json.dumps({
                "ready": True, "state": "streaming", "overlayKey": key, "controlRevision": 0,
                "placement": {"width": 2.4, "distance": 2.0}}), encoding="utf-8")
            env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   "XDG_RUNTIME_DIR": str(runtime), "TEST_ACTIVE": "active", "TEST_PID": "123",
                   "TEST_SYSTEMCTL_CALLS": str(calls)}
            changed = threading.Event()

            def owner():
                deadline = time.monotonic() + 3
                control = folder / "control.json"
                while time.monotonic() < deadline:
                    try:
                        update = json.loads(control.read_text())
                        if "width" in update and "distance" in update:
                            current = json.loads(state_file.read_text())
                            current["controlRevision"] = update["revision"]
                            current["placement"] = {"width": update["width"], "distance": update["distance"]}
                            temp_state = folder / "state.next"
                            temp_state.write_text(json.dumps(current), encoding="utf-8")
                            temp_state.replace(state_file)
                            changed.set()
                            return
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.02)

            thread = threading.Thread(target=owner, daemon=True)
            thread.start()
            result = subprocess.run(["bash", "-s", "--", "adjust", "3.25", "2.75"],
                                    input=frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"),
                                    text=True, capture_output=True, env=env, timeout=10)
            thread.join(timeout=1)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(changed.is_set())
            response = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(response, {"ready": True, "state": "streaming", "overlayKey": key,
                                        "placement": {"width": 3.25, "distance": 2.75}})
            control = json.loads((folder / "control.json").read_text())
            self.assertEqual(set(control), {"revision", "width", "distance"})
            self.assertEqual(control["revision"], 1)
            self.assertEqual((folder / "control.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((folder / "control.lock").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret", json.dumps(control).lower())
            self.assertTrue(all(line.split()[1] == "show" for line in calls.read_text().splitlines()))

            env["TEST_ACTIVE"] = "inactive"
            (folder / "control.json").unlink()
            stopped = subprocess.run(["bash", "-s", "--", "adjust", "3", "2"],
                                     input=frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"),
                                     text=True, capture_output=True, env=env, timeout=10)
            self.assertNotEqual(stopped.returncode, 0)
            self.assertIn("not ready", stopped.stderr)
            self.assertFalse((folder / "control.json").exists())

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_native_helper_acknowledgements_require_the_exact_requested_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            folder = runtime / "frame-workspace"
            folder.mkdir()
            bindir = root / "bin"
            bindir.mkdir()
            systemctl = bindir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "case \"$4\" in\n"
                "  --property=ActiveState) printf '%s\\n' active ;;\n"
                "  --property=MainPID) printf '%s\\n' 123 ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n", encoding="utf-8")
            systemctl.chmod(0o755)
            key = f"com.frame-workspace.native.{os.getuid()}.workspace"
            state_file = folder / "state.json"
            control_file = folder / "control.json"
            control_file.write_text(json.dumps({"revision": 1}), encoding="utf-8")
            state = {"ready": True, "state": "streaming", "overlayKey": key,
                     "placement": {"width": 3.25, "distance": 2.75},
                     "controlRevision": 3, "controlResultRevision": 4,
                     "curvatureResult": {"supported": False, "applied": False,
                                          "requested": 0.5, "value": None, "errorCode": 23}}
            state_file.write_text(json.dumps(state), encoding="utf-8")
            env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   "XDG_RUNTIME_DIR": str(runtime)}

            def advance_state(revision, field, value, patch):
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    try:
                        update = json.loads(control_file.read_text())
                        if update.get("revision") == revision and update.get(field) == value:
                            time.sleep(0.6)
                            current = json.loads(state_file.read_text())
                            current.update(patch)
                            temporary = folder / "state.next"
                            temporary.write_text(json.dumps(current), encoding="utf-8")
                            temporary.replace(state_file)
                            return
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.02)

            script = frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8")
            thread = threading.Thread(target=advance_state,
                                      args=(2, "width", 3.25, {"controlRevision": 2}), daemon=True)
            thread.start()
            started = time.monotonic()
            adjusted = subprocess.run(["bash", "-s", "--", "adjust", "3.25", "2.75"],
                                      input=script, text=True, capture_output=True, env=env, timeout=10)
            adjust_elapsed = time.monotonic() - started
            thread.join(timeout=1)
            self.assertEqual(adjusted.returncode, 0, adjusted.stderr)
            self.assertGreater(adjust_elapsed, 0.45,
                               "a matching placement with a higher stale revision must not be acknowledged")
            self.assertEqual(json.loads(control_file.read_text())["revision"], 2)

            thread = threading.Thread(target=advance_state,
                                      args=(3, "curvature", 0.5, {"controlResultRevision": 3}), daemon=True)
            thread.start()
            started = time.monotonic()
            curved = subprocess.run(["bash", "-s", "--", "curvature", "0.5"],
                                    input=script, text=True, capture_output=True, env=env, timeout=10)
            curvature_elapsed = time.monotonic() - started
            thread.join(timeout=1)
            self.assertEqual(curved.returncode, 0, curved.stderr)
            self.assertGreater(curvature_elapsed, 0.45,
                               "a supported response with a higher stale revision must not be acknowledged")
            self.assertEqual(json.loads(control_file.read_text())["revision"], 3)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_native_focus_helper_normalizes_legacy_revision_and_increments_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            folder = runtime / "frame-workspace"
            folder.mkdir()
            bindir = root / "bin"
            bindir.mkdir()
            systemctl = bindir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "case \"$4\" in\n"
                "  --property=ActiveState) printf '%s\\n' active ;;\n"
                "  --property=MainPID) printf '%s\\n' 123 ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n", encoding="utf-8")
            systemctl.chmod(0o755)
            key = f"com.frame-workspace.native.{os.getuid()}.workspace"
            (folder / "state.json").write_text(json.dumps({
                "ready": True, "state": "streaming", "overlayKey": key,
                "placement": {"width": 2.4, "distance": 2.0}}), encoding="utf-8")
            control = folder / "control.json"
            control.write_text(json.dumps({"revision": 10**18}), encoding="utf-8")
            env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   "XDG_RUNTIME_DIR": str(runtime)}
            script = frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8")
            for expected_revision in (1, 2):
                focused = subprocess.run(["bash", "-s", "--", "focus"], input=script,
                                         text=True, capture_output=True, env=env, timeout=10)
                self.assertEqual(focused.returncode, 0, focused.stderr)
                self.assertIn("focus requested", focused.stdout)
                self.assertEqual(json.loads(control.read_text()), {
                    "revision": expected_revision, "focus": True, "recenter": True})

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_native_curvature_helper_returns_unsupported_readback_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir()
            folder = runtime / "frame-workspace"
            folder.mkdir()
            bindir = root / "bin"
            bindir.mkdir()
            systemctl = bindir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "case \"$4\" in\n"
                "  --property=ActiveState) printf '%s\\n' active ;;\n"
                "  --property=MainPID) printf '%s\\n' 123 ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n", encoding="utf-8")
            systemctl.chmod(0o755)
            key = f"com.frame-workspace.native.{os.getuid()}.workspace"
            state_file = folder / "state.json"
            base_state = {"ready": True, "state": "streaming", "overlayKey": key,
                          "placement": {"width": 2.4, "distance": 2.0},
                          "controlResultRevision": 0,
                          "curvatureResult": {"supported": False, "applied": False,
                                               "requested": 0, "value": None, "errorCode": 23}}
            state_file.write_text(json.dumps(base_state), encoding="utf-8")
            control = folder / "control.json"
            control.write_text(json.dumps({"revision": 10**18}), encoding="utf-8")
            env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   "XDG_RUNTIME_DIR": str(runtime)}

            def owner(expected_curvature):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        update = json.loads(control.read_text())
                        if update.get("curvature") != expected_curvature:
                            time.sleep(0.02)
                            continue
                        current = dict(base_state)
                        current["controlResultRevision"] = update["revision"]
                        current["curvatureResult"] = {"supported": False, "applied": False,
                                                       "requested": update["curvature"],
                                                       "value": None, "errorCode": 23}
                        temp_state = folder / "state.next"
                        temp_state.write_text(json.dumps(current), encoding="utf-8")
                        temp_state.replace(state_file)
                        return
                    except (OSError, KeyError, ValueError):
                        pass
                    time.sleep(0.02)

            thread = threading.Thread(target=owner, args=(0.75,), daemon=True)
            thread.start()
            result = subprocess.run(["bash", "-s", "--", "curvature", "0.75"],
                                    input=frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"),
                                    text=True, capture_output=True, env=env, timeout=10)
            thread.join(timeout=1)
            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertFalse(response["ok"], "unsupported curvature must not look like a successful command")
            self.assertEqual(response["curvature"], {"supported": False, "applied": False,
                                                      "requested": 0.75, "value": None,
                                                      "errorCode": 23})
            self.assertEqual(json.loads((folder / "control.json").read_text()),
                             {"revision": 1, "curvature": 0.75})
            thread.join(timeout=1)
            thread = threading.Thread(target=owner, args=(0.5,), daemon=True)
            thread.start()
            second = subprocess.run(["bash", "-s", "--", "curvature", "0.5"],
                                    input=frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"),
                                    text=True, capture_output=True, env=env, timeout=10)
            thread.join(timeout=1)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_response = json.loads(second.stdout.strip().splitlines()[-1])
            self.assertFalse(second_response["ok"])
            self.assertEqual(second_response["curvature"]["requested"], 0.5)
            self.assertEqual(json.loads(control.read_text()), {"revision": 2, "curvature": 0.5})
            for invalid in ("nan", "-0.1", "1.01", "1e-3"):
                with self.subTest(invalid=invalid):
                    rejected = subprocess.run(["bash", "-s", "--", "curvature", invalid],
                                              input=frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text(encoding="utf-8"),
                                              text=True, capture_output=True, env=env, timeout=10)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("between 0 and 1", rejected.stderr)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_workspace_viewer_service_ownership_and_failed_readiness_cleanup(self):
        script = frame_macview.WORKSPACE_VIEWER_SCRIPT
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindir = root / "bin"
            bindir.mkdir()
            state, calls, main_pid_reads = root / "state", root / "calls", root / "main-pid-reads"
            state.write_text("not-found\ninactive\n0\n", encoding="utf-8")
            tag = "fc0a0b0c00"
            key = f"com.frame-control.workspace.{os.getuid()}.{tag}"
            display_file = Path(f"/tmp/frame-workspace-viewer-{os.getuid()}-{tag}.display")

            def stub(name, body):
                path = bindir / name
                path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
                path.chmod(0o755)

            stub("systemctl", '''printf 'systemctl %s\\n' "$*" >> "$TEST_CALLS"
if [ "$2" = show ]; then
  # Builtins keep this fake cheap across the production stop/retry loops.
  # Spawning three sed processes per property query can exhaust the test's
  # outer deadline on macOS before the production loop itself terminates.
  { IFS= read -r load; IFS= read -r active; IFS= read -r pid; } < "$TEST_STATE"
  case "$4" in
    --property=LoadState) printf '%s\\n' "$load" ;;
    --property=ActiveState) printf '%s\\n' "$active" ;;
    --property=MainPID)
      printf '%s\\n' "$pid"
      if [ "$active" = deactivating ] && [ "$TEST_STOP_MODE" = drain ]; then
        reads=0
        [ ! -f "$TEST_MAIN_PID_READS" ] || reads=$(cat "$TEST_MAIN_PID_READS")
        reads=$((reads + 1))
        printf '%s\\n' "$reads" > "$TEST_MAIN_PID_READS"
        if [ "$reads" -ge "$TEST_DRAIN_AFTER" ]; then
          printf 'loaded\\ninactive\\n0\\n' > "$TEST_STATE"
        fi
      fi
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [ "$2" = kill ]; then
  case "$3" in
    --signal=TERM) printf 'loaded\\ndeactivating\\n4321\\n' > "$TEST_STATE" ;;
    --signal=KILL)
      if [ "$TEST_STOP_MODE" = kill ]; then printf 'loaded\\ninactive\\n0\\n' > "$TEST_STATE"; fi
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [ "$2" = --no-block ] && [ "$3" = stop ]; then exit 0; fi
exit 1
''')
            stub("systemd-run", '''printf 'systemd-run\\n' >> "$TEST_CALLS"
printf '%s\\n' "$@" > "$TEST_ARGS"
if [ "$TEST_SYSTEMD_RUN_FAIL" = 1 ]; then
  printf 'failed while opening %s\\n' "$TEST_URL" >&2
  exit 1
fi
printf ':77\\n\\n' > "$TEST_DISPLAY"
printf 'loaded\\nactive\\n4321\\n' > "$TEST_STATE"
''')
            stub("flatpak", '''[ "$1" = info ] && exit 0
exit 0
''')
            stub("gamescope", "exit 99\n")
            stub("xwininfo", '''if [ "$1" = -root ]; then
  printf '  0x123 "Chromium [fc0a0b0c00]": ("Chromium" "Chromium") %sx%s+0+0\\n' "$TEST_WIDTH" "$TEST_HEIGHT"
else
  printf '  Width: %s\\n  Height: %s\\n  Map State: IsViewable\\n' "$TEST_WIDTH" "$TEST_HEIGHT"
fi
''')
            stub("xprop", '''printf '_NET_WM_NAME(STRING) = "Frame Workspace [fc0a0b0c00]"\\n'
''')
            stub("vrcmd", '''if [ "${TEST_NO_OVERLAY:-0}" = 0 ]; then
  printf '%s\\n' '---- OVERLAYS ----' "'$TEST_OVERLAY_KEY' -- 'Frame Workspace', 0x0 visible VROverlayType_Dashboard_Main"
fi
''')
            stub("sleep", "exit 0\n")
            stub("journalctl", "exit 1\n")
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": temp,
                   "TEST_STATE": str(state), "TEST_MAIN_PID_READS": str(main_pid_reads),
                   "TEST_STOP_MODE": "drain", "TEST_DRAIN_AFTER": "3", "TEST_SYSTEMD_RUN_FAIL": "0",
                   "TEST_WIDTH": "5120", "TEST_HEIGHT": "1440",
                   "TEST_CALLS": str(calls), "TEST_ARGS": str(root / "args"),
                   "TEST_DISPLAY": str(display_file), "TEST_OVERLAY_KEY": key, "TEST_NO_OVERLAY": "0",
                   "FRAME_WORKSPACE_VRCMD": str(bindir / "vrcmd")}

            url = "http://127.0.0.1/view?ticket=private-token&scope=workspace"
            env["TEST_URL"] = url
            started = subprocess.run(["bash", str(script), "start", url, "5120", "1440", tag],
                                     env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            self.assertIn("registered OpenVR key", started.stdout)
            args = (root / "args").read_text(encoding="utf-8")
            for expected in ("--description=Frame Workspace viewer", "--property=TimeoutStopSec=3s",
                             "--property=KillMode=control-group", "--backend", "openvr", "--nested-width",
                             "5120", "--output-width", "--vr-overlay-key", key,
                             "--vr-overlay-explicit-name", "Frame Workspace", "--vr-overlay-show-immediately",
                             "--vr-overlay-physical-width", "5.34",
                             "--start-fullscreen", "--disable-background-mode", "workspace-viewer"):
                self.assertIn(expected, args)
            self.assertNotIn("--keep-alive", args)
            argv = args.splitlines()
            self.assertEqual(argv[argv.index("--nested-width") + 1], "5120")
            self.assertEqual(argv[argv.index("--nested-height") + 1], "1440")
            # A 5K backing triggers Gamescope's fixed upload-buffer assertion.
            # Chrome stays full-sized while the black backing fits the cap.
            self.assertEqual(argv[argv.index("--output-width") + 1], "2560")
            self.assertEqual(argv[argv.index("--output-height") + 1], "720")
            self.assertNotIn("--force-windows-fullscreen", argv)
            stopped = subprocess.run(["bash", str(script), "stop"], env=env, text=True, capture_output=True,
                                     timeout=10)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "loaded\ninactive\n0\n")
            self.assertGreaterEqual(int(main_pid_reads.read_text(encoding="utf-8")), 3,
                                    "deactivating with a live MainPID must not count as stopped")
            stop_calls = calls.read_text(encoding="utf-8")
            self.assertIn("--signal=TERM --kill-whom=all frame-workspace-viewer-", stop_calls)
            self.assertNotIn("is-active", stop_calls, "stopped detection must include deactivating units")

            env["TEST_WIDTH"], env["TEST_HEIGHT"] = "2560", "2880"
            portrait = subprocess.run(["bash", str(script), "start", url, "2560", "2880", tag],
                                      env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(portrait.returncode, 0, portrait.stdout + portrait.stderr)
            argv = (root / "args").read_text(encoding="utf-8").splitlines()
            self.assertEqual(argv[argv.index("--nested-width") + 1], "2560")
            self.assertEqual(argv[argv.index("--nested-height") + 1], "2880")
            self.assertEqual(argv[argv.index("--vr-overlay-physical-width") + 1], "2.67")
            stopped = subprocess.run(["bash", str(script), "stop"], env=env, text=True, capture_output=True,
                                     timeout=10)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)

            env["TEST_WIDTH"], env["TEST_HEIGHT"] = "2880", "2560"
            landscape = subprocess.run(["bash", str(script), "start", url, "2880", "2560", tag],
                                       env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(landscape.returncode, 0, landscape.stdout + landscape.stderr)
            argv = (root / "args").read_text(encoding="utf-8").splitlines()
            self.assertEqual(argv[argv.index("--nested-width") + 1], "2880")
            self.assertEqual(argv[argv.index("--nested-height") + 1], "2560")
            self.assertEqual(argv[argv.index("--vr-overlay-physical-width") + 1], "3.0")
            stopped = subprocess.run(["bash", str(script), "stop"], env=env, text=True, capture_output=True,
                                     timeout=10)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)

            env["TEST_WIDTH"], env["TEST_HEIGHT"] = "3840", "2160"
            landscape = subprocess.run(["bash", str(script), "start", url, "3840", "2160", tag],
                                       env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(landscape.returncode, 0, landscape.stdout + landscape.stderr)
            argv = (root / "args").read_text(encoding="utf-8").splitlines()
            self.assertEqual(argv[argv.index("--nested-width") + 1], "3840")
            self.assertEqual(argv[argv.index("--nested-height") + 1], "2160")
            self.assertEqual(argv[argv.index("--output-width") + 1], "1920")
            self.assertEqual(argv[argv.index("--output-height") + 1], "1080")
            self.assertEqual(argv[argv.index("--vr-overlay-physical-width") + 1], "3.0")
            stopped = subprocess.run(["bash", str(script), "stop"], env=env, text=True, capture_output=True,
                                     timeout=10)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)

            env["TEST_WIDTH"], env["TEST_HEIGHT"] = "5120", "1440"
            env["TEST_NO_OVERLAY"] = "1"
            env["TEST_STOP_MODE"] = "kill"
            failed = subprocess.run(["bash", str(script), "start", url, "5120", "1440", tag],
                                    env=env, text=True, capture_output=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("registered OpenVR overlay did not become ready", failed.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "loaded\ninactive\n0\n",
                             "failed readiness must stop only the owned service")
            stop_calls = calls.read_text(encoding="utf-8")
            self.assertIn("--signal=KILL --kill-whom=all frame-workspace-viewer-", stop_calls)
            self.assertFalse(display_file.exists())

            env["TEST_STOP_MODE"] = "stuck"
            stuck = subprocess.run(["bash", str(script), "start", url, "5120", "1440", tag],
                                   env=env, text=True, capture_output=True, timeout=10)
            self.assertNotEqual(stuck.returncode, 0)
            self.assertIn("service could not be stopped", stuck.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "loaded\ndeactivating\n4321\n",
                             "a live process in deactivating state must be reported as a stop failure")

            state.write_text("not-found\ninactive\n0\n", encoding="utf-8")
            env["TEST_SYSTEMD_RUN_FAIL"] = "1"
            launch_failed = subprocess.run(["bash", str(script), "start", url, "5120", "1440", tag],
                                           env=env, text=True, capture_output=True, timeout=10)
            self.assertNotEqual(launch_failed.returncode, 0)
            self.assertIn("[redacted URL]", launch_failed.stderr)
            self.assertNotIn("private-token", launch_failed.stderr)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_frame_browser_profile_and_cleanup_are_fork_scoped(self):
        for profile in (
            "$HOME/.local/share/frame-workspace/mac-view",
            "$HOME/.var/app/org.chromium.Chromium/data/frame-workspace/mac-view",
            "$HOME/.var/app/com.google.Chrome/data/frame-workspace/mac-view",
        ):
            with self.subTest(profile=profile):
                self.assertIn(profile, frame_macview.LAUNCH)
        self.assertNotIn("frame-control/mac-view", frame_macview.LAUNCH)
        self.assertNotIn("data/frame-mac-view", frame_macview.LAUNCH)

        # Exercise the production cleanup script with fake process-table tools.
        # The first 15 checks simulate a browser that ignores TERM; after KILL,
        # pgrep reports that the fork-owned profile processes have exited.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindir = root / "bin"
            bindir.mkdir()
            state, log = root / "pgrep-count", root / "signals"
            pgrep = bindir / "pgrep"
            pgrep.write_text(
                "#!/bin/sh\n"
                "state=$FRAME_TEST_STATE\n"
                "n=$(cat \"$state\" 2>/dev/null || echo 0)\n"
                "n=$((n + 1))\n"
                "echo \"$n\" > \"$state\"\n"
                "[ \"$n\" -le 15 ]\n",
                encoding="utf-8")
            pgrep.chmod(0o755)
            pkill = bindir / "pkill"
            pkill.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$FRAME_TEST_LOG\"\n",
                encoding="utf-8")
            pkill.chmod(0o755)
            env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   "FRAME_TEST_STATE": str(state), "FRAME_TEST_LOG": str(log)}
            result = subprocess.run(["bash", "-s"], input=frame_macview.CLOSE_BROWSER, text=True,
                                    capture_output=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            signals = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(signals), 2, signals)
            self.assertTrue(signals[0].startswith("-TERM -f "), signals)
            self.assertTrue(signals[1].startswith("-KILL -f "), signals)
            for command in signals:
                self.assertIn("frame-workspace/mac-view", command)
                self.assertNotIn("frame-control/mac-view", command)
                self.assertNotIn("data/frame-mac-view", command)

    def test_show_checks_the_source_before_anything_else(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no ssh"), "frame")
        with self.assertRaises(frame_macview.MacViewError):
            mv.show("rm -rf /")

    def test_missing_browser_is_explained(self):
        calls = []

        def run(remote, stdin=None, timeout=30):
            calls.append(remote)
            e = RuntimeError("exit 3")
            e.stdout = "NO_BROWSER\n"
            raise e

        mv = frame_macview.MacView(["ssh"], run, "frame")
        mv.call = lambda path, **kw: {"screen": True, "ticket": "tk"}
        mv.ensure_tunnel = lambda **kw: None
        mv.remote_port = 47900
        with self.assertRaises(frame_macview.MacViewError) as cm:
            mv.show("window:5")
        self.assertIn("Chromium", str(cm.exception))
        self.assertTrue(calls[0].startswith("bash -s -- "))

    def test_separate_windows_need_accessibility(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no ssh"), "frame")
        mv.call = lambda path, **kw: {"screen": True, "accessibility": False}
        with self.assertRaises(frame_macview.MacViewError) as cm:
            mv.show("separate:42")
        self.assertIn("Accessibility", str(cm.exception))

    def test_screen_permission_is_checked_before_the_headset(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no ssh"), "frame")
        mv.call = lambda path, **kw: {"screen": False}
        with self.assertRaises(frame_macview.MacViewError) as cm:
            mv.show("display:1")
        self.assertIn("Screen Recording", str(cm.exception))

    def test_stop_all_ends_the_viewer_browser_unless_shown_again(self):
        calls = []
        mv = frame_macview.MacView(["ssh"], lambda remote, **kw: calls.append(remote) or "", "frame")
        mv.agent = mock.Mock(poll=lambda: None)
        mv.call = lambda path, **kw: {"closed": 1}
        mv.shown = {"window:5"}
        with mock.patch.object(frame_macview.time, "sleep"):
            gen = mv.shows
            mv.shown.clear()
            mv._end_viewer_browser(gen)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], "bash -s")
            mv.shows += 1  # Show pressed during the wait: leave the new viewer alone
            mv._end_viewer_browser(gen)
            self.assertEqual(len(calls), 1)
            mv.launching = 1  # a Show replacing its own stream is still launching
            mv._end_viewer_browser(mv.shows)
            self.assertEqual(len(calls), 1)
        # A Show waits while the cleanup checks and runs pkill.
        mv.launching = 0
        in_pkill, release, entered = threading.Event(), threading.Event(), threading.Event()

        def blocking_pkill(remote, **kw):
            if kw.get("stdin") == frame_macview.CLOSE_BROWSER:
                in_pkill.set()
                release.wait(5)
                return ""
            entered.set()
            return "panel opened"
        mv.run = blocking_pkill
        mv.remote_port = 47900
        mv.call = lambda path, **kw: (
            {"screen": True, "streams": [], "finished": []} if path == "/status"
            else {"ticket": "one-use"} if path == "/ticket" else {"closed": 1})
        mv.ensure_tunnel = lambda **kw: None
        with mock.patch.object(frame_macview.time, "sleep"):
            cleanup = threading.Thread(target=mv._end_viewer_browser, args=(mv.shows,))
            cleanup.start()
            self.assertTrue(in_pkill.wait(2))
            shower = threading.Thread(target=mv.show, args=("window:5",))
            shower.start()
            self.assertFalse(entered.wait(0.3), "Show started while the cleanup held the lock")
            release.set()
            self.assertTrue(entered.wait(2))
            cleanup.join()
            shower.join()

    def test_shutdown_waits_for_browser_cleanup_before_returning(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def run(remote, stdin=None, **kwargs):
            self.assertEqual(stdin, frame_macview.CLOSE_BROWSER)
            entered.set()
            self.assertTrue(release.wait(3))
            finished.set()
            return ""
        mv = frame_macview.MacView(["ssh"], run, "frame")
        with mock.patch.object(frame_macview.time, "sleep"):
            thread = threading.Thread(target=mv.shutdown)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(thread.is_alive())
            self.assertFalse(finished.is_set())
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())

    def test_tunnel_prefers_the_usb_c_network_when_plugged_in(self):
        mv = frame_macview.MacView(["ssh"], lambda remote, **kw: "13: usb0 inet 10.86.200.233/29 scope global", "frame")
        ssh_g = mock.Mock(stdout="user steamos\nhostname frame.example.ts.net\n")
        with mock.patch.object(frame_macview.socket, "create_connection") as conn, \
                mock.patch.object(frame_macview.subprocess, "run", return_value=ssh_g):
            self.assertEqual(mv._usb_route(), ["-o", "HostName=10.86.200.233", "-o", "HostKeyAlias=frame.example.ts.net"])
            conn.assert_called_once_with(("10.86.200.233", 22), timeout=1)
            ssh_g.stdout = "hostname frame.example.ts.net\nhostkeyalias paired-frame\n"  # a configured alias wins
            conn.side_effect = None
            self.assertIn("HostKeyAlias=paired-frame", mv._usb_route())
            conn.side_effect = OSError("unplugged")
            self.assertEqual(mv._usb_route(), [])
        mv.run = lambda remote, **kw: ""  # no usb0
        self.assertEqual(mv._usb_route(), [])
        mv.prefer_usb = False
        mv.run = lambda remote, **kw: self.fail("no ssh when USB is off")
        self.assertEqual(mv._usb_route(), [])

    def test_a_failed_usb_tunnel_falls_back_to_the_network(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv._usb_route = lambda: ["-o", "HostName=10.86.200.233"]
        tried = []

        def attempt(via, ports):
            tried.append(list(via))
            mv._last_tunnel_error = "Connection refused"
            return not via  # USB fails, the normal path works
        mv._open_tunnel = attempt
        mv.tunnel_up = lambda: False
        mv.ensure_tunnel()
        self.assertEqual(tried, [["-o", "HostName=10.86.200.233"], []])
        self.assertEqual(mv.route, "network")


class HelperLog(unittest.TestCase):
    """The helper's log must survive restarts and sit beside the app's server.log."""

    def test_the_log_is_beside_server_log_for_the_workspace_app_and_frame_control(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": "/x/Library/Logs/Frame Workspace/server.log"}):
            self.assertEqual(frame_macview.MacView.helper_log_path(),
                             Path("/x/Library/Logs/Frame Workspace/mac-helper.log"))
        env = {k: v for k, v in os.environ.items() if k != "FRAME_CONTROL_LOG"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(frame_macview.MacView.helper_log_path(),
                             Path.home() / "Library" / "Logs" / "Frame Control" / "mac-helper.log")

    def test_each_helper_adds_to_the_log_and_a_big_log_rolls_over(self):
        class Helper:
            def __init__(self, pid, lines):
                self.pid, self.stdout = pid, iter(lines)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": f"{tmp}/Frame Workspace/server.log"}):
            mv = frame_macview.MacView([], mock.Mock(), "fake")
            log = Path(tmp) / "Frame Workspace" / "mac-helper.log"
            mv._keep_helper_log(Helper(11, ["frame-mac-view: dock action id=d1-1 open com.apple.Terminal\n"]))
            mv._keep_helper_log(Helper(12, ["frame-mac-view: dock action id=d1-2 open com.apple.Notes\n"]))
            text = log.read_text()
            self.assertIn("---- helper pid 11 started ----", text)
            self.assertIn("---- helper pid 12 started ----", text)
            self.assertIn("dock action id=d1-1", text)
            self.assertIn("dock action id=d1-2", text)
            mv.HELPER_LOG_LIMIT = 10
            mv._keep_helper_log(Helper(13, ["third\n"]))
            self.assertIn("dock action id=d1-1", (log.parent / "mac-helper.log.1").read_text())
            self.assertNotIn("dock action id=d1-1", log.read_text())
            self.assertIn("third", log.read_text())


class SpatialPolicy(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FRAME_WINDOW_DISPLAYS": "1", "FRAME_SPATIAL_DECODE_MP": "18"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 100.0
        self.clock = mock.patch.object(frame_macview.time, "monotonic", side_effect=lambda: self.now)
        self.wall = mock.patch.object(frame_macview.time, "time", side_effect=lambda: self.now + 900)
        self.clock.start()
        self.wall.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.wall.stop)
        self.mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
        self.mv.agent = object()
        self.status = {"streams": []}
        self.calls = []
        self.policies = {}
        self.mv.call = self.call
        # Exercise the production request handler without background timing.
        self.mv._dash_run = lambda busy, fn, *args: fn(*args)
        self.mv._action_run = lambda fn, *args: fn(*args)

    def call(self, path, method="GET", **q):
        self.calls.append((path, method, q))
        if path == "/status":
            return self.status
        self.assertEqual(method, "POST")
        if path == "/streams/live":
            selected = set(q.get("src", "").split(","))
            for s in self.status["streams"]:
                if s["src"].startswith(("window:", "separate:", "spatial:")) or s["src"] in frame_macview.SPATIAL_AUX_ROLES:
                    s["live"] = q.get("all") == "1" or s["src"] in selected
        elif path == "/streams/policy":
            if q.get("all") == "1":
                self.policies.clear()
            else:
                self.policies[q["src"]] = dict(q)
        elif path == "/focus":
            self.status["focusedSrc"] = q["src"]
        else:
            self.assertEqual(path, "/pointer")
        return {}

    def stream(self, src, mp, age=0, **extra):
        width = extra.pop("width", int(mp * 1000))
        height = extra.pop("height", 1000)
        requested_width = extra.pop("requestedWidth", width)
        requested_height = extra.pop("requestedHeight", height)
        s = {"id": len(self.status["streams"]) + 1, "src": src, "width": width, "height": height,
             "requestedWidth": requested_width, "requestedHeight": requested_height,
             "lastChanged": self.now + 900 - age, "live": True, **extra}
        if src in frame_macview.SPATIAL_AUX_ROLES:
            s.setdefault("visible", True)
        self.status["streams"].append(s)
        return s

    def tick(self, at=None):
        if at is not None:
            self.now = at
        self.mv._spatial_tick()

    def live(self):
        return {s["src"] for s in self.status["streams"] if s["live"]}

    def panel(self, src, number):
        key = f"com.frame-workspace.native.1000.panel-w{number}"
        self.mv.native_panels[src] = {"key": key}
        return key

    def focus(self, key, u=.25, v=.75):
        self.mv._handle_frame_requests(json.dumps({"action": "focus", "key": key, "u": u, "v": v}))

    def test_budget_reserves_workspace_and_prefers_focus_then_newest_change(self):
        self.stream("display:1", 8)
        self.stream("separate:1", 6, age=20)
        self.stream("separate:2", 4, age=2)
        self.stream("separate:3", 3)
        self.status["focusedSrc"] = "separate:1"
        self.tick()
        self.assertEqual(self.live(), {"display:1", "separate:1"})
        self.tick(101)
        self.assertEqual(self.live(), {"display:1", "separate:1", "separate:3"})
        self.assertLessEqual(sum(s["width"] * s["height"] for s in self.status["streams"] if s["live"]), 18_000_000)
        writes = len([c for c in self.calls if c[0] == "/streams/live"])
        self.tick(102)
        self.assertEqual(len([c for c in self.calls if c[0] == "/streams/live"]), writes)

    def test_recent_change_hold_expires_and_continuous_changes_refresh_it(self):
        s = self.stream("separate:1", 4)
        self.tick()
        self.tick(101)
        self.assertIn(s["src"], self.live())
        s["lastChanged"] = 1004
        self.tick(106)
        self.assertIn(s["src"], self.live())
        self.tick(110)
        self.assertNotIn(s["src"], self.live())

    def test_admission_delay_and_residency_prevent_newest_window_churn(self):
        self.stream("display:1", 14)
        a = self.stream("separate:1", 4)
        self.tick()
        self.tick(101)
        b = self.stream("separate:2", 4, lastChanged=1002)
        self.tick(102)
        self.tick(103)
        self.assertIn(a["src"], self.live())
        self.assertNotIn(b["src"], self.live())
        self.tick(104)
        self.assertIn(b["src"], self.live())
        self.status["focusedSrc"] = a["src"]
        self.tick(104.1)
        self.assertIn(a["src"], self.live())
        self.assertNotIn(b["src"], self.live())

    def test_budget_reduction_and_resize_override_residency(self):
        s = self.stream("separate:1", 4)
        self.tick()
        self.tick(101)
        s["width"] = 19000
        s["requestedWidth"] = 19000
        self.status["focusedSrc"] = s["src"]
        self.tick(101.1)
        self.assertEqual(self.live(), {s["src"]}, "focus stays live while its capture is resized")
        self.assertEqual(self.mv._spatial_policy_targets[s["src"]][3:], (1280, 30))
        s["width"] = 4000
        s["requestedWidth"] = 4000
        self.tick(101.2)
        self.assertEqual(self.mv._spatial_policy_targets[s["src"]][3], 1280,
                         "quality restoration needs two seconds of spare capacity")
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "3"}):
            self.tick(101.3)
            self.assertEqual(self.live(), {s["src"]})

    def test_focused_stream_survives_unknown_auxiliary_cost_and_backgrounds_yield(self):
        focused = self.stream("spatial:1", 20)
        background = self.stream("spatial:2", 6)
        self.stream("third-party-display", 0, width=None, height=None)
        self.status["focusedSrc"] = focused["src"]
        self.tick()
        self.tick(101)
        self.assertEqual(self.live() & {focused["src"], background["src"]}, {focused["src"]})
        self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3:], (1280, 30))
        self.assertEqual(self.mv._spatial_policy_targets[background["src"]][1], "background")
        self.assertEqual(self.mv._spatial_policy_targets[background["src"]][4], 10)

    def test_backgrounds_wait_until_the_focused_resize_is_visible_in_status(self):
        focused = self.stream("spatial:1", 20)
        background = self.stream("spatial:2", 6)
        self.status["focusedSrc"] = focused["src"]
        self.tick()
        self.tick(101)
        self.assertEqual(self.live() & {focused["src"], background["src"]}, {focused["src"]})
        self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3], 1280)
        # Model the next helper status after ScreenCaptureKit applied the maxLong
        # update; the background then uses only the confirmed remaining capacity.
        focused.update(width=1280, height=64)
        self.tick(102)
        self.assertEqual(self.live() & {focused["src"], background["src"]},
                         {focused["src"], background["src"]})

    def test_oversized_known_auxiliary_yields_before_focused_light_minimum(self):
        focused = self.stream("spatial:1", 20, width=4000, height=5000,
                              requestedWidth=4000, requestedHeight=5000)
        background = self.stream("spatial:2", 6)
        menu = self.stream("menu:0", 17)
        self.status["focusedSrc"] = focused["src"]
        self.tick()
        self.tick(101)
        self.assertEqual(self.live() & {focused["src"], background["src"]}, {focused["src"]})
        self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3:], (1280, 30))
        self.assertFalse(menu["live"], "the oversized menu surface is suspended before lowering focus FPS")
        self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][2], "focused-decode-budget")
        # Wait for the helper to confirm its in-place capture update; background
        # work can then use the confirmed remaining cost.
        focused.update(width=1280, height=410)
        self.tick(102)
        self.assertEqual(self.live() & {focused["src"], background["src"]},
                         {focused["src"], background["src"]})

    def test_hidden_auxiliary_surface_is_not_counted_or_resumed(self):
        focused = self.stream("spatial:1", 16)
        hidden_menu = self.stream("menu:0", 17, visible=False, live=False)
        self.status["focusedSrc"] = focused["src"]
        self.tick()
        self.assertEqual(self.live() & {focused["src"]}, {focused["src"]})
        self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3:], (0, 0))
        self.assertFalse(hidden_menu["live"], "an invisible menu slot remains still")

    def test_light_minimum_over_budget_reports_limited_without_lowering_fps(self):
        focused = self.stream("spatial:1", 20, width=4000, height=5000,
                              requestedWidth=4000, requestedHeight=5000)
        self.status["focusedSrc"] = focused["src"]
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "0.1"}):
            self.tick()
            self.tick(101)
        policy = self.mv._spatial_policy_targets[focused["src"]]
        self.assertEqual(self.live() & {focused["src"]}, {focused["src"]})
        self.assertEqual(policy[2], "focused-decode-limited")
        self.assertEqual(policy[3:], (1280, 30), "an unavoidable load is reported without shrinking below Light")

    def test_focused_quality_restores_after_two_seconds_of_spare_capacity(self):
        focused = self.stream("spatial:1", 4)
        self.status["focusedSrc"] = focused["src"]
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "3"}):
            self.tick(100)
            self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3], 1280)
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "18"}):
            self.tick(101)
            self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3], 1280)
            self.tick(102.99)
            self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3], 1280)
            self.tick(103.01)
            self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3], 0)

    def test_focused_policy_does_not_raise_a_lower_requested_quality(self):
        focused = self.stream("spatial:1", 0.48, width=800, height=600)
        self.status["focusedSrc"] = focused["src"]
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "0.5"}):
            self.tick()
        self.assertEqual(self.mv._spatial_policy_targets[focused["src"]][3], 0,
                         "a focused stream already below budget keeps its lower requested dimensions")
        self.assertEqual(self.live() & {focused["src"]}, {focused["src"]})

    def test_unknown_sizes_and_duplicate_viewers_do_not_overcommit(self):
        self.stream("display:1", 8)
        self.stream("separate:1", 6)
        self.stream("separate:1", 6)  # two decoders for the same source
        self.stream("window:2", 1, width=0)
        self.tick()
        self.tick(101)
        self.assertEqual(self.live(), {"display:1"})
        self.status["streams"][0]["width"] = 0
        self.stream("separate:3", 1)
        self.tick(102)
        self.tick(103)
        self.assertEqual(self.live(), {"display:1"})

    def test_reconnect_and_restart_reapply_selection_and_pointer_mode(self):
        s = self.stream("separate:1", 4)
        self.tick()
        self.tick(101)
        s["id"] = 999
        self.tick(102)
        self.assertEqual(self.live(), set())
        self.tick(103)
        self.assertEqual(self.live(), {s["src"]})
        self.mv.agent = object()
        self.tick(104)
        self.assertEqual(len([c for c in self.calls if c[0] == "/pointer"]), 2)

    def test_disable_restores_current_and_future_streams_then_stops_policy(self):
        self.stream("separate:1", 4, age=20)
        self.tick()
        self.assertEqual(self.live(), set())
        with mock.patch.dict(os.environ, {"FRAME_WINDOW_DISPLAYS": "0"}):
            self.tick(101)
            self.assertEqual(self.live(), {"separate:1"})
            self.assertIn(("/pointer", "POST", {"shared": "0"}), self.calls)
            self.assertEqual(self.calls[-1], ("/streams/live", "POST", {"all": "1"}))
            count = len(self.calls)
            self.tick(102)
            self.assertEqual(len(self.calls), count)

    def test_failed_selection_is_retried(self):
        self.stream("separate:1", 4)
        self.mv.call = mock.Mock(side_effect=[{}, self.status, {}, frame_macview.MacViewError("not answering")])
        with self.assertRaises(frame_macview.MacViewError):
            self.tick()
        self.assertIsNone(self.mv._spatial_applied)
        self.mv.call = self.call
        self.tick(101)
        self.assertEqual(self.live(), {"separate:1"})

    def test_focus_maps_key_to_source_and_focus_is_live_without_admission_delay(self):
        key = self.panel("separate:42", 42)
        self.stream("separate:42", 4, age=30)
        self.focus(key)
        self.assertEqual(self.calls, [("/focus", "POST", {"src": "separate:42", "u": .25, "v": .75})])
        self.tick()
        self.assertEqual(self.live(), {"separate:42"})

    def test_spatial_focus_and_auxiliary_pixels_leave_idle_menu_slots_free(self):
        key = self.panel("spatial:42", 42)
        self.stream("spatial:42", 6, age=30)
        self.stream("spatial:43", 8)
        self.stream("menubar", 1)
        self.stream("status", 1)
        menu = self.stream("menu:0", 2)
        for i in range(1, 4):
            self.stream(f"menu:{i}", 0, width=None, height=None)
        self.focus(key)
        self.assertEqual(self.calls[-1], ("/focus", "POST", {"src": "spatial:42", "u": .25, "v": .75}))
        self.tick()
        self.tick(101)
        self.assertEqual(self.live() & {"spatial:42", "spatial:43"}, {"spatial:42", "spatial:43"})
        menu.update(width=3000)
        self.tick(102)
        self.assertEqual(self.live() & {"spatial:42", "spatial:43"}, {"spatial:42"})
        for _, method, query in (c for c in self.calls if c[0] == "/streams/live"):
            self.assertEqual(method, "POST")
            self.assertTrue(all(s.startswith("spatial:") or s in frame_macview.SPATIAL_AUX_ROLES
                                for s in query["src"].split(",") if s))

    def test_focus_coalesces_latest_and_is_limited_to_four_per_second(self):
        a, b = self.panel("window:1", 1), self.panel("separate:2", 2)
        self.focus(a)
        self.now += .05
        self.focus(b)
        self.focus(a, u=.4)
        self.assertEqual(len(self.calls), 1)
        self.now += .2
        self.mv._flush_frame_focus()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][2], {"src": "window:1", "u": .4, "v": .75})
        self.now += .25
        self.mv._handle_frame_requests("\n".join(json.dumps({"action": "focus", "key": k, "u": 0, "v": 1})
                                                 for k in (a, b, a, b)))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1][2]["src"], "separate:2")

    def test_direct_native_focus_steers_live_set_without_replaying_focus(self):
        a, b = self.panel("spatial:1", 1), self.panel("spatial:2", 2)
        self.focus(a)
        self.assertEqual(len(self.calls), 1)
        self.now += .05
        self.focus(b)  # queued behind the four-per-second cap
        self.mv._handle_frame_requests(json.dumps({"action": "focus", "key": a, "u": .5, "v": .5, "direct": True}))
        self.now += .3
        self.mv._flush_frame_focus()
        self.assertEqual(len(self.calls), 1, "an acknowledged direct focus drops the older queued one")
        self.assertEqual(self.mv._spatial_focused, "spatial:1")
        self.mv._handle_frame_requests(json.dumps({"action": "focus", "key": b, "u": .5, "v": .5, "direct": True}))
        self.assertEqual(len(self.calls), 1, "direct focus never posts /focus")
        self.assertEqual(self.mv._spatial_focused, "spatial:2")

    def test_active_window_is_live_first_and_looked_at_windows_never_displace_it(self):
        # FW-15: the eyes only highlight. Busy windows the user looks at (they
        # change, so they want to be live) compete for what is left after the
        # active window, never for its place.
        self.stream("display:1", 8)
        active = self.stream("spatial:1", 6, age=30)
        busy = [self.stream(f"spatial:{n}", 6) for n in (2, 3)]
        self.status["focusedSrc"] = active["src"]
        for second in range(8):
            for b in busy:
                b["lastChanged"] = self.now + 900
            self.tick(100 + second)
            self.assertIn(active["src"], self.live(), f"active window live at {second}s")
        self.assertEqual(len(self.live() & {b["src"] for b in busy}), 0, "no room beside it in 18 MP")
        order = [c for c in self.calls if c[0] == "/streams/live"]
        self.assertTrue(all(active["src"] in q["src"].split(",") for _, _, q in order))

    def test_active_window_stays_live_while_its_size_is_unknown(self):
        active = self.stream("spatial:1", 0, width=None, height=None)
        other = self.stream("spatial:2", 2)
        self.status["focusedSrc"] = active["src"]
        self.tick()
        self.tick(101)
        self.assertEqual(self.live(), {active["src"]}, "the active window first, nothing optional beside an unknown size")
        active.update(width=4000, height=1000)
        self.tick(102)
        self.assertEqual(self.live() & {active["src"], other["src"]}, {active["src"], other["src"]})

    def test_only_the_macs_focus_orders_the_live_set(self):
        # A focus the helper has not confirmed (a stale direct request) does not
        # put another window first: the helper's focusedSrc does.
        a, b = self.panel("spatial:1", 1), self.panel("spatial:2", 2)
        self.stream("display:1", 8)
        self.stream("spatial:1", 6, age=30)
        self.stream("spatial:2", 6, age=30)
        self.status["focusedSrc"] = "spatial:1"
        self.mv._handle_frame_requests(json.dumps({"action": "focus", "key": b, "u": .5, "v": .5, "direct": True}))
        self.tick()
        self.assertEqual(self.live() & {"spatial:1", "spatial:2"}, {"spatial:1"})
        self.status["focusedSrc"] = "spatial:2"  # the Mac moved focus (a click, the switcher, a long stare)
        self.tick(101)
        self.assertEqual(self.live() & {"spatial:1", "spatial:2"}, {"spatial:2"})
        self.assertFalse([c for c in self.calls if c[0] == "/focus"], "nothing replays a raise")
        del a

    def test_focus_rejects_unknown_stale_keys_bad_coordinates_and_disabled_mode(self):
        key = self.panel("separate:42", 42)
        for u, v in ((None, 0), (True, 0), ("0.2", 0), (-.1, 0), (1.1, 0), (math.nan, 0), (0, math.inf)):
            self.focus(key, u, v)
        self.focus("com.frame-workspace.native.1000.panel-w99")
        self.assertEqual(self.calls, [])
        with mock.patch.dict(os.environ, {"FRAME_WINDOW_DISPLAYS": "0"}):
            self.focus(key)
        self.assertEqual(self.calls, [])
        self.focus(key)
        self.now += .1
        self.focus(key)
        self.mv.native_panels.clear()
        self.now += 1
        self.mv._flush_frame_focus()
        self.assertEqual(len(self.calls), 1)

    def test_decode_budget_validation(self):
        for value in ("0", "-1", "NaN", "inf", "invalid", "1e308"):
            with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": value}):
                with self.assertRaises(frame_macview.MacViewError):
                    frame_macview.spatial_decode_pixels()
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "12.5"}):
            self.assertEqual(frame_macview.spatial_decode_pixels(), 12_500_000)

    def test_supervisor_runs_local_policy_even_when_tunnel_is_down(self):
        self.mv.agent = mock.Mock()
        self.mv.agent.poll.return_value = None
        self.mv.tunnel_up = mock.Mock(return_value=False)
        self.mv.ensure_tunnel = mock.Mock()

        def once():
            self.mv.closing = True
        self.mv._spatial_tick = mock.Mock(side_effect=once)
        with mock.patch.object(frame_macview.time, "sleep"):
            self.mv._supervise()
            self.mv.supervisor.join(timeout=2)
        self.assertFalse(self.mv.supervisor.is_alive())
        self.mv._spatial_tick.assert_called_once_with()
        self.mv.ensure_tunnel.assert_not_called()


class SpatialMode(unittest.TestCase):
    """Drive the actual companion launch/stop paths against an in-memory helper."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native",
            "FRAME_WORKSPACE_DECODER": "v4l2", "FRAME_WINDOW_DISPLAYS": "0", "FRAME_NATIVE_HOST": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("FRAME_WINDOW_DECODER", None)
        self.mv = frame_macview.MacView(["ssh"], self.remote_run, "fake-frame")
        self.mv._exit_helper_frame = self.mv.frame  # the bar installed its local helper before Start
        self.mv.agent = mock.Mock(poll=lambda: None)
        self.mv.workspace_manager = mock.Mock()
        self.mv.workspace_manager.state.return_value = {"state": "stopped"}
        self.mv.ensure_tunnel = mock.Mock()
        self.mv._supervise = mock.Mock()
        self.mv._end_viewer_browser = mock.Mock()
        self.mv.call = self.call
        self.jobs, self.events, self.launches = [], [], []
        self.mv._dash_run = lambda busy, fn, *args: self.jobs.append((fn, args))
        self.mv._action_run = lambda fn, *args: self.jobs.append((fn, args))
        # Window launches run as jobs too, so their order is deterministic here.
        self.mv._launch_run = lambda fn, *args: self.jobs.append((fn, args))
        self.running, self.removed, self.fail_start = False, True, False
        self.failed_panels = set()
        self.windows = [self.window(42)]
        self.streams = []
        # What the Frame's viewer reports (scripts/workspace-native-update-on-frame.py status).
        self.viewer = {"installed": {"present": True, "identity": self.mv.headset_viewer.expected(), "protocol": 2,
                                     "capabilities": ["host", "roles", "surfaces", "wall-slots", "idle-auxiliary", "window-layout",
                                                      "content-input", "gaze-highlight",
                                                      "capabilities"]},
                       "sessionActive": False, "job": None}

    @staticmethod
    def window(wid):
        return {"id": wid, "src": f"window:{wid}", "title": f"Shell {wid}", "app": "Terminal",
                "bundleID": "com.apple.Terminal", "pid": 123, "x": 1672, "y": 1205, "w": 900, "h": 600}

    def call(self, path, method="GET", **query):
        self.events.append((path, query))
        if path == "/spatial/start":
            if self.fail_start:
                raise frame_macview.MacViewError("Mirror Setup Failed")
            self.running, self.removed = True, False
        if path == "/spatial/stop":
            self.running = False
            self.streams.clear()
        if path == "/spatial/cover":
            if not self.running:
                raise frame_macview.MacViewError("The Mac Streaming Helper: Spatial Mode is not running")
            return {"cover": query["show"] == "1"}
        if path.startswith("/spatial"):
            return {"state": "running" if self.running else "stopped", "removed": self.removed,
                    "windows": [{k: v for k, v in w.items() if k != "src"} for w in self.windows],
                    "display": {"id": 99, "bounds": [1672, 1169, 3600, 2250], "scale": 2, "menuBarDisplay": 99},
                    "skipped": [{"id": 44, "reason": "Full Screen"}], "menus": [],
                    "focusedSrc": "spatial:42", "revision": 1}
        if path == "/status":
            return {"screen": True, "accessibility": True, "streams": list(self.streams), "finished": []}
        if path == "/windows":
            return {"windows": list(self.windows)}
        if path == "/ticket":
            self.streams.append({"src": query["src"]})
            return {"ticket": "single-use"}
        if path == "/close":
            self.streams = [s for s in self.streams if s["src"] != query.get("src")]
            return {"closed": 1}
        return {}

    def remote_run(self, command, **kwargs):
        self.events.append(("remote", command))
        if command == "python3 - status":
            return json.dumps(self.viewer) + "\n"
        if "'start'" in command:
            args = shlex.split(command)
            panel = args[args.index("--panel") + 1]
            self.launches.append(args)
            if panel in self.failed_panels:
                raise frame_macview.MacViewError("Panel Launch Failed")
            # The launcher's own words: a window panel has a picture; an idle
            # Menu Bar, Status or menu slot registers with none.
            idle = "" if panel.startswith("w") else " (idle, no picture yet)"
            return ("Native Workspace ready: registered OpenVR key com.frame-workspace.native.1000.panel-" + panel +
                    " in frame-workspace-host-1000.service" + idle)
        return "stopped"

    def jobs_once(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)

    def drain(self):
        for _ in range(50):
            if not self.jobs:
                return
            self.jobs_once()
        self.fail("reconciliation kept queueing jobs")

    def panel_launches(self):
        return [a[a.index("--panel") + 1] for a in self.launches]

    def window_launch(self, panel="w42"):
        return next(a for a in reversed(self.launches) if a[a.index("--panel") + 1] == panel)

    def start(self):
        self.mv.set_mode("spatial")
        result = self.mv.start_workspace(quality="balanced")
        self.drain()
        return result

    def test_wall_slots_follow_focus_order_reuse_and_saved_pose_precedence(self):
        self.windows = [self.window(90), self.window(42), self.window(3)]
        self.start()
        # All three launch together; the focused window takes the centre.
        self.assertEqual(self.panel_launches()[:3], ["w42", "w90", "w3"])
        self.assertEqual(self.mv.native_panels["spatial:42"]["slot"], 0)
        self.assertEqual(self.mv.native_panels["spatial:90"]["slot"], 1)
        self.assertEqual(self.mv.native_panels["spatial:3"]["slot"], 2)
        for launch in self.launches:
            if launch[launch.index("--panel") + 1].startswith("w"):
                self.assertIn("--slot", launch)
            else:
                self.assertNotIn("--slot", launch)
        self.windows = [self.window(90), self.window(3), self.window(77)]
        self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.mv.native_panels["spatial:77"]["slot"], 0)
        pose = {"version": 1, "width": .2, "distance": 1.75, "curvature": 0,
                "matrix": [1,0,0,0,0,1,0,0,0,0,1,-1.75]}
        self.mv.show("spatial:90", pose=pose)
        self.assertIsNone(self.mv.native_panels["spatial:90"]["slot"])
        self.assertIn("--pose", self.launches[-1])
        self.assertNotIn("--slot", self.launches[-1])
        self.windows.append(self.window(88))
        self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.mv.native_panels["spatial:88"]["slot"], 1)

    def placeholder_syncs(self):
        """Each placeholder list sent to the Frame, newest last."""
        lists = []
        for event, command in self.events:
            if event == "remote" and "placeholders" in command:
                args = shlex.split(command)
                lists.append(json.loads(args[args.index("placeholders") + 1])["panels"])
        return lists

    def test_window_panels_launch_in_parallel_bounded_with_focused_first(self):
        self.windows = [self.window(i) for i in (10, 11, 12, 42, 13, 14, 15)]
        self.mv.set_mode("spatial")
        self.mv.start_workspace(quality="balanced")
        self.jobs_once()  # one reconciliation
        launches = [fn for fn, _ in self.jobs if fn == self.mv._launch_spatial_window]
        self.assertEqual(len(launches), 4, "four at a time")
        self.assertEqual([args[0] for fn, args in self.jobs if fn == self.mv._launch_spatial_window],
                         ["spatial:42", "spatial:10", "spatial:11", "spatial:12"])
        # Every window shows a placeholder at its own slot straight away.
        first = self.placeholder_syncs()[-1]
        self.assertEqual([p["panel"] for p in first], ["w10", "w11", "w12", "w42", "w13", "w14", "w15"])
        slots = {p["panel"]: p["slot"] for p in first}
        self.assertEqual(slots["w42"], 0)
        self.assertEqual(sorted(slots.values()), list(range(7)))
        self.assertEqual(first[0]["name"], "Shell 10")
        self.assertEqual((first[0]["bundleID"], first[0]["points"]), ("com.apple.Terminal", [900, 600]))
        self.assertNotIn("error", first[0])
        # A second reconciliation while four are in flight starts no more.
        self.mv._reconcile_spatial_windows(self.mv._mode_generation)
        self.assertEqual(sum(1 for fn, _ in self.jobs if fn == self.mv._launch_spatial_window), 4)
        self.drain()
        windows = [p for p in self.panel_launches() if p.startswith("w")]
        self.assertEqual(windows[:4], ["w42", "w10", "w11", "w12"])
        self.assertEqual(sorted(windows[4:]), ["w13", "w14", "w15"])
        # Menu Bar, Status and the menu slots only after every window.
        last_window = max(i for i, p in enumerate(self.panel_launches()) if p.startswith("w"))
        first_aux = min(i for i, p in enumerate(self.panel_launches()) if not p.startswith("w"))
        self.assertLess(last_window, first_aux)
        for panel in ("w42", "w13"):
            args = self.window_launch(panel)
            self.assertEqual(args[args.index("--slot") + 1], str(slots[panel]), "the panel opens at its placeholder's slot")
            self.assertEqual(args[args.index("--bundle-id") + 1], "com.apple.Terminal")
            self.assertEqual(args[args.index("--points") + 1], "900x600")
        self.assertEqual(self.placeholder_syncs()[-1], [], "live panels replace every placeholder")
        self.assertTrue(self.mv.workspace_state()["panelsReady"])

    def test_window_launches_really_overlap_on_their_own_threads(self):
        self.windows = [self.window(42), self.window(43), self.window(44)]
        self.mv._launch_run = self.mv._launch_thread
        gate, entered, guard = threading.Event(), [], threading.Lock()
        real = self.remote_run

        def run(command, **kwargs):
            if "'start'" in command and "'--panel' 'w4" in command:
                with guard:
                    entered.append(command)
                gate.wait(5)
            return real(command, **kwargs)
        self.mv.run = run
        self.mv.set_mode("spatial")
        self.mv.start_workspace(quality="balanced")
        self.jobs_once()
        deadline = time.monotonic() + 5
        while len(entered) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(entered), 3, "three window launches in progress at once")
        gate.set()
        while len([s for s in self.mv.native_panels if s.startswith("spatial:")]) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual({s for s in self.mv.native_panels if s.startswith("spatial:")},
                         {"spatial:42", "spatial:43", "spatial:44"})
        self.assertEqual(self.mv._spatial_launching, {})

    def test_launch_parallelism_is_configurable_and_validated(self):
        self.windows = [self.window(i) for i in (10, 11, 12)]
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_LAUNCH_PARALLEL": "1"}):
            self.mv.set_mode("spatial")
            self.mv.start_workspace(quality="balanced")
            self.jobs_once()
            self.assertEqual(sum(1 for fn, _ in self.jobs if fn == self.mv._launch_spatial_window), 1)
            self.drain()
        self.assertEqual([p for p in self.panel_launches() if p.startswith("w")], ["w10", "w11", "w12"])
        self.removed = True
        self.mv.stop_workspace()
        for bad in ("0", "9", "four"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"FRAME_SPATIAL_LAUNCH_PARALLEL": bad}):
                with self.assertRaisesRegex(frame_macview.MacViewError, "FRAME_SPATIAL_LAUNCH_PARALLEL"):
                    self.mv.start_workspace(quality="balanced")

    def test_failed_launch_shows_its_error_on_the_placeholder_and_retries(self):
        self.windows = [self.window(42), self.window(43)]
        self.failed_panels.add("w43")
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.start()
        self.assertIn("spatial:43", self.mv.workspace_state()["panelErrors"])
        failed = self.placeholder_syncs()[-1]
        self.assertEqual([p["panel"] for p in failed], ["w43"])
        self.assertEqual(failed[0]["error"], frame_macview.PLACEHOLDER_ERROR)
        self.assertEqual(failed[0]["slot"], 1, "the failed window keeps its slot")
        self.assertIn("Couldn't Show Terminal: Panel Launch Failed", [e["message"] for e in self.mv.errors_state()])
        self.failed_panels.clear()
        with mock.patch.object(frame_macview.time, "monotonic", return_value=106):
            self.mv._spatial_mode_tick(); self.drain()
        self.assertIn("spatial:43", self.mv.native_panels)
        self.assertEqual(self.mv.native_panels["spatial:43"]["slot"], 1)
        self.assertEqual(self.placeholder_syncs()[-1], [])

    def test_a_leftover_capture_is_replaced_without_suppressing_the_window(self):
        self.windows = [self.window(42), self.window(43)]
        self.streams.append({"src": "spatial:43"})  # left by an earlier attempt
        self.start()
        self.assertIn("spatial:43", self.mv.native_panels)
        self.assertNotIn("spatial:43", self.mv._spatial_suppressed)
        self.assertIn(("/close", {"src": "spatial:43"}), self.events)

    def test_a_placeholder_close_stops_that_window_opening(self):
        self.windows = [self.window(42), self.window(43)]
        self.failed_panels.add("w43")
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.start()
        self.mv._handle_frame_requests('{"action":"close","key":"com.frame-workspace.native.1000.panel-w43"}')
        self.drain()
        self.assertIn("spatial:43", self.mv._spatial_suppressed)
        self.assertEqual(self.placeholder_syncs()[-1], [])
        launches = len(self.launches)
        self.failed_panels.clear()
        with mock.patch.object(frame_macview.time, "monotonic", return_value=110):
            self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(len(self.launches), launches, "a closed placeholder's window is not retried")

    def test_stop_clears_placeholders_left_on_the_frame(self):
        self.windows = [self.window(42), self.window(43)]
        self.failed_panels.add("w43")
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.start()
        self.assertTrue(self.mv._placeholders_on_frame)
        self.removed = True
        self.mv.stop_workspace()
        self.assertEqual(self.placeholder_syncs()[-1], [])
        self.assertFalse(self.mv._placeholders_on_frame)

    def test_arrange_windows_from_the_bar_and_the_mac_page(self):
        with self.assertRaisesRegex(frame_macview.MacViewError, "Needs Spatial Mode"):
            self.mv.arrange_windows()
        self.start()
        self.mv.spatial_apps.state = {"mode": "spatial", "apps": []}
        result = self.mv.arrange_windows()
        self.assertTrue(result["arranged"])
        self.assertEqual(self.mv.spatial_apps.arrange_seq, 1)
        self.assertEqual(self.mv.spatial_apps.state["arrangeSeq"], 1)
        self.mv._handle_frame_requests('{"action":"arrange_windows"}')
        self.assertEqual(self.mv.spatial_apps.state["arrangeSeq"], 2)
        self.assertEqual(self.launches[-1], self.launches[-1])  # no panel relaunch
        launches = len(self.launches)
        self.drain()
        self.assertEqual(len(self.launches), launches)

    def test_hand_movement_releases_only_current_generation_slot(self):
        self.start()
        panel = self.mv.native_panels["spatial:42"]
        request = {"action": "wall_release", "key": panel["key"], "generation": "stale"}
        self.mv._handle_frame_requests(json.dumps(request))
        self.assertEqual(panel["slot"], 0)
        request["generation"] = panel["generation"]
        self.mv._handle_frame_requests(json.dumps(request))
        self.assertIsNone(panel["slot"])
        self.windows.append(self.window(43))
        self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.mv.native_panels["spatial:43"]["slot"], 0)

    def test_start_uses_shared_display_spatial_sources_and_shared_host(self):
        result = self.start()
        self.assertEqual(result["mode"], "spatial")
        self.assertEqual(result["workspace"]["state"], "running")
        self.mv.workspace_manager.start.assert_not_called()
        self.assertIsNone(self.mv.workspace_panel_key)
        self.assertEqual(self.mv.shown, {"spatial:42", *frame_macview.SPATIAL_AUX_ROLES})
        self.assertTrue(self.mv.native_panels["spatial:42"]["host"])
        self.assertEqual(self.mv.native_panels["spatial:42"]["title"], "Shell 42")
        self.assertEqual(self.window_launch()[0], "FRAME_NATIVE_HOST=1")
        self.assertIn("src=spatial%3A42", " ".join(self.window_launch()))
        self.assertTrue(self.mv.spatial_workspace["panelsReady"])
        self.assertTrue(self.mv.dash_state()["workspace"]["running"])
        self.assertEqual(self.mv.dash_state()["workspace"]["mode"], "spatial")
        self.assertLess(next(i for i, e in enumerate(self.events) if e[0] == "remote"),
                        next(i for i, e in enumerate(self.events) if e[0] == "/spatial/start"))

    def test_shared_display_snapshot_and_actual_window_size_reach_panel_launch(self):
        self.windows[0].update(w=3600, h=2119)
        self.start()
        snapshot = self.mv.workspace_state()["home"]
        self.assertEqual(snapshot["display"]["bounds"], [1672, 1169, 3600, 2250])
        self.assertEqual(snapshot["skipped"], [{"id": 44, "reason": "Full Screen"}])
        self.assertEqual(snapshot["menus"], [])
        self.assertEqual(snapshot["focusedSrc"], "spatial:42")
        self.assertNotIn("src", snapshot["windows"][0])
        args = self.window_launch()
        start = args.index("start")
        self.assertEqual(args[start + 2:start + 4], list(map(str, frame_macview.fit(3600, 2119))))
        self.assertNotIn("spatial:44", self.mv.native_panels)

    def test_windows_launch_before_idle_auxiliary_roles_once_and_stop_with_the_mode(self):
        self.start()
        self.assertEqual(self.panel_launches(), ["w42", "menubar", "status", "menu0", "menu1", "menu2", "menu3"])
        for args, (source, role) in zip(self.launches[1:], frame_macview.SPATIAL_AUX_ROLES.items()):
            self.assertEqual(args[args.index("--panel") + 1], source.replace(":", ""))
            self.assertEqual(args[args.index("--role") + 1], role)
            self.assertIn("src=" + frame_macview.quote(source, safe=""), args[args.index("start") + 1])
            self.assertTrue(self.mv.native_panels[source]["host"])
        for args in self.launches:
            self.assertEqual(args[-2:], ["software", "cpu"])
        self.assertEqual(self.mv.native_panels["spatial:42"]["slot"], 0)
        self.mv._spatial_mode_tick()
        self.jobs_once()
        self.assertEqual(len(self.launches), 7)
        self.removed = True
        self.mv.stop_workspace()
        self.assertFalse(self.mv.native_panels)
        self.assertFalse(self.mv.shown)
        stopped = [shlex.split(cmd) for event, cmd in self.events
                   if event == "remote" and shlex.split(cmd)[-1:] == ["stop"]]
        self.assertEqual({args[args.index("--panel") + 1] for args in stopped},
                         {"w42", "menubar", "status", "menu0", "menu1", "menu2", "menu3"})

    def test_invalid_window_decoder_fails_before_spatial_mode_or_display_change(self):
        with mock.patch.dict(os.environ, {"FRAME_WINDOW_DECODER": "auto"}):
            with self.assertRaisesRegex(frame_macview.MacViewError, "FRAME_WINDOW_DECODER must be software or v4l2"):
                self.mv.start_workspace(mode="spatial")
        self.assertEqual(self.mv.mode, "desktop")
        self.assertEqual(self.events, [])
        self.assertEqual(self.launches, [])
        self.mv.ensure_tunnel.assert_not_called()

    def test_invalid_window_decoder_does_not_stop_running_workspace_on_mode_switch(self):
        self.mv.workspace_manager.state.return_value = {"state": "running"}
        self.mv.stop_workspace = mock.Mock()
        with mock.patch.dict(os.environ, {"FRAME_WINDOW_DECODER": "auto"}):
            with self.assertRaisesRegex(frame_macview.MacViewError, "FRAME_WINDOW_DECODER must be software or v4l2"):
                self.mv.set_mode("spatial")
        self.assertEqual(self.mv.mode, "desktop")
        self.mv.stop_workspace.assert_not_called()
        self.assertEqual(self.events, [])

    def test_failing_auxiliary_panels_never_delay_window_panels(self):
        self.failed_panels.update({"menubar", "status", "menu0", "menu1", "menu2", "menu3"})
        self.windows = [self.window(42), self.window(43)]
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.mv.set_mode("spatial")
            self.mv.start_workspace(quality="balanced")
            self.jobs_once()  # reconciliation: both windows start launching at once
            self.assertEqual(self.panel_launches(), [])
            self.jobs_once()
            self.assertEqual(self.panel_launches(), ["w42", "w43"])
            self.drain()
        self.assertEqual(self.panel_launches()[:2], ["w42", "w43"])
        self.assertEqual(set(self.panel_launches()[2:]), {"menubar", "status", "menu0", "menu1", "menu2", "menu3"})
        self.assertEqual({s for s in self.mv.native_panels}, {"spatial:42", "spatial:43"})
        errors = [e["message"] for e in self.mv.errors_state()]
        self.assertIn("Couldn't Open The Menu Bar Panel: Panel Launch Failed", errors)
        self.assertFalse(self.mv.workspace_state()["panelsReady"])
        # A window that appears while every auxiliary panel is retrying goes first.
        self.windows.append(self.window(44))
        with mock.patch.object(frame_macview.time, "monotonic", return_value=101):
            self.mv._spatial_mode_tick()
            self.jobs_once()
            self.jobs_once()
        self.assertEqual(self.panel_launches()[-1], "w44")

    def covers(self):
        return [q["show"] for path, q in self.events if path == "/spatial/cover"]

    def test_laptop_cover_waits_for_the_first_window_panel_and_lifts_without_one(self):
        self.windows = []
        self.start()
        self.assertEqual(self.covers(), [], "auxiliary panels alone never black out the laptop")
        self.windows = [self.window(42)]
        self.failed_panels.add("w42")
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.covers(), [], "a failed window launch keeps the laptop visible")
        self.failed_panels.clear()
        with mock.patch.object(frame_macview.time, "monotonic", return_value=106):
            self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.covers(), ["1"])
        cover_at = next(i for i, e in enumerate(self.events) if e[0] == "/spatial/cover")
        launched_at = max(i for i, e in enumerate(self.events) if e[0] == "remote" and "'w42'" in e[1])
        self.assertLess(launched_at, cover_at, "the cover follows the window panel's registration")
        self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.covers(), ["1"], "no repeated requests")
        self.windows = []
        self.mv._spatial_mode_tick(); self.drain()
        self.assertEqual(self.covers(), ["1", "0"], "the last window panel closing uncovers the laptop")

    def test_failed_spatial_start_never_asks_for_the_cover(self):
        self.mv.set_mode("spatial")
        self.fail_start = True
        with self.assertRaises(frame_macview.MacViewError):
            self.mv.start_workspace()
        self.drain()
        self.assertEqual(self.covers(), [])

    def test_viewer_without_spatial_capabilities_is_refused_before_the_mac_changes(self):
        for installed, message in (({"present": True, "capabilities": None}, "too old for Spatial Mode"),
                                   ({"present": True, "identity": "a" * 64, "protocol": 2,
                                     "capabilities": ["host", "roles"]}, "too old for Spatial Mode"),
                                   # Before FW-15 window layout: it would stack windows at stale poses.
                                   ({"present": True, "identity": "a" * 64, "protocol": 2,
                                     "capabilities": ["host", "roles", "surfaces", "wall-slots", "idle-auxiliary",
                                                      "content-input", "capabilities"]}, "too old for Spatial Mode"),
                                   ({"present": False}, "Install the native window host")):
            with self.subTest(installed=installed):
                self.events.clear()
                self.viewer["installed"] = installed
                self.mv.set_mode("spatial")
                with self.assertRaisesRegex(frame_macview.MacViewError, message):
                    self.mv.start_workspace(quality="balanced")
                self.assertNotIn("/spatial/start", [e[0] for e in self.events])
                self.assertNotIn("/spatial/cover", [e[0] for e in self.events])
                self.assertFalse(self.launches)
                self.assertEqual(self.mv.workspace_state()["state"], "stopped")
        self.assertEqual(self.mv.headset_viewer.state()["state"], "missing")

    def test_capable_viewer_from_other_sources_starts_and_offers_the_update(self):
        self.viewer["installed"]["identity"] = "b" * 64
        self.start()
        self.assertIn("spatial:42", self.mv.native_panels)
        viewer = self.mv.headset_viewer.state()
        self.assertEqual((viewer["state"], viewer["label"], viewer["canUpdate"]),
                         ("outdated", "Different From This Mac App", True))

    def test_no_workspace_starts_while_the_viewer_updates_and_no_update_while_running(self):
        self.mv.headset_viewer.update = {"state": "building"}
        self.mv.set_mode("spatial")
        with self.assertRaisesRegex(frame_macview.MacViewError, "headset viewer is updating"):
            self.mv.start_workspace(quality="balanced")
        self.assertFalse([e for e in self.events if e[0] == "/spatial/start"])
        self.mv.headset_viewer.update = {"state": "idle"}
        self.start()
        with self.assertRaisesRegex(frame_macview.MacViewError, "Stop the Workspace before updating"):
            self.mv.update_headset_viewer()

    def test_mac_page_stop_preempts_startup_between_stages(self):
        self.mv.set_mode("spatial")
        real_call = self.call
        def call(path, method="GET", **query):
            if path == "/spatial/start":
                self.mv._stop_requested += 1  # Stop pressed on the Mac page while the display comes up
            return real_call(path, method, **query)
        self.mv.call = call
        with self.assertRaisesRegex(frame_macview.MacViewError, "Workspace Stop cancelled Spatial Mode startup"):
            self.mv.start_workspace(quality="balanced")
        self.assertEqual(self.events[-1][0], "/spatial/stop")
        self.assertFalse(self.launches)
        self.assertFalse(self.jobs)

    def test_stop_during_a_window_launch_ends_reconciliation_at_that_stage(self):
        self.windows = [self.window(42), self.window(43)]
        self.mv.set_mode("spatial")
        self.mv.start_workspace(quality="balanced")
        real_run = self.remote_run
        def run(command, **kwargs):
            out = real_run(command, **kwargs)
            if "'start'" in command:
                self.mv._stop_requested += 1
            return out
        self.mv.run = run
        self.jobs_once()  # reconciliation queues both launches
        self.jobs_once()  # the first one's Stop cancels the second before it starts
        self.assertEqual(self.panel_launches(), ["w42"])
        self.assertFalse(self.jobs, "no follow-up launch is queued once Stop is waiting")
        self.assertNotIn("spatial:42", self.mv.native_panels, "a launch that finished after Stop is closed")
        self.mv._stop_requested -= 1
        self.removed = True
        self.mv.run = real_run
        self.mv.stop_workspace()
        self.assertFalse(self.mv.native_panels)
        self.assertEqual(self.mv._stop_requested, 0)

    def test_failed_workspace_job_is_recorded_for_the_bar_and_mac_page(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        def fail():
            raise frame_macview.MacViewError("Spatial Mode Could Not Start: Mirror Setup Failed")
        mv._dash_run("starting", fail)
        mv.dash_jobs.join()
        self.assertEqual([e["message"] for e in mv.errors_state()], ["Spatial Mode Could Not Start: Mirror Setup Failed"])
        self.assertEqual(mv.dash_state()["workspace"]["notice"], "Spatial Mode Could Not Start: Mirror Setup Failed")
        with mock.patch.object(frame_macview.time, "time", return_value=time.time() + 60):
            self.assertNotIn("notice", mv.dash_state()["workspace"])

    def test_auxiliary_launch_failure_retries_without_relaunching_other_panels(self):
        self.failed_panels.add("menu2")
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.start()
        self.assertNotIn("menu:2", self.mv.native_panels)
        self.assertNotIn("menu:2", self.mv.shown)
        self.assertIn(("/close", {"src": "menu:2"}), self.events)
        self.assertIn("menu:2", self.mv.workspace_state()["panelErrors"])
        self.assertFalse(self.mv.workspace_state()["panelsReady"])
        self.failed_panels.clear()
        with mock.patch.object(frame_macview.time, "monotonic", return_value=104):
            self.mv._spatial_mode_tick()
            self.jobs_once()
        self.assertEqual(len(self.launches), 7)
        with mock.patch.object(frame_macview.time, "monotonic", return_value=105):
            self.mv._spatial_mode_tick()
            self.jobs_once()
        self.assertEqual(len(self.launches), 8)
        self.assertTrue(self.mv.workspace_state()["panelsReady"])
        self.assertEqual(self.mv.workspace_state()["panelErrors"], {})

    def test_spatial_sources_need_spatial_mode_and_separate_sources_stay_desktop_only(self):
        with self.assertRaisesRegex(frame_macview.MacViewError, "Start Spatial Mode"):
            self.mv.show("spatial:42")
        self.start()
        with self.assertRaisesRegex(frame_macview.MacViewError, "Require Desktop Mode"):
            self.mv.show("separate:42")

    def test_window_show_waits_for_an_active_spatial_setup(self):
        self.mv.set_mode("spatial")
        with self.assertRaisesRegex(frame_macview.MacViewError, "Start Spatial Mode"):
            self.mv.show("window:42")
        self.assertFalse(self.launches)

    def test_bar_mode_requests_use_the_same_lifecycle(self):
        self.mv.set_mode = mock.Mock()
        self.mv._handle_frame_requests('{"action":"mode_spatial"}\n{"action":"mode_desktop"}')
        self.jobs_once()
        self.assertEqual(self.mv.set_mode.call_args_list, [mock.call("spatial"), mock.call("desktop")])

    def test_new_and_closed_windows_reconcile_without_duplicate_launches(self):
        self.start()
        self.windows.append(self.window(43))
        self.mv._spatial_mode_tick()
        self.mv._spatial_mode_tick()  # a single job while one is pending
        self.assertEqual(len(self.jobs), 1)
        self.jobs_once()
        self.jobs_once()
        self.assertEqual(set(self.mv.native_panels), {"spatial:42", "spatial:43", *frame_macview.SPATIAL_AUX_ROLES})
        self.windows.pop(0)
        self.mv._spatial_mode_tick()
        self.jobs_once()
        self.assertEqual(set(self.mv.native_panels), {"spatial:43", *frame_macview.SPATIAL_AUX_ROLES})
        self.assertEqual(len(self.launches), 8)

    def test_failed_window_launch_keeps_its_pose_and_retries_the_spatial_source(self):
        self.start()
        self.windows.append(self.window(43))
        pose = {"version": 1, "width": 1.2, "distance": 2, "curvature": .2,
                "matrix": [1, 0, 0, 0, 0, 1, 0, 1.4, 0, 0, 1, -2]}
        self.mv._spatial_resume_poses["spatial:43"] = pose
        self.failed_panels.add("w43")
        with mock.patch.object(frame_macview.time, "monotonic", return_value=100):
            self.mv._spatial_mode_tick()
            self.drain()
        self.assertIn("spatial:43", self.mv.workspace_state()["panelErrors"])
        self.assertEqual(self.mv._spatial_resume_poses["spatial:43"], pose)
        self.failed_panels.clear()
        with mock.patch.object(frame_macview.time, "monotonic", return_value=105):
            self.mv._spatial_mode_tick()
            self.drain()
        self.assertIn("spatial:43", self.mv.native_panels)
        self.assertNotIn("spatial:43", self.mv._spatial_resume_poses)
        args = self.launches[-1]
        self.assertEqual(json.loads(args[args.index("--pose") + 1]), pose)

    def test_unconfirmed_stop_blocks_mode_change_then_retry_restores_first(self):
        self.start()
        self.mv.before_workspace_stop = lambda: self.events.append(("snapshot", self.mv.mode))
        with self.assertRaisesRegex(frame_macview.MacViewError, "Removal Is Still Pending"):
            self.mv.set_mode("desktop")
        self.assertEqual(self.mv.mode, "spatial")
        self.assertEqual(self.mv.workspace_state()["state"], "error")
        self.mv.workspace_manager.start.assert_not_called()
        self.removed = True
        self.mv.set_mode("desktop")
        self.assertEqual(self.mv.mode, "desktop")
        self.assertFalse(self.mv.native_panels)
        self.assertLess(next(i for i, e in enumerate(self.events) if e[0] == "snapshot"),
                        next(i for i, e in enumerate(self.events) if e[0] == "/spatial/stop"))

    def test_switch_running_mode_restarts_only_after_cleanup(self):
        self.start()
        self.removed = True
        def desktop(*args):
            self.events.append(("desktop-start", None))
            self.assertFalse(self.mv.native_panels)
            self.assertFalse(self.running)
            return {"workspace": {"state": "running"}}
        self.mv._start_workspace = desktop
        self.mv.set_mode("desktop")
        self.assertEqual(self.events[-1][0], "desktop-start")

    def test_failed_start_rolls_back_and_preserves_unconfirmed_cleanup(self):
        self.mv.set_mode("spatial")
        self.fail_start, self.removed = True, False
        with self.assertRaisesRegex(frame_macview.MacViewError, "Mirror Setup Failed"):
            self.mv.start_workspace()
        self.assertEqual(self.events[-1][0], "/spatial/stop")
        self.assertFalse(self.mv.workspace_state()["removed"])
        self.assertFalse(self.launches)

    def test_old_panel_job_cannot_run_after_stop_or_mode_change(self):
        self.mv.set_mode("spatial")
        self.mv.start_workspace()
        self.removed = True
        self.mv.stop_workspace()
        self.jobs_once()
        self.assertFalse(self.launches)

    def test_restarted_helper_does_not_inherit_running_spatial_ownership(self):
        self.start()
        self.mv.agent = mock.Mock(poll=lambda: None)
        self.running, self.removed = False, True
        self.mv._spatial_mode_tick()
        self.jobs_once()
        self.assertEqual(self.mv.workspace_state()["state"], "error")
        self.assertTrue(self.mv.workspace_state()["removed"])
        self.assertFalse(self.mv.native_panels)
        self.assertEqual(len(self.launches), 7)

    def test_frame_panel_cleanup_failure_keeps_stop_available_after_mac_restore(self):
        self.start()
        self.removed = True
        self.mv._stop_native_panels = mock.Mock()  # remote host did not acknowledge the removal
        with self.assertRaisesRegex(frame_macview.MacViewError, "Panel Shutdown Is Unconfirmed"):
            self.mv.stop_workspace()
        self.assertFalse(self.running)
        self.assertTrue(self.mv.workspace_state()["removed"])
        self.assertTrue(self.mv.workspace_state()["panelCleanupPending"])
        self.assertEqual(self.mv.workspace_state()["state"], "error")

    def test_mode_choice_persists_without_dashboard_and_old_preferences_still_load(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "last-start.json"
            self.mv.last_workspace_file = path
            self.mv.set_mode("spatial")
            again = frame_macview.MacView([], mock.Mock(), "fake", last_workspace_file=path)
            self.assertEqual(again.mode, "spatial")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text(json.dumps({"preset": "2880x2560", "scale": 1, "quality": "sharp"}))
            old = frame_macview.MacView([], mock.Mock(), "fake", last_workspace_file=path)
            self.assertEqual(old.mode, "desktop")
            self.assertEqual(old.last_workspace[0], "2880x2560")

    def test_spatial_overrides_desktop_browser_flags_and_accepts_saved_spatial_pose(self):
        self.start()
        pose = {"version": 1, "width": 1.2, "distance": 2, "curvature": .2,
                "matrix": [1, 0, 0, 0, 0, 1, 0, 1.4, 0, 0, 1, -2]}
        self.windows.append(self.window(43))
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "browser", "FRAME_WINDOW_VIEWER": "browser"}):
            self.mv.show("window:43", pose=pose)
        args = self.launches[-1]
        self.assertEqual(json.loads(args[args.index("--pose") + 1]), pose)
        self.assertEqual(args[0], "FRAME_NATIVE_HOST=1")
        self.assertIn("spatial:43", self.mv.native_panels)

class WorkspaceLifecycle(unittest.TestCase):
    @staticmethod
    def fake_helper(path):
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if '--recover-orphan' in sys.argv:\n"
            " failed = os.environ.get('FRAME_WORKSPACE_TEST_FAIL_RECOVERY') == '1'\n"
            " print(json.dumps({'state':'error' if failed else 'stopped','removed':False if failed else True,"
            "'error':'test recovery could not confirm removal'}), flush=True)\n"
            " raise SystemExit(1 if failed else 0)\n"
            "width = int(sys.argv[sys.argv.index('--width') + 1])\n"
            "height = int(sys.argv[sys.argv.index('--height') + 1])\n"
            "scale = int(sys.argv[sys.argv.index('--scale') + 1])\n"
            "actual = {'logicalWidth':width,'logicalHeight':height,'bufferWidth':width*scale,"
            "'bufferHeight':height*scale,'scale':float(scale)}\n"
            "print(json.dumps({'state':'ready','name':'Frame Workspace','src':'display:4242','displayID':4242,"
            "'requested':{'logicalWidth':width,'logicalHeight':height,'scale':scale},'actual':actual,"
            "'bounds':{'x':1800,'y':0,'width':width,'height':height},'mirrored':False,'mirrorDisplayID':None,"
            "'main':False,'physicalLayoutUnchanged':True,'windowRestoreAvailable':True,'snapshottedWindows':3}), flush=True)\n"
            "sys.stdin.read()\n"
            "failed = os.environ.get('FRAME_WORKSPACE_TEST_FAIL_REMOVAL') == '1'\n"
            "registered = os.environ.get('FRAME_WORKSPACE_TEST_REGISTERED')\n"
            "if registered:\n"
            " open(registered, 'w').close() if failed else (os.path.exists(registered) and os.unlink(registered))\n"
            "print(json.dumps({'state':'stopped','displayID':4242,'reason':'stdin_eof','removed':not failed,"
            "'physicalLayoutUnchanged':True,'windowsRestored':1,"
            "'error':'test display removal not confirmed' if failed else None}), flush=True)\n",
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_manager_supervises_stable_workspace_key_and_fresh_display_source(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = self.fake_helper(Path(temp) / "fake-frame-mac-view")
            tracked = []
            manager = frame_macview.WorkspaceDisplayManager(executable, track=tracked.append)
            state, created = manager.start()
            self.assertTrue(created)
            self.assertEqual(state["id"], "workspace")
            self.assertEqual(state["captureSrc"], "display:4242")
            self.assertEqual(state["display"], {"logicalWidth": 5120, "logicalHeight": 1440,
                                                "bufferWidth": 5120, "bufferHeight": 1440})
            self.assertEqual(state["stream"], {"width": 4096, "height": 1152, "maxLong": 4096,
                                                "fps": 30, "codec": "h264"})
            self.assertEqual(frame_macview.quality_settings("sharp"), frame_macview.QUALITY["sharp"])
            self.assertEqual(frame_macview.quality_settings("sharp", workspace=True),
                             {"max": 4096, "fps": 30, "bpp": 0.14, "codec": "h264"})
            again, created = manager.start()
            self.assertFalse(created)
            self.assertEqual(again["id"], "workspace")
            self.assertEqual(len(tracked), 1)
            balanced, created = manager.start(quality="balanced")
            self.assertFalse(created)
            self.assertEqual(balanced["captureSrc"], state["captureSrc"])
            self.assertEqual(balanced["stream"]["width"], 1920)
            self.assertEqual(len(tracked), 1)
            stopped = manager.stop()
            self.assertTrue(stopped["removed"])
            self.assertEqual(stopped["windowsRestored"], 1)
            self.assertEqual(manager.state()["state"], "stopped")

    def test_start_rolls_back_display_when_frame_panel_cannot_open(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = frame_macview.WorkspaceDisplayManager(self.fake_helper(Path(temp) / "fake-frame-mac-view"))
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv._exit_helper_frame = mv.frame
            mv.workspace_manager = manager
            mv.build = lambda: None
            mv.remote_port = 47900
            mv.call = lambda path, method="GET", **query: (
                {"screen": True, "accessibility": True, "streams": [], "finished": []}
                if path == "/status" else {"ticket": "one-use"} if path == "/ticket" else {"closed": 1})
            mv.ensure_tunnel = lambda **kwargs: None
            def fail_launch(*args, **kwargs):
                error = RuntimeError("panel launch failed")
                error.stdout = ""
                raise error
            mv.run = fail_launch
            mv._end_viewer_browser = lambda *args: None
            with self.assertRaises(frame_macview.MacViewError) as cm:
                mv.start_workspace()
            self.assertIn("rolled back", str(cm.exception))
            self.assertEqual(manager.state()["state"], "stopped")
            self.assertNotIn("display:4242", mv.shown)

    def test_start_and_stop_use_workspace_key_but_close_only_its_display_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = frame_macview.WorkspaceDisplayManager(self.fake_helper(Path(temp) / "fake-frame-mac-view"))
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv._exit_helper_frame = mv.frame
            mv.workspace_manager = manager
            mv.build = lambda: None
            mv.remote_port = 47900
            mv.agent = mock.Mock(poll=lambda: None)
            closes = []
            active = False
            def call(path, method="GET", **query):
                nonlocal active
                if path == "/status":
                    return {"screen": True, "accessibility": True,
                            "streams": ([{"src": "display:4242"}] if active else []), "finished": []}
                if path == "/ticket":
                    return {"ticket": "one-use"}
                if path == "/close":
                    closes.append(query.get("src"))
                    active = False
                    return {"closed": 1}
                return {"displays": []}
            mv.call = call
            mv.ensure_tunnel = lambda **kwargs: None
            launches = []
            def run(remote, **kwargs):
                nonlocal active
                launches.append((remote, kwargs))
                if "bash -s -- '--session'" in remote and "'start'" in remote:
                    tag = re.search(r"'(fc[0-9a-f]{8})'$", remote).group(1)
                    active = True
                    return f"Workspace viewer ready: 5120x1440 and registered OpenVR key com.frame-control.workspace.1000.{tag}"
                if "--showdashboard" in remote:
                    return "focus accepted"
                return "panel opened"
            mv.run = run
            mv._end_viewer_browser = lambda *args: None
            shown = mv.start_workspace()
            self.assertEqual(shown["src"], "workspace")
            self.assertEqual(shown["captureSrc"], "display:4242")
            self.assertEqual(shown["workspace"]["id"], "workspace")
            self.assertEqual(shown["panel"], "com.frame-control.workspace.1000." + re.search(r"fc[0-9a-f]{8}", shown["detail"]).group(0))
            self.assertIn("bash -s -- '--session'", launches[0][0])
            self.assertIn("--nested-width", launches[0][1]["stdin"])
            self.assertIn("--vr-overlay-explicit-name \"Frame Workspace\"", launches[0][1]["stdin"])
            already = mv.start_workspace(quality="sharp")
            self.assertTrue(already["alreadyRunning"])
            self.assertEqual(already["panel"], shown["panel"])
            self.assertIn("--showdashboard", launches[-1][0])
            changed = mv.start_workspace(quality="balanced")
            self.assertEqual(changed["workspace"]["quality"], "balanced")
            self.assertIn("'1920' '540'", launches[2][0])
            self.assertEqual(closes, ["display:4242"])
            stopped = mv.stop_workspace()
            self.assertTrue(stopped["removed"])
            self.assertEqual(stopped["panelClosed"], 1)
            self.assertEqual(closes, ["display:4242", "display:4242"])
            self.assertFalse(mv.shown)

    def test_running_workspace_switches_layouts_only_after_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = self.fake_helper(Path(temp) / "fake-frame-mac-view")
            registered = Path(temp) / "registered-workspace"
            starts, viewer_starts, viewer_stops, closes, events = [], [], [], [], []

            def track(proc):
                starts.append(proc.args)
                events.append("display-start")

            manager = frame_macview.WorkspaceDisplayManager(helper, track=track)
            stop_owner = manager.stop

            def stop_owner_and_record():
                result = stop_owner()
                events.append(("display-removed", result.get("removed")))
                return result

            manager.stop = stop_owner_and_record
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv._exit_helper_frame = mv.frame
            mv.workspace_manager = manager
            mv.build = mock.Mock()
            mv.remote_port = 47900
            mv.agent = mock.Mock(poll=lambda: None)
            active = False

            def call(path, method="GET", **query):
                nonlocal active
                if path == "/status":
                    stream = ([{"src": manager.state()["captureSrc"]}] if active else [])
                    return {"screen": True, "accessibility": True, "streams": stream, "finished": []}
                if path == "/ticket":
                    return {"ticket": "one-use"}
                if path == "/close":
                    closes.append(query.get("src"))
                    events.append("stream-close")
                    active = False
                    return {"closed": 1}
                if path == "/displays":
                    return {"displays": ([{"name": "Frame Workspace", "id": 4242, "src": "display:4242"}]
                                         if registered.exists() else [])}
                return {}

            def run(remote, **kwargs):
                nonlocal active
                if remote == "bash -s -- stop":
                    viewer_stops.append(remote)
                    events.append("viewer-stop")
                    active = False
                    return "Workspace viewer stopped"
                if "bash -s -- '--session'" in remote and "'start'" in remote:
                    viewer_starts.append((remote, kwargs))
                    events.append("viewer-start")
                    active = True
                    tag = re.search(r"'(fc[0-9a-f]{8})'$", remote).group(1)
                    return f"Workspace viewer ready: 5120x1440 and registered OpenVR key com.frame-control.workspace.1000.{tag}"
                if "--showdashboard" in remote:
                    return "focus accepted"
                return "panel opened"

            mv.call = call
            mv.ensure_tunnel = lambda **kwargs: None
            mv.run = run
            mv._end_viewer_browser = lambda *args: None

            wide = mv.start_workspace(preset="5120x1440")
            portrait = mv.start_workspace(preset="2560x2880")
            nine_eight = mv.start_workspace(preset="2880x2560")
            landscape = mv.start_workspace(preset="3840x2160")
            back_to_nine_eight = mv.start_workspace(preset="2880x2560")
            back_to_portrait = mv.start_workspace(preset="2560x2880")
            back_to_wide = mv.start_workspace(preset="5120x1440")

            self.assertEqual(wide["workspace"]["display"]["logicalWidth"], 5120)
            self.assertEqual(wide["workspace"]["display"]["logicalHeight"], 1440)
            self.assertTrue(portrait["replaced"])
            self.assertEqual(portrait["previousPreset"], "5120x1440")
            self.assertEqual(portrait["workspace"]["preset"], "2560x2880")
            self.assertEqual(portrait["workspace"]["display"]["logicalWidth"], 2560)
            self.assertEqual(portrait["workspace"]["display"]["logicalHeight"], 2880)
            self.assertEqual(5120 * 1440, 2560 * 2880, "wide and portrait presets keep the same pixel area")
            self.assertTrue(nine_eight["replaced"])
            self.assertEqual(nine_eight["previousPreset"], "2560x2880")
            self.assertEqual(nine_eight["workspace"]["preset"], "2880x2560")
            self.assertEqual(nine_eight["workspace"]["display"]["logicalWidth"], 2880)
            self.assertEqual(nine_eight["workspace"]["display"]["logicalHeight"], 2560)
            self.assertEqual(nine_eight["workspace"]["stream"]["width"], 2880)
            self.assertEqual(nine_eight["workspace"]["stream"]["height"], 2560)
            self.assertTrue(landscape["replaced"])
            self.assertEqual(landscape["previousPreset"], "2880x2560")
            self.assertEqual(landscape["workspace"]["preset"], "3840x2160")
            self.assertEqual(landscape["workspace"]["display"]["logicalWidth"], 3840)
            self.assertEqual(landscape["workspace"]["display"]["logicalHeight"], 2160)
            self.assertEqual(landscape["workspace"]["stream"]["width"], 3840)
            self.assertEqual(landscape["workspace"]["stream"]["height"], 2160)
            self.assertTrue(back_to_nine_eight["replaced"])
            self.assertEqual(back_to_nine_eight["previousPreset"], "3840x2160")
            self.assertTrue(back_to_portrait["replaced"])
            self.assertEqual(back_to_portrait["previousPreset"], "2880x2560")
            self.assertTrue(back_to_wide["replaced"])
            self.assertEqual(back_to_wide["previousPreset"], "2560x2880")
            self.assertEqual(back_to_wide["workspace"]["preset"], "5120x1440")
            self.assertEqual([(args[args.index("--width") + 1], args[args.index("--height") + 1])
                              for args in starts], [("5120", "1440"), ("2560", "2880"), ("2880", "2560"),
                                                   ("3840", "2160"), ("2880", "2560"),
                                                   ("2560", "2880"), ("5120", "1440")])
            self.assertEqual(len(viewer_starts), 7)
            self.assertEqual(len(viewer_stops), 6)
            self.assertEqual(closes, ["display:4242"] * 6)
            self.assertEqual(events, [
                "display-start", "viewer-start",
                "viewer-stop", "stream-close", ("display-removed", True), "display-start", "viewer-start",
                "viewer-stop", "stream-close", ("display-removed", True), "display-start", "viewer-start",
                "viewer-stop", "stream-close", ("display-removed", True), "display-start", "viewer-start",
                "viewer-stop", "stream-close", ("display-removed", True), "display-start", "viewer-start",
                "viewer-stop", "stream-close", ("display-removed", True), "display-start", "viewer-start",
                "viewer-stop", "stream-close", ("display-removed", True), "display-start", "viewer-start",
            ])

    def test_workspace_size_switch_never_starts_a_second_display_after_removal_failure(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "FRAME_WORKSPACE_TEST_FAIL_REMOVAL": "1",
                "FRAME_WORKSPACE_TEST_FAIL_RECOVERY": "1",
                "FRAME_WORKSPACE_TEST_REGISTERED": str(Path(temp) / "registered-workspace")}):
            helper = self.fake_helper(Path(temp) / "fake-frame-mac-view")
            registered = Path(temp) / "registered-workspace"
            starts, viewer_starts, closes = [], [], []
            manager = frame_macview.WorkspaceDisplayManager(helper, track=lambda proc: starts.append(proc.args))
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv._exit_helper_frame = mv.frame
            mv.workspace_manager = manager
            mv.build = mock.Mock()
            mv.remote_port = 47900
            mv.agent = mock.Mock(poll=lambda: None)
            active = False

            def call(path, method="GET", **query):
                nonlocal active
                if path == "/status":
                    stream = ([{"src": manager.state()["captureSrc"]}] if active else [])
                    return {"screen": True, "accessibility": True, "streams": stream, "finished": []}
                if path == "/ticket":
                    return {"ticket": "one-use"}
                if path == "/close":
                    closes.append(query.get("src"))
                    active = False
                    return {"closed": 1}
                if path == "/displays":
                    return {"displays": ([{"name": "Frame Workspace", "id": 4242, "src": "display:4242"}]
                                         if registered.exists() else [])}
                return {}

            def run(remote, **kwargs):
                nonlocal active
                if remote == "bash -s -- stop":
                    active = False
                    return "Workspace viewer stopped"
                if "bash -s -- '--session'" in remote and "'start'" in remote:
                    viewer_starts.append(remote)
                    active = True
                    tag = re.search(r"'(fc[0-9a-f]{8})'$", remote).group(1)
                    return f"Workspace viewer ready: 5120x1440 and registered OpenVR key com.frame-control.workspace.1000.{tag}"
                return "panel opened"

            mv.call = call
            mv.ensure_tunnel = lambda **kwargs: None
            mv.run = run
            mv._end_viewer_browser = lambda *args: None

            mv.start_workspace(preset="5120x1440")
            with self.assertRaisesRegex(frame_macview.MacViewError, "could not confirm removal"):
                mv.start_workspace(preset="2560x2880")
            create_starts = [args for args in starts if "--width" in args]
            self.assertEqual(len(create_starts), 1, "only the original display owner may have started")
            self.assertEqual(len(viewer_starts), 1)
            self.assertEqual(closes, ["display:4242"])
            self.assertTrue(registered.exists())
            with self.assertRaisesRegex(frame_macview.MacViewError, "test recovery could not confirm removal"):
                mv.start_workspace(preset="2560x2880")
            create_starts = [args for args in starts if "--width" in args]
            self.assertEqual(len(create_starts), 1, "a registered but unowned display must block another creation")

    def test_orphan_stop_recovers_without_a_live_capture_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "fake-frame-mac-view"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if sys.argv[1:] == ['workspace', '--recover-orphan']:\n"
                " print(json.dumps({'state':'stopped','removed':True,'recoveredOrphan':True,'physicalLayoutUnchanged':True}))\n"
                " sys.exit(0)\n"
                "sys.exit(2)\n",
                encoding="utf-8")
            helper.chmod(0o755)
            manager = frame_macview.WorkspaceDisplayManager(helper)
            calls = []
            mv = frame_macview.MacView(["ssh"], lambda remote, **kw: calls.append((remote, kw)) or "already stopped", "frame")
            mv.workspace_manager = manager
            stopped = mv.stop_workspace()
            self.assertTrue(stopped["removed"])
            self.assertTrue(stopped["recoveredOrphan"])
            self.assertFalse(stopped["owned"])
            self.assertEqual(calls[0][0], "bash -s -- stop")
            self.assertIn("frame-workspace-viewer-", calls[0][1]["stdin"])

    def test_explicit_orphan_stop_waits_until_owner_lease_is_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "fake-frame-mac-view"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if sys.argv[1:] == ['workspace', '--recover-orphan']:\n"
                " print(json.dumps({'state':'stopped','removed':True,'recoveredOrphan':True}))\n"
                " sys.exit(0)\n"
                "sys.exit(2)\n",
                encoding="utf-8")
            helper.chmod(0o755)
            manager = frame_macview.WorkspaceDisplayManager(helper)
            calls = []
            mv = frame_macview.MacView(["ssh"], lambda remote, **kw: calls.append((remote, kw)) or "already stopped", "frame")
            mv.workspace_manager = manager
            self.assertTrue(mv.stop_workspace()["removed"])
            self.assertEqual(calls[0][0], "bash -s -- stop")

            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'state':'error','removed':False,'error':'A live Workspace owner exists.'}))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8")
            helper.chmod(0o755)
            calls.clear()
            with self.assertRaisesRegex(frame_macview.MacViewError, "live Workspace owner"):
                mv.stop_workspace()
            self.assertEqual(calls, [], "a rejected live-owner recovery must not stop its service")

    def test_unused_stop_all_never_starts_workspace_recovery(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no owned viewer to stop"), "frame")
        mv.workspace_manager.recover_orphan = mock.Mock(side_effect=AssertionError("unowned recovery"))
        with mock.patch.object(frame_macview, "SUPPORTED", False), \
                mock.patch.object(frame_macview.subprocess, "Popen") as popen:
            self.assertEqual(mv.stop(), {"closed": 0})
        popen.assert_not_called()
        mv.workspace_manager.recover_orphan.assert_not_called()

    def test_workspace_60_fps_reaches_viewer_without_changing_resolution(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_FPS": "60"}):
            manager = frame_macview.WorkspaceDisplayManager(self.fake_helper(Path(temp) / "fake-frame-mac-view"))
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv.workspace_manager = manager
            mv.remote_port = 47900
            mv.ensure_tunnel = lambda **kwargs: None
            mv.call = lambda path, **kw: {"screen": True, "streams": [], "finished": []} if path == "/status" else {"ticket": "one-use"}
            requests = []
            def launch(remote, **kwargs):
                args = shlex.split(remote)
                requests.append(args)
                return f"registered OpenVR key com.frame-control.workspace.1000.{args[-1]}"
            mv.run = launch
            try:
                state, _ = manager.start()
                self.assertEqual(state["display"]["logicalWidth"], 5120)
                self.assertEqual(state["stream"], {"width": 4096, "height": 1152, "maxLong": 4096,
                                                 "fps": 60, "codec": "h264"})
                mv.show("workspace", quality="sharp")
                query = parse_qs(urlsplit(requests[0][4]).query)
                self.assertEqual(query["fps"], ["60"])
                self.assertEqual(query["max"], ["4096"])
                self.assertEqual(requests[0][5:7], ["4096", "1152"])
                self.assertEqual(frame_macview.quality_settings("sharp"), frame_macview.QUALITY["sharp"])
                self.assertEqual(frame_macview.WORKSPACE_QUALITY["sharp"]["fps"], 30)
            finally:
                manager.stop()

    def test_invalid_workspace_fps_fails_before_display_creation(self):
        manager = frame_macview.WorkspaceDisplayManager("/does/not/matter")
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_FPS": "120"}), \
                mock.patch.object(frame_macview.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(frame_macview.MacViewError, "must be 30 or 60"):
                manager.start()
        popen.assert_not_called()

    def test_native_workspace_preserves_stream_auth_resolution_and_owns_focus(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_DECODER": "v4l2", "FRAME_WORKSPACE_CONVERSION": "gpu"}):
            manager = frame_macview.WorkspaceDisplayManager(self.fake_helper(Path(temp) / "fake-frame-mac-view"))
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv.workspace_manager = manager
            mv.remote_port = 47900
            mv.ensure_tunnel = lambda **kwargs: None
            mv.call = lambda path, **kw: {"screen": True, "streams": [], "finished": []} if path == "/status" else {"ticket": "one-use"}
            mv.browser_flags = ["--remote-debugging-port=9999"]
            calls = []
            def run(remote, **kwargs):
                calls.append((remote, kwargs))
                return "registered OpenVR key com.frame-workspace.native.1000.workspace"
            mv.run = run
            try:
                manager.start("3840x2160")
                shown = mv.show("workspace", quality="sharp")
                args = shlex.split(calls[0][0])
                query = parse_qs(urlsplit(args[4]).query)
                self.assertEqual(query["src"], ["display:4242"])
                self.assertEqual(query["t"], ["one-use"])
                self.assertEqual(query["max"], ["4096"])
                self.assertEqual(args[5:7], ["3840", "2160"])
                # Gaze-directed detail is requested by default (the agent applies
                # it only on capped links); FRAME_WORKSPACE_GAZE=0 leaves it out.
                self.assertEqual(args[-3:], ["v4l2", "gpu", "gaze"])
                with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_GAZE": "0"}):
                    self.assertEqual(frame_macview.workspace_gaze(), "0")
                self.assertNotIn("--remote-debugging-port=9999", args)
                self.assertEqual(calls[0][1]["stdin"], frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text())
                self.assertEqual(shown["panel"], "com.frame-workspace.native.1000.workspace")
                with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_FOVEATION": "1"}):
                    mv.show("workspace", quality="sharp")
                self.assertEqual(shlex.split(calls[-1][0])[-3:], ["v4l2", "gpu", "1"])
                with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_FOVEATION": "1", "FRAME_MAC_VIEW_DIAGNOSTIC_GAZE": "1", "FRAME_WORKSPACE_DIAGNOSTIC_GAZE": "scan"}):
                    mv.show("workspace", quality="sharp")
                self.assertEqual(shlex.split(calls[-1][0])[-4:], ["v4l2", "gpu", "1", "scan"])
                mv._focus_workspace_viewer()
                self.assertEqual(calls[-1][0], "bash -s -- focus")
                self.assertNotIn("--showdashboard", calls[-1][0])
            finally:
                manager.stop()

    def test_workspace_placement_reads_only_the_current_ready_owner(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        owner = {"key": mv.workspace_panel_key, "state": "streaming",
                 "placement": {"width": 2.4, "distance": 2.0}}
        mv.run = mock.Mock(return_value=json.dumps(owner))
        self.assertEqual(mv.workspace_placement({"state": "running"}), owner["placement"])
        self.assertEqual(mv.run.call_args.args[0], "python3 - --native-owner")
        mv.run.reset_mock()
        self.assertIsNone(mv.workspace_placement({"state": "stopped"}))
        mv.run.assert_not_called()
        for result in (None, {**owner, "key": "other"},
                       {**owner, "placement": {"width": True, "distance": 2}},
                       {**owner, "placement": {"width": 2, "distance": 20}}):
            mv.run.return_value = json.dumps(result)
            self.assertIsNone(mv.workspace_placement({"state": "running"}))
        mv.run.side_effect = RuntimeError("offline")
        self.assertIsNone(mv.workspace_placement({"state": "running"}))
        mv.workspace_panel_key = "com.frame-control.workspace.1000.fc01234567"
        mv.run.reset_mock()
        self.assertIsNone(mv.workspace_placement({"state": "running"}))
        mv.run.assert_not_called()

    def test_native_placement_adjust_uses_live_owner_and_verifies_readback(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager = mock.Mock()
        mv.workspace_manager.state.return_value = {"state": "running"}
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        calls = []
        def run(remote, **kwargs):
            calls.append((remote, kwargs))
            return json.dumps({"ready": True, "state": "streaming", "overlayKey": mv.workspace_panel_key,
                               "placement": {"width": 3.25, "distance": 2.75}})
        mv.run = run
        result = mv.adjust_workspace(3.25, 2.75)
        self.assertEqual(result, {"ready": True, "state": "streaming",
                                  "overlayKey": "com.frame-workspace.native.1000.workspace",
                                  "placement": {"width": 3.25, "distance": 2.75}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "bash -s -- adjust 3.250000 2.750000")
        self.assertEqual(calls[0][1]["stdin"], frame_macview.WORKSPACE_NATIVE_SCRIPT.read_text())
        self.assertEqual(calls[0][1]["timeout"], 10)

        for width, distance in ((True, 2), (float("nan"), 2), (2, float("inf")),
                                (8.1, 2), (2, 10.1)):
            with self.subTest(width=width, distance=distance), self.assertRaises(frame_macview.MacViewError):
                mv.adjust_workspace(width, distance)
        self.assertEqual(len(calls), 1, "invalid numbers must not reach the Frame helper")

        mv.workspace_manager.state.return_value = {"state": "stopped"}
        with self.assertRaisesRegex(frame_macview.MacViewError, "display is stopped"):
            mv.adjust_workspace(3, 2)
        self.assertEqual(len(calls), 1, "a stopped Workspace must not be recreated by placement controls")

    def test_native_placement_rejects_non_native_owner_and_unconfirmed_state(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager = mock.Mock()
        mv.workspace_manager.state.return_value = {"state": "running"}
        mv.workspace_panel_key = "com.frame-control.workspace.1000.fc01234567"
        mv.run = mock.Mock(return_value=json.dumps({"ready": True, "state": "streaming",
                                                    "overlayKey": "com.frame-workspace.native.1000.workspace",
                                                    "placement": {"width": 3, "distance": 2}}))
        with self.assertRaisesRegex(frame_macview.MacViewError, "active native"):
            mv.adjust_workspace(3, 2)
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        mv.run.return_value = json.dumps({"ready": False, "state": "connecting",
                                          "overlayKey": mv.workspace_panel_key,
                                          "placement": {"width": 3, "distance": 2}})
        with self.assertRaisesRegex(frame_macview.MacViewError, "did not confirm"):
            mv.adjust_workspace(3, 2)
        self.assertEqual(mv.run.call_count, 1, "a dashboard-owned or unconfirmed panel is never resized")

    def test_native_curvature_returns_owner_readback_and_rejects_unsupported_owner(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager = mock.Mock()
        mv.workspace_manager.state.return_value = {"state": "running"}
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        result_doc = {"ready": True, "ok": True, "state": "streaming", "overlayKey": mv.workspace_panel_key,
                      "curvature": {"supported": True, "applied": True, "requested": 0.7,
                                    "value": 0.7, "errorCode": 0}}
        mv.run = mock.Mock(return_value=json.dumps(result_doc))
        result = mv.set_workspace_curvature(0.7)
        self.assertEqual(result, result_doc)
        self.assertEqual(mv.run.call_args.args[0], "bash -s -- curvature 0.700000")
        self.assertEqual(mv.run.call_args.kwargs["timeout"], 10)
        for value in (True, float("nan"), -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(frame_macview.MacViewError):
                mv.set_workspace_curvature(value)
        self.assertEqual(mv.run.call_count, 1)

        unsupported = {"ready": True, "ok": False, "state": "streaming", "overlayKey": mv.workspace_panel_key,
                       "curvature": {"supported": False, "applied": False, "requested": 0.7,
                                     "value": None, "errorCode": 23}}
        mv.run.return_value = json.dumps(unsupported)
        self.assertEqual(mv.set_workspace_curvature(0.7), unsupported,
                         "an unsupported curvature command is returned as unsupported, never success")

    def test_native_configuration_errors_precede_display_creation(self):
        manager = frame_macview.WorkspaceDisplayManager("/does/not/matter")
        for env, quality, message in [({"FRAME_WORKSPACE_VIEWER": "wrong"}, "sharp", "browser or native"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native"}, "compatible", "needs H.264"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_DECODER": "auto"}, "sharp", "software or v4l2"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_CONVERSION": "auto"}, "sharp", "cpu or gpu"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_FOVEATION": "auto"}, "sharp", "0 or 1"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_DIAGNOSTIC_GAZE": "scan"}, "sharp", "Diagnostic gaze requires"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_FOVEATION": "1", "FRAME_MAC_VIEW_DIAGNOSTIC_GAZE": "1", "FRAME_WORKSPACE_DIAGNOSTIC_GAZE": "nan,0"}, "sharp", "finite U,V"),
                                      ({"FRAME_WORKSPACE_VIEWER": "native", "FRAME_WORKSPACE_FOVEATION": "1", "FRAME_MAC_VIEW_DIAGNOSTIC_GAZE": "1", "FRAME_WORKSPACE_DIAGNOSTIC_GAZE": "0,1.1"}, "sharp", "finite U,V")]:
            with self.subTest(env=env, quality=quality), mock.patch.dict(os.environ, env), \
                    mock.patch.object(frame_macview.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(frame_macview.MacViewError, message):
                    manager.start(quality=quality)
                popen.assert_not_called()

    def test_helper_build_failure_precedes_display_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = self.fake_helper(Path(temp) / "fake-frame-mac-view")
            manager = frame_macview.WorkspaceDisplayManager(helper)
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv.workspace_manager = manager
            mv.build = mock.Mock(side_effect=frame_macview.MacViewError("build failed"))
            with mock.patch.object(frame_macview.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(frame_macview.MacViewError, "build failed"):
                    mv.start_workspace()
            popen.assert_not_called()
            self.assertEqual(manager.state()["state"], "stopped")

    def test_invalid_workspace_request_fails_before_starting_a_process(self):
        manager = frame_macview.WorkspaceDisplayManager("/does/not/matter")
        for args in (("2560x1440", 1, "sharp"), ("5120x1440", True, "sharp"),
                     ("5120x1440", 1, "unknown")):
            with self.subTest(args=args), self.assertRaises(frame_macview.MacViewError), \
                    mock.patch.object(frame_macview.subprocess, "Popen") as popen:
                manager.start(*args)
                popen.assert_not_called()


class WS:
    """A minimal WebSocket client (masked frames out, plain frames in)."""

    def __init__(self, port, path):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        self.status = int(head.split()[1])
        self.buf = b""

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        return b0 & 0x0F, self._read(n)

    def send_text(self, text):
        data, mask = text.encode(), os.urandom(4)
        head = bytes([0x81, 0x80 | len(data)]) if len(data) < 126 else bytes([0x81, 0xFE]) + struct.pack(">H", len(data))
        self.s.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def close(self):
        self.s.close()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "the agent is macOS-only")
class Agent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="macview-test-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.bin = Path(cls.tmp) / "frame-mac-view"
        r = subprocess.run(["/bin/sh", str(ROOT / "mac" / "frame-mac-view" / "build.sh"), str(cls.bin)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode:  # a real failure on a Mac with Xcode: don't hide it as a skip
            raise AssertionError("agent didn't build:\n" + (r.stderr or r.stdout)[-2000:])
        cls.token = "t0ken-" + os.urandom(6).hex()
        cls.proc = subprocess.Popen([str(cls.bin), "serve", "--port", "0", "--page", str(ROOT / "ui" / "mac-view.html"),
                                     "--exit-on-eof"], env={**os.environ, "FRAME_MAC_VIEW_TOKEN": cls.token},
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = cls.proc.stdout.readline()
        cls.port = int(line.rsplit(":", 1)[1])

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()  # --exit-on-eof
        cls.proc.stdout.close()
        try:
            cls.proc.wait(5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def get(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path)
        r = c.getresponse()
        return r.status, r.read()

    def test_token_is_required(self):
        self.assertEqual(self.get("/status")[0], 403)
        self.assertEqual(self.get("/status?k=wrong")[0], 403)
        for path in ("/streams/live?src=", "/pointer?shared=1", "/focus?src=separate:1&u=0&v=0",
                     "/spatial/start", "/spatial/stop", "/spatial/cover?show=1"):
            self.assertEqual(self.get(path, method="POST")[0], 403)
        self.assertEqual(self.get("/spatial")[0], 403)
        status, body = self.get(f"/status?k={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("screen", json.loads(body))

    def test_spatial_status_is_read_only_before_start(self):
        before = json.loads(self.get(f"/displays?k={self.token}")[1])["displays"]
        status, body = self.get(f"/spatial?k={self.token}")
        self.assertEqual(status, 200)
        spatial = json.loads(body)
        self.assertEqual(spatial["state"], "stopped")
        self.assertEqual(spatial["windows"], [])
        self.assertIsInstance(spatial["revision"], int)
        self.assertIs(spatial["cover"], False)
        # The laptop cover exists only inside a running Spatial Mode.
        self.assertEqual(self.get(f"/spatial/cover?show=1&k={self.token}", method="POST")[0], 409)
        self.assertEqual(self.get(f"/spatial/cover?show=yes&k={self.token}", method="POST")[0], 400)
        self.assertEqual(json.loads(self.get(f"/status?k={self.token}")[1])["spatial"]["state"], "stopped")
        self.assertEqual(json.loads(self.get(f"/displays?k={self.token}")[1])["displays"], before)

    def test_serves_the_viewer_page(self):
        status, body = self.get(f"/view?k={self.token}&src=test")
        self.assertEqual(status, 200)
        self.assertIn(b"VideoDecoder", body)

    def test_snapshot_needs_the_key(self):
        self.assertEqual(self.get("/snapshot?display=1")[0], 403)

    def test_separate_without_accessibility_explains(self):
        if json.loads(self.get(f"/status?k={self.token}")[1])["accessibility"]:
            self.skipTest("this Mac allows Accessibility here")
        ws = WS(self.port, f"/stream?k={self.token}&src=separate:1")
        self.assertEqual(ws.status, 101)
        for _ in range(5):
            op, data = ws.recv()
            msg = json.loads(data) if op == 1 else {}
            if msg.get("t") in ("error", "closed"):
                break
        self.assertIn("Accessibility", msg.get("message", msg.get("reason", "")))
        ws.close()

    def test_lists_displays(self):
        status, body = self.get(f"/displays?k={self.token}")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body)["displays"], list)

    def test_workspace_validation_does_not_attach_a_display(self):
        before = json.loads(self.get(f"/displays?k={self.token}")[1])["displays"]
        env = {**os.environ, "FRAME_MAC_VIEW_RUN_DISPLAY_TESTS": "0"}
        result = subprocess.run(
            ["sh", str(ROOT / "mac/frame-mac-view/test-workspace.sh"), str(self.bin)],
            capture_output=True, text=True, timeout=45, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS workspace argument validation", result.stdout)
        self.assertEqual(json.loads(self.get(f"/displays?k={self.token}")[1])["displays"], before)

    def ticket(self, src):
        status, body = self.get(f"/ticket?k={self.token}&src={src}", method="POST")
        self.assertEqual(status, 200)
        return json.loads(body)["ticket"]

    def test_ping_and_page_are_open_but_streams_are_not(self):
        self.assertEqual(self.get("/ping"), (200, b"frame-mac-view"))
        self.assertEqual(WS(self.port, "/stream?src=test").status, 403)
        self.assertEqual(self.get("/ticket?src=test", method="POST")[0], 403)

    def test_tickets_are_single_use_and_tied_to_one_source(self):
        t = self.ticket("test")
        self.assertEqual(WS(self.port, f"/stream?src=display:1&t={t}").status, 403)  # wrong source
        ws = WS(self.port, f"/stream?src=test&t={t}")
        self.assertEqual(ws.status, 101)
        hello = json.loads(ws.recv()[1])
        self.assertEqual(hello["t"], "hello")
        # Until the viewer acknowledges, a retry (the hello got lost) gets the
        # same key, and replaces the first viewer rather than adding one.
        retry = WS(self.port, f"/stream?src=test&t={t}")
        self.assertEqual(retry.status, 101)
        self.assertEqual(json.loads(retry.recv()[1])["r"], hello["r"])
        time.sleep(0.3)
        _, body = self.get(f"/status?k={self.token}")
        self.assertEqual(len(json.loads(body)["streams"]), 1)
        ws.close()
        ws = retry
        ws.send_text(json.dumps({"t": "ack"}))
        time.sleep(0.3)
        self.assertEqual(WS(self.port, f"/stream?src=test&t={t}").status, 403)  # spent
        again = WS(self.port, f"/stream?src=test&r={hello['r']}")  # the viewer reconnecting
        self.assertEqual(again.status, 101)
        again.close()
        ws.close()
        # Stop revokes the reconnect key.
        self.get(f"/close?k={self.token}&src=test", method="POST")
        self.assertEqual(WS(self.port, f"/stream?src=test&r={hello['r']}").status, 403)

    def test_stop_revokes_tickets_not_yet_used(self):
        t = self.ticket("test")
        self.get(f"/close?k={self.token}&src=test", method="POST")
        self.assertEqual(WS(self.port, f"/stream?src=test&t={t}").status, 403)

    def test_bad_frame_lengths_close_the_socket_not_the_agent(self):
        ws = WS(self.port, f"/stream?src=test&t={self.ticket('test')}")
        self.assertEqual(ws.status, 101)
        # A masked frame claiming 2^63 bytes.
        ws.s.sendall(bytes([0x81, 0xFF]) + struct.pack(">Q", 1 << 63) + os.urandom(4))
        with self.assertRaises((EOFError, OSError)):
            for _ in range(1000):
                ws.recv()
        ws.close()
        self.assertEqual(self.get(f"/status?k={self.token}")[0], 200)

    def test_stream_input_and_close(self):
        ws = WS(self.port, f"/stream?k={self.token}&src=test&codec=h264&fps=30&max=640")
        self.assertEqual(ws.status, 101)
        info = None
        key = None
        for _ in range(200):
            op, data = ws.recv()
            if op == 1:
                msg = json.loads(data)
                if msg["t"] == "hello":
                    continue
                if msg["t"] == "error":
                    self.skipTest("no H.264 encoder here: " + msg["message"])
                if msg["t"] == "info":
                    info = msg
            elif op == 2 and data[0] == 1:
                key = data
            if info and key:
                break
        self.assertEqual(info["src"], "test")
        self.assertTrue(info["input"])
        # A keyframe: flags, 8-byte timestamp, sequence number, input echoed,
        # then Annex B with the SPS first.
        self.assertEqual(key[17:21], b"\x00\x00\x00\x01")
        self.assertEqual(key[21] & 0x1F, 7)  # NAL type 7, SPS
        ws.send_text(json.dumps({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.5}))
        ws.send_text(json.dumps({"t": "key-frame"}))
        got_key = False
        for _ in range(200):
            op, data = ws.recv()
            if op == 2 and data[0] == 1:
                got_key = True
                break
        self.assertTrue(got_key, "no keyframe after asking for one")
        _, body = self.get(f"/status?k={self.token}")
        state = json.loads(body)
        self.assertEqual([s["src"] for s in state["streams"]], ["test"])
        stream = state["streams"][0]
        self.assertEqual((stream["width"], stream["height"]), (640, 360))
        self.assertTrue(stream["live"])
        self.assertGreater(stream["lastChanged"], 0)
        self.assertIsInstance(state["pointerShared"], bool)
        status, body = self.get(f"/close?k={self.token}", method="POST")
        self.assertEqual(json.loads(body)["closed"], 1)
        for _ in range(400):
            op, data = ws.recv()
            if op == 1 and json.loads(data)["t"] == "close":
                break
        self.assertEqual(json.loads(data)["t"], "close")
        # Without the viewer doing anything, the agent ends the stream itself.
        time.sleep(1)
        _, body = self.get(f"/status?k={self.token}")
        self.assertEqual(json.loads(body)["streams"], [])
        ws.close()

    def test_timing_reports_and_input_echo(self):
        # What a viewer does: sync clocks, report each frame, stamp input.
        ws = WS(self.port, f"/stream?k={self.token}&src=test&codec=h264&fps=30&max=640")
        self.assertEqual(ws.status, 101)
        ws.send_text(json.dumps({"t": "w"}))  # keep-warm filler: ignored
        ws.send_text(json.dumps({"t": "ping", "c": 1.5}))
        pong, echoed, seqs, info = None, None, [], None
        clicked = False
        deadline = time.time() + 10
        while time.time() < deadline and not (pong and echoed):
            op, data = ws.recv()
            if op == 1:
                msg = json.loads(data)
                if msg["t"] == "error":
                    self.skipTest("no H.264 encoder here: " + msg["message"])
                if msg["t"] == "pong":
                    pong = msg
                if msg["t"] == "info":
                    info = msg
            elif op == 2:
                seq, echo = struct.unpack(">II", data[9:17])
                seqs.append(seq)
                now = pong["a"] if pong else 0
                ws.send_text(json.dumps({"t": "rx", "s": seq, "r": now}))
                ws.send_text(json.dumps({"t": "fd", "f": [[seq, now + 1, now + 2, now + 3]], "drop": 0}))
                if echo == 7:
                    echoed = seq
                if len(seqs) == 3 and not clicked:
                    ws.send_text(json.dumps({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.5, "i": 7, "tv": now}))
                    clicked = True
        self.assertEqual(pong["c"], 1.5)
        self.assertGreater(pong["a"], 0)
        self.assertEqual(seqs[:3], sorted(seqs[:3]))
        self.assertIsNotNone(echoed, "no frame was tagged as the first reply to the click")
        time.sleep(1.7)  # frames are reported once the viewer has had time
        stream = json.loads(self.get(f"/stats?k={self.token}")[1])["streams"][0]
        first = stream["frames"][0]
        self.assertGreater(first["e1"], first["e0"])
        self.assertGreaterEqual(first["e0"], first["cap"])
        self.assertEqual(first["vs"] - first["rx"], 3)
        self.assertEqual([i["frame"] for i in stream["inputs"] if i["id"] == 7], [echoed])
        self.assertIn("total", stream["summary"])
        self.assertEqual(info["warm"], 0)  # off unless FRAME_MAC_VIEW_WARM is set
        live = json.loads(self.get(f"/status?k={self.token}")[1])["streams"][0]
        self.assertEqual(live["controller"]["tier"], 0)
        self.assertIn("fps", live["stats"])
        ws.close()


if __name__ == "__main__":
    unittest.main()
