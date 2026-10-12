"""Imported first by every test module: nothing a test does reaches this person's
app data, their ~/.ssh, their telemetry, or the shared compatibility database.

Must run before any ui module is imported, since those read these at import time.
The variables are set in os.environ, so servers and scripts the tests start
inherit them too (they pass {**os.environ, ...}).
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

# The environment and real ~/.ssh from before the changes below: test_sandbox
# reruns tests from that environment and checks ~/.ssh is left alone.
ORIGINAL_ENV = dict(os.environ)
REAL_SSH_DIR = Path.home() / ".ssh"

_dir = tempfile.mkdtemp(prefix="frame-control-tests-")
atexit.register(shutil.rmtree, _dir, ignore_errors=True)
os.environ["FRAME_CONTROL_DATA_DIR"] = _dir
os.environ["FRAME_CONTROL_TELEMETRY"] = "0"
# A maintainer's machine holds the database key; send anything that slips through nowhere.
os.environ["FRAME_COMPAT_DB_URL"] = "http://127.0.0.1:9"

# A throwaway home, so Path.home() / "~" land here: ~/.ssh (config, host-key pins,
# its lock file) and the other per-user folders some modules use. A server started
# without FRAME_CONTROL_SSH_DIR used to import the real ~/.ssh/config and seed a pin
# into the real ~/.ssh/frame-control-hosts on every run.
#
# tests/e2e must not import this (directly or through a test_* module): they run in
# a throwaway container whose real ~/.ssh points `ssh frame` at the fake Frame, and
# OpenSSH finds ~/.ssh from the password database, not $HOME. Shared helpers they
# need live in sandbox-free modules such as apk_fixtures.py.
HOME = os.path.join(_dir, "home")
SSH_DIR = os.path.join(HOME, ".ssh")
os.makedirs(SSH_DIR, mode=0o700)
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME  # Path.home() on Windows
os.environ["FRAME_CONTROL_SSH_DIR"] = SSH_DIR  # frame_devices.ssh_dir(), frame_connect.SSH_DIR
# The per-user folders that don't follow HOME when these are set: frame_host.cache_dir()
# (servers warm indexes there and sweep its staging folders) and data_dir()'s fallbacks.
for _name, _sub in (("LOCALAPPDATA", "AppData/Local"), ("APPDATA", "AppData/Roaming"),
                    ("XDG_CACHE_HOME", ".cache"), ("XDG_DATA_HOME", ".local/share"),
                    ("XDG_STATE_HOME", ".local/state"), ("XDG_CONFIG_HOME", ".config")):
    os.environ[_name] = os.path.join(HOME, *_sub.split("/"))
