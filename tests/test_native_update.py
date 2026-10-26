"""Headset viewer identity, the bundle that carries its sources, and Update
Headset Viewer end to end: the companion's real transport commands run the real
Frame-side updater locally, with fake systemctl/systemd-run and a fake build."""
import sandbox  # noqa: F401  (first: keep tests off real user data and services)
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
import frame_native_update  # noqa: E402
from frame_native_update import HeadsetViewer, ViewerError  # noqa: E402

identity_module = frame_native_update._identity_module(ROOT)

# Stands in for the real compiler on the "Frame": the viewer it "builds" reports
# the source identity, like the real build's -DFRAME_WORKSPACE_SOURCE_ID.
FAKE_BUILD = r'''#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
[ "$1" = -j2 ] && shift
out=$1
[ -z "${FAKE_BUILD_FAIL:-}" ] || { echo "fatal error: GL/gl.h: No such file" >&2; exit 1; }
identity=$(python3 "$repo/scripts/native_source_identity.py" "$repo")
mkdir -p "$out"
cat > "$out/frame-workspace-native" <<EOF
#!/bin/sh
case "\$1" in
  --capabilities) echo '{"name":"frame-workspace-native","protocol":2,"sourceIdentity":"$identity","capabilities":["host","roles","surfaces","wall-slots","idle-auxiliary","capabilities"]}' ;;
  --self-test) echo "native self-test passed" ;;
  *) exit 2 ;;
esac
EOF
printf '#!/bin/sh\nexit 0\n' > "$out/frame-workspace-dash"
chmod 755 "$out/frame-workspace-native" "$out/frame-workspace-dash"
printf '{"source_identity":"%s","source_revision":"%s"}\n' "$identity" "${FRAME_NATIVE_SOURCE_REVISION:-}" > "$out/BUILD-METADATA.json"
echo "native build verified"
'''


def electron_filter_matches(pattern, name):
    """electron-builder's file filters as this repo uses them."""
    if pattern.endswith("/**/*"):
        return name.startswith(pattern[:-4])
    if "/" not in pattern:
        return "/" not in name and re.fullmatch(re.escape(pattern).replace(r"\*", "[^/]*"), name) is not None
    folder, leaf = pattern.rsplit("/", 1)
    return (name.startswith(folder + "/") and "/" not in name[len(folder) + 1:] and
            re.fullmatch(re.escape(leaf).replace(r"\*", "[^/]*"), name[len(folder) + 1:]) is not None)


class Identity(unittest.TestCase):
    def test_companion_and_build_share_one_identity_over_the_build_inputs(self):
        files = identity_module.files(ROOT)
        for required in ("native/main.cpp", "native/dash.cpp", "native/gaze_bridge.cpp", "native/video.hpp",
                         "native/input/actions.json", "native/vendor/openvr/openvr_capi.h",
                         "scripts/build-workspace-native.sh", "scripts/native_source_identity.py"):
            self.assertIn(required, files)
        for excluded in ("native/README.md", "native/tests_host.py", "native/scene/example.json"):
            self.assertNotIn(excluded, files)
        self.assertFalse([f for f in files if f.startswith("native/evidence/")])
        expected = HeadsetViewer(lambda *a, **k: "", root=ROOT).expected()
        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        self.assertEqual(expected, identity_module.identity(ROOT))
        script = subprocess.run([sys.executable, str(ROOT / "scripts/native_source_identity.py"), str(ROOT)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(script.stdout.strip(), expected)
        build = (ROOT / "scripts/build-workspace-native.sh").read_text()
        self.assertIn('-DFRAME_WORKSPACE_SOURCE_ID="\\"$identity\\""', build)
        self.assertIn("native_source_identity.py", build)

    def test_any_source_change_changes_the_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp)
            for name in identity_module.files(ROOT):
                (copy / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, copy / name)
            before = identity_module.identity(copy)
            self.assertEqual(before, identity_module.identity(ROOT))
            (copy / "native/README.md").write_text("docs are not a build input")
            self.assertEqual(identity_module.identity(copy), before)
            with (copy / "native/spatial.hpp").open("a") as stream:
                stream.write("\n")
            self.assertNotEqual(identity_module.identity(copy), before)

    def test_the_app_bundle_carries_exactly_the_identity_files(self):
        config = json.loads((ROOT / "app/package.json").read_text())["build"]["extraResources"]
        native = next(r for r in config if r["from"] == "../native")
        scripts = next(r for r in config if r["from"] == "../scripts")
        self.assertEqual((native["to"], scripts["to"]), ("native", "scripts"))
        on_disk = [p.relative_to(ROOT / "native").as_posix() for p in (ROOT / "native").rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and "build" not in p.parts]
        bundled = {"native/" + n for n in on_disk if any(electron_filter_matches(f, n) for f in native["filter"])}
        identity = set(identity_module.files(ROOT))
        self.assertEqual(bundled, {f for f in identity if f.startswith("native/")})
        for name in [f for f in identity if f.startswith("scripts/")] + ["scripts/workspace-native-update-on-frame.py"]:
            self.assertTrue(any(electron_filter_matches(f, name[len("scripts/"):]) for f in scripts["filter"]), name)
        check = (ROOT / "app/build/check-resources.js").read_text()
        for name in ("scripts/build-workspace-native.sh", "scripts/native_source_identity.py",
                     "scripts/workspace-native-update-on-frame.py", "native/main.cpp", "native/dash.cpp"):
            self.assertIn(f'"{name}"', check)


class FakeFrame:
    """A Frame account: the companion's commands run here with bash, as over ssh."""

    def __init__(self, temp):
        self.root = Path(temp)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        for folder in (self.home, self.bin):
            folder.mkdir()
        self.data = self.home / ".local/share/frame-workspace"
        self.install = self.data / "native"
        self.install.mkdir(parents=True)
        self.units = self.root / "units"
        self.units.write_text("")
        self.calls = self.root / "calls"
        self.env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home),
                    "XDG_DATA_HOME": str(self.home / ".local/share"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "FRAME_TEST_UNITS": str(self.units), "FRAME_TEST_CALLS": str(self.calls), "LC_ALL": "C.UTF-8"}
        self._bin("systemctl", r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ['FRAME_TEST_CALLS'], 'a') as out: out.write('systemctl ' + ' '.join(args) + '\n')
if args[:2] == ['--user', 'list-units']:
    for name in open(os.environ['FRAME_TEST_UNITS']).read().split():
        print(name + ' loaded active running Frame Workspace')
    raise SystemExit(0)
if args[:2] == ['--user', 'is-active']:
    raise SystemExit(1)
raise SystemExit(0)
''')
        # A transient user unit: drop the options, run the command detached.
        self._bin("systemd-run", r'''#!/usr/bin/env bash
printf 'systemd-run %s\n' "$*" >> "$FRAME_TEST_CALLS"
while [ "$#" -gt 0 ] && [ "${1#--}" != "$1" ]; do shift; done
nohup "$@" >/dev/null 2>&1 &
''')
        # The viewer that ran on the Frame before: it doesn't know --capabilities.
        self.old_viewer = "#!/bin/sh\necho 'unknown option' >&2\nexit 1\n"
        (self.install / "frame-workspace-native").write_text(self.old_viewer)
        (self.install / "frame-workspace-dash").write_text("#!/bin/sh\nexit 0\n")
        for name in ("frame-workspace-native", "frame-workspace-dash"):
            (self.install / name).chmod(0o755)
        (self.install / "BUILD-METADATA.json").write_text('{"source_revision": "6ffa755"}\n')
        (self.install / "workspace-exit-on-frame.py").write_text("# installed by the companion\n")
        (self.data / "workspace-pose.json").write_text('{"version":1}\n')

    def _bin(self, name, text):
        (self.bin / name).write_text(text)
        (self.bin / name).chmod(0o755)

    def run(self, command, stdin=None, timeout=30):
        result = subprocess.run(["bash", "-c", command], input=stdin, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"exit {result.returncode}")
        return result.stdout


class UpdateHeadsetViewer(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="native-update-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        # The Mac app's resources, with the fake build in place of the compiler.
        self.app = base / "app"
        for name in identity_module.files(ROOT) + ["scripts/workspace-native-update-on-frame.py"]:
            (self.app / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.app / name)
        (self.app / "scripts/build-workspace-native.sh").write_text(FAKE_BUILD)
        (base / "frame").mkdir()
        self.frame = FakeFrame(base / "frame")
        self.viewer = HeadsetViewer(self.frame.run, root=self.app)
        self.expected = self.viewer.expected()

    def wait(self, timeout=30):
        deadline = time.monotonic() + timeout
        while self.viewer.updating() and time.monotonic() < deadline:
            time.sleep(.05)
        self.viewer.thread.join(timeout)
        self.assertFalse(self.viewer.thread.is_alive())
        return self.viewer.state()

    def update(self, idle=True):
        if not hasattr(self, "_fast"):
            orig = self.viewer._update
            self.viewer._update = lambda frame, payload, expected: orig(frame, payload, expected, poll=.05, limit=30)
            self._fast = True
        return self.viewer.start_update("frame", idle)

    def test_old_viewer_reads_as_too_old_for_spatial_and_spatial_is_refused(self):
        status = self.viewer.probe("frame")
        self.assertTrue(status["present"])
        self.assertIsNone(status["capabilities"])
        self.assertFalse(status["spatial"])
        self.assertEqual(self.viewer.state()["state"], "incompatible")
        self.assertTrue(self.viewer.state()["canUpdate"])
        with self.assertRaisesRegex(ViewerError, "too old for Spatial Mode"):
            self.viewer.require_spatial("frame")
        (self.frame.install / "frame-workspace-native").unlink()
        with self.assertRaisesRegex(ViewerError, "Install the native window host"):
            self.viewer.require_spatial("frame")
        self.assertEqual(self.viewer.state()["label"], "Not Installed")

    def test_connect_checks_once_per_frame_and_never_raises(self):
        calls = []
        run = self.viewer.run
        self.viewer.run = lambda *a, **k: (calls.append(a[0]), run(*a, **k))[1]
        self.assertIsNotNone(self.viewer.check("frame"))
        self.viewer.check("frame")
        self.assertEqual(len(calls), 1)
        self.viewer.check("frame-2")
        self.viewer.check("frame-2", force=True)
        self.assertEqual(len(calls), 3)
        self.viewer.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssh: connect timed out"))
        self.assertIsNone(self.viewer.check("frame-3"))
        self.assertEqual(self.viewer.state()["label"], "Couldn't Check", "another headset's answer isn't shown")

    def test_update_builds_tests_and_swaps_the_viewer_keeping_one_backup_and_state(self):
        result = self.update()
        self.assertEqual(result["state"], "updating")
        state = self.wait()
        self.assertEqual(state["update"]["state"], "installed", state)
        self.assertEqual(state["state"], "current")
        self.assertEqual(state["installed"], self.expected)
        caps = json.loads(subprocess.run([str(self.frame.install / "frame-workspace-native"), "--capabilities"],
                                         capture_output=True, text=True).stdout)
        self.assertEqual(caps["sourceIdentity"], self.expected)
        self.assertEqual(json.loads((self.frame.install / "BUILD-METADATA.json").read_text())["source_identity"],
                         self.expected)
        self.assertTrue((self.frame.install / "input/actions.json").exists())
        previous = self.frame.data / "native-previous"
        self.assertEqual((previous / "frame-workspace-native").read_text(), self.frame.old_viewer)
        self.assertIn("6ffa755", (previous / "BUILD-METADATA.json").read_text())
        # Poses, the Stop helper and other state are not part of the swap.
        self.assertEqual((self.frame.data / "workspace-pose.json").read_text(), '{"version":1}\n')
        self.assertEqual((self.frame.install / "workspace-exit-on-frame.py").read_text(), "# installed by the companion\n")
        self.assertFalse((self.frame.install / ".update-staged").exists())
        calls = self.frame.calls.read_text()
        self.assertIn("--unit=frame-workspace-native-update-%d.service" % os.getuid(), calls)
        self.assertIn("systemctl --user stop frame-workspace-dash-%d.service" % os.getuid(), calls)
        cache = self.frame.home / ".cache/frame-workspace/native-update"
        jobs = list(cache.glob("job-*"))
        self.assertEqual(len(jobs), 1)
        self.assertFalse((jobs[0] / "src").exists() or (jobs[0] / "out").exists(), "build files are removed")
        # A second update is a fresh build, and keeps one backup only.
        self.update()
        self.assertEqual(self.wait()["update"]["state"], "installed")
        self.assertEqual(json.loads(subprocess.run([str(previous / "frame-workspace-native"), "--capabilities"],
                                                   capture_output=True, text=True).stdout)["sourceIdentity"],
                         self.expected)
        self.assertEqual(len(list(cache.glob("job-*"))), 1)

    def test_build_failure_leaves_the_installed_viewer_and_reports_the_log(self):
        self.frame.env["FAKE_BUILD_FAIL"] = "1"
        self.update()
        state = self.wait()
        self.assertEqual(state["update"]["state"], "failed")
        self.assertIn("The build failed", state["update"]["message"])
        self.assertIn("GL/gl.h", state["update"]["message"])
        self.assertTrue(state["canUpdate"])
        self.assertEqual((self.frame.install / "frame-workspace-native").read_text(), self.frame.old_viewer)
        self.assertFalse((self.frame.data / "native-previous").exists())

    def test_running_workspace_blocks_the_update_on_both_sides(self):
        with self.assertRaisesRegex(ViewerError, "Stop the Workspace"):
            self.viewer.start_update("frame", workspace_idle=False)
        self.frame.units.write_text("frame-workspace-host-1000.service\n")
        with self.assertRaisesRegex(ViewerError, "Stop the Workspace"):
            self.viewer.start_update("frame", workspace_idle=True)
        self.assertFalse((self.frame.home / ".cache/frame-workspace/native-update/updater.py").exists())
        # The Frame checks again itself, even if asked directly.
        self.frame.units.write_text("")
        self.frame.run("set -e; base=\"$HOME/.cache/frame-workspace/native-update\"; mkdir -p \"$base\"; cat > \"$base/updater.py\"",
                       stdin=frame_native_update.UPDATER.read_text())
        self.frame.units.write_text("frame-workspace-viewer-1000.service\n")
        with self.assertRaisesRegex(RuntimeError, "Stop the Workspace"):
            self.frame.run(f'python3 "$HOME/.cache/frame-workspace/native-update/updater.py" start --identity {self.expected}')

    def test_received_sources_must_match_the_identity(self):
        self.frame.run("set -e; base=\"$HOME/.cache/frame-workspace/native-update\"; mkdir -p \"$base\"; cat > \"$base/updater.py\"",
                       stdin=frame_native_update.UPDATER.read_text())
        import base64
        payload = base64.b64encode(frame_native_update.source_archive(self.app)).decode()
        self.assertIn("NATIVE_SOURCES_RECEIVED",
                      self.frame.run('python3 "$HOME/.cache/frame-workspace/native-update/updater.py" receive', stdin=payload))
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            self.frame.run('python3 "$HOME/.cache/frame-workspace/native-update/updater.py" start --identity ' + "0" * 64)
        self.assertEqual((self.frame.install / "frame-workspace-native").read_text(), self.frame.old_viewer)


if __name__ == "__main__":
    unittest.main()
