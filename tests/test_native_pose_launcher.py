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

    def start_args(self, prefix, *options):
        return [*prefix, *options, "start", "http://127.0.0.1:48888/view?ticket=local", "800", "600",
                "fc12345678", "software", "cpu", "1"]

    def run_launcher(self, env, args):
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, text=True,
                              capture_output=True, timeout=10)

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
