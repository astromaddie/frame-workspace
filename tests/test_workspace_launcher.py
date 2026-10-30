"""Exercise the real Frame Workspace library launcher and dashboard script."""
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import sandbox  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "workspace-launch-on-frame.py"
DASH = ROOT / "scripts" / "workspace-dash-on-frame.sh"
EXIT_HELPER = ROOT / "scripts" / "workspace-exit-on-frame.py"


class WorkspaceLauncher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frame-workspace-launcher-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "data"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.service = self.root / "dashboard-active"
        self.vrcmd_log = self.root / "vrcmd.log"
        self._write("systemctl", '''#!/bin/sh
shift
case "$1" in
  is-active) [ -f "$FRAME_TEST_SERVICE" ] ;;
  reset-failed) exit 0 ;;
  list-units) exit 0 ;;
  --no-block) [ "$2" = stop ] && rm -f "$FRAME_TEST_SERVICE" ;;
  stop) rm -f "$FRAME_TEST_SERVICE" ;;
  *) exit 2 ;;
esac
''')
        self._write("systemd-run", '''#!/bin/sh
[ -z "${FRAME_TEST_SYSTEMD_RUN_STARTED:-}" ] || touch "$FRAME_TEST_SYSTEMD_RUN_STARTED"
sleep "${FRAME_TEST_SYSTEMD_RUN_DELAY:-0}"
touch "$FRAME_TEST_SERVICE"
exit 0
''')
        self._write("vrcmd", '''#!/bin/sh
if [ "$1" = --overlays ]; then
  echo "---- OVERLAYS ----"
  echo "'com.frame-workspace.dash' -- 'Frame Workspace', visible VROverlayType_Dashboard_Main"
  exit 0
fi
printf '%s\\n' "$*" >> "$FRAME_TEST_VRCMD_LOG"
''')
        self._write("pgrep", "#!/bin/sh\nexit 1\n")
        self._write("pkill", "#!/bin/sh\nexit 1\n")
        self.logger_log = self.root / "logger.log"
        self._write("logger", '''#!/bin/sh
printf '%s\\n' "$*" >> "$FRAME_TEST_LOGGER_LOG"
''')
        self._write(self.data / "frame-workspace" / "native" / "frame-workspace-dash", "#!/bin/sh\nexit 0\n")
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "FRAME_TEST_SERVICE": str(self.service),
            "FRAME_TEST_VRCMD_LOG": str(self.vrcmd_log),
            "FRAME_TEST_LOGGER_LOG": str(self.logger_log),
            "FRAME_WORKSPACE_VRCMD": str(self.bin / "vrcmd"),
            "FRAME_WORKSPACE_CONNECT_TIMEOUT": "2",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, content):
        path = Path(name) if isinstance(name, Path) else self.bin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o700)
        return path

    def _launch(self, *, check=True, env=None, timeout=10):
        return subprocess.run(["python3", str(LAUNCHER)], env=env or self.env, capture_output=True,
                              text=True, timeout=timeout, check=check)

    def _companion_sync(self, state):
        return subprocess.run(["bash", "-s", "--", "sync", json.dumps(state, separators=(",", ":"))],
                              input=DASH.read_text(encoding="utf-8"), env=self.env, capture_output=True,
                              text=True, timeout=5, check=True)

    def _companion_reopen(self, state):
        return subprocess.run(["bash", "-s", "--", "reopen", json.dumps(state, separators=(",", ":"))],
                              input=DASH.read_text(encoding="utf-8"), env=self.env, capture_output=True,
                              text=True, timeout=5, check=True)

    def test_offline_launch_opens_dashboard_without_touching_companion_state_or_queuing_action(self):
        runtime = self.runtime / "frame-workspace"
        runtime.mkdir()
        state = runtime / "dash-state.json"
        original = '{"connected":true,"workspace":{"running":true}}\n'
        state.write_text(original, encoding="utf-8")
        old = time.time() - 20
        os.utime(state, (old, old))

        result = self._launch(env={**self.env, "FRAME_WORKSPACE_CONNECT_TIMEOUT": "0.15"})

        self.assertIn("dashboard is open", result.stdout)
        self.assertIn("not connected", result.stdout)
        self.assertEqual(state.read_text(encoding="utf-8"), original)
        self.assertFalse((runtime / "requests").exists() and list((runtime / "requests").glob("launcher-*.json")))
        self.assertEqual(self.vrcmd_log.read_text(encoding="utf-8").splitlines(),
                         ["--showdashboard com.frame-workspace.dash"])

    def test_repeated_running_sync_stays_closed_and_still_polls_frame_requests(self):
        requests = self.runtime / "frame-workspace" / "requests"
        requests.mkdir(parents=True)
        self.service.touch()
        exited = subprocess.run([sys.executable, str(EXIT_HELPER), "exit-current", "--request-dir", str(requests)],
                                env=self.env, capture_output=True, text=True, timeout=8, check=True)
        self.assertIn("viewer session stopped", exited.stdout)
        closed = self.data / "frame-workspace" / "native" / "dashboard-closed.json"
        self.assertTrue(closed.exists(), "the production exit helper must persist its explicit close")
        request = requests / "dash-1-1.json"
        request.write_text('{"action":"recenter"}\n', encoding="utf-8")
        state = {"connected": True, "workspace": {"running": True}}

        first = self._companion_sync(state)
        second = self._companion_sync(state)

        self.assertIn("DASH_CLOSED", first.stdout)
        self.assertIn('{"action":"recenter"}', first.stdout.splitlines())
        self.assertTrue(any(json.loads(line).get("action") == "workspace_stop"
                            for line in first.stdout.splitlines() if line.startswith("{")),
                        "the Mac restore request produced by explicit exit must still be polled")
        self.assertIn("DASH_CLOSED", second.stdout)
        self.assertTrue(closed.exists(), "ordinary running status must not clear explicit close")
        self.assertFalse(self.service.exists(), "sync must not respawn the dashboard")
        self.assertFalse(request.exists(), "the connected companion must continue draining requests")
        self.assertNotIn("--showdashboard", self.vrcmd_log.read_text(encoding="utf-8"),
                         "closed sync must not reopen the SteamVR dashboard")

    def test_exit_waits_for_inflight_sync_then_closes_its_dashboard_start(self):
        requests = self.runtime / "frame-workspace" / "requests"
        requests.mkdir(parents=True)
        started = self.root / "systemd-run-started"
        delayed = {**self.env, "FRAME_TEST_SYSTEMD_RUN_STARTED": str(started),
                   "FRAME_TEST_SYSTEMD_RUN_DELAY": "0.3"}
        script = DASH.open(encoding="utf-8")
        sync = subprocess.Popen(["bash", "-s", "--", "sync", '{"connected":true,"workspace":{"running":false}}'],
                                stdin=script, env=delayed, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        script.close()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not started.exists():
                time.sleep(0.01)
            self.assertTrue(started.exists(), "the sync must be inside dashboard startup before Exit begins")
            close = subprocess.run([sys.executable, str(EXIT_HELPER), "exit-current", "--request-dir", str(requests)],
                                   env=self.env, capture_output=True, text=True, timeout=8)
            sync_out, sync_err = sync.communicate(timeout=5)
        finally:
            if sync.poll() is None:
                sync.kill()
                sync.wait(timeout=2)

        self.assertEqual(sync.returncode, 0, sync_err)
        self.assertEqual(close.returncode, 0, close.stderr)
        self.assertTrue((self.data / "frame-workspace" / "native" / "dashboard-closed.json").exists())
        self.assertFalse(self.service.exists(), "the close must retire a service started by an in-flight sync")
        subsequent = self._companion_sync({"connected": True, "workspace": {"running": True}})
        self.assertIn("DASH_CLOSED", subsequent.stdout)
        self.assertFalse(self.service.exists())

    def test_fresh_steam_launch_clears_the_close_latch(self):
        native = self.data / "frame-workspace" / "native"
        native.mkdir(parents=True, exist_ok=True)
        closed = native / "dashboard-closed.json"
        closed.write_text('{"version":1,"session":"' + "a" * 32 + '"}\n', encoding="utf-8")

        result = self._launch(env={**self.env, "FRAME_WORKSPACE_CONNECT_TIMEOUT": "0"})

        self.assertIn("dashboard is open", result.stdout)
        self.assertFalse(closed.exists())
        self.assertTrue(self.service.exists())
        self.assertEqual(self.vrcmd_log.read_text(encoding="utf-8").splitlines(),
                         ["--showdashboard com.frame-workspace.dash"])

    def test_successful_mac_start_reopens_dashboard_and_polls_requests(self):
        native = self.data / "frame-workspace" / "native"
        native.mkdir(parents=True, exist_ok=True)
        closed = native / "dashboard-closed.json"
        closed.write_text('{"version":1,"session":"' + "a" * 32 + '"}\n', encoding="utf-8")
        requests = self.runtime / "frame-workspace" / "requests"
        requests.mkdir(parents=True)
        request = requests / "dash-2-1.json"
        request.write_text('{"action":"recenter"}\n', encoding="utf-8")

        result = self._companion_reopen({"connected": True, "workspace": {"running": True}})

        self.assertFalse(closed.exists())
        self.assertTrue(self.service.exists())
        self.assertIn('{"action":"recenter"}', result.stdout.splitlines())
        self.assertFalse(request.exists())
        self.assertFalse(self.vrcmd_log.exists(), "Mac Start must not foreground SteamVR's dashboard overlay")

    def test_fresh_companion_request_uses_saved_start_verb_and_real_sync_consumes_it(self):
        process = subprocess.Popen(["python3", str(LAUNCHER)], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.vrcmd_log.exists():
                time.sleep(0.02)
            self.assertTrue(self.vrcmd_log.exists(), "launcher did not show the existing dashboard")
            state = {"connected": True, "workspace": {"running": False, "mode": "spatial"}}
            state_file = self.runtime / "frame-workspace" / "dash-state.json"
            state_file.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")

            requests = self.runtime / "frame-workspace" / "requests"
            deadline = time.monotonic() + 4
            pending = []
            while time.monotonic() < deadline:
                pending = list(requests.glob("launcher-*.json")) if requests.exists() else []
                if pending:
                    break
                if process.poll() is not None:
                    out, err = process.communicate()
                    self.fail(f"launcher exited before sending request: {out} {err}")
                time.sleep(0.02)
            self.assertEqual(len(pending), 1)
            request = json.loads(pending[0].read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "workspace_start")
            self.assertEqual(request["source"], "steam-library")
            self.assertGreater(request["expiresAt"], time.time())
            self.assertLessEqual(request["expiresAt"], time.time() + 10)

            consumed = self._companion_sync(state)
            consumed_request = next(line for line in consumed.stdout.splitlines() if line.startswith("{"))
            self.assertEqual(json.loads(consumed_request), request)
            out, err = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, err)
            self.assertIn("last saved monitor settings", out)
            self.assertFalse(pending[0].exists())
            repeated = self._launch()
            self.assertIn("ignoring a duplicate launch", repeated.stdout)
            self.assertEqual(len(self.vrcmd_log.read_text(encoding="utf-8").splitlines()), 1)
            self.assertFalse(list(requests.glob("launcher-*.json")))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_concurrent_launch_does_not_start_a_second_request(self):
        first = subprocess.Popen(["python3", str(LAUNCHER)], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.vrcmd_log.exists():
                time.sleep(0.02)
            second = self._launch(timeout=3)
            self.assertIn("already opening", second.stdout)
            self.assertEqual(len(self.vrcmd_log.read_text(encoding="utf-8").splitlines()), 1)

            state = {"connected": True, "workspace": {"running": True, "mode": "spatial"}}
            (self.runtime / "frame-workspace" / "dash-state.json").write_text(
                json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")
            requests = self.runtime / "frame-workspace" / "requests"
            deadline = time.monotonic() + 4
            pending = []
            while time.monotonic() < deadline:
                pending = list(requests.glob("launcher-*.json")) if requests.exists() else []
                if pending:
                    break
                time.sleep(0.02)
            self.assertEqual(len(pending), 1)
            self._companion_sync(state)
            out, err = first.communicate(timeout=4)
            self.assertEqual(first.returncode, 0, err)
            self.assertIn("last saved monitor settings", out)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait(timeout=2)

    def test_sync_drops_only_old_library_requests(self):
        runtime = self.runtime / "frame-workspace"
        requests = runtime / "requests"
        requests.mkdir(parents=True)
        stale = requests / "launcher-old.json"
        stale.write_text('{"action":"workspace_start"}\n', encoding="utf-8")
        old = time.time() - 30
        os.utime(stale, (old, old))
        unrelated = requests / "dash-123-1.json"
        unrelated.write_text('{"action":"recenter"}\n', encoding="utf-8")

        result = self._companion_sync({"connected": True, "workspace": {"running": True}})

        self.assertFalse(stale.exists())
        self.assertIn('{"action":"recenter"}', result.stdout)
        self.assertNotIn("workspace_start", result.stdout)
        self.assertFalse(unrelated.exists())

    def test_sync_logs_and_forwards_a_shelf_click(self):
        requests = self.runtime / "frame-workspace" / "requests"
        requests.mkdir(parents=True)
        click = '{"action":"app","verb":"open","bundleID":"com.apple.Terminal","id":"d7-3","selected":"k"}'
        (requests / "spatial-7-3.json").write_text(click + "\n", encoding="utf-8")
        (requests / "dash-1-1.json").write_text('{"action":"recenter"}\n', encoding="utf-8")

        result = self._companion_sync({"connected": True, "workspace": {"running": True}})

        self.assertIn(click, result.stdout.splitlines())
        self.assertFalse(list(requests.glob("*.json")), "taken requests are removed")
        lines = self.logger_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["-t frame-workspace -- dock: poller picked up spatial-7-3.json " + click,
                                 "-t frame-workspace -- dock: poller forwarded spatial-7-3.json to the companion"])

    def test_sync_accepts_a_state_with_many_long_window_titles_written_as_escapes(self):
        # Titles are written as \\u escapes, six bytes a character and twelve for an emoji: 32 titles
        # of 64 made a state the script refused, and the refusal took the poll for shelf clicks with it.
        sys.path.insert(0, str(ROOT / "ui"))
        import frame_macview
        mv = frame_macview.MacView([], lambda *a, **k: "", "fake")
        mv.mode = "spatial"
        mv.native_panels = {f"window:{n}": {"slot": n, "key": f"com.frame-workspace.native.1000.panel-w{n}",
                                            "title": "\U0001F600" * 64, "role": "window"} for n in range(40)}
        raw = json.dumps(mv.dash_state(), separators=(",", ":"), allow_nan=False)
        self.assertGreater(len(raw), 16384, "the old limit would have refused this")
        text = mv.dash_state_text()
        state = json.loads(text)
        self.assertLessEqual(len(text), mv.DASH_STATE_LIMIT)
        self.assertTrue(state["windows"] and state["workspace"]["mode"] == "spatial")
        requests = self.runtime / "frame-workspace" / "requests"
        requests.mkdir(parents=True)
        (requests / "spatial-1-1.json").write_text('{"action":"app","verb":"open","bundleID":"a.b","id":"d1-1"}\n')
        result = subprocess.run(["bash", str(DASH), "sync", text], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"id":"d1-1"', result.stdout)


if __name__ == "__main__":
    unittest.main()
