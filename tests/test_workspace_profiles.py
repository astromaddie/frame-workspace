"""FW-11 profile storage, fresh matching, and the real MacView restore path."""
import json
import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import os
from pathlib import Path
import re
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
            self.assertEqual(json.loads(path.read_text()), {"schemaVersion": 1, "profiles": [saved]})
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
        with self.assertRaisesRegex(profiles.ProfileError, "bundleID and an optional titleHint only"):
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

            path.write_text('{"schemaVersion":2,"profiles":[]}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(profiles.ProfileStoreError, "unsupported.*version: 2"):
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
            self.assertEqual(store.reset(), {"schemaVersion": 1, "profiles": []})
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
