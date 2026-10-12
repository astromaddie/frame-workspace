import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
import frame_connect  # noqa: E402
import frame_devices  # noqa: E402
import frame_host  # noqa: E402


def inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def snapshot(folder):
    """{relative path: (size, mtime)} for everything under FOLDER."""
    out = {}
    for base, dirs, files in os.walk(folder):
        for name in dirs + files:
            p = os.path.join(base, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, folder)] = (st.st_size, st.st_mtime_ns)
    return out


class Sandbox(unittest.TestCase):
    """Tests once wrote a host-key pin into the real ~/.ssh/frame-control-hosts on every
    run (a server started without FRAME_CONTROL_SSH_DIR imported the real ~/.ssh/config)."""

    def test_home_and_ssh_dir_are_throwaway(self):
        tmp = tempfile.gettempdir()
        self.assertTrue(inside(Path.home(), tmp), Path.home())
        self.assertTrue(inside(os.path.expanduser("~"), tmp))
        for d in (frame_devices.ssh_dir(), frame_devices.pin_dir(), frame_connect.SSH_DIR,
                  os.environ["FRAME_CONTROL_SSH_DIR"], frame_host.cache_dir(), frame_host.data_dir()):
            self.assertTrue(inside(d, tmp), d)
            self.assertFalse(inside(d, sandbox.REAL_SSH_DIR), d)

    def test_cache_folders_on_every_platform_are_throwaway(self):
        """cache_dir() reads LOCALAPPDATA on Windows and XDG_CACHE_HOME on Linux, which a
        moved HOME alone doesn't cover: servers sweep staging folders there."""
        tmp = tempfile.gettempdir()
        for platform in ("MAC", "WINDOWS", None):
            with mock.patch.object(frame_host, "MAC", platform == "MAC"), \
                    mock.patch.object(frame_host, "WINDOWS", platform == "WINDOWS"):
                self.assertTrue(inside(frame_host.cache_dir("apk-sources"), tmp), platform)
        for name in ("LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            self.assertTrue(inside(os.environ[name], tmp), name)

    def test_end_to_end_tests_stay_out_of_it(self):
        """tests/e2e keeps the container's real home (OpenSSH reads ~/.ssh from the password
        database, not $HOME), so nothing there may import sandbox, even through a test_* module."""
        here = Path(__file__).resolve().parent
        pattern = re.compile(r"^\s*(import sandbox|from sandbox |import test_|from test_)", re.M)
        for f in [*(here / "e2e").glob("*.py"), *(here / "smoke").glob("*.py"), here / "apk_fixtures.py"]:
            self.assertIsNone(pattern.search(f.read_text(encoding="utf-8")), f)

    def test_end_to_end_tests_start_no_server_outside_their_container(self):
        """Plain discovery over tests/e2e, with or without FRAME_E2E=1, must skip everything
        and never start ui/server.py: outside scripts/e2e.sh's container it would run on
        this computer's real app data, ~/.ssh and cache."""
        e2e = str(Path(__file__).resolve().parent / "e2e")
        code = ("import io, sys, unittest; sys.path.insert(0, sys.argv[1]); import harness; calls = []\n"
                "harness.Server.start = classmethod(lambda cls: calls.append(cls))\n"
                "suite = unittest.defaultTestLoader.discover(sys.argv[1], top_level_dir=sys.argv[1])\n"
                "r = unittest.TextTestRunner(stream=io.StringIO()).run(suite)\n"
                "print(len(calls), r.testsRun, len(r.skipped), len(r.errors) + len(r.failures))")
        for extra in ({}, {"FRAME_E2E": "1"}):
            env = {k: v for k, v in os.environ.items() if not k.startswith("FRAME_E2E")}
            env.update(extra, PYTHONDONTWRITEBYTECODE="1")
            out = subprocess.run([sys.executable, "-c", code, e2e], env=env, capture_output=True, text=True,
                                 timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            started, ran, skipped, broken = map(int, out.stdout.split())
            self.assertEqual((started, broken), (0, 0), (extra, out.stdout))
            self.assertEqual(ran, skipped, (extra, out.stdout))
            self.assertGreater(skipped, 0, extra)

    def test_child_processes_inherit_it(self):
        out = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import frame_devices, frame_connect;"
             " from pathlib import Path; print(Path.home()); print(frame_devices.ssh_dir()); print(frame_connect.SSH_DIR)",
             str(ROOT / "ui")], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, check=True).stdout.split("\n")
        for d in filter(None, out):
            self.assertFalse(inside(d, sandbox.REAL_SSH_DIR), d)
            self.assertTrue(inside(d, tempfile.gettempdir()), d)

    def test_real_ssh_folder_untouched_by_tests_that_start_servers(self):
        """Reruns, from the environment the suite was started in, the tests that used to
        leak (servers started without FRAME_CONTROL_SSH_DIR) plus the device and connect
        tests, and checks nothing under the real ~/.ssh changed."""
        real = sandbox.REAL_SSH_DIR
        if inside(real, tempfile.gettempdir()):
            self.skipTest("this run's HOME is already a temporary folder")
        before = snapshot(real)
        env = {k: v for k, v in sandbox.ORIGINAL_ENV.items() if k != "FRAME_CONTROL_SSH_DIR"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_server.OneServer", "test_server.LocalMode",
                            "test_devices", "test_connect"],
                           cwd=str(Path(__file__).resolve().parent), env=env, capture_output=True, text=True,
                           timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        after = snapshot(real)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)
                         and not k.startswith("agent"))  # ssh-agent sockets come and go on their own
        self.assertEqual(changed, [], f"tests changed the real {real}")


if __name__ == "__main__":
    unittest.main()
