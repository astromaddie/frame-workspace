#!/usr/bin/env python3
"""Retire one Frame Workspace session without stopping SteamVR or other apps.

The native SteamVR dashboard invokes ``exit`` locally so its Stop button still
works when the Mac is disconnected. The Mac companion later consumes the
durable request and restores its owned macOS windows/displays.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

SESSION = re.compile(r"[0-9a-f]{32}\Z")
PANEL_UNIT = re.compile(r"frame-workspace-panel-(\d+)-[a-z0-9]{1,16}\.service\Z")
BROWSER_PATTERN = r"[f]rame-workspace/(mac-view|workspace-viewer)|[d]ata/frame-workspace/(mac-view|workspace-viewer)"


class ExitError(Exception):
    pass


def paths():
    uid = os.getuid()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}") / "frame-workspace"
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "frame-workspace"
    native = data / "native"
    return uid, runtime, data, native, native / "exit-state.json"


def valid_session(value):
    if not isinstance(value, str) or not SESSION.fullmatch(value):
        raise ExitError("Workspace session must be 32 lowercase hexadecimal characters.")
    return value


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "active": None, "retired": []}
    except (OSError, ValueError) as exc:
        raise ExitError("Could not read the Workspace session ownership state.") from exc
    if (not isinstance(value, dict) or value.get("version") != 1 or
            (value.get("active") is not None and not SESSION.fullmatch(str(value["active"]))) or
            not isinstance(value.get("retired"), list) or
            any(not isinstance(item, str) or not SESSION.fullmatch(item) for item in value["retired"])):
        raise ExitError("The Workspace session ownership state is invalid; no session was changed.")
    value["retired"] = list(dict.fromkeys(value["retired"]))[-128:]
    return value


def write_state(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
            output.write("\n")
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


def locked_state(state_path):
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_path.parent, 0o700)
    fd = os.open(state_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "r+")


@contextmanager
def session_guard(session):
    """Serialize a short Frame-side launch mutation with Stop retirement."""
    _uid, _runtime, _data, _native, state_path = paths()
    session = valid_session(session)
    with locked_state(state_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_state(state_path)
        if state["active"] != session or session in state["retired"]:
            raise ExitError("This Workspace start was cancelled by Stop.")
        yield


def session_command(action, session):
    uid, _runtime, _data, _native, state_path = paths()
    session = valid_session(session)
    with locked_state(state_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_state(state_path)
        active = state["active"]
        retired = state["retired"]
        if action == "adopt":
            if session in retired:
                raise ExitError("This Workspace session was already stopped; start a new session.")
            if active and active != session and active not in retired:
                retired.append(active)
                state["retired"] = retired[-128:]
            state["active"] = session
            write_state(state_path, state)
            print("Workspace session adopted.")
            return
        if action == "begin":
            if session in retired:
                raise ExitError("This Workspace session was already stopped; start a new session.")
            if active and active != session and active not in retired:
                raise ExitError("Another Frame Workspace session is still active; stop it before starting again.")
            state["active"] = session
            write_state(state_path, state)
            print("Workspace session accepted.")
            return
        if action == "check":
            if active != session or session in retired:
                raise ExitError("This Workspace start was cancelled by Stop.")
            return
        if action in ("exit", "stop"):
            if active and active != session:
                raise ExitError("A newer Workspace session is active; the old Stop request was ignored.")
            if not active:
                state["active"] = session
            if session not in retired:
                retired.append(session)
                state["retired"] = retired[-128:]
            write_state(state_path, state)
            return
        raise ExitError("Unknown Workspace session operation.")


def guarded_run(session, command, timeout=2):
    if not command:
        raise ExitError("guarded-run needs a Frame launch command.")
    with session_guard(session):
        try:
            result = subprocess.run(command, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExitError("The guarded Frame Workspace launch command did not finish promptly.") from exc
    return result.returncode


def retire_and_stop(session, request_dir=None):
    """Retire and close under one lock so a newer session cannot be clobbered."""
    _uid, _runtime, _data, _native, state_path = paths()
    session = valid_session(session)
    with locked_state(state_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_state(state_path)
        active = state["active"]
        if active and active != session:
            raise ExitError("A newer Workspace session is active; the old Stop request was ignored.")
        if not active:
            state["active"] = session
        if session not in state["retired"]:
            state["retired"].append(session)
            state["retired"] = state["retired"][-128:]
        write_state(state_path, state)
        stop_owned_session(session, request_dir=request_dir)


def retire_current_and_stop(request_dir=None):
    """User-initiated orphan cleanup when an offline dashboard has no session ID."""
    _uid, _runtime, _data, _native, state_path = paths()
    with locked_state(state_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_state(state_path)
        session = state["active"]
        if not session or session in state["retired"]:
            session = secrets.token_hex(16)
            state["active"] = session
        if session not in state["retired"]:
            state["retired"].append(session)
            state["retired"] = state["retired"][-128:]
        write_state(state_path, state)
        stop_owned_session(session, request_dir=request_dir)


def run(argv, timeout=1.0):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def units_for_user(uid):
    result = run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--no-pager"], .25)
    if result is None or result.returncode != 0:
        raise ExitError("Could not list Frame Workspace services; owned viewer cleanup is unconfirmed.")
    units = set()
    workspace_host = f"frame-workspace-host-{uid}.service"
    workspace_viewer = f"frame-workspace-viewer-{uid}.service"
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        unit, active = parts[0], parts[2]
        panel = PANEL_UNIT.fullmatch(unit)
        if unit in (workspace_host, workspace_viewer) or (panel and panel.group(1) == str(uid)):
            if active in ("active", "activating", "deactivating"):
                units.add(unit)
    return sorted(units)


def _inside(path, root):
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


def owned_panel_file(runtime, panel_id, field, raw):
    """Accept only a known per-panel runtime path that resolves inside our tree."""
    if not isinstance(raw, str) or not re.fullmatch(r"[a-z0-9]{1,16}", panel_id):
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    runtime_abs = Path(os.path.abspath(runtime))
    candidate_abs = Path(os.path.abspath(candidate))
    if not _inside(candidate_abs, runtime_abs):
        return None
    relative = candidate_abs.relative_to(runtime_abs)
    if len(relative.parts) != 2 or relative.parts[0] != f"panel-{panel_id}":
        return None
    name = relative.parts[1]
    if field == "urlFile" and not re.fullmatch(r"view-fc[0-9a-f]{8}\.url", name):
        return None
    if field == "controlFile" and name != "control.json":
        return None
    if field == "stateFile" and name != "state.json":
        return None
    runtime_real = Path(runtime).resolve()
    parent_real = candidate_abs.parent.resolve()
    candidate_real = candidate_abs.resolve(strict=False)
    if not _inside(parent_real, runtime_real) or not _inside(candidate_real, runtime_real):
        return None
    return candidate_abs


def remove_session_specs(runtime, session):
    if runtime.is_symlink():
        raise ExitError("The Frame Workspace runtime directory is a symlink; refusing to remove runtime files.")
    host_panels = runtime / "host" / "panels"
    runtime_real = runtime.resolve()
    if host_panels.is_symlink() or not _inside(host_panels.resolve(), runtime_real):
        host_specs = ()
    elif host_panels.is_dir():
        host_specs = host_panels.glob("*.json")
    else:
        host_specs = ()
    for spec_path in host_specs:
        if spec_path.is_symlink() or not _inside(spec_path.resolve().parent, runtime_real):
            continue
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A malformed spec inside the private app-owned directory cannot
            # name files for deletion; removing just the spec is safe.
            spec_path.unlink(missing_ok=True)
            continue
        if not isinstance(spec, dict):
            spec_path.unlink(missing_ok=True)
            continue
        panel_id = spec_path.stem
        if not re.fullmatch(r"[a-z0-9]{1,16}", panel_id):
            continue
        for field in ("urlFile", "controlFile", "stateFile"):
            path = owned_panel_file(runtime, panel_id, field, spec.get(field))
            if path is not None:
                path.unlink(missing_ok=True)
        # State ownership is protected by the session lock and the active
        # session check; prior-session specs are stale app-owned runtime too.
        spec_path.unlink(missing_ok=True)
    # These files are runtime-only; saved Workspace and per-window poses live
    # under XDG_DATA_HOME and are deliberately preserved.
    (runtime / "spatial-ui.json").unlink(missing_ok=True)
    for name in ("state.json", "control.json"):
        (runtime / name).unlink(missing_ok=True)


def remove_runtime_panel_files(runtime):
    if runtime.is_symlink():
        raise ExitError("The Frame Workspace runtime directory is a symlink; refusing to remove runtime files.")
    for panel_runtime in runtime.glob("panel-*"):
        if panel_runtime.is_dir() and not panel_runtime.is_symlink():
            shutil.rmtree(panel_runtime, ignore_errors=True)
    for pattern in ("view-fc????????.url", "view-fc????????.url.tmp"):
        for path in runtime.glob(pattern):
            path.unlink(missing_ok=True)
    for name in ("state.json", "control.json"):
        (runtime / name).unlink(missing_ok=True)


def owned_processes_alive(pattern):
    result = run(["pgrep", "-f", pattern], .2)
    if result is None or result.returncode not in (0, 1):
        return None
    return result.returncode == 0


def terminate_browsers():
    # The profile path belongs to this app. Avoid all generic Chromium names.
    pattern = BROWSER_PATTERN
    run(["pkill", "-TERM", "-f", pattern], .2)
    return pattern


def stop_available():
    uid, _runtime, _data, _native, _state_path = paths()
    try:
        units = units_for_user(uid)
    except ExitError:
        # Enable the existing app-scoped Stop when inventory cannot be read.
        return True
    pattern = BROWSER_PATTERN
    return bool(units) or owned_processes_alive(pattern) is not False


def stop_owned_session(session, request_dir=None):
    uid, runtime, _data, _native, _state_path = paths()
    # Persist the Mac restore intent before local cleanup. The dashboard stays
    # visible if any part of the stop cannot be confirmed, so Stop can be
    # retried without restarting the companion.
    request_error = None
    if request_dir is not None:
        try:
            write_stop_request(Path(request_dir), session)
        except (OSError, ExitError) as exc:
            request_error = exc
    errors = []
    try:
        remove_session_specs(runtime, session)
    except ExitError as exc:
        errors.append(str(exc))
    try:
        units = units_for_user(uid)
    except ExitError as exc:
        # Even if systemd cannot enumerate services, still close the owned
        # Chromium profile and report that service cleanup could not be
        # confirmed.
        units = None
        errors.append(str(exc))
    # A just-issued systemd-run can appear after the inventory snapshot. Stop
    # the two fixed shared owners explicitly, plus this UID's enumerated panels.
    owners = [f"frame-workspace-host-{uid}.service", f"frame-workspace-viewer-{uid}.service"]
    to_stop = sorted(set(owners + (units or [])))
    run(["systemctl", "--user", "--no-block", "stop", *to_stop], .35)
    browser_pattern = terminate_browsers()
    deadline = time.monotonic() + .5
    remaining_units = units
    while time.monotonic() < deadline:
        try:
            remaining_units = units_for_user(uid)
        except ExitError as exc:
            if str(exc) not in errors:
                errors.append(str(exc))
            remaining_units = None
        browsers = owned_processes_alive(browser_pattern)
        if remaining_units == [] and browsers is False:
            break
        time.sleep(.05)
    if remaining_units:
        run(["systemctl", "--user", "kill", "--signal=KILL", "--kill-whom=all", *remaining_units], .3)
        run(["systemctl", "--user", "--no-block", "stop", *remaining_units], .3)
    if owned_processes_alive(browser_pattern):
        run(["pkill", "-KILL", "-f", browser_pattern], .2)
    try:
        final_units = units_for_user(uid)
    except ExitError as exc:
        final_units = None
        if str(exc) not in errors:
            errors.append(str(exc))
    browsers_alive = owned_processes_alive(browser_pattern)
    if final_units or browsers_alive is not False:
        raise ExitError("Frame Workspace visuals are closing, but an owned viewer is still active or cannot be verified; retry Stop.")
    try:
        remove_runtime_panel_files(runtime)
    except ExitError as exc:
        errors.append(str(exc))
    if errors:
        raise ExitError("Frame Workspace visuals stopped, but owned services could not be fully verified; retry Stop. "
                        + errors[0])
    if request_error:
        raise ExitError("Frame Workspace visuals stopped, but the Mac restore request could not be saved; retry Stop.") from request_error
    vrcmd = os.environ.get("FRAME_WORKSPACE_VRCMD", "/opt/steamvr/bin/linuxarm64/vrcmd")
    hide_result = run([vrcmd, "--hidedashboard"], .4)
    if hide_result is None or hide_result.returncode != 0:
        raise ExitError("Frame Workspace viewers stopped, but SteamVR did not confirm hiding its dashboard; retry Stop.")
    print("Frame Workspace viewer session stopped.")


def write_stop_request(request_dir, session):
    _uid, runtime, _data, _native, _state_path = paths()
    expected = Path(os.path.abspath(runtime / "requests"))
    request_dir = Path(os.path.abspath(request_dir))
    if (runtime.is_symlink() or request_dir != expected or request_dir.is_symlink() or
            not _inside(request_dir.parent.resolve(), runtime.resolve())):
        raise ExitError("The Mac restore request directory is outside Frame Workspace runtime; refusing to write it.")
    request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(request_dir, 0o700)
    name = f"local-stop-{time.time_ns()}-{os.getpid()}.json"
    target = request_dir / name
    fd, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=request_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"action": "workspace_stop", "session": session}, output, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
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


def main(argv):
    if not argv or argv[0] not in ("begin", "adopt", "check", "exit", "exit-current", "stop", "status", "guarded-run"):
        raise ExitError("Usage: workspace-exit-on-frame.py OPERATION [--session ID] [--request-dir DIR] [-- COMMAND]")
    operation, session, request_dir, command = argv[0], None, None, []
    if operation == "status":
        if len(argv) != 1:
            raise ExitError("The status operation takes no arguments.")
        print(json.dumps({"stopAvailable": stop_available()}, separators=(",", ":")))
        return
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--":
            command = argv[index + 1:]
            break
        if value in ("--session", "--request-dir") and index + 1 < len(argv):
            if value == "--session":
                session = argv[index + 1]
            else:
                request_dir = argv[index + 1]
            index += 2
            continue
        raise ExitError("Unknown Workspace exit-helper argument: " + value)
    if session is None:
        if operation == "exit-current":
            if request_dir is None:
                raise ExitError("The orphan Stop operation needs a request directory.")
            retire_current_and_stop(request_dir=request_dir)
            return
        raise ExitError("Workspace session is required.")
    if operation in ("exit", "stop"):
        retire_and_stop(session, request_dir=request_dir if operation == "exit" else None)
    elif operation == "guarded-run":
        raise SystemExit(guarded_run(session, command))
    else:
        session_command(operation, session)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ExitError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
