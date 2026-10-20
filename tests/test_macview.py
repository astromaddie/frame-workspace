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
                                          "FRAME_WORKSPACE_CONVERSION": "gpu"}):
            mv._show_native_window("window:42", "http://127.0.0.1:1/view?x", "fc12345678", 1200, 800)
        self.assertTrue(runs[-1].startswith("bash -s -- "))
        args = shlex.split(runs[-1])[3:]
        self.assertEqual(args[:4], ["--panel", "w42", "--name", "Terminal zsh"])
        self.assertEqual(args[4], "--initial-pose")
        self.assertEqual(json.loads(args[5])["width"], 1.0)
        self.assertEqual(args[6:8], ["start", "http://127.0.0.1:1/view?x"])
        self.assertEqual(args[-2:], ["v4l2", "gpu"])
        self.assertEqual(mv.native_panels["window:42"]["key"], "com.frame-workspace.native.1000.panel-w42")
        self.assertIs(mv.native_panels["window:42"]["host"], False)

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
            path.write_text(json.dumps(pose))
            (base / "workspace-pose.json").write_text(json.dumps(pose))
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")

            def local_reader(cmd, stdin, timeout):
                result = subprocess.run([sys.executable, *shlex.split(cmd)[1:]], input=stdin,
                                        env={**os.environ, "XDG_DATA_HOME": str(root)},
                                        text=True, capture_output=True, timeout=timeout, check=True)
                return result.stdout

            mv.run.side_effect = local_reader
            self.assertEqual(mv.read_panel_poses(["workspace", "window:42", "window:7"]),
                             {"workspace": pose, "window:42": pose})
            path.write_text(" " * 4097)
            with self.assertRaisesRegex(frame_macview.MacViewError, "capture saved native panel poses"):
                mv.read_panel_poses(["window:42"])
            mv.run.reset_mock()
            with self.assertRaisesRegex(frame_macview.MacViewError, "Invalid native panel source"):
                mv.read_panel_poses(["window:42;touch bad"])
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
        mv.last_workspace = ("2880x2560", 1, "sharp")
        calls = []
        mv.start_workspace = lambda *a, **k: calls.append(("start", a)) if k == {"resume": True} else None
        mv.stop_workspace = lambda: calls.append(("stop",))
        mv._focus_workspace_viewer = lambda: calls.append(("recenter",))
        mv._handle_frame_requests("DASH_STARTED\n" + "\n".join(
            json.dumps({"action": a}) for a in ("workspace_start", "recenter", "workspace_stop", "reboot")))
        mv.dash_jobs.join()
        self.assertEqual(calls, [("start", ("2880x2560", 1, "sharp")), ("recenter",), ("stop",)])

    def test_bar_panel_state_lists_the_workspace_and_windows(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.workspace_manager.state = lambda: {"state": "running", "preset": "2880x2560"}
        mv.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
        mv.native_panels = {
            "window:9": {"panel": "w9", "key": "com.frame-workspace.native.1000.panel-w9", "slot": 1, "title": "Notes"},
            "window:4": {"panel": "w4", "key": "com.frame-workspace.native.1000.panel-w4", "slot": 0, "title": "Terminal"},
        }
        state = mv.dash_state()
        self.assertEqual(state["workspace"], {"running": True, "busy": None, "label": "9:8 · 2880 × 2560", "mode": "desktop"})
        self.assertEqual([w["title"] for w in state["windows"]], ["Terminal", "Notes"])
        mv.workspace_manager.state = lambda: {"state": "stopped"}
        mv.workspace_panel_key = None
        self.assertFalse(mv.dash_state()["workspace"]["running"])

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
        self.mv.call = self.call
        # Exercise the production request handler without background timing.
        self.mv._dash_run = lambda busy, fn, *args: fn(*args)

    def call(self, path, method="GET", **q):
        self.calls.append((path, method, q))
        if path == "/status":
            return self.status
        self.assertEqual(method, "POST")
        if path == "/streams/live":
            selected = set(q.get("src", "").split(","))
            for s in self.status["streams"]:
                if s["src"].startswith(("window:", "separate:")):
                    s["live"] = q.get("all") == "1" or s["src"] in selected
        elif path == "/focus":
            self.status["focusedSrc"] = q["src"]
        else:
            self.assertEqual(path, "/pointer")
        return {}

    def stream(self, src, mp, age=0, **extra):
        s = {"id": len(self.status["streams"]) + 1, "src": src, "width": int(mp * 1000), "height": 1000,
             "lastChanged": self.now + 900 - age, "live": True, **extra}
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
        self.status["focusedSrc"] = s["src"]
        self.tick(101.1)
        self.assertEqual(self.live(), set())  # even focus cannot exceed the hard pixel cap
        s["width"] = 4000
        self.tick(101.2)
        self.assertIn(s["src"], self.live())
        with mock.patch.dict(os.environ, {"FRAME_SPATIAL_DECODE_MP": "3"}):
            self.tick(101.3)
            self.assertEqual(self.live(), set())

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
        self.mv.call = mock.Mock(side_effect=[{}, self.status, frame_macview.MacViewError("not answering")])
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
            "FRAME_WINDOW_DISPLAYS": "0", "FRAME_NATIVE_HOST": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mv = frame_macview.MacView(["ssh"], self.remote_run, "fake-frame")
        self.mv.agent = mock.Mock(poll=lambda: None)
        self.mv.workspace_manager = mock.Mock()
        self.mv.workspace_manager.state.return_value = {"state": "stopped"}
        self.mv.ensure_tunnel = mock.Mock()
        self.mv._supervise = mock.Mock()
        self.mv._end_viewer_browser = mock.Mock()
        self.mv.call = self.call
        self.jobs, self.events, self.launches = [], [], []
        self.mv._dash_run = lambda busy, fn, *args: self.jobs.append((fn, args))
        self.running, self.removed, self.fail_start = False, True, False
        self.windows = [self.window(42)]
        self.streams = []

    @staticmethod
    def window(wid):
        return {"id": wid, "src": f"window:{wid}", "title": f"Shell {wid}", "app": "Terminal",
                "bundleID": "com.apple.Terminal", "w": 900, "h": 600}

    def call(self, path, method="GET", **query):
        self.events.append((path, query))
        if path == "/spatial/start":
            if self.fail_start:
                raise frame_macview.MacViewError("Mirror Setup Failed")
            self.running, self.removed = True, False
        if path == "/spatial/stop":
            self.running = False
            self.streams.clear()
        if path.startswith("/spatial"):
            return {"state": "running" if self.running else "stopped", "removed": self.removed,
                    "windows": list(self.windows), "revision": 1}
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
        if "'start'" in command:
            args = shlex.split(command)
            panel = args[args.index("--panel") + 1]
            self.launches.append(args)
            return "registered OpenVR key com.frame-workspace.native.1000.panel-" + panel
        return "stopped"

    def jobs_once(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)

    def start(self):
        self.mv.set_mode("spatial")
        result = self.mv.start_workspace(quality="balanced")
        self.jobs_once()
        return result

    def test_start_uses_hidden_home_separate_sources_and_shared_host(self):
        result = self.start()
        self.assertEqual(result["mode"], "spatial")
        self.assertEqual(result["workspace"]["state"], "running")
        self.mv.workspace_manager.start.assert_not_called()
        self.assertIsNone(self.mv.workspace_panel_key)
        self.assertEqual(self.mv.shown, {"separate:42"})
        self.assertTrue(self.mv.native_panels["separate:42"]["host"])
        self.assertEqual(self.mv.native_panels["separate:42"]["title"], "Shell 42")
        self.assertEqual(self.launches[0][0], "FRAME_NATIVE_HOST=1")
        self.assertIn("src=separate%3A42", " ".join(self.launches[0]))
        self.assertTrue(self.mv.spatial_workspace["panelsReady"])
        self.assertTrue(self.mv.dash_state()["workspace"]["running"])
        self.assertEqual(self.mv.dash_state()["workspace"]["mode"], "spatial")
        self.assertLess(next(i for i, e in enumerate(self.events) if e[0] == "remote"),
                        next(i for i, e in enumerate(self.events) if e[0] == "/spatial/start"))

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
        self.assertEqual(set(self.mv.native_panels), {"separate:42", "separate:43"})
        self.windows.pop(0)
        self.mv._spatial_mode_tick()
        self.jobs_once()
        self.assertEqual(set(self.mv.native_panels), {"separate:43"})
        self.assertEqual(len(self.launches), 2)

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
        self.assertEqual(len(self.launches), 1)

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

    def test_spatial_overrides_desktop_browser_flags_and_accepts_saved_separate_pose(self):
        self.start()
        pose = {"version": 1, "width": 1.2, "distance": 2, "curvature": .2,
                "matrix": [1, 0, 0, 0, 0, 1, 0, 1.4, 0, 0, 1, -2]}
        self.windows.append(self.window(43))
        with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "browser", "FRAME_WINDOW_VIEWER": "browser"}):
            self.mv.show("window:43", pose=pose)
        args = self.launches[-1]
        self.assertEqual(json.loads(args[args.index("--pose") + 1]), pose)
        self.assertEqual(args[0], "FRAME_NATIVE_HOST=1")
        self.assertIn("separate:43", self.mv.native_panels)

    def test_combined_ui_sync_writes_atomic_private_spatial_state(self):
        with tempfile.TemporaryDirectory() as temp:
            state = {"mode": "spatial", "apps": [{"name": "A 'Quoted' App"}],
                     "switcher": {"open": False, "index": 0, "seq": 0, "chosen": None}}
            result = subprocess.run(["bash", "-s", "--", "sync", "{}", json.dumps(state)],
                input=frame_macview.SPATIAL_UI_SYNC + 'test "$#" -eq 2 && test "$1" = sync\n',
                env={**os.environ, "XDG_RUNTIME_DIR": temp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            path = Path(temp) / "frame-workspace/spatial-ui.json"
            self.assertEqual(json.loads(path.read_text()), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob(".spatial-ui-*")), [])

    def test_owned_switch_requests_focus_in_both_modes_and_dispatch_app_hooks(self):
        self.mv.native_panels = {"window:42": {"key": "com.frame-workspace.native.1000.panel-w42", "slot": 0}}
        handled = []
        self.mv.spatial_request_handler = handled.append
        self.mv._handle_frame_requests('\n'.join(json.dumps(r) for r in [
            {"action": "switch", "key": "com.frame-workspace.native.1000.panel-w99"},
            {"action": "switch", "key": "com.frame-workspace.native.1000.panel-w42"},
            {"action": "app", "verb": "hide", "bundleID": "com.apple.Terminal"},
            {"action": "switcher", "verb": "open"}]))
        self.jobs_once()
        self.assertEqual([e for e in self.events if e[0] == "/focus"],
                         [("/focus", {"src": "window:42", "u": .5, "v": .5})])
        self.assertEqual([r["action"] for r in handled], ["switch", "app", "switcher"])


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
                if remote.startswith("bash -s -- 'start'"):
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
            self.assertTrue(launches[0][0].startswith("bash -s -- 'start' "))
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
                if remote.startswith("bash -s -- 'start'"):
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
                if remote.startswith("bash -s -- 'start'"):
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
        cls.tmp = tempfile.mkdtemp(prefix=".macview-test-", dir=ROOT)
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
                     "/spatial/start", "/spatial/stop"):
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
