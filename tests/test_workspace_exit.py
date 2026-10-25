"""Production-path tests for Frame Workspace's local Stop helper."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import sandbox  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "workspace-exit-on-frame.py"


class WorkspaceExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frame-workspace-exit-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "data"
        self.runtime = self.root / "runtime"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.units = self.root / "units.json"
        self.calls = self.root / "calls"
        self.vrcmd = self.bin / "vrcmd"
        self.vrcmd.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FRAME_TEST_VRCMD"\n[ "${FRAME_TEST_HIDE_FAIL:-0}" = 0 ]\n', encoding="utf-8")
        self.vrcmd.chmod(0o755)
        self._write_bin("systemctl", '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
def log(value):
    with open(os.environ['FRAME_TEST_CALLS'], 'a') as out: out.write(value + '\\n')
if args[:2] == ['--user', 'list-units']:
    if os.environ.get('FRAME_TEST_LIST_FAIL') == '1': raise SystemExit(2)
    units = json.loads(pathlib.Path(os.environ['FRAME_TEST_UNITS']).read_text())
    for name in units: print(name + ' loaded active running Frame Workspace')
    raise SystemExit(0)
if len(args) >= 3 and args[:2] == ['--user', '--no-block'] and args[2] == 'stop':
    names = args[3:]
    log('stop ' + ' '.join(names))
    if os.environ.get('FRAME_TEST_STUCK') != '1':
        path = pathlib.Path(os.environ['FRAME_TEST_UNITS'])
        units = json.loads(path.read_text())
        path.write_text(json.dumps([name for name in units if name not in names]))
    raise SystemExit(0)
if len(args) >= 3 and args[:2] == ['--user', 'kill']:
    log('kill ' + ' '.join(args[3:]))
    raise SystemExit(0)
raise SystemExit(0)
''')
        self._write_bin("pgrep", "#!/bin/sh\nexit 1\n")
        self._write_bin("pkill", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FRAME_TEST_PKILL"\nexit 1\n')
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FRAME_TEST_UNITS": str(self.units),
            "FRAME_TEST_CALLS": str(self.calls),
            "FRAME_TEST_PKILL": str(self.root / "pkill.log"),
            "FRAME_TEST_VRCMD": str(self.root / "vrcmd.log"),
            "FRAME_WORKSPACE_VRCMD": str(self.vrcmd),
        }
        self.units.write_text("[]", encoding="utf-8")
        self.session = "a" * 32

    def tearDown(self):
        self.temp.cleanup()

    def _write_bin(self, name, value):
        path = self.bin / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _run(self, operation, session=None, *extra, env=None, check=False):
        args = [sys.executable, str(HELPER), operation, "--session", session or self.session, *extra]
        return subprocess.run(args, env=env or self.env, capture_output=True, text=True,
                              timeout=8, check=check)

    def test_offline_exit_closes_only_owned_units_writes_mac_intent_and_preserves_pose(self):
        uid = os.getuid()
        owned = [f"frame-workspace-host-{uid}.service", f"frame-workspace-viewer-{uid}.service",
                 f"frame-workspace-panel-{uid}-w42.service"]
        foreign = ["steamvr.service", f"frame-workspace-panel-{uid + 1}-w9.service"]
        self.units.write_text(json.dumps(owned + foreign), encoding="utf-8")
        self._run("begin", check=True)

        runtime = self.runtime / "frame-workspace"
        (runtime / "host" / "panels").mkdir(parents=True)
        url = runtime / "panel-w42" / "view-fc12345678.url"
        url.parent.mkdir(parents=True)
        url.write_text("http://127.0.0.1/view?t=private", encoding="utf-8")
        external = self.root / "user-owned.json"
        external.write_text('{"keep":true}', encoding="utf-8")
        escaped_state = url.parent / "state.json"
        escaped_state.symlink_to(external)
        (runtime / "host" / "panels" / "w42.json").write_text(json.dumps({
            "session": self.session, "urlFile": str(url), "controlFile": str(external),
            "stateFile": str(escaped_state), "overlayKey": "owned-panel"
        }), encoding="utf-8")
        (runtime / "spatial-ui.json").write_text('{"visible":true}', encoding="utf-8")
        pose = self.data / "frame-workspace" / "workspace-pose.json"
        pose.parent.mkdir(parents=True, exist_ok=True)
        pose.write_text('{"pose":"saved"}', encoding="utf-8")
        requests = runtime / "requests"

        result = self._run("exit", self.session, "--request-dir", str(requests), check=True)

        remaining = json.loads(self.units.read_text(encoding="utf-8"))
        self.assertEqual(remaining, foreign)
        stop = next(line for line in self.calls.read_text(encoding="utf-8").splitlines() if line.startswith("stop "))
        self.assertEqual(set(stop.split()[1:]), set(owned))
        self.assertFalse((runtime / "host" / "panels" / "w42.json").exists())
        self.assertFalse(url.exists())
        self.assertFalse(url.parent.exists())
        self.assertEqual(external.read_text(encoding="utf-8"), '{"keep":true}')
        self.assertFalse((runtime / "spatial-ui.json").exists())
        self.assertEqual(pose.read_text(encoding="utf-8"), '{"pose":"saved"}')
        stop_requests = list(requests.glob("local-stop-*.json"))
        self.assertEqual(len(stop_requests), 1)
        self.assertEqual(json.loads(stop_requests[0].read_text()),
                         {"action": "workspace_stop", "session": self.session})
        self.assertIn("viewer session stopped", result.stdout)

    def test_stale_stop_cannot_kill_a_new_session_and_repeat_stop_is_idempotent(self):
        uid = os.getuid()
        new_session = "b" * 32
        self._run("begin", check=True)
        self._run("stop", check=True)
        self._run("stop", check=True)
        self._run("begin", new_session, check=True)
        host = f"frame-workspace-host-{uid}.service"
        self.units.write_text(json.dumps([host]), encoding="utf-8")

        stale = self._run("exit", self.session)

        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("newer Workspace session", stale.stderr)
        self.assertEqual(json.loads(self.units.read_text()), [host])
        self.assertEqual(self._run("check", new_session).returncode, 0)

    def test_adopt_recovers_a_mac_companion_restart_without_accepting_old_requests(self):
        old_session, new_session = self.session, "b" * 32
        self._run("begin", old_session, check=True)
        self.units.write_text(json.dumps([f"frame-workspace-host-{os.getuid()}.service"]), encoding="utf-8")

        adopted = self._run("adopt", new_session, check=True)
        old_check = self._run("check", old_session)
        stale_stop = self._run("exit", old_session)

        self.assertIn("adopted", adopted.stdout)
        self.assertNotEqual(old_check.returncode, 0)
        self.assertNotEqual(stale_stop.returncode, 0)
        self.assertEqual(json.loads(self.units.read_text()), [f"frame-workspace-host-{os.getuid()}.service"])
        self.assertEqual(self._run("begin", new_session).returncode, 0)
        status = subprocess.run([sys.executable, str(HELPER), "status"], env=self.env,
                                capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(json.loads(status.stdout), {"stopAvailable": True})

    def test_status_keeps_stop_available_for_orphan_services_and_not_when_clean(self):
        status_cmd = [sys.executable, str(HELPER), "status"]
        clean = subprocess.run(status_cmd, env=self.env, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(json.loads(clean.stdout), {"stopAvailable": False})

        self.units.write_text(json.dumps([f"frame-workspace-host-{os.getuid()}.service"]), encoding="utf-8")
        orphan = subprocess.run(status_cmd, env=self.env, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(json.loads(orphan.stdout), {"stopAvailable": True})

    def test_symlinked_runtime_root_is_not_traversed_for_cleanup(self):
        external_runtime = self.root / "user-runtime"
        host_panels = external_runtime / "host" / "panels"
        host_panels.mkdir(parents=True)
        external_spec = host_panels / "w42.json"
        external_file = external_runtime / "panel-w42" / "control.json"
        external_file.parent.mkdir()
        external_spec.write_text('{"session":"' + self.session + '","controlFile":"' +
                                 str(external_file) + '"}', encoding="utf-8")
        external_file.write_text('{"user":"data"}', encoding="utf-8")
        self.runtime.mkdir()
        (self.runtime / "frame-workspace").symlink_to(external_runtime, target_is_directory=True)
        self._run("begin", check=True)

        failed = self._run("stop")

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("runtime directory is a symlink", failed.stderr)
        self.assertTrue(external_spec.exists())
        self.assertEqual(external_file.read_text(encoding="utf-8"), '{"user":"data"}')

    def test_unconfirmed_local_cleanup_is_reported_and_retryable(self):
        uid = os.getuid()
        host = f"frame-workspace-host-{uid}.service"
        self.units.write_text(json.dumps([host]), encoding="utf-8")
        self._run("begin", check=True)

        failed = self._run("stop", env={**self.env, "FRAME_TEST_STUCK": "1"})

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("still active or cannot be verified; retry Stop", failed.stderr)
        self.assertEqual(self._run("check").returncode, 1)
        retried = self._run("stop", check=True)
        self.assertIn("viewer session stopped", retried.stdout)
        self.assertEqual(json.loads(self.units.read_text()), [])

    def test_mac_restore_intent_write_failure_is_reported_even_when_no_viewers_exist(self):
        self._run("begin", check=True)
        invalid_request_dir = self.root / "not-a-directory"
        invalid_request_dir.write_text("file", encoding="utf-8")

        failed = self._run("exit", self.session, "--request-dir", str(invalid_request_dir))

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Mac restore request could not be saved", failed.stderr)
        self.assertFalse((self.root / "vrcmd.log").exists(), "leave the dashboard available to retry Mac restore")

        request_dir = self.runtime / "frame-workspace" / "requests"
        retried = self._run("exit", self.session, "--request-dir", str(request_dir), check=True)
        self.assertIn("viewer session stopped", retried.stdout)
        self.assertEqual(len(list(request_dir.glob("local-stop-*.json"))), 1)

    def test_mac_restore_request_does_not_follow_symlinked_runtime_request_dir(self):
        self._run("begin", check=True)
        runtime_requests = self.runtime / "frame-workspace" / "requests"
        runtime_requests.parent.mkdir(parents=True)
        outside_requests = self.root / "user-requests"
        outside_requests.mkdir()
        marker = outside_requests / "keep.json"
        marker.write_text('{"user":"data"}', encoding="utf-8")
        runtime_requests.symlink_to(outside_requests, target_is_directory=True)

        failed = self._run("exit", self.session, "--request-dir", str(runtime_requests))

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Mac restore request could not be saved", failed.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"user":"data"}')
        self.assertEqual(sorted(path.name for path in outside_requests.iterdir()), ["keep.json"])
        self.assertFalse((self.root / "vrcmd.log").exists(), "keep Stop available until restore intent can be saved")

        runtime_requests.unlink()
        retried = self._run("exit", self.session, "--request-dir", str(runtime_requests), check=True)
        self.assertIn("viewer session stopped", retried.stdout)
        self.assertEqual(len(list(runtime_requests.glob("local-stop-*.json"))), 1)

    def test_unavailable_service_inventory_does_not_skip_owned_browser_cleanup(self):
        self._run("begin", check=True)

        failed = self._run("stop", env={**self.env, "FRAME_TEST_LIST_FAIL": "1"})

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("could not be fully verified", failed.stderr)
        self.assertIn("-TERM", (self.root / "pkill.log").read_text(encoding="utf-8"))

    def test_dashboard_hide_failure_keeps_stop_retryable(self):
        self._run("begin", check=True)

        failed = self._run("stop", env={**self.env, "FRAME_TEST_HIDE_FAIL": "1"})

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("did not confirm hiding its dashboard", failed.stderr)
        retried = self._run("stop", check=True)
        self.assertIn("viewer session stopped", retried.stdout)
        self.assertEqual((self.root / "vrcmd.log").read_text(encoding="utf-8").splitlines(),
                         ["--hidedashboard", "--hidedashboard"])

    def test_guarded_start_and_retirement_share_a_lock(self):
        self._run("begin", check=True)
        marker = self.root / "started"
        command = [sys.executable, "-c", "import pathlib,time; time.sleep(.35); pathlib.Path(%r).write_text('started')" % str(marker)]
        start = subprocess.Popen([sys.executable, str(HELPER), "guarded-run", "--session", self.session,
                                  "--", *command], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            # Guarded launch owns the state lock until its short service-start command returns.
            time.sleep(.08)
            stop = subprocess.run([sys.executable, str(HELPER), "stop", "--session", self.session],
                                  env=self.env, capture_output=True, text=True, timeout=8)
            self.assertEqual(stop.returncode, 0, stop.stderr)
            out, err = start.communicate(timeout=2)
            self.assertEqual(start.returncode, 0, out + err)
            self.assertTrue(marker.exists())
            denied = self._run("guarded-run", self.session, "--", *command)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("cancelled by Stop", denied.stderr)
        finally:
            if start.poll() is None:
                start.kill()
                start.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
