"""Production launcher checks for the saved native pose override."""
import sandbox  # noqa: F401  (first: keep tests off real user data and services)
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "workspace-native-on-frame.sh"
VALID_POSE = {
    "version": 1,
    "width": 3.25,
    "distance": 4.5,
    "curvature": 0.75,
    "matrix": [1, 0, 0, 1.25, 0, 1, 0, 1.5, 0, 0, 1, -3.5],
}
VALID_RAW = json.dumps(VALID_POSE, separators=(",", ":"))
OLD_POSE = '{"version":1,"width":2.4,"distance":2,"curvature":0,"matrix":[1,0,0,0,0,1,0,1.3,0,0,1,-2]}'


class NativePoseLauncherTests(unittest.TestCase):
    def fixture(self, panel=False):
        context = tempfile.TemporaryDirectory(prefix=".native-pose-", dir=ROOT / "tests")
        root = Path(context.name)
        home = root / "home"
        data = root / "xdg-data"
        runtime = root / "xdg-runtime"
        bindir = root / "bin"
        bindir.mkdir()
        install = data / "frame-workspace" / "native"
        install.mkdir(parents=True)
        binary = install / "frame-workspace-native"
        binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        binary.chmod(0o755)

        if panel:
            pose_file = data / "frame-workspace" / "panels" / "p1-pose.json"
            prefix = ["--panel", "p1"]
        else:
            pose_file = data / "frame-workspace" / "workspace-pose.json"
            prefix = []
        pose_file.parent.mkdir(parents=True, exist_ok=True)
        pose_file.write_text(OLD_POSE, encoding="utf-8")
        pose_file.chmod(0o644)

        state = root / "service-state"
        state.write_text("loaded\nactive\n4321\n", encoding="utf-8")
        calls = root / "calls"
        events = root / "events"
        common_env = {
            **os.environ,
            "HOME": str(home),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(runtime),
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "TEST_STATE": str(state),
            "TEST_CALLS": str(calls),
            "TEST_EVENTS": str(events),
            "TEST_POSE_PATH": str(pose_file),
            "TEST_ROOT": str(root),
        }
        (bindir / "systemctl").write_text(
            """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$TEST_CALLS"
if [ "$2" = show ]; then
  { IFS= read -r load; IFS= read -r active; IFS= read -r pid; } < "$TEST_STATE"
  case "$4" in
    --property=LoadState) printf '%s\\n' "$load" ;;
    --property=ActiveState) printf '%s\\n' "$active" ;;
    --property=MainPID)
      printf '%s\\n' "$pid"
      if [ "$active" = inactive ] && [ "$pid" = 0 ]; then
        if [ ! -f "$TEST_ROOT/pose-at-confirm" ]; then
          cp "$TEST_POSE_PATH" "$TEST_ROOT/pose-at-confirm"
          printf 'confirmed\\n' >> "$TEST_EVENTS"
        fi
      fi
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [ "$2" = --no-block ] && [ "$3" = stop ]; then
  cp "$TEST_POSE_PATH" "$TEST_ROOT/pose-at-stop"
  printf 'stop\\n' >> "$TEST_EVENTS"
  printf 'loaded\\ninactive\\n0\\n' > "$TEST_STATE"
  exit 0
fi
exit 1
""",
            encoding="utf-8",
        )
        (bindir / "systemctl").chmod(0o755)
        (bindir / "systemd-run").write_text(
            """#!/bin/sh
printf 'systemd-run\\n' >> "$TEST_EVENTS"
[ -f "$TEST_ROOT/pose-at-confirm" ] || exit 31
cp "$TEST_POSE_PATH" "$TEST_ROOT/pose-at-systemd-run"
printf '%s\\n' "$@" > "$TEST_ROOT/systemd-run-args"
printf 'confirmed-at-run\\n' > "$TEST_ROOT/confirmed-at-run"
exit 0
""",
            encoding="utf-8",
        )
        (bindir / "systemd-run").chmod(0o755)
        return context, root, common_env, pose_file, runtime, prefix, calls, events

    def host_fixture(self):
        context = tempfile.TemporaryDirectory(prefix=".native-host-launcher-", dir=ROOT / "tests")
        root = Path(context.name)
        home = root / "home"
        data = root / "xdg-data"
        runtime = root / "xdg-runtime"
        bindir = root / "bin"
        bindir.mkdir()
        install = data / "frame-workspace" / "native"
        install.mkdir(parents=True)
        binary = install / "frame-workspace-native"
        binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        binary.chmod(0o755)
        status = root / "host-service"
        status.write_text("not-found\ninactive\n0\n", encoding="utf-8")
        legacy_status = root / "legacy-service"
        legacy_status.write_text("not-found\ninactive\n0\n", encoding="utf-8")
        calls = root / "calls"
        events = root / "events"
        run_count = root / "systemd-run-count"
        vrcmd = bindir / "vrcmd"
        vrcmd.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['XDG_RUNTIME_DIR']) / 'frame-workspace' / 'host' / 'panels'
for path in sorted(root.glob('*.json')):
    spec = json.loads(path.read_text())
    print("'%s' -- '%s', 800x600 visible VROverlayType_Overlay" %
          (spec['overlayKey'], spec['overlayName']))
""",
            encoding="utf-8",
        )
        vrcmd.chmod(0o755)
        (bindir / "systemctl").write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['TEST_CALLS'], 'a') as f: f.write('systemctl ' + ' '.join(args) + '\\n')
if len(args) >= 4 and args[0] == '--user' and args[1] == 'show':
    unit = args[2]
    status_path = pathlib.Path(os.environ['TEST_LEGACY_STATUS']) if unit.startswith('frame-workspace-panel-') else pathlib.Path(os.environ['TEST_HOST_STATUS'])
    status = status_path.read_text().splitlines()
    load, active, pid = status[0], status[1], int(status[2])
    prop = args[-2].split('=', 1)[1]
    runtime = pathlib.Path(os.environ['XDG_RUNTIME_DIR']) / 'frame-workspace'
    specs = runtime / 'host' / 'panels'
    if unit.startswith('frame-workspace-panel-') and active == 'inactive' and prop == 'ActiveState':
        with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('legacy-confirm\\n')
    if unit.startswith('frame-workspace-host-') and os.environ.get('TEST_HOST_SYNC') != '0':
        for panel_runtime in runtime.glob('panel-*'):
            state_path = panel_runtime / 'state.json'
            panel = panel_runtime.name.removeprefix('panel-')
            spec_path = specs / (panel + '.json')
            previous = {}
            try: previous = json.loads(state_path.read_text())
            except (OSError, ValueError): pass
            if active == 'active' and spec_path.exists():
                spec = json.loads(spec_path.read_text())
                control_present = (panel_runtime / 'control.json').exists()
                with open(os.environ['TEST_EVENTS'], 'a') as f:
                    f.write('spec:' + spec['generation'] + ':control:' + str(control_present) + '\\n')
                if os.environ.get('TEST_HOST_ERROR') == '1':
                    state = {'ready': False, 'state': 'error', 'hostGeneration': spec['generation'],
                             'hostPid': pid, 'overlayKey': spec['overlayKey']}
                else:
                    state = {'ready': True, 'state': 'streaming', 'hostGeneration': spec['generation'],
                             'hostPid': pid, 'overlayKey': spec['overlayKey'],
                             'textureWidth': 800, 'textureHeight': 600,
                             'placement': {'width': 2.4, 'distance': 2.0}}
                control_path = panel_runtime / 'control.json'
                if control_path.exists():
                    try:
                        control = json.loads(control_path.read_text())
                        state['placement'] = {'width': control.get('width', 2.4),
                                              'distance': control.get('distance', 2.0)}
                        if 'revision' in control:
                            state['controlRevision'] = control['revision']
                            if 'curvature' in control:
                                state['controlResultRevision'] = control['revision']
                                state['curvatureResult'] = {'applied': True, 'supported': True,
                                    'requested': control['curvature'], 'value': control['curvature'], 'errorCode': 0}
                    except (OSError, ValueError): pass
                state_path.write_text(json.dumps(state))
            elif active == 'active' and previous.get('hostGeneration'):
                mismatch_mode = os.environ.get('TEST_STOP_ACK_MODE') == 'mismatch-once'
                if not mismatch_mode or prop == 'LoadState':
                    ack_pid, ack_key = pid, previous.get('overlayKey')
                    if mismatch_mode:
                        attempts = panel_runtime / 'ack-attempts'
                        if not attempts.exists():
                            attempts.write_text('1')
                            ack_pid, ack_key = pid + 1, 'wrong-overlay-key'
                            with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('stop-ack-mismatch\\n')
                        else:
                            ack_key = 'com.frame-workspace.native.%d.panel-%s' % (os.getuid(), panel)
                            with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('stop-ack-matched\\n')
                    state_path.write_text(json.dumps({'ready': False, 'state': 'stopped',
                        'hostGeneration': previous['hostGeneration'], 'hostPid': ack_pid,
                        'overlayKey': ack_key}))
                pose = pathlib.Path(os.environ['XDG_DATA_HOME']) / 'frame-workspace' / 'panels' / (panel + '-pose.json')
                value = pose.read_text() if pose.exists() else '<missing>'
                with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('stopped:' + value + '\\n')
                urls = list(panel_runtime.glob('view-*.url'))
                with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('stop-url-present:' + str(bool(urls)) + '\\n')
            elif active == 'active' and not state_path.exists() and not spec_path.exists():
                urls = list(panel_runtime.glob('view-fc????????.url'))
                if len(urls) == 1:
                    generation = urls[0].stem.removeprefix('view-')
                    key = 'com.frame-workspace.native.%d.panel-%s' % (os.getuid(), panel)
                    state_path.write_text(json.dumps({'ready': False, 'state': 'stopped',
                        'hostGeneration': generation, 'hostPid': pid, 'overlayKey': key}))
                    with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('url-only-ack\\n')
    print({'ActiveState': active, 'MainPID': str(pid),
           'LoadState': load}.get(prop, ''))
    sys.exit(1 if prop == 'LoadState' and load == 'not-found' else 0)
if len(args) >= 4 and args[0] == '--user' and args[1] == '--no-block' and args[2] == 'stop':
    unit = args[3]
    if unit.startswith('frame-workspace-panel-'):
        pathlib.Path(os.environ['TEST_LEGACY_STATUS']).write_text('loaded\\ninactive\\n0\\n')
        with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('legacy-stop\\n')
        sys.exit(0)
    sys.exit(1)
if len(args) >= 2 and args[0] == '--user' and args[1] == 'reset-failed': sys.exit(0)
sys.exit(0)
""",
            encoding="utf-8",
        )
        (bindir / "systemctl").chmod(0o755)
        (bindir / "systemd-run").write_text(
            """#!/usr/bin/env python3
import os, pathlib, sys
count = pathlib.Path(os.environ['TEST_RUN_COUNT'])
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
pathlib.Path(os.environ['TEST_HOST_STATUS']).write_text('loaded\\nactive\\n4321\\n')
pathlib.Path(os.environ['TEST_RUN_ARGS']).write_text('\\n'.join(sys.argv[1:]))
with open(os.environ['TEST_EVENTS'], 'a') as f: f.write('host-start\\n')
""",
            encoding="utf-8",
        )
        (bindir / "systemd-run").chmod(0o755)
        common_env = {
            **os.environ,
            "HOME": str(home),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(runtime),
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "FRAME_NATIVE_HOST": "1",
            "FRAME_NATIVE_VRCMD": str(vrcmd),
            "TEST_HOST_STATUS": str(status),
            "TEST_LEGACY_STATUS": str(legacy_status),
            "TEST_CALLS": str(calls),
            "TEST_EVENTS": str(events),
            "TEST_RUN_COUNT": str(run_count),
            "TEST_RUN_ARGS": str(root / "systemd-run-args"),
            "TEST_HOST_ERROR": "0",
        }
        return context, root, common_env, status, runtime, calls, events, run_count

    def start_args(self, prefix, *options):
        return [*prefix, *options, "start", "http://127.0.0.1:48888/view?ticket=local", "800", "600",
                "fc12345678", "software", "cpu", "1"]

    def run_launcher(self, env, args, timeout=10):
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, text=True,
                              capture_output=True, timeout=timeout)

    def host_start_args(self, panel, tag, *options):
        return ["--panel", panel, "--name", "Window " + panel, *options, "start",
                "http://127.0.0.1:48888/view?ticket=" + panel, "800", "600", tag,
                "v4l2", "gpu", "1"]

    def test_pose_override_replaces_workspace_and_panel_only_after_stop(self):
        for panel in (False, True):
            with self.subTest(panel=panel):
                context, root, env, pose_file, runtime, prefix, calls, events = self.fixture(panel)
                try:
                    run = self.run_launcher(env, self.start_args(prefix, "--pose", VALID_RAW))
                    # systemd-run is stubbed, so startup correctly observes an
                    # exited viewer before it can call the fixed Frame vrcmd.
                    self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                    self.assertIn("exited during startup", run.stderr)
                    self.assertEqual((root / "pose-at-stop").read_text(encoding="utf-8"), OLD_POSE)
                    self.assertEqual((root / "pose-at-confirm").read_text(encoding="utf-8"), OLD_POSE)
                    self.assertEqual((root / "pose-at-systemd-run").read_text(encoding="utf-8"), VALID_RAW)
                    self.assertEqual(pose_file.read_text(encoding="utf-8"), VALID_RAW)
                    self.assertEqual(pose_file.stat().st_mode & 0o777, 0o600)
                    self.assertTrue((root / "confirmed-at-run").exists())
                    ordered = events.read_text(encoding="utf-8").splitlines()
                    self.assertLess(ordered.index("stop"), ordered.index("confirmed"))
                    self.assertLess(ordered.index("confirmed"), ordered.index("systemd-run"))
                    self.assertTrue((runtime / "frame-workspace").is_dir())
                    self.assertTrue(calls.exists())
                finally:
                    context.cleanup()

    def test_pose_override_keeps_a_bend(self):
        context, root, env, pose_file, _, prefix, _, _ = self.fixture()
        try:
            raw = json.dumps({**VALID_POSE, "bend": {"vertical": True, "a": 0.0, "b": 0.35}}, separators=(",", ":"))
            run = self.run_launcher(env, self.start_args(prefix, "--pose", raw))
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertEqual(pose_file.read_text(encoding="utf-8"), raw)
        finally:
            context.cleanup()

    def test_without_override_preserves_existing_workspace_and_panel_pose(self):
        for panel in (False, True):
            with self.subTest(panel=panel):
                context, root, env, pose_file, _, prefix, _, _ = self.fixture(panel)
                try:
                    initial = json.dumps(VALID_POSE, separators=(",", ":"))
                    run = self.run_launcher(env, self.start_args(prefix, "--initial-pose", initial))
                    self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                    self.assertTrue((root / "confirmed-at-run").exists())
                    self.assertEqual((root / "pose-at-systemd-run").read_text(encoding="utf-8"), OLD_POSE)
                    self.assertEqual(pose_file.read_text(encoding="utf-8"), OLD_POSE)
                    self.assertEqual(pose_file.stat().st_mode & 0o777, 0o644)
                    args = (root / "systemd-run-args").read_text(encoding="utf-8").splitlines()
                    self.assertEqual(args[args.index("--initial-pose") + 1], initial)
                    if panel:
                        self.assertIn("--unit=frame-workspace-panel-%d-p1.service" % os.getuid(), args)
                        self.assertNotIn("--host-dir", args)
                finally:
                    context.cleanup()

    def test_opt_in_host_specs_reuse_one_host_and_stop_only_requested_panel(self):
        context, root, env, status, runtime, calls, events, run_count = self.host_fixture()
        try:
            initial = json.dumps(VALID_POSE, separators=(",", ":"))
            first = self.run_launcher(env, self.host_start_args("p1", "fc11111111", "--initial-pose", initial))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            host = runtime / "frame-workspace" / "host"
            specs = host / "panels"
            spec_path = specs / "p1.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            self.assertEqual(set(spec), {"version", "generation", "urlFile", "overlayKey", "overlayName",
                                        "controlFile", "stateFile", "poseFile", "initialPose", "requestDir",
                                        "decoder", "conversion", "foveation", "gaze"})
            self.assertEqual(spec["version"], 1)
            self.assertEqual(spec["generation"], "fc11111111")
            self.assertEqual(spec["initialPose"], initial)
            self.assertEqual(spec["decoder"], "v4l2")
            self.assertEqual(spec["conversion"], "gpu")
            self.assertIs(spec["foveation"], True)
            self.assertIs(spec["gaze"], False)
            self.assertEqual(spec["overlayKey"], "com.frame-workspace.native.%d.panel-p1" % os.getuid())
            self.assertEqual(Path(spec["urlFile"]).parent, runtime / "frame-workspace" / "panel-p1")
            self.assertTrue(Path(spec["urlFile"]).is_file())
            self.assertEqual(spec["stateFile"], str(runtime / "frame-workspace" / "panel-p1" / "state.json"))
            self.assertEqual(spec["controlFile"], str(runtime / "frame-workspace" / "panel-p1" / "control.json"))
            self.assertEqual(spec["poseFile"], str(env["XDG_DATA_HOME"] + "/frame-workspace/panels/p1-pose.json"))
            self.assertEqual(spec["requestDir"], str(runtime / "frame-workspace" / "requests"))
            self.assertEqual(spec_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((host / "launcher.lock").stat().st_mode & 0o777, 0o600)
            args = (root / "systemd-run-args").read_text(encoding="utf-8").splitlines()
            self.assertIn("--unit=frame-workspace-host-%d.service" % os.getuid(), args)
            self.assertEqual(args[args.index("--host-dir") + 1], str(specs))
            self.assertEqual(args[args.index("--state-file") + 1], str(host / "state.json"))

            second = self.run_launcher(env, self.host_start_args("p2", "fc22222222"))
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertTrue((specs / "p1.json").exists())
            self.assertTrue((specs / "p2.json").exists())
            self.assertEqual(run_count.read_text(encoding="utf-8"), "1")

            stopped = self.run_launcher(env, ["--panel", "p1", "--generation", "fc11111111", "stop"])
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            self.assertFalse((specs / "p1.json").exists())
            self.assertFalse((runtime / "frame-workspace" / "panel-p1").exists())
            self.assertTrue((specs / "p2.json").exists())
            self.assertEqual(status.read_text(encoding="utf-8"), "loaded\nactive\n4321\n")
            self.assertEqual(run_count.read_text(encoding="utf-8"), "1")
            self.assertNotIn("systemctl --user stop", calls.read_text(encoding="utf-8"))
            self.assertIn("stop-url-present:True", events.read_text(encoding="utf-8"))
        finally:
            context.cleanup()

    def test_first_host_start_handles_loadstate_not_found_with_nonzero_systemctl_result(self):
        context, _, env, status, runtime, calls, _, run_count = self.host_fixture()
        try:
            run = self.run_launcher(env, self.host_start_args("p1", "fc10101010"))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(run_count.read_text(encoding="utf-8"), "1")
            self.assertEqual(status.read_text(encoding="utf-8"), "loaded\nactive\n4321\n")
            self.assertIn("--property=LoadState --value", calls.read_text(encoding="utf-8"))
            self.assertTrue((runtime / "frame-workspace" / "host" / "panels" / "p1.json").exists())
        finally:
            context.cleanup()

    def test_normal_host_replacement_confirms_old_generation_and_clears_its_control(self):
        context, _, env, _, runtime, _, events, run_count = self.host_fixture()
        try:
            first = self.run_launcher(env, self.host_start_args("p1", "fc12121212"))
            sibling = self.run_launcher(env, self.host_start_args("p2", "fc13131313"))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(sibling.returncode, 0, sibling.stdout + sibling.stderr)
            panel_runtime = runtime / "frame-workspace" / "panel-p1"
            stale_control = panel_runtime / "control.json"
            stale_control.write_text('{"revision":1,"move":[0.1,0,0]}', encoding="utf-8")

            replacement = self.run_launcher(env, self.host_start_args("p1", "fc14141414"))
            self.assertEqual(replacement.returncode, 0, replacement.stdout + replacement.stderr)
            observed = events.read_text(encoding="utf-8").splitlines()
            self.assertIn("stop-url-present:True", observed)
            self.assertIn("spec:fc14141414:control:False", observed)
            specs = runtime / "frame-workspace" / "host" / "panels"
            self.assertEqual(json.loads((specs / "p1.json").read_text())['generation'], "fc14141414")
            self.assertEqual(json.loads((specs / "p2.json").read_text())['generation'], "fc13131313")
            self.assertFalse(stale_control.exists())
            self.assertEqual(run_count.read_text(encoding="utf-8"), "1")
        finally:
            context.cleanup()

    def test_host_migration_stops_and_confirms_legacy_panel_unit_before_claim(self):
        context, _, env, _, runtime, calls, events, _ = self.host_fixture()
        try:
            legacy_runtime = runtime / "frame-workspace" / "panel-p1"
            legacy_runtime.mkdir(parents=True)
            (legacy_runtime / "state.json").write_text(
                json.dumps({"ready": True, "state": "streaming", "overlayKey":
                            "com.frame-workspace.native.%d.panel-p1" % os.getuid()}), encoding="utf-8")
            (legacy_runtime / "control.json").write_text('{"revision":5}', encoding="utf-8")
            Path(env["TEST_LEGACY_STATUS"]).write_text("loaded\nactive\n7777\n", encoding="utf-8")

            run = self.run_launcher(env, self.host_start_args("p1", "fc15151515", "--pose", VALID_RAW))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            observed = events.read_text(encoding="utf-8").splitlines()
            self.assertLess(observed.index("legacy-stop"), observed.index("legacy-confirm"))
            self.assertLess(observed.index("legacy-confirm"), observed.index("host-start"))
            self.assertIn("--no-block stop frame-workspace-panel-%d-p1.service" % os.getuid(),
                          calls.read_text(encoding="utf-8"))
            self.assertIn("spec:fc15151515:control:False", observed)
            self.assertEqual((Path(env["XDG_DATA_HOME"]) / "frame-workspace" / "panels" / "p1-pose.json").read_text(), VALID_RAW)
        finally:
            context.cleanup()

    def test_host_migration_requires_zero_pid_even_for_inactive_legacy_unit(self):
        context, _, env, _, runtime, calls, _, run_count = self.host_fixture()
        try:
            Path(env["TEST_LEGACY_STATUS"]).write_text("loaded\ninactive\n7777\n", encoding="utf-8")
            pose = Path(env["XDG_DATA_HOME"]) / "frame-workspace" / "panels" / "p1-pose.json"
            pose.parent.mkdir(parents=True)
            pose.write_text(OLD_POSE, encoding="utf-8")
            run = self.run_launcher(env, self.host_start_args("p1", "fc20202020", "--pose", VALID_RAW))
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("no confirmed process state", run.stderr)
            self.assertEqual(pose.read_text(encoding="utf-8"), OLD_POSE)
            self.assertFalse(run_count.exists())
            self.assertFalse((runtime / "frame-workspace" / "host" / "panels" / "p1.json").exists())
            self.assertIn("frame-workspace-panel-%d-p1.service --property=MainPID" % os.getuid(),
                          calls.read_text(encoding="utf-8"))
        finally:
            context.cleanup()

    def test_host_panel_controls_use_shared_owner_and_keep_panel_runtime_paths(self):
        context, _, env, _, runtime, calls, _, _ = self.host_fixture()
        try:
            started = self.run_launcher(env, self.host_start_args("p1", "fc16161616"))
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            calls_before_controls = calls.read_text(encoding="utf-8")
            panel_runtime = runtime / "frame-workspace" / "panel-p1"
            state_path = panel_runtime / "state.json"
            state = json.loads(state_path.read_text())
            state["placement"] = {"width": 2.4, "distance": 2.0}
            state_path.write_text(json.dumps(state), encoding="utf-8")

            env["TEST_HOST_SYNC"] = "0"
            state["hostPid"] = 9999
            state_path.write_text(json.dumps(state), encoding="utf-8")
            rejected = self.run_launcher(env, ["--panel", "p1", "focus"])
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((panel_runtime / "control.json").exists())

            env["TEST_HOST_SYNC"] = "1"
            for args in (["--panel", "p1", "focus"],
                         ["--panel", "p1", "adjust", "3.0", "4.0"],
                         ["--panel", "p1", "pose", "0.1", "0", "0", "1", "0", "0"],
                         ["--panel", "p1", "curvature", "0.5"]):
                result = self.run_launcher(env, list(args))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            log = calls.read_text(encoding="utf-8")[len(calls_before_controls):]
            self.assertIn("frame-workspace-host-%d.service --property=MainPID" % os.getuid(), log)
            self.assertNotIn("frame-workspace-panel-%d-p1.service --property" % os.getuid(), log)
            self.assertEqual(json.loads((panel_runtime / "state.json").read_text())["hostPid"], 4321)
            spec = json.loads((runtime / "frame-workspace" / "host" / "panels" / "p1.json").read_text())
            self.assertEqual(spec["controlFile"], str(panel_runtime / "control.json"))
            self.assertEqual(spec["stateFile"], str(panel_runtime / "state.json"))
            self.assertEqual(json.loads((panel_runtime / "control.json").read_text())["curvature"], 0.5)
        finally:
            context.cleanup()

    def test_host_stop_waits_past_terminal_state_with_wrong_pid_and_overlay(self):
        context, _, env, _, runtime, _, events, _ = self.host_fixture()
        try:
            started = self.run_launcher(env, self.host_start_args("p1", "fc17171717"))
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            env["TEST_STOP_ACK_MODE"] = "mismatch-once"
            stopped = self.run_launcher(env, ["--panel", "p1", "--generation", "fc17171717", "stop"], timeout=20)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            observed = events.read_text(encoding="utf-8").splitlines()
            self.assertIn("stop-ack-mismatch", observed)
            self.assertIn("stop-ack-matched", observed)
            self.assertFalse((runtime / "frame-workspace" / "panel-p1").exists())
            self.assertEqual((runtime / "frame-workspace" / "host" / "panels" / "p1.json").exists(), False)
        finally:
            context.cleanup()

    def test_host_stop_with_removed_spec_and_missing_state_waits_for_url_generation_ack(self):
        context, _, env, _, runtime, _, events, _ = self.host_fixture()
        try:
            started = self.run_launcher(env, self.host_start_args("p1", "fc18181818"))
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            panel_runtime = runtime / "frame-workspace" / "panel-p1"
            spec_path = runtime / "frame-workspace" / "host" / "panels" / "p1.json"
            self.assertTrue((panel_runtime / "view-fc18181818.url").exists())
            spec_path.unlink()
            (panel_runtime / "state.json").unlink()

            stopped = self.run_launcher(env, ["--panel", "p1", "--generation", "fc18181818", "stop"])
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            self.assertIn("url-only-ack", events.read_text(encoding="utf-8"))
            self.assertFalse(panel_runtime.exists())
        finally:
            context.cleanup()

    def test_host_route_rejects_diagnostic_gaze_before_mutations(self):
        context, _, env, _, runtime, calls, _, run_count = self.host_fixture()
        try:
            args = self.host_start_args("p1", "fc19191919") + ["scan"]
            run = self.run_launcher(env, args)
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertIn("Diagnostic gaze is not supported", run.stderr)
            self.assertFalse(calls.exists())
            self.assertFalse(run_count.exists())
            self.assertFalse((runtime / "frame-workspace").exists())
        finally:
            context.cleanup()

    def test_host_opt_in_leaves_workspace_on_its_existing_separate_unit(self):
        context, root, env, _, _, prefix, _, _ = self.fixture()
        try:
            env["FRAME_NATIVE_HOST"] = "1"
            run = self.run_launcher(env, self.start_args(prefix))
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertIn("exited during startup", run.stderr)
            args = (root / "systemd-run-args").read_text(encoding="utf-8").splitlines()
            self.assertIn("--unit=frame-workspace-viewer-%d.service" % os.getuid(), args)
            self.assertNotIn("--host-dir", args)
            self.assertFalse((Path(env["XDG_RUNTIME_DIR"]) / "frame-workspace" / "host").exists())
        finally:
            context.cleanup()

    def test_host_pose_override_waits_for_old_generation_and_error_cleans_only_its_panel(self):
        context, root, env, _, runtime, _, events, run_count = self.host_fixture()
        try:
            pose_path = Path(env["XDG_DATA_HOME"]) / "frame-workspace" / "panels" / "p1-pose.json"
            pose_path.parent.mkdir(parents=True)
            pose_path.write_text(OLD_POSE, encoding="utf-8")
            pose_path.chmod(0o644)
            first = self.run_launcher(env, self.host_start_args("p1", "fc33333333"))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            changed = self.run_launcher(env, self.host_start_args("p1", "fc44444444", "--pose", VALID_RAW))
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            stop_events = [line for line in events.read_text(encoding="utf-8").splitlines()
                           if line.startswith("stopped:")]
            self.assertIn("stopped:" + OLD_POSE, stop_events[0])
            self.assertIn("stop-url-present:True", events.read_text(encoding="utf-8"))
            self.assertEqual(pose_path.read_text(encoding="utf-8"), VALID_RAW)
            self.assertEqual(pose_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((runtime / "frame-workspace" / "host" / "panels" / "p1.json").read_text())["generation"],
                             "fc44444444")

            env["TEST_HOST_ERROR"] = "1"
            failed = self.run_launcher(env, self.host_start_args("p2", "fc55555555"))
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertIn("reported an error", failed.stderr)
            self.assertFalse((runtime / "frame-workspace" / "host" / "panels" / "p2.json").exists())
            self.assertFalse((runtime / "frame-workspace" / "panel-p2").exists())
            self.assertTrue((runtime / "frame-workspace" / "host" / "panels" / "p1.json").exists())
            self.assertEqual(run_count.read_text(encoding="utf-8"), "1")
        finally:
            context.cleanup()

    def test_invalid_host_pose_is_rejected_before_host_or_runtime_mutation(self):
        context, root, env, _, runtime, calls, _, run_count = self.host_fixture()
        try:
            invalid = json.dumps({**VALID_POSE, "width": 0.1}, separators=(",", ":"))
            run = self.run_launcher(env, self.host_start_args("p1", "fc66666666", "--pose", invalid))
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertFalse(calls.exists(), "invalid pose must not query the shared host service")
            self.assertFalse(run_count.exists(), "invalid pose must not start the shared host")
            self.assertFalse((runtime / "frame-workspace").exists(), "invalid pose must not create host or panel runtime")
            self.assertFalse((Path(env["XDG_DATA_HOME"]) / "frame-workspace" / "panels" / "p1-pose.json").exists())
        finally:
            context.cleanup()

    def test_invalid_pose_is_rejected_before_service_or_runtime_mutation(self):
        cases = []
        def changed(**updates):
            pose = {**VALID_POSE, **updates}
            return json.dumps(pose, separators=(",", ":"))
        cases.extend([
            ("empty", ""),
            ("malformed", "{"),
            ("duplicate key", VALID_RAW[:-1] + ',"width":2}'),
            ("unknown key", changed(extra=1)),
            ("missing key", json.dumps({k: v for k, v in VALID_POSE.items() if k != "curvature"})),
            ("version boolean", changed(version=True)),
            ("version float", changed(version=1.0)),
            ("width boolean", changed(width=True)),
            ("width low", changed(width=0.399)),
            ("distance high", changed(distance=10.001)),
            ("curvature high", changed(curvature=1.001)),
            ("non-finite constant", changed(width=float("nan"))),
            ("overflow number", changed(distance=1e999)),
            ("short matrix", changed(matrix=VALID_POSE["matrix"][:-1])),
            ("matrix boolean", changed(matrix=[True, *VALID_POSE["matrix"][1:]])),
            ("matrix magnitude", changed(matrix=[101, *VALID_POSE["matrix"][1:]])),
            ("matrix not orthonormal", changed(matrix=[2, *VALID_POSE["matrix"][1:]])),
            ("left handed", changed(matrix=[1, 0, 0, 1.25, 0, 1, 0, 1.5, 0, 0, -1, -3.5])),
            ("oversized", (" " * 4097) + VALID_RAW),
            ("bend not an object", changed(bend=1)),
            ("bend missing a half", changed(bend={"vertical": False, "a": 0.2})),
            ("bend extra field", changed(bend={"vertical": False, "a": 0.2, "b": 0.1, "c": 0})),
            ("bend vertical not boolean", changed(bend={"vertical": 1, "a": 0.2, "b": 0.1})),
            ("bend half out of range", changed(bend={"vertical": True, "a": 1.2, "b": 0.1})),
        ])
        for label, raw in cases:
            with self.subTest(case=label):
                context, root, env, pose_file, runtime, prefix, calls, _ = self.fixture()
                try:
                    before = (pose_file.read_bytes(), pose_file.stat().st_mode & 0o777)
                    run = self.run_launcher(env, self.start_args(prefix, "--pose", raw))
                    self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                    self.assertFalse(calls.exists(), "invalid input must not query or stop systemd")
                    self.assertFalse((runtime / "frame-workspace").exists(), "invalid input must not create runtime files")
                    self.assertEqual((pose_file.read_bytes(), pose_file.stat().st_mode & 0o777), before)
                finally:
                    context.cleanup()

    def test_pose_rejects_non_start_operation_and_initial_pose_conflict(self):
        for options, operation in [
            (("--pose", VALID_RAW), "stop"),
            (("--initial-pose", VALID_RAW, "--pose", VALID_RAW), "start"),
        ]:
            with self.subTest(options=options[:2], operation=operation):
                context, root, env, pose_file, runtime, prefix, calls, _ = self.fixture()
                try:
                    args = [*prefix, *options, operation]
                    if operation == "start":
                        args += ["http://127.0.0.1:48888/view?ticket=local", "800", "600", "fc12345678"]
                    run = self.run_launcher(env, args)
                    self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                    self.assertFalse(calls.exists())
                    self.assertFalse((runtime / "frame-workspace").exists())
                    self.assertEqual(pose_file.read_text(encoding="utf-8"), OLD_POSE)
                    self.assertEqual(pose_file.stat().st_mode & 0o777, 0o644)
                    self.assertFalse((root / "pose-at-stop").exists())
                finally:
                    context.cleanup()


if __name__ == "__main__":
    unittest.main()
