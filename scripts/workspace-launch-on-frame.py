#!/usr/bin/env python3
"""Open the Frame Workspace SteamVR dashboard and resume its saved monitor.

This is the one-shot target for the Steam library entry. It uses only the
Frame's local dashboard state/request protocol; it has no companion URL or key.
"""
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time


def runtime_paths(env):
    uid = os.getuid()
    runtime = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}") / "frame-workspace"
    return runtime, runtime / "dash-state.json", runtime / "requests"


def state_mtime(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        return info.st_mtime_ns
    except OSError:
        return None


def connected_after(path, baseline):
    modified = state_mtime(path)
    now = time.time()
    if modified is None or (baseline is not None and modified <= baseline):
        return False
    age = now - modified / 1_000_000_000
    if age < -1 or age > 6:
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("connected") is True


def atomic_private_text(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".launcher-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_private_json(path, value):
    atomic_private_text(path, json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")


def write_request(requests):
    name = f"launcher-{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}.json"
    target = requests / name
    atomic_private_json(target, {
        "action": "workspace_start",
        "source": "steam-library",
        "expiresAt": time.time() + 10,
    })
    return target


def wait_seconds(env):
    raw = env.get("FRAME_WORKSPACE_CONNECT_TIMEOUT", "8")
    try:
        value = float(raw)
    except ValueError:
        value = 8
    return min(8, max(0, value))


def show_dashboard():
    script = Path(__file__).resolve().with_name("workspace-dash-on-frame.sh")
    if not script.is_file():
        raise RuntimeError(f"Workspace dashboard helper is missing: {script}")
    try:
        result = subprocess.run(["bash", str(script), "show"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not open the Frame Workspace dashboard: {error}") from error
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "The Frame Workspace dashboard did not open.")
    return result.stderr


def main(env=None):
    env = os.environ if env is None else env
    runtime, state, requests = runtime_paths(env)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    lock_path = runtime / "launcher.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Frame Workspace is already opening; its dashboard will appear shortly.")
            return 0

        last_request = runtime / "launcher-last"
        try:
            previous = float(last_request.read_text(encoding="ascii"))
        except (OSError, ValueError):
            previous = 0
        now_mono = time.monotonic()
        if 0 <= now_mono - previous < 2:
            print("Frame Workspace start was just requested; ignoring a duplicate launch.")
            return 0

        baseline = state_mtime(state)
        show_dashboard()

        deadline = time.monotonic() + wait_seconds(env)
        while time.monotonic() < deadline:
            if connected_after(state, baseline):
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        else:
            print("Frame Workspace dashboard is open. The Mac companion is not connected; open Frame Workspace on the Mac to start the saved monitor.")
            return 0

        request = write_request(requests)
        atomic_private_text(last_request, f"{time.monotonic():.9f}\n")

        consume_deadline = time.monotonic() + 2.5
        while request.exists() and time.monotonic() < consume_deadline:
            time.sleep(0.1)
        if request.exists():
            try:
                request.unlink()
            except FileNotFoundError:
                pass
            print("Frame Workspace dashboard is open, but the Mac companion did not receive the start request. Check the companion connection and launch again.", file=sys.stderr)
            return 1
        print("Frame Workspace start requested with the last saved monitor settings.")
        return 0
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
