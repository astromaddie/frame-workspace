import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
import frame_connect  # noqa: E402
import frame_devices  # noqa: E402


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
                  os.environ["FRAME_CONTROL_SSH_DIR"]):
            self.assertTrue(inside(d, tmp), d)
            self.assertFalse(inside(d, sandbox.REAL_SSH_DIR), d)

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
