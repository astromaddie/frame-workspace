"""FW-11 profile storage, fresh matching, and the real MacView restore path."""
import json
import copy
import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import os
from pathlib import Path
import re
import shlex
import sys
import threading
import time
import tempfile
import unittest
from unittest import mock

import sandbox  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
import frame_macview  # noqa: E402
import frame_workspace_profiles as profiles  # noqa: E402

cli_spec = importlib.util.spec_from_file_location("workspace_profiles_cli", ROOT / "scripts" / "workspace-profiles.py")
profile_cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(profile_cli)


def sample_profile(windows=None):
    return {"name": "Coding", "workspace": {"preset": "5120x1440", "scale": 1, "quality": "sharp"},
            "windows": windows or []}


def sample_pose(x=0):
    return {"version": 1, "width": 2.4, "distance": 2, "curvature": .3,
            "matrix": [1, 0, 0, x, 0, 1, 0, 1.3, 0, 0, 1, -2]}


class ProfileStoreTests(unittest.TestCase):
    def test_saved_profiles_remain_readable_when_backend_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            compatible = sample_profile()
            compatible["name"] = "Browser fallback"
            compatible["workspace"]["quality"] = "compatible"
            store.save(compatible)
            store.save(sample_profile())
            with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native",
                                               "FRAME_WORKSPACE_DECODER": "v4l2"}):
                self.assertEqual(store.get("Browser fallback"), compatible)
                self.assertEqual(len(store.list()), 2)
                self.assertEqual(store.get("Coding"), sample_profile())
                # Restoring the incompatible profile still fails before display
                # creation through the normal runtime request validator.
                with self.assertRaisesRegex(frame_macview.MacViewError, "needs H.264"):
                    frame_macview.WorkspaceDisplayManager._validate_request(**compatible["workspace"])

    def test_atomic_round_trip_owner_only_mode_and_case_insensitive_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "profiles.json"
            store = profiles.ProfileStore(path)
            saved = store.save(sample_profile([{"bundleID": "com.google.Chrome", "titleHint": "Project"}]))
            self.assertEqual(store.get("coding"), saved)
            self.assertEqual(store.list(), [saved])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text()), {"schemaVersion": 2, "profiles": [saved]})
            replacement = sample_profile([{"bundleID": "com.apple.Terminal"}])
            store.save(replacement)  # replaces the same name, ignoring case
            self.assertEqual(store.list(), [replacement])
            store.delete("CODING")
            self.assertEqual(store.list(), [])

    def test_rejects_unknown_secret_and_unsafe_fields(self):
        invalid = sample_profile()
        invalid["ticket"] = "one-use"
        with self.assertRaisesRegex(profiles.ProfileError, "unknown fields: ticket"):
            profiles.validate_profile(invalid)

        invalid = sample_profile([{"bundleID": "com.google.Chrome", "sessionID": "old-session"}])
        with self.assertRaisesRegex(profiles.ProfileError, "bundleID, optional titleHint and optional pose only"):
            profiles.validate_profile(invalid)

        invalid = sample_profile([{"bundleID": "../secret"}])
        with self.assertRaisesRegex(profiles.ProfileError, "valid stable bundle identifier"):
            profiles.validate_profile(invalid)

        invalid = sample_profile()
        invalid["workspace"]["displayID"] = 99
        with self.assertRaisesRegex(profiles.ProfileError, "exactly preset, scale and quality"):
            profiles.validate_profile(invalid)

    def test_corruption_and_unknown_schema_are_reported_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profiles.json"
            path.write_text("{broken", encoding="utf-8")
            path.chmod(0o600)
            original = path.read_bytes()
            with self.assertRaisesRegex(profiles.ProfileStoreError, "corrupted.*left unchanged"):
                profiles.ProfileStore(path).list()
            self.assertEqual(path.read_bytes(), original)

            path.write_text('{"schemaVersion":3,"profiles":[]}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(profiles.ProfileStoreError, "unsupported.*version: 3"):
                profiles.ProfileStore(path).list()

            path.write_text('{"schemaVersion":1,"profiles":[],"profiles":[]}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(profiles.ProfileStoreError, "duplicate JSON keys"):
                profiles.ProfileStore(path).list()

    def test_rejects_loose_permissions_and_symlink_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "profiles.json"
            path.write_text('{"schemaVersion":1,"profiles":[]}', encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(profiles.ProfileStoreError, "permissions must be 0600"):
                profiles.ProfileStore(path).list()
            path.unlink()
            target = root / "target.json"
            target.write_text('{"schemaVersion":1,"profiles":[]}', encoding="utf-8")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaisesRegex(profiles.ProfileStoreError, "regular file"):
                profiles.ProfileStore(path).list()

    def test_reset_only_clears_saved_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profiles.json"
            store = profiles.ProfileStore(path)
            store.save(sample_profile())
            self.assertEqual(store.reset(), {"schemaVersion": 2, "profiles": []})
            self.assertEqual(store.list(), [])

    def test_concurrent_saves_keep_every_distinct_profile(self):
        class SlowWriteStore(profiles.ProfileStore):
            def _write(self, document):
                # Give simultaneous requests time to overlap after their read.
                # Without a transaction lock they all serialize stale snapshots.
                time.sleep(0.03)
                return super()._write(document)

        with tempfile.TemporaryDirectory() as temp:
            store = SlowWriteStore(Path(temp) / "profiles.json")
            count = 8
            ready = threading.Barrier(count)

            def save(index):
                profile = sample_profile()
                profile["name"] = f"Concurrent {index}"
                ready.wait(timeout=5)
                store.save(profile)

            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(save, index) for index in range(count)]
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual({item["name"] for item in store.list()},
                             {f"Concurrent {index}" for index in range(count)})

    def test_v1_migrates_in_memory_without_adding_poses_or_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profiles.json"
            old = sample_profile([{"bundleID": "com.apple.Terminal"}])
            path.write_text(json.dumps({"schemaVersion": 1, "profiles": [old]}))
            path.chmod(0o600)
            original = path.read_bytes()
            store = profiles.ProfileStore(path)
            self.assertEqual(store.get("Coding"), old)
            self.assertEqual(path.read_bytes(), original)
            new = sample_profile()
            new["name"] = "Reading"
            store.save(new)
            self.assertEqual(json.loads(path.read_text())["schemaVersion"], 2)
            self.assertEqual(store.get("Coding"), old)
            old["workspace"]["pose"] = sample_pose()
            with self.assertRaises(profiles.ProfileError):
                profiles.validate_document({"schemaVersion": 1, "profiles": [old]})

    def test_pose_validation_is_strict_for_workspace_and_window_rules(self):
        bad = [None, [], {}, dict(sample_pose(), version=True), dict(sample_pose(), version=1.0),
               dict(sample_pose(), extra=1), dict(sample_pose(), width=.009), dict(sample_pose(), width=8.001),
               dict(sample_pose(), distance=.399), dict(sample_pose(), distance=10.001),
               dict(sample_pose(), curvature=-.01), dict(sample_pose(), curvature=1.01),
               dict(sample_pose(), width=True), dict(sample_pose(), distance="2"),
               dict(sample_pose(), width=float("nan")), dict(sample_pose(), curvature=float("inf")),
               dict(sample_pose(), width=10**400), dict(sample_pose(), matrix=[1] * 11)]
        for index, value in ((0, 1.02), (1, .1), (3, 100.01), (0, -1), (0, True),
                             (7, float("inf")), (11, "-2")):
            pose = sample_pose()
            pose["matrix"][index] = value
            bad.append(pose)
        for pose in bad:
            for target in ("workspace", "window"):
                with self.subTest(pose=pose, target=target):
                    profile = sample_profile([{"bundleID": "com.apple.Terminal"}])
                    (profile["workspace"] if target == "workspace" else profile["windows"][0])["pose"] = pose
                    with self.assertRaises(profiles.ProfileError):
                        profiles.validate_profile(profile)
        for width, distance, curvature in ((.01, .4, 0), (.2, 1.75, 0), (.4, .4, 0), (8, 10, 1)):
            pose = dict(sample_pose(), width=width, distance=distance, curvature=curvature)
            pose["matrix"][0] = .999999  # native six-decimal serialization tolerance
            profile = sample_profile([{"bundleID": "com.apple.Terminal", "pose": pose}])
            profile["workspace"]["pose"] = pose
            with tempfile.TemporaryDirectory() as temp:
                store = profiles.ProfileStore(Path(temp) / "profiles.json")
                self.assertEqual(store.save(profile), profile)
                self.assertEqual(store.get("Coding"), profile)


class ProfileCaptureTests(unittest.TestCase):
    def test_spatial_profiles_capture_window_poses_without_a_workspace_panel(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "browser"}):
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            mv = frame_macview.MacView([], mock.Mock(), "fake")
            mv.mode = "spatial"
            mv.state = mock.Mock(return_value={"available": True, "screen": True, "windows": [
                {"id": 42, "src": "window:42", "bundleID": "com.apple.Terminal", "title": "Shell"}]})
            pose = sample_pose(1)
            mv.read_panel_poses = mock.Mock(return_value={"spatial:42": pose})
            value = sample_profile([{"bundleID": "com.apple.Terminal"}])
            value["workspace"]["pose"] = sample_pose(-1)
            saved = profiles.save(store, mv, value)
            mv.read_panel_poses.assert_called_once_with(["spatial:42"])
            self.assertNotIn("pose", saved["workspace"])
            self.assertEqual(saved["windows"][0]["pose"], pose)
            # A previously saved Desktop Workspace pose remains in the store,
            # but Spatial restore never counts it as a launched window pose.
            value["windows"][0]["pose"] = pose
            store.save(value)
            mv.start_workspace = mock.Mock(return_value={"workspace": {"state": "running"}})
            mv.show = mock.Mock(return_value={"panel": "key42"})
            result = profiles.restore(store, mv, value["name"])
            self.assertNotIn("pose", mv.start_workspace.call_args.kwargs)
            self.assertEqual(result["spatialRestore"]["restoredPoses"], 1)
            self.assertIn("pose", store.get(value["name"])["workspace"])

    def test_save_captures_matched_rules_and_workspace_through_run(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "FRAME_WORKSPACE_VIEWER": "native", "FRAME_WINDOW_VIEWER": "native"}):
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
            mv.state = mock.Mock(return_value={"available": True, "windows": [
                {"id": 42, "src": "window:42", "bundleID": "com.apple.Terminal", "title": "Shell"},
                {"id": 7, "src": "window:7", "bundleID": "com.example.Editor", "title": "Repo"},
                {"id": 8, "src": "window:8", "bundleID": "com.apple.Preview", "title": "One"},
                {"id": 9, "src": "window:9", "bundleID": "com.apple.Preview", "title": "Two"}]})
            window_pose, workspace_pose = sample_pose(1), sample_pose(-1)
            mv.run = mock.Mock(return_value=json.dumps({"w42": json.dumps(window_pose),
                                                       "workspace": json.dumps(workspace_pose)}))
            profile = sample_profile([{"bundleID": "com.apple.Terminal"}, {"bundleID": "com.example.Editor"},
                                      {"bundleID": "com.example.Missing"}, {"bundleID": "com.apple.Preview"},
                                      {"bundleID": "com.apple.Terminal", "titleHint": "Shell"},
                                      {"bundleID": "com.example.Editor", "titleHint": "Old Title"}])
            original = copy.deepcopy(profile)
            saved = profiles.save(store, mv, profile)
            self.assertEqual(saved["windows"][0]["pose"], window_pose)
            self.assertEqual(saved["workspace"]["pose"], workspace_pose)
            self.assertTrue(all("pose" not in rule for rule in saved["windows"][1:]))
            self.assertEqual(shlex.split(mv.run.call_args.args[0]), ["python3", "-", "workspace", "w42", "w7"])
            self.assertIn("stream.read(4097)", mv.run.call_args.kwargs["stdin"])
            self.assertEqual(profile, original)
            self.assertEqual(store.get("Coding"), saved)
            self.assertNotIn("window:42", store.path.read_text())

    def test_invalid_or_unreadable_pose_aborts_without_overwriting_profile(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            store.save(sample_profile())
            original = store.path.read_bytes()
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
            mv.state = mock.Mock(return_value={"available": True, "windows": []})
            for raw in ("not json", '{"workspace":"{}"}', '{"workspace":1}',
                        json.dumps({"workspace": json.dumps(dict(sample_pose(), width=float("nan")))}),
                        json.dumps({"workspace": '{"version":1,"version":1}'}),
                        '{"workspace":"{}","workspace":"{}"}', '{"unexpected":"{}"}'):
                with self.subTest(raw=raw):
                    mv.run = mock.Mock(return_value=raw)
                    with self.assertRaises(frame_macview.MacViewError):
                        profiles.save(store, mv, sample_profile())
                    self.assertEqual(store.path.read_bytes(), original)
            mv.run = mock.Mock(side_effect=RuntimeError("offline"))
            with self.assertRaisesRegex(frame_macview.MacViewError, "offline"):
                profiles.save(store, mv, sample_profile())
            self.assertEqual(store.path.read_bytes(), original)

    def test_missing_files_remove_old_snapshot_and_browser_save_needs_no_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
            mv.state = mock.Mock(return_value={"available": True, "windows": []})
            mv.run = mock.Mock(return_value="{}")
            profile = sample_profile([{"bundleID": "com.apple.Terminal", "pose": sample_pose()}])
            profile["workspace"]["pose"] = sample_pose()
            with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
                self.assertEqual(profiles.save(store, mv, profile),
                                 sample_profile([{"bundleID": "com.apple.Terminal"}]))
            mv.run.reset_mock()
            mv.state.reset_mock()
            with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "browser"}):
                self.assertEqual(profiles.save(store, mv, sample_profile()), sample_profile())
            mv.run.assert_not_called()
            mv.state.assert_not_called()


class ProfileApiTests(unittest.TestCase):
    def test_workspace_mode_actions_validate_and_forward_to_the_same_lifecycle(self):
        import server
        mv = mock.Mock()
        mv.set_mode.return_value = {"mode": "spatial", "changed": True}
        with mock.patch.object(server, "macview", mv), mock.patch.object(server, "LOCAL", False):
            for action, mode in (("mode_spatial", "spatial"), ("mode_desktop", "desktop")):
                server.macview_action({"action": action})
                mv.set_mode.assert_called_with(mode)
            server.macview_action({"action": "workspace_mode", "mode": "spatial"})
            mv.set_mode.assert_called_with("spatial")
            for request in ({"action": "mode_spatial", "extra": True},
                            {"action": "workspace_mode"}, {"action": "workspace_mode", "mode": "other"},
                            {"action": "workspace_start", "mode": "other"}):
                with self.subTest(request=request), self.assertRaises(server.Failure) as error:
                    server.macview_action(request)
                self.assertEqual(error.exception.status, 400)
            server.macview_action({"action": "workspace_start", "mode": "spatial"})
            mv.start_workspace.assert_called_once_with("5120x1440", 1, "sharp", resume=True, mode="spatial")

    def test_profile_save_action_captures_poses_and_reports_failure_without_overwrite(self):
        import server

        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            mv = frame_macview.MacView(["ssh"], mock.Mock(return_value=json.dumps({
                "workspace": json.dumps(sample_pose())})), "frame")
            mv.state = mock.Mock(return_value={"available": True, "windows": []})
            with mock.patch.object(server, "workspace_profiles", store), mock.patch.object(server, "macview", mv), \
                    mock.patch.object(server, "LOCAL", False):
                saved = server.macview_action({"action": "profile_save", "profile": sample_profile()})
                self.assertEqual(saved["profile"]["workspace"]["pose"], sample_pose())
                self.assertEqual(server.macview_action({"action": "profile_list"})["schemaVersion"], 2)
                original = store.path.read_bytes()
                mv.run.side_effect = RuntimeError("offline")
                with self.assertRaises(server.Failure) as failure:
                    server.macview_action({"action": "profile_save", "profile": sample_profile()})
                self.assertEqual(failure.exception.status, 502)
                self.assertEqual(store.path.read_bytes(), original)


class FreshWindowMatchingTests(unittest.TestCase):
    def test_unique_matches_and_actionable_missing_retitled_ambiguous_and_duplicate_results(self):
        rules = [
            {"bundleID": "com.google.Chrome", "titleHint": "Project Alpha"},
            {"bundleID": "com.apple.Terminal", "titleHint": "missing-app"},
            {"bundleID": "com.apple.TextEdit", "titleHint": "Renamed"},
            {"bundleID": "com.apple.Preview"},
            {"bundleID": "com.example.Editor", "titleHint": "Project"},
            {"bundleID": "com.example.Editor", "titleHint": "Alpha"},
        ]
        windows = [
            {"id": 410, "src": "window:410", "bundleID": "com.google.Chrome", "title": "Project Alpha - Repo"},
            {"id": 420, "src": "window:420", "bundleID": "com.apple.TextEdit", "title": "Untitled"},
            {"id": 430, "src": "window:430", "bundleID": "com.apple.Preview", "title": "one"},
            {"id": 431, "src": "window:431", "bundleID": "com.apple.Preview", "title": "two"},
            {"id": 440, "src": "window:440", "bundleID": "com.example.Editor", "title": "Project Alpha"},
        ]
        result = profiles.match_windows(sample_profile(rules), windows)
        self.assertEqual([item["status"] for item in result],
                         ["matched", "missing", "retitled", "ambiguous", "matched", "duplicate"])
        self.assertEqual(result[0]["source"], "window:410")
        self.assertIn("open the app", result[1]["message"])
        self.assertIn("update or remove", result[2]["message"])
        self.assertIn("add a title hint", result[3]["message"])
        self.assertEqual(result[4]["source"], "window:440")
        self.assertEqual(result[5]["status"], "duplicate")

    def test_invalid_or_non_window_transient_sources_fail_closed(self):
        rule = sample_profile([{"bundleID": "com.example.Editor"}])
        windows = [{"id": 31, "src": "display:31", "bundleID": "com.example.Editor", "title": "Editor"}]
        self.assertEqual(profiles.match_windows(rule, windows)[0]["status"], "unavailable")

    def test_spatial_sources_match_only_the_current_window_id(self):
        rule = sample_profile([{"bundleID": "com.example.Editor"}])
        for source, expected in (("spatial:31", "matched"), ("spatial:32", "unavailable"),
                                 ("menubar", "unavailable"), ("status", "unavailable"),
                                 ("menu:0", "unavailable")):
            with self.subTest(source=source):
                result = profiles.match_windows(rule, [{"id": 31, "src": source, "bundleID": "com.example.Editor"}])[0]
                self.assertEqual(result["status"], expected)


class ProfileCliTests(unittest.TestCase):
    class Client:
        def __init__(self):
            self.requests = []

        def request(self, path, body):
            self.requests.append((path, body))
            if body["action"] == "profile_list":
                return {"schemaVersion": 1, "profiles": []}
            return {"ok": True}

    def test_save_parses_repeated_window_rules_and_posts_only_logical_settings(self):
        args = profile_cli.parser().parse_args([
            "save", "Coding", "--preset", "3440x1440", "--scale", "2", "--quality", "balanced",
            "--window", "com.google.Chrome::Project", "--window", "com.apple.Terminal",
        ])
        client, output = self.Client(), io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(profile_cli.run(args, client), 0)
        self.assertEqual(client.requests, [("/api/macview", {"action": "profile_save", "profile": {
            "name": "Coding", "workspace": {"preset": "3440x1440", "scale": 2, "quality": "balanced"},
            "windows": [{"bundleID": "com.google.Chrome", "titleHint": "Project"},
                        {"bundleID": "com.apple.Terminal"}]}})])

    def test_reset_requires_explicit_confirmation_before_api_mutation(self):
        args = profile_cli.parser().parse_args(["reset"])
        client = self.Client()
        with self.assertRaisesRegex(ValueError, "reset cancelled"):
            profile_cli.run(args, client, input_fn=lambda _: "no")
        self.assertEqual(client.requests, [])
        with contextlib.redirect_stdout(io.StringIO()):
            profile_cli.run(args, client, input_fn=lambda _: "RESET")
        self.assertEqual(client.requests[-1], ("/api/macview", {
            "action": "profile_reset", "confirmation": "RESET"}))


class ProductionRestorePathTests(unittest.TestCase):
    @staticmethod
    def fake_helper(path):
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "w = int(sys.argv[sys.argv.index('--width') + 1])\n"
            "h = int(sys.argv[sys.argv.index('--height') + 1])\n"
            "s = int(sys.argv[sys.argv.index('--scale') + 1])\n"
            "print(json.dumps({'state':'ready','name':'Frame Workspace','src':'display:4242','displayID':4242,"
            "'actual':{'logicalWidth':w,'logicalHeight':h,'bufferWidth':w*s,'bufferHeight':h*s},"
            "'mirrored':False,'main':False,'physicalLayoutUnchanged':True}), flush=True)\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'state':'stopped','displayID':4242,'removed':True}), flush=True)\n",
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_saved_poses_follow_fresh_window_ids_and_replace_running_workspace_viewer(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "FRAME_WORKSPACE_VIEWER": "native", "FRAME_WINDOW_VIEWER": "native",
                "FRAME_WORKSPACE_DECODER": "v4l2", "FRAME_WORKSPACE_CONVERSION": "gpu"}):
            root = Path(temp)
            profile = sample_profile([{"bundleID": "com.apple.Terminal", "titleHint": "Shell", "pose": sample_pose(3)},
                                      {"bundleID": "com.example.Editor"}])
            profile["workspace"]["pose"] = sample_pose(-2)
            store = profiles.ProfileStore(root / "profiles.json")
            store.save(profile)
            manager = frame_macview.WorkspaceDisplayManager(self.fake_helper(root / "fake-frame-mac-view"))
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
            mv.workspace_manager = manager
            mv.build = mock.Mock()
            mv.remote_port = 47900
            mv.ensure_tunnel = mock.Mock()
            window_id = 42
            workspace_stream = False
            calls, tickets = [], []

            def windows():
                return [{"id": window_id, "src": f"window:{window_id}", "title": "Shell", "bundleID": "com.apple.Terminal"},
                        {"id": 73, "src": "window:73", "title": "Repo", "bundleID": "com.example.Editor"}]

            mv.state = lambda: {"available": True, "screen": True, "windows": windows()}

            def call(path, **query):
                if path == "/status":
                    return {"screen": True, "streams": [{"src": "display:4242"}] if workspace_stream else [], "finished": []}
                if path == "/windows":
                    return {"windows": windows()}
                if path == "/ticket":
                    tickets.append(query["src"])
                    return {"ticket": "fresh-ticket"}
                if path == "/close":
                    return {"closed": 1}
                return {"displays": []}

            def run(remote, **kwargs):
                nonlocal window_id, workspace_stream
                calls.append(remote)
                args = shlex.split(remote)
                if "start" in args:
                    if "--panel" in args:
                        return "registered OpenVR key com.frame-workspace.native.1000.panel-" + args[args.index("--panel") + 1]
                    window_id += 100  # re-enumerate after every viewer start
                    workspace_stream = True
                    return "registered OpenVR key com.frame-workspace.native.1000.workspace"
                if remote.startswith("cat "):
                    return json.dumps({"ready": True, "placement": sample_pose(-2)})
                return "stopped"

            mv.call, mv.run = call, mock.Mock(side_effect=run)
            try:
                first = profiles.restore(store, mv, "Coding")
                self.assertEqual(first["placement"], "saved")
                self.assertEqual(first["spatialRestore"]["restoredPoses"], 2)
                self.assertEqual([r["poseRestored"] for r in first["windows"]], [True, False])
                self.assertIn("window:142", tickets)
                second = profiles.restore(store, mv, "Coding")
                self.assertEqual(second["spatialRestore"]["restoredPoses"], 2)
                self.assertIn("window:242", tickets)
                starts = [shlex.split(c)[3:] for c in calls if "'start'" in c]
                workspace_starts = [a for a in starts if "--panel" not in a]
                self.assertEqual(len(workspace_starts), 2)  # no recenter/focus shortcut
                for args in workspace_starts:
                    self.assertEqual(json.loads(args[args.index("--pose") + 1]), sample_pose(-2))
                for panel in ("w142", "w242"):
                    args = next(a for a in starts if panel in a)
                    self.assertEqual(json.loads(args[args.index("--pose") + 1]), sample_pose(3))
                    self.assertNotIn("--initial-pose", args)
                for args in (a for a in starts if "w73" in a):
                    self.assertIn("--initial-pose", args)
                    self.assertNotIn("--pose", args)
                self.assertNotIn("bash -s -- focus", calls)
                self.assertNotIn("fresh-ticket", store.path.read_text())
                self.assertEqual(store.get("Coding"), profile)
            finally:
                manager.stop()

    def test_saved_poses_fail_before_starting_an_incompatible_viewer(self):
        with tempfile.TemporaryDirectory() as temp:
            store = profiles.ProfileStore(Path(temp) / "profiles.json")
            profile = sample_profile([{"bundleID": "com.apple.Terminal", "pose": sample_pose()}])
            store.save(profile)
            mv = frame_macview.MacView(["ssh"], mock.Mock(), "frame")
            mv.state = mock.Mock()
            mv.start_workspace = mock.Mock()
            for backend, windows in (("browser", "browser"), ("native", "browser")):
                with mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": backend, "FRAME_WINDOW_VIEWER": windows}):
                    with self.assertRaisesRegex(frame_macview.MacViewError, "native"):
                        profiles.restore(store, mv, "Coding")
            mv.state.assert_not_called()
            mv.start_workspace.assert_not_called()

    def test_restore_reenumerates_ids_and_uses_real_macview_start_and_show(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = profiles.ProfileStore(root / "profiles.json")
            store.save(sample_profile([{"bundleID": "com.google.Chrome", "titleHint": "Project"}]))
            helper = self.fake_helper(root / "fake-frame-mac-view")
            manager = frame_macview.WorkspaceDisplayManager(helper)
            mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
            mv.workspace_manager = manager
            mv.unavailable = lambda: None  # supply a MacView-capable host to the real state method
            mv.build = lambda: None        # the helper is the test executable above
            mv.remote_port = 47900
            mv.ensure_tunnel = lambda **kwargs: None  # local fake boundary; keep the real show/start logic

            window_id = 111
            ended_windows = []
            active_sources = []
            tickets = []
            launches = []

            def call(path, method="GET", **query):
                if path == "/status":
                    return {"screen": True, "accessibility": True,
                            "streams": [{"src": src} for src in active_sources],
                            "finished": list(ended_windows)}
                if path == "/windows":
                    return {"windows": [{"id": window_id, "src": f"window:{window_id}",
                                          "bundleID": "com.google.Chrome", "title": "Project - repo",
                                          "app": "Chrome"}]}
                if path == "/displays":
                    return {"displays": []}
                if path == "/ticket":
                    value = f"fresh-ticket-{len(tickets) + 1}"
                    tickets.append((query["src"], value))
                    return {"ticket": value}
                if path == "/close":
                    active_sources[:] = [src for src in active_sources if src != query.get("src")]
                    return {"closed": 1}
                raise AssertionError(path)

            def run(remote, **kwargs):
                nonlocal window_id
                launches.append(remote)
                if remote.startswith("bash -s -- 'start'"):
                    tag = re.search(r"(fc[0-9a-f]{8})", remote).group(1)
                    active_sources.append("display:4242")
                    # Startup changes the current enumeration after preflight.
                    window_id = 222
                    return f"Workspace viewer ready: 5120x1440 and registered OpenVR key com.frame-control.workspace.1000.{tag}"
                if "--showdashboard" in remote:
                    # A later reconnect can rehome/close that window too.
                    ended_windows.append(f"window:{window_id}")
                    window_id = 333
                    return "focus accepted"
                for source in ("window:111", "window:222", "window:333"):
                    if source in remote:
                        active_sources.append(source)
                return "panel opened"

            mv.call = call
            mv.run = run
            first = profiles.restore(store, mv, "Coding")
            self.assertEqual(first["placement"], "manual")
            self.assertFalse(first["spatialRestore"]["available"])
            self.assertEqual(first["windows"][0]["status"], "shown")
            self.assertEqual(tickets[-1][0], "window:222")
            self.assertIn("t=fresh-ticket-2", launches[-1])
            self.assertNotIn("displayID", first["workspace"])
            self.assertNotIn("captureSrc", first["workspace"])

            # The Frame reconnects after macOS has assigned another window ID.
            second = profiles.restore(store, mv, "Coding")
            self.assertEqual(second["windows"][0]["status"], "shown")
            self.assertEqual(tickets[-1][0], "window:333")
            self.assertIn("t=fresh-ticket-3", launches[-1])
            self.assertIn("window:333", mv.shown)
            self.assertNotIn("window:222", mv.shown)
            data = (root / "profiles.json").read_text(encoding="utf-8")
            for transient in ("window:111", "window:222", "window:333", "display:4242", "fresh-ticket"):
                self.assertNotIn(transient, data)
            self.assertEqual([ticket[0] for ticket in tickets],
                             ["display:4242", "window:222", "window:333"])
            manager.stop()


if __name__ == "__main__":
    unittest.main()


class SessionKeeperTests(unittest.TestCase):
    """The auto-saved Last Session: captured while the Workspace runs, kept
    out of the profile list, and brought back on the next start."""

    class FakeMacView:
        def __init__(self):
            self.closing = False
            self.workspace_panel_key = "com.frame-workspace.native.1000.workspace"
            self.native_panels = {"window:5": {"key": "k5"}, "window:9": {"key": "k9"}}
            self.windows = [
                {"id": 5, "src": "window:5", "bundleID": "com.apple.Terminal", "title": "zsh — 80×24", "app": "Terminal"},
                {"id": 9, "src": "window:9", "bundleID": "com.apple.finder", "title": "Downloads", "app": "Finder"},
                {"id": 11, "src": "window:11", "bundleID": "com.apple.finder", "title": "Desktop", "app": "Finder"},
            ]
            self.shown = []
            self.fail_poses = False
            self.workspace_manager = mock.Mock()
            self.workspace_manager.state.return_value = {"state": "running", "preset": "2880x2560", "scale": 1,
                                                         "quality": "sharp"}
            self.before_workspace_stop = self.after_workspace_start = None

        def state(self):
            return {"available": True, "screen": True, "windows": self.windows}

        def read_panel_poses(self, sources):
            if self.fail_poses:
                raise frame_macview.MacViewError("Frame unreachable")
            return {s: sample_pose(i) for i, s in enumerate(sources)}

        def show(self, src, quality="balanced", pose=None):
            self.shown.append((src, pose))
            self.native_panels[src] = {"key": src}
            return {"panel": src}

    def keeper(self, tmp):
        mv = self.FakeMacView()
        store = profiles.ProfileStore(os.path.join(tmp, "last-session.json"))
        return mv, store, profiles.SessionKeeper(store, mv)

    def test_snapshot_records_windows_and_poses_with_hints_only_where_needed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            self.assertTrue(keeper.snapshot())
            saved = store.get(profiles.LAST_SESSION)
            rules = {r["bundleID"]: r for r in saved["windows"]}
            self.assertNotIn("titleHint", rules["com.apple.Terminal"])  # one Terminal window: no hint
            self.assertEqual(rules["com.apple.finder"]["titleHint"], "Downloads")  # two Finder windows
            self.assertTrue(all("pose" in r for r in saved["windows"]) and "pose" in saved["workspace"])

    def test_unreachable_frame_keeps_the_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            keeper.snapshot()
            before = store.get(profiles.LAST_SESSION)
            mv.fail_poses = True
            mv.native_panels.pop("window:5")
            self.assertFalse(keeper.snapshot())
            self.assertEqual(store.get(profiles.LAST_SESSION), before)

    def test_no_workspace_saves_nothing_and_a_stop_freezes_the_last_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            keeper.final_snapshot()  # just before a stop: saved, then frozen
            self.assertEqual(len(store.get(profiles.LAST_SESSION)["windows"]), 2)
            mv.native_panels.clear()  # the windows close as the Workspace stops
            self.assertFalse(keeper.snapshot())
            self.assertEqual(len(store.get(profiles.LAST_SESSION)["windows"]), 2)
            mv.workspace_manager.state.return_value = {"state": "stopped"}
            keeper.paused = False
            self.assertFalse(keeper.snapshot())

    def test_resume_shows_missing_windows_at_their_poses_and_unpauses(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            keeper.snapshot()
            saved = store.get(profiles.LAST_SESSION)
            keeper.final_snapshot()
            mv.native_panels = {"window:9": {"key": "k9"}}  # Finder still shown, Terminal not
            shown = keeper.resume()
            self.assertEqual(shown, ["window:5"])
            terminal = next(r for r in saved["windows"] if r["bundleID"] == "com.apple.Terminal")
            self.assertEqual(mv.shown, [("window:5", terminal["pose"])])
            self.assertFalse(keeper.paused)

    def test_a_named_profile_start_carries_on_saving_without_resuming(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            keeper.start()
            keeper.paused = True
            mv.after_workspace_start(False)
            self.assertFalse(keeper.paused)
            self.assertEqual(mv.shown, [])
            self.assertEqual(profiles.ProfileStore(os.path.join(tmp, "profiles.json")).list(), [])
            mv.closing = True

    @staticmethod
    def spatial(mv, ready=True):
        mv.mode = "spatial"
        mv.workspace_panel_key = None
        mv.native_panels = {s.replace("window:", "spatial:"): p for s, p in mv.native_panels.items()}
        mv.workspace_state = lambda: {"state": "running", "mode": "spatial", "preset": "2880x2560",
                                      "scale": 1, "quality": "sharp", "panelsReady": ready}

    def test_modes_keep_independent_last_sessions_and_spatial_panel_poses(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            self.assertTrue(keeper.snapshot())
            desktop = store.path.read_bytes()
            self.spatial(mv)
            self.assertTrue(keeper.snapshot())
            saved = keeper.stores["spatial"].get(profiles.LAST_SESSION)
            self.assertNotIn("pose", saved["workspace"])  # there is no Workspace panel in Spatial Mode
            self.assertTrue(all("pose" in r for r in saved["windows"]))
            self.assertEqual(store.path.read_bytes(), desktop)
            self.assertNotEqual(saved["windows"], keeper.store.get(profiles.LAST_SESSION)["windows"])
            self.assertEqual(keeper.stores["spatial"].path.stat().st_mode & 0o777, 0o600)

    def test_auxiliary_panels_are_excluded_from_last_session_and_pose_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            mv, store, keeper = self.keeper(tmp)
            self.spatial(mv)
            for source in frame_macview.SPATIAL_AUX_ROLES:
                mv.native_panels[source] = {"key": "auxiliary"}
                mv.windows.append({"id": 99, "src": source, "bundleID": "com.example.Auxiliary"})
            mv.read_panel_poses = mock.Mock(wraps=mv.read_panel_poses)
            self.assertTrue(keeper.snapshot())
            mv.read_panel_poses.assert_called_once_with(["spatial:5", "spatial:9"])
            saved = keeper.stores["spatial"].get(profiles.LAST_SESSION)
            self.assertEqual(len(saved["windows"]), 2)
            self.assertEqual({w["bundleID"] for w in saved["windows"]}, {"com.apple.Terminal", "com.apple.finder"})

    def test_spatial_resume_does_not_duplicate_existing_shared_display_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            mv, store, keeper = self.keeper(tmp)
            self.spatial(mv)
            self.assertTrue(keeper.snapshot())
            self.assertEqual(keeper.resume(), [])
            self.assertEqual(mv.shown, [])

    def test_spatial_resume_matches_fresh_ids_before_first_automatic_launch(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            self.spatial(mv)
            keeper.snapshot()
            saved = keeper.stores["spatial"].get(profiles.LAST_SESSION)
            mv.windows[0].update(id=105, src="window:105", title="New Terminal Title")
            mv.call = mock.Mock(return_value={"windows": [{k: v for k, v in w.items() if k != "src"} for w in mv.windows]})
            mv.native_panels.clear()
            keeper.paused = True
            keeper.resume_async(True)
            self.assertEqual(mv._spatial_resume_poses["spatial:105"], saved["windows"][0]["pose"])
            self.assertEqual(mv.shown, [])  # reconciler starts each window once with its saved pose
            self.assertFalse(keeper.paused)

    def test_partial_spatial_startup_does_not_overwrite_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            self.spatial(mv)
            self.assertTrue(keeper.snapshot())
            before = keeper.stores["spatial"].path.read_bytes()
            self.spatial(mv, ready=False)
            mv.native_panels.clear()
            self.assertFalse(keeper.snapshot())
            self.assertEqual(keeper.stores["spatial"].path.read_bytes(), before)

    def test_last_session_can_save_more_windows_than_named_profile_limit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            self.spatial(mv)
            mv.windows = [{"id": i + 1, "src": f"window:{i + 1}", "bundleID": f"com.example.app{i}"}
                          for i in range(35)]
            mv.native_panels = {f"spatial:{i + 1}": {"key": f"key{i}"} for i in range(35)}
            self.assertTrue(keeper.snapshot())
            saved = keeper.stores["spatial"].get(profiles.LAST_SESSION)
            self.assertEqual(len(saved["windows"]), 35)
            with self.assertRaises(profiles.ProfileError):
                profiles.validate_profile(saved)  # named profiles keep their existing bounded contract

    def test_stale_desktop_resume_cannot_touch_a_new_spatial_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            mv, store, keeper = self.keeper(tmp)
            self.spatial(mv)
            mv._mode_generation = 2
            keeper.paused = True
            self.assertEqual(keeper.resume("desktop", 1), [])
            self.assertEqual(mv.shown, [])
            self.assertTrue(keeper.paused)

    def test_desktop_workspace_pose_is_loaded_before_the_viewer_launch(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FRAME_WORKSPACE_VIEWER": "native"}):
            mv, store, keeper = self.keeper(tmp)
            keeper.snapshot()
            pose = keeper.workspace_pose()
            self.assertEqual(pose, store.get(profiles.LAST_SESSION)["workspace"]["pose"])
            real = frame_macview.MacView([], mock.Mock(), "fake")
            real.resume_workspace_pose = keeper.workspace_pose
            real._start_workspace = mock.Mock(return_value={"workspace": {"state": "running"}})
            real.start_workspace(resume=True)
            self.assertEqual(real._start_workspace.call_args.args[3], pose)
