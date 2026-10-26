#!/usr/bin/env bash
# Owned native Workspace overlay. Runs on the Frame; does not restart SteamVR.
set -eu
uid=$(id -u)
unit="frame-workspace-viewer-$uid.service"
runtime_base="${XDG_RUNTIME_DIR:-/run/user/$uid}/frame-workspace"
runtime="$runtime_base"
install="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native"
key="com.frame-workspace.native.$uid.workspace"
pose_file="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/workspace-pose.json"
# Window panels: `--panel ID` selects one native viewer of its own (unit,
# runtime files, OpenVR key and saved pose); without it this is the Workspace.
# `--name` is the panel title SteamVR shows; `--initial-pose JSON` places a new
# panel (for example beside the Workspace) until it has a saved pose. `--pose`
# replaces the saved pose for the next start, after the previous viewer stops;
# a window panel's is stamped as set by the user, for this window (FW-15).
# `--bundle-id ID` and `--points WxH` name the Mac window's app and its size in
# points: the host draws the app icon on its placeholder and sizes the panel.
# `placeholders JSON` (shared host only) keeps placeholder-only panels for
# windows still waiting to launch or retrying after a failure.
role="window"
wall_slot="" wall_slot_set=0
panel="" panel_name="" initial_pose="" pose_override="" expected_generation="" session="" bundle_id="" points=""
initial_pose_set=0 pose_override_set=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --panel) [ "$#" -ge 2 ] || exit 2; panel=$2; shift 2 ;;
    --slot) [ "$#" -ge 2 ] || exit 2; wall_slot=$2; wall_slot_set=1; shift 2 ;;
    --role) [ "$#" -ge 2 ] || exit 2; role=$2; shift 2 ;;
    --name) [ "$#" -ge 2 ] || exit 2; panel_name=$2; shift 2 ;;
    --generation) [ "$#" -ge 2 ] || exit 2; expected_generation=$2; shift 2 ;;
    --session) [ "$#" -ge 2 ] || exit 2; session=$2; shift 2 ;;
    --initial-pose) [ "$#" -ge 2 ] || exit 2; initial_pose=$2; initial_pose_set=1; shift 2 ;;
    --pose) [ "$#" -ge 2 ] || exit 2; pose_override=$2; pose_override_set=1; shift 2 ;;
    --bundle-id) [ "$#" -ge 2 ] || exit 2; bundle_id=$2; shift 2 ;;
    --points) [ "$#" -ge 2 ] || exit 2; points=$2; shift 2 ;;
    *) break ;;
  esac
done
case "$session" in
  "") ;;
  *[!0-9a-f]*) echo '--session must be 32 lowercase hexadecimal characters.' >&2; exit 2 ;;
esac
[ -z "$session" ] || [ "${#session}" -eq 32 ] || { echo '--session must be 32 lowercase hexadecimal characters.' >&2; exit 2; }
exit_helper="$install/workspace-exit-on-frame.py"
session_command() {
  [ -n "$session" ] || return 0
  [ -x "$exit_helper" ] || { echo 'The local Workspace Stop helper is missing; restart the companion before opening Workspace.' >&2; return 3; }
  python3 "$exit_helper" "$1" --session "$session"
}
if [ "$wall_slot_set" -eq 1 ]; then
  case "$wall_slot" in ""|*[!0-9]*|?????*) echo '--slot must be an integer from 0 to 1023.' >&2; exit 2 ;; esac
  [ "$((10#$wall_slot))" -le 1023 ] && [ "$role" = window ] && [ -n "$panel" ] &&
    [ "${FRAME_NATIVE_HOST:-}" = 1 ] && [ "${1:-}" = start ] || {
      echo '--slot requires a shared host window start and a slot from 0 to 1023.' >&2; exit 2; }
fi
case "$bundle_id" in
  "") ;;
  *[!A-Za-z0-9._-]*) echo '--bundle-id must be a bundle identifier.' >&2; exit 2 ;;
esac
[ "${#bundle_id}" -le 255 ] || { echo '--bundle-id must be a bundle identifier.' >&2; exit 2; }
case "$points" in
  "") ;;
  *[!0-9x]*|x*|*x|*x*x*) echo '--points must be WIDTHxHEIGHT in points.' >&2; exit 2 ;;
  *x*) [ "${#points}" -le 13 ] && [ "$((10#${points%x*}))" -ge 1 ] && [ "$((10#${points%x*}))" -le 100000 ] &&
         [ "$((10#${points#*x}))" -ge 1 ] && [ "$((10#${points#*x}))" -le 100000 ] ||
         { echo '--points must be WIDTHxHEIGHT in points.' >&2; exit 2; } ;;
  *) echo '--points must be WIDTHxHEIGHT in points.' >&2; exit 2 ;;
esac
case "$role" in
  window|menubar|status|menu) ;;
  *) echo '--role must be window, menubar, status or menu.' >&2; exit 2 ;;
esac
if [ "$role" != window ]; then
  [ "${FRAME_NATIVE_HOST:-}" = 1 ] || { echo 'Auxiliary roles require FRAME_NATIVE_HOST=1.' >&2; exit 2; }
  case "$role:$panel" in menubar:menubar|status:status|menu:menu[0-3]) ;;
    *) echo 'Auxiliary roles require their reserved panel ID.' >&2; exit 2 ;; esac
  [ "$pose_override_set" -eq 0 ] && [ "$initial_pose_set" -eq 0 ] || {
    echo 'Auxiliary panels have no saved or initial pose.' >&2; exit 2; }
  [ -z "$bundle_id" ] && [ -z "$points" ] || { echo 'Auxiliary panels have no window identity.' >&2; exit 2; }
fi
if [ "$pose_override_set" -eq 1 ]; then
  [ "${1:-}" = start ] || { echo '--pose is only valid with start.' >&2; exit 2; }
  [ "$initial_pose_set" -eq 0 ] || { echo '--pose conflicts with --initial-pose.' >&2; exit 2; }
  [ -n "$pose_override" ] || { echo '--pose must contain a pose JSON object.' >&2; exit 2; }
  # Keep this validation ahead of every service query and runtime or pose-file
  # mutation. It mirrors the native parser while rejecting values it coerces.
  if ! python3 - "$pose_override" <<'PYPOSE'
import json, math, sys
raw = sys.argv[1]
if len(raw.encode('utf-8')) > 4096:
    raise SystemExit('--pose JSON must be at most 4096 bytes.')
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result
def reject_constant(value):
    raise ValueError('non-finite JSON number')
try:
    pose = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
except (ValueError, json.JSONDecodeError):
    raise SystemExit('--pose must be valid JSON without duplicate fields or non-finite values.')
base = {'version', 'width', 'distance', 'curvature', 'matrix'}
if not isinstance(pose, dict) or set(pose) not in (base, base | {'bend'}):
    raise SystemExit('--pose must contain exactly version, width, distance, curvature and matrix, with an optional bend.')
if type(pose['version']) is not int or pose['version'] != 1:
    raise SystemExit('--pose version must be the integer 1.')
def number(value, label, minimum, maximum):
    if type(value) not in (int, float):
        raise SystemExit(f'--pose {label} must be a finite number.')
    try:
        value = float(value)
    except OverflowError:
        value = math.inf
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise SystemExit(f'--pose {label} must be between {minimum:g} and {maximum:g}.')
    return value
number(pose['width'], 'width', 0.01, 8.0)
number(pose['distance'], 'distance', 0.4, 10.0)
number(pose['curvature'], 'curvature', 0.0, 1.0)
if 'bend' in pose:
    bend = pose['bend']
    if not isinstance(bend, dict) or set(bend) != {'vertical', 'a', 'b'} or type(bend['vertical']) is not bool:
        raise SystemExit('--pose bend must contain exactly vertical (true or false), a and b.')
    number(bend['a'], 'bend a', 0.0, 1.0)
    number(bend['b'], 'bend b', 0.0, 1.0)
matrix = pose['matrix']
if not isinstance(matrix, list) or len(matrix) != 12:
    raise SystemExit('--pose matrix must contain exactly 12 numbers.')
values = [number(value, 'matrix value', -100.0, 100.0) for value in matrix]
for row in range(3):
    for other in range(3):
        dot = sum(values[row * 4 + col] * values[other * 4 + col] for col in range(3))
        if abs(dot - (1.0 if row == other else 0.0)) > 0.02:
            raise SystemExit('--pose matrix rotation must be orthonormal within 0.02.')
a, b, c, d, e, f, g, h, i = (values[row * 4 + col] for row in range(3) for col in range(3))
determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
if not determinant > 0:
    raise SystemExit('--pose matrix rotation must be right-handed.')
PYPOSE
  then
    exit 2
  fi
fi
if [ -n "$panel" ]; then
  case "$panel" in *[!a-z0-9]*) echo 'Panel IDs are lowercase letters and digits.' >&2; exit 2 ;; esac
  [ "${#panel}" -le 16 ] || exit 2
  unit="frame-workspace-panel-$uid-$panel.service"
  runtime="$runtime_base/panel-$panel"
  key="com.frame-workspace.native.$uid.panel-$panel"
  pose_file="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/panels/$panel-pose.json"
fi
legacy_unit="$unit"
owner_unit="$unit"
owner_host=0
if [ "${FRAME_NATIVE_HOST:-}" = 1 ] && [ -n "$panel" ]; then
  owner_unit="frame-workspace-host-$uid.service"
  owner_host=1
fi
[ "${#panel_name}" -le 64 ] || exit 2
case "$panel_name" in *$'\n'*|*$'\r'*|*$'\t'*) exit 2 ;; esac
[ "${#initial_pose}" -le 1024 ] || exit 2

# The opt-in native host is a shared process. This self-contained helper is
# embedded here because this script is sent over SSH on stdin by the companion.
# Every add/remove/replacement holds one private host lock while it updates the
# immutable generation spec and waits for the host's per-panel acknowledgement.
host_panel() {
  local mode=$1
  shift
  python3 - "$mode" "$@" <<'PYHOST'
import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

(mode, panel, uid, legacy_unit, host_root_raw, panel_runtime_raw, pose_path_raw, key,
 expected_generation, role, *start_args) = sys.argv[1:]
host_root = pathlib.Path(host_root_raw)
panel_runtime = pathlib.Path(panel_runtime_raw)
pose_path = pathlib.Path(pose_path_raw)
host_unit = f'frame-workspace-host-{uid}.service'
spec_dir = host_root / 'panels'
spec_path = spec_dir / f'{panel}.json'
state_path = panel_runtime / 'state.json'
vrcmd = os.environ.get('FRAME_NATIVE_VRCMD', '/opt/steamvr/bin/linuxarm64/vrcmd')

def atomic_write(path, content):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)
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

def stamp_pose(raw, bundle_id, title, session):
    """A --pose restore is the user's own placement: record that, and whose."""
    pose = json.loads(raw)
    pose.update(userSet=True, bundleID=bundle_id, title=title, session=session, savedAt=int(time.time()))
    return json.dumps(pose, separators=(',', ':'), allow_nan=False)

def read_json(path):
    try:
        with open(path, encoding='utf-8') as source:
            value = json.load(source)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None

def systemctl(*args, timeout=5):
    try:
        return subprocess.run(['systemctl', '--user', *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None

def service_status(unit):
    load = systemctl('show', unit, '--property=LoadState', '--value')
    if load is not None and load.stdout.strip() == 'not-found':
        return 'not-found', 0
    if load is None or load.returncode != 0 or not load.stdout.strip():
        return None, None
    result = systemctl('show', unit, '--property=ActiveState', '--value')
    if result is None or result.returncode != 0:
        return None, None
    active = result.stdout.strip()
    pid_result = systemctl('show', unit, '--property=MainPID', '--value')
    if pid_result is None or pid_result.returncode != 0:
        return active, None
    try:
        return active, int(pid_result.stdout.strip())
    except ValueError:
        return active, None

def current_spec():
    value = read_json(spec_path)
    return value if value and isinstance(value.get('generation'), str) else None

def url_generations():
    import re
    generations = []
    if panel_runtime.exists():
        for path in panel_runtime.glob('view-*.url'):
            match = re.fullmatch(r'view-(fc[0-9a-f]{8})\.url', path.name)
            if match:
                generations.append(match.group(1))
    return generations

def wait_stopped(generation, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active, pid = service_status(host_unit)
        if active is None:
            raise RuntimeError('Could not inspect the native host service while stopping a panel.')
        if active in ('not-found', 'inactive', 'failed') and pid == 0:
            return
        if active in ('active', 'activating', 'deactivating') and pid and pid > 0:
            state = read_json(state_path)
            if (state and state.get('hostGeneration') == generation and
                    state.get('overlayKey') == key and state.get('hostPid') == pid and
                    state.get('ready') is False and state.get('state') in ('stopped', 'error')):
                return
        time.sleep(0.1)
    raise RuntimeError('The native host did not confirm that the requested panel stopped.')

def remove_generation(generation):
    current = current_spec()
    if current is not None:
        actual = current['generation']
        if generation and actual != generation:
            raise RuntimeError('The requested native panel generation has changed; leaving it running.')
        generation = actual
        spec_path.unlink(missing_ok=True)
        if current.get('placeholderOnly') is True:
            # A placeholder has no stream and no URL witness: wait only if the
            # host created it, and leave its folder if it may be creating it.
            state = read_json(state_path)
            if state and state.get('hostGeneration') == generation:
                wait_stopped(generation)
                shutil.rmtree(panel_runtime, ignore_errors=True)
            return
    elif spec_path.exists():
        raise RuntimeError('Could not read the native panel spec; leaving its runtime files in place.')
    elif generation is None:
        if state_path.exists():
            state = read_json(state_path)
            if state is None:
                raise RuntimeError('Could not read the native panel state; leaving its runtime files in place.')
            generation = state.get('hostGeneration')
        if not generation:
            pending = url_generations()
            if len(pending) > 1:
                raise RuntimeError('Multiple pending native panel generations exist; leaving them in place.')
            generation = pending[0] if pending else None
    if generation:
        # A spec can be removed just before the host writes its state file.
        # Its URL remains as the generation witness until the host confirms
        # removal, so never treat a missing state file as an acknowledgement.
        wait_stopped(generation)
    elif state_path.exists():
        raise RuntimeError('The native panel state has no generation; leaving its runtime files in place.')
    if panel_runtime.exists():
        shutil.rmtree(panel_runtime)

def stop_legacy_panel():
    active, pid = service_status(legacy_unit)
    if active is None:
        raise RuntimeError('Could not inspect the standalone native panel service before host migration.')
    if active in ('not-found', 'inactive', 'failed') and pid == 0:
        return False
    if active not in ('active', 'activating', 'deactivating') or not pid or pid <= 0:
        raise RuntimeError('The standalone native panel service has no confirmed process state.')
    systemctl('--no-block', 'stop', legacy_unit)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        active, pid = service_status(legacy_unit)
        if active is None:
            raise RuntimeError('Could not confirm standalone native panel shutdown before host migration.')
        if active in ('not-found', 'inactive', 'failed') and pid == 0:
            return True
        time.sleep(0.1)
    raise RuntimeError('The standalone native panel service did not stop before host migration.')

def lock_host():
    host_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(host_root, 0o700)
    spec_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(spec_dir, 0o700)
    fd = os.open(host_root / 'launcher.lock', os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return os.fdopen(fd, 'r+')

def ensure_host(binary, runtime_dir, session=None, exit_helper=None):
    deadline = time.monotonic() + 12
    active, pid = service_status(host_unit)
    if active == 'active' and pid and pid > 0:
        return pid
    if active in ('active', 'activating', 'deactivating'):
        while time.monotonic() < deadline:
            active, pid = service_status(host_unit)
            if active == 'active' and pid and pid > 0:
                return pid
            if active not in ('active', 'activating', 'deactivating'):
                break
            time.sleep(0.1)
        if active in ('active', 'activating', 'deactivating'):
            raise RuntimeError('The native host service is transitioning without a usable process ID.')
    if active is None:
        raise RuntimeError('Could not inspect the native host service before startup.')
    systemctl('reset-failed', host_unit)
    command = [
        'systemd-run', '--user', f'--unit={host_unit}',
        '--description=Frame Workspace native host', '--collect',
        '--property=Type=exec', '--property=KillMode=control-group',
        '--property=Restart=no', '--property=TimeoutStopSec=3s',
        f'--setenv=HOME={os.environ.get("HOME", "/")}',
        f'--setenv=XDG_RUNTIME_DIR={runtime_dir}', '--setenv=DISPLAY=:0',
        '--setenv=LC_ALL=C.UTF-8', binary, '--host-dir', str(spec_dir),
        '--state-file', str(host_root / 'state.json')]
    try:
        if session and exit_helper is not None:
            # Serialize only the short systemd-run mutation with Stop. Waiting
            # for the host to become ready happens after releasing the lock.
            launch_status = exit_helper.guarded_run(session, command, timeout=2)
            launch_failed = launch_status != 0
        else:
            launch = subprocess.run(command, capture_output=True, text=True, timeout=10)
            launch_failed = launch.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        launch_failed = True
    except Exception as exc:
        if exit_helper is None or not isinstance(exc, exit_helper.ExitError):
            raise
        launch_failed = True
    if launch_failed:
        raise RuntimeError('Could not start the native host service.')
    while time.monotonic() < deadline:
        active, pid = service_status(host_unit)
        if active == 'active' and pid and pid > 0:
            return pid
        if active not in ('active', 'activating', 'deactivating') and active is not None:
            break
        time.sleep(0.1)
    raise RuntimeError('The native host did not become active.')

def start_panel(lock):
    (url, width, height, generation, decoder, conversion, title, initial_pose,
     pose_override, foveation, diagnostic_gaze, binary, runtime_dir, wall_slot, session,
     bundle_id, points) = start_args
    exit_helper = None
    if session:
        import importlib.util
        exit_helper_path = pathlib.Path(binary).with_name('workspace-exit-on-frame.py')
        spec = importlib.util.spec_from_file_location('frame_workspace_exit', exit_helper_path)
        if spec is None or spec.loader is None:
            raise RuntimeError('The local Workspace Stop helper is missing.')
        exit_helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exit_helper)
        exit_helper.session_command('begin', session)
    old = current_spec()
    if old is None and spec_path.exists():
        raise RuntimeError('Could not read the existing native panel spec; leaving it in place.')
    # The window's placeholder (FW-15) is replaced in one step: the host swaps
    # a spec for its next generation at the same path.
    placeholder = old is not None and old.get('placeholderOnly') is True
    old_generation = old.get('generation') if old and not placeholder else None
    old_url = old.get('urlFile') if old and not placeholder else None
    # A host flag can change between launches. Retire any prior standalone
    # owner for this key before the shared host can claim it.
    stop_legacy_panel()
    if old_generation:
        remove_generation(old_generation)
    elif placeholder:
        pass
    elif state_path.exists():
        previous_state = read_json(state_path)
        if previous_state is None:
            raise RuntimeError('The existing native panel state has no generation; leaving it in place.')
        if previous_state.get('hostGeneration'):
            remove_generation(previous_state['hostGeneration'])
        elif not previous_state.get('hostPid'):
            # A prior standalone owner has ended (or was just stopped above).
            shutil.rmtree(panel_runtime)
        else:
            raise RuntimeError('The existing native host state has no generation; leaving it in place.')
    elif panel_runtime.exists():
        pending = url_generations()
        if pending:
            if len(pending) != 1:
                raise RuntimeError('Multiple pending native panel generations exist; leaving them in place.')
            remove_generation(pending[0])
        else:
            # No spec, state or generation URL means this panel has no host
            # owner. A legacy unit may have owned just these runtime files.
            shutil.rmtree(panel_runtime)
    if pose_override:
        # Pose is shared by this panel across generations; both old owner
        # kinds have been confirmed stopped before replacing it.
        atomic_write(pose_path, stamp_pose(pose_override, bundle_id, title, session) if role == 'window'
                     else pose_override)
    ensure_host(binary, runtime_dir, session=session, exit_helper=exit_helper)
    panel_runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(panel_runtime, 0o700)
    url_path = panel_runtime / f'view-{generation}.url'
    atomic_write(url_path, url + '\n')
    request_dir = pathlib.Path(runtime_dir) / 'frame-workspace' / 'requests'
    request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(request_dir, 0o700)
    spec = {
        'version': 1,
        'role': role,
        'generation': generation,
        'urlFile': str(url_path),
        'overlayKey': key,
        'overlayName': title,
        'controlFile': str(panel_runtime / 'control.json'),
        'stateFile': str(state_path),
        'poseFile': str(pose_path),
        'initialPose': initial_pose,
        'requestDir': str(request_dir),
        'decoder': decoder,
        'conversion': conversion,
    }
    if session:
        spec['session'] = session
    if wall_slot:
        spec['slot'] = int(wall_slot)
    if bundle_id:
        spec['bundleID'] = bundle_id
    if points:
        spec['points'] = [int(v) for v in points.split('x')]
    if role == 'window' and foveation in ('1', 'gaze'):
        spec['foveation'] = foveation == '1'
        spec['gaze'] = foveation == 'gaze'
    try:
        if exit_helper is not None:
            with exit_helper.session_guard(session):
                atomic_write(spec_path, json.dumps(spec, separators=(',', ':'), allow_nan=False) + '\n')
        else:
            atomic_write(spec_path, json.dumps(spec, separators=(',', ':'), allow_nan=False) + '\n')
    except BaseException:
        if exit_helper is not None:
            try:
                subprocess.run([sys.executable, str(exit_helper_path), 'guarded-run', '--session', session,
                                '--', 'systemctl', '--user', '--no-block', 'stop', host_unit],
                               capture_output=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        url_path.unlink(missing_ok=True)
        raise

    # A window is ready once it has submitted a decoded picture. Menu Bar,
    # Status and menu slots can be idle with nothing to draw (a closed menu
    # sends no video), so they count once the host reports them registered:
    # connected, with their first surface message.
    auxiliary = role != 'window'
    try:
        timeout = min(max(float(os.environ.get('FRAME_NATIVE_READY_SECONDS', '40')), 0.5), 120)
    except ValueError:
        timeout = 40
    # The spec is written: other panels may start while this one waits for its
    # first picture (the companion launches several at once). Any cleanup
    # takes the lock again.
    fcntl.flock(lock, fcntl.LOCK_UN)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = current_spec()
        if current is None or current.get('generation') != generation:
            # Stopped (or replaced) by another launcher call meanwhile; it
            # owns the cleanup.
            raise RuntimeError('The native panel was stopped or replaced during startup.')
        active, current_pid = service_status(host_unit)
        state = read_json(state_path)
        if active not in ('active', 'activating') or not current_pid or current_pid <= 0:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                remove_generation(generation)
            except Exception as exc:
                raise RuntimeError('The native host exited during panel startup, and panel shutdown was not confirmed: ' + str(exc))
            raise RuntimeError('The native host exited during panel startup.')
        if state and state.get('hostGeneration') == generation:
            if state.get('state') == 'error' and state.get('ready') is False:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    remove_generation(generation)
                except Exception as exc:
                    raise RuntimeError('The native host reported a panel error, and shutdown was not confirmed: ' + str(exc))
                raise RuntimeError('The native host reported an error for this panel generation.')
            try:
                owned = state.get('hostPid') == current_pid and state.get('overlayKey') == key
                pictured = (state.get('ready') is True and
                            0 < int(state.get('textureWidth', 0)) <= 5120 and
                            0 < int(state.get('textureHeight', 0)) <= 5120)
                idle = auxiliary and state.get('registered') is True and state.get('role') == role
                ready = owned and (pictured or idle)
            except (TypeError, ValueError):
                ready = False
            if ready:
                try:
                    overlays = subprocess.run([vrcmd, '--overlays'], capture_output=True,
                                              text=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    overlays = None
                if overlays and overlays.returncode == 0 and f"'{key}' -- '" in overlays.stdout:
                    if old_url and old_url != str(url_path):
                        pathlib.Path(old_url).unlink(missing_ok=True)
                    detail = '' if pictured else ' (idle, no picture yet)'
                    print(f'Native Workspace ready: registered OpenVR key {key} in {host_unit}{detail}')
                    return
        time.sleep(0.1)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        remove_generation(generation)
    except Exception:
        # Preserve URL and runtime evidence if shutdown could not be confirmed.
        raise RuntimeError('The native panel did not become ready; its spec was removed but shutdown was not confirmed.')
    if auxiliary:
        raise RuntimeError(f'The native host did not register this {role} panel within {timeout:g} seconds.')
    raise RuntimeError(f'The native host did not submit a decoded texture and register this panel within {timeout:g} seconds.')

def sync_placeholders(raw):
    """Placeholder-only panels for windows waiting to launch or retrying: the
    listed ones are written (or kept), unlisted placeholder-only specs removed.
    A window's own (streaming) spec is never touched."""
    import re
    import secrets
    if len(raw.encode('utf-8')) > 262144:
        raise RuntimeError('Placeholder list is too large.')
    data = json.loads(raw)
    items = data.get('panels') if isinstance(data, dict) else None
    session = data.get('session') if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > 128 or (
            session is not None and (not isinstance(session, str) or not re.fullmatch(r'[0-9a-f]{32}', session))):
        raise RuntimeError('Invalid placeholder list.')
    def text(value, limit):
        if value is None:
            return ''
        if not isinstance(value, str) or len(value) > limit or re.search(r'[\x00-\x1f\x7f]', value):
            raise RuntimeError('Invalid placeholder text.')
        return value
    data_home = pathlib.Path(os.environ.get('XDG_DATA_HOME') or pathlib.Path.home() / '.local/share')
    runtime_root = pathlib.Path(start_args[0]) / 'frame-workspace'
    request_dir = runtime_root / 'requests'
    request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    desired = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('panel'), str) or \
                not re.fullmatch(r'w[0-9]{1,15}', item['panel']) or item['panel'] in desired:
            raise RuntimeError('Invalid placeholder panel.')
        panel_id = item['panel']
        folder = runtime_root / f'panel-{panel_id}'
        spec = {'version': 1, 'role': 'window', 'urlFile': '',
                'overlayKey': f'com.frame-workspace.native.{uid}.panel-{panel_id}',
                'overlayName': text(item.get('name'), 64) or 'Mac Window',
                'controlFile': str(folder / 'control.json'), 'stateFile': str(folder / 'state.json'),
                'poseFile': str(data_home / 'frame-workspace' / 'panels' / f'{panel_id}-pose.json'),
                'requestDir': str(request_dir), 'decoder': 'software', 'conversion': 'cpu',
                'placeholderOnly': True}
        bundle = text(item.get('bundleID'), 255)
        if bundle:
            if not re.fullmatch(r'[A-Za-z0-9._-]+', bundle):
                raise RuntimeError('Invalid placeholder bundle ID.')
            spec['bundleID'] = bundle
        size = item.get('points')
        if size is not None:
            if (not isinstance(size, list) or len(size) != 2 or
                    any(type(v) is not int or not 1 <= v <= 100000 for v in size)):
                raise RuntimeError('Invalid placeholder size.')
            spec['points'] = size
        slot = item.get('slot')
        if slot is not None:
            if type(slot) is not int or not 0 <= slot <= 1023:
                raise RuntimeError('Invalid placeholder slot.')
            spec['slot'] = slot
        pose = item.get('pose')
        if pose is not None:
            encoded = json.dumps(pose, separators=(',', ':'), allow_nan=False)
            if not isinstance(pose, dict) or len(encoded) > 1024:
                raise RuntimeError('Invalid placeholder pose.')
            spec['initialPose'] = encoded
        error = text(item.get('error'), 160)
        if error:
            spec['placeholderError'] = error
        if session:
            spec['session'] = session
        desired[panel_id] = spec
    for path in spec_dir.glob('*.json'):
        current = read_json(path)
        if current and current.get('placeholderOnly') is True and path.stem not in desired:
            path.unlink(missing_ok=True)
    shown = 0
    for panel_id, spec in desired.items():
        path = spec_dir / f'{panel_id}.json'
        current = read_json(path)
        if path.exists() and (current is None or current.get('placeholderOnly') is not True):
            continue  # the window's own panel, starting or streaming
        shown += 1
        if current and {k: v for k, v in current.items() if k != 'generation'} == spec:
            continue
        folder = pathlib.Path(spec['stateFile']).parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        spec['generation'] = 'ph' + secrets.token_hex(6)
        atomic_write(path, json.dumps(spec, separators=(',', ':'), allow_nan=False) + '\n')
    print(f'Placeholders: {shown}')

host_root.mkdir(mode=0o700, parents=True, exist_ok=True)
lock = lock_host()
try:
    if mode == 'stop':
        remove_generation(expected_generation or None)
        print(f'Stopped native panel {panel}; shared host remains running.')
    elif mode == 'start':
        start_panel(lock)
    elif mode == 'placeholders':
        sync_placeholders(start_args[1])
    else:
        raise RuntimeError('Invalid native host launcher operation.')
except Exception as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
finally:
    lock.close()
PYHOST
}

case "${1:-}" in
  stop)
    # Stops this viewer (a window panel, or the Workspace) and removes its
    # runtime files; its saved pose stays for next time.
    [ "$#" -eq 1 ] || exit 2
    if [ "${FRAME_NATIVE_HOST:-}" = 1 ] && [ -n "$panel" ]; then
      host_panel stop "$panel" "$uid" "$legacy_unit" "$runtime_base/host" "$runtime" "$pose_file" "$key" "$expected_generation" "$role"
      exit $?
    fi
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
    if [ -n "$panel" ]; then rm -rf "$runtime"; fi
    echo "Stopped $unit"
    exit 0
    ;;
  focus)
    [ "$#" -eq 1 ] || exit 2
    python3 - "$runtime" "$owner_unit" "$key" focus "$owner_host" <<'PY'
import fcntl, json, math, os, pathlib, subprocess, sys
p = pathlib.Path(sys.argv[1])
unit, key, action, host_mode = sys.argv[2:]
def systemd(prop):
    try:
        r = subprocess.run(['systemctl', '--user', 'show', unit, '--property=' + prop, '--value'],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''
def owner():
    if systemd('ActiveState') != 'active':
        return None
    try:
        owner_pid = int(systemd('MainPID'))
        if owner_pid <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
            return None
        if host_mode == '1' and (type(s.get('hostPid')) is not int or s.get('hostPid') != owner_pid):
            return None
        return s
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
def next_revision(target):
    try:
        data = json.loads(target.read_text())
        old = data.get('revision') if isinstance(data, dict) else None
        if (type(old) not in (int, float) or
                (type(old) is float and (not math.isfinite(old) or not old.is_integer())) or
                old < 0 or old >= (1 << 53) - 1):
            return 1
        return int(old) + 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
s = owner()
if s is None:
    raise SystemExit('The native Workspace viewer is not ready; use Show Workspace again.')
lock_fd = os.open(p / 'control.lock', os.O_CREAT | os.O_RDWR, 0o600)
with os.fdopen(lock_fd, 'r+') as lock:
    os.fchmod(lock.fileno(), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    target = p / 'control.json'
    revision = next_revision(target)
    tmp = p / ('control.' + str(os.getpid()) + '.tmp')
    # Show again keeps its existing focus and recenter behavior.
    tmp.write_text(json.dumps({'revision': revision, 'focus': True, 'recenter': True}))
    os.chmod(tmp, 0o600)
    tmp.replace(target)
print('Native Workspace focus requested')
PY
    exit 0
    ;;
  adjust)
    [ "$#" -eq 3 ] || exit 2
    python3 - "$runtime" "$owner_unit" "$key" "$2" "$3" "$owner_host" <<'PY'
import fcntl, json, math, os, pathlib, re, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
unit, key, raw_width, raw_distance, host_mode = sys.argv[2:]
def number(raw, label, minimum, maximum):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', raw):
        raise SystemExit(f'{label} must be a finite number.')
    value = float(raw)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise SystemExit(f'{label} must be between {minimum:g} and {maximum:g} meters.')
    return value
width = number(raw_width, 'Workspace width', 0.4, 8.0)
distance = number(raw_distance, 'Workspace distance', 0.4, 10.0)
def systemd(prop):
    try:
        r = subprocess.run(['systemctl', '--user', 'show', unit, '--property=' + prop, '--value'],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''
def owner():
    if systemd('ActiveState') != 'active':
        return None
    try:
        owner_pid = int(systemd('MainPID'))
        if owner_pid <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
            return None
        if host_mode == '1' and (type(s.get('hostPid')) is not int or s.get('hostPid') != owner_pid):
            return None
        return s
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
def next_revision(target):
    try:
        data = json.loads(target.read_text())
        old = data.get('revision') if isinstance(data, dict) else None
        if (type(old) not in (int, float) or
                (type(old) is float and (not math.isfinite(old) or not old.is_integer())) or
                old < 0 or old >= (1 << 53) - 1):
            return 1
        return int(old) + 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
if owner() is None:
    raise SystemExit('The native Workspace viewer is stopped or not ready; use Show Workspace again.')
lock_fd = os.open(p / 'control.lock', os.O_CREAT | os.O_RDWR, 0o600)
with os.fdopen(lock_fd, 'r+') as lock:
    os.fchmod(lock.fileno(), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    target = p / 'control.json'
    revision = next_revision(target)
    tmp = p / ('control.' + str(os.getpid()) + '.tmp')
    # Placement changes use the native owner’s live resize path. They never
    # request focus/recenter and never restart the viewer or Mac display.
    tmp.write_text(json.dumps({'revision': revision, 'width': width, 'distance': distance}))
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        s = owner()
        if s is None:
            raise SystemExit('The native Workspace owner stopped before confirming placement.')
        try:
            applied_revision = s.get('controlRevision')
            placement = s['placement']
            actual_width = float(placement['width'])
            actual_distance = float(placement['distance'])
            if (type(applied_revision) in (int, float) and math.isfinite(applied_revision) and
                    applied_revision == revision and math.isclose(actual_width, width, abs_tol=0.00001) and
                    math.isclose(actual_distance, distance, abs_tol=0.00001)):
                print(json.dumps({'ready': True, 'state': s['state'], 'overlayKey': key,
                                  'placement': {'width': actual_width, 'distance': actual_distance}}))
                raise SystemExit(0)
        except (KeyError, TypeError, ValueError):
            pass
        time.sleep(0.1)
    raise SystemExit('The native Workspace owner did not confirm the requested placement.')
PY
    exit $?
    ;;
  pose)
    # Relative moves (m: right, up, toward the viewer) and turns (degrees:
    # yaw, pitch, roll) in the panel's own frame.
    [ "$#" -eq 7 ] || exit 2
    python3 - "$runtime" "$owner_unit" "$key" "$2" "$3" "$4" "$5" "$6" "$7" "$owner_host" <<'PY'
import fcntl, json, math, os, pathlib, re, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
unit, key = sys.argv[2:4]
host_mode = sys.argv[10]
def number(raw, label, limit, unit_name):
    if not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', raw):
        raise SystemExit(f'{label} must be a finite number.')
    value = float(raw)
    if not math.isfinite(value) or abs(value) > limit:
        raise SystemExit(f'{label} must be within {limit:g} {unit_name}.')
    return value
move = [number(v, n, 2, 'meters') for v, n in zip(sys.argv[4:7], ('Move right', 'Move up', 'Move closer'))]
rotate = [number(v, n, 90, 'degrees') for v, n in zip(sys.argv[7:10], ('Yaw', 'Pitch', 'Roll'))]
def systemd(prop):
    try:
        r = subprocess.run(['systemctl', '--user', 'show', unit, '--property=' + prop, '--value'],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''
def owner():
    if systemd('ActiveState') != 'active':
        return None
    try:
        owner_pid = int(systemd('MainPID'))
        if owner_pid <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
            return None
        if host_mode == '1' and (type(s.get('hostPid')) is not int or s.get('hostPid') != owner_pid):
            return None
        return s
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
def next_revision(target):
    try:
        data = json.loads(target.read_text())
        old = data.get('revision') if isinstance(data, dict) else None
        if (type(old) not in (int, float) or
                (type(old) is float and (not math.isfinite(old) or not old.is_integer())) or
                old < 0 or old >= (1 << 53) - 1):
            return 1
        return int(old) + 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
if owner() is None:
    raise SystemExit('The native Workspace viewer is stopped or not ready; use Show Workspace again.')
lock_fd = os.open(p / 'control.lock', os.O_CREAT | os.O_RDWR, 0o600)
with os.fdopen(lock_fd, 'r+') as lock:
    os.fchmod(lock.fileno(), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    target = p / 'control.json'
    revision = next_revision(target)
    tmp = p / ('control.' + str(os.getpid()) + '.tmp')
    # Live and relative; never requests focus/recenter or restarts anything.
    tmp.write_text(json.dumps({'revision': revision, 'move': move, 'rotate': rotate}))
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        s = owner()
        if s is None:
            raise SystemExit('The native Workspace owner stopped before confirming placement.')
        try:
            applied_revision = s.get('controlRevision')
            if type(applied_revision) in (int, float) and applied_revision == revision:
                print(json.dumps({'ready': True, 'state': s['state'], 'overlayKey': key,
                                  'placement': s['placement']}))
                raise SystemExit(0)
        except (KeyError, TypeError, ValueError):
            pass
        time.sleep(0.1)
    raise SystemExit('The native Workspace owner did not confirm the move.')
PY
    exit $?
    ;;
  curvature)
    [ "$#" -eq 2 ] || exit 2
    python3 - "$runtime" "$owner_unit" "$key" "$2" "$owner_host" <<'PY'
import fcntl, json, math, os, pathlib, re, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
unit, key, raw, host_mode = sys.argv[2:]
if not re.fullmatch(r'(?:0(?:\.[0-9]+)?|1(?:\.0+)?)', raw):
    raise SystemExit('Workspace curvature must be a finite number between 0 and 1.')
curvature = float(raw)
if not math.isfinite(curvature) or not 0 <= curvature <= 1:
    raise SystemExit('Workspace curvature must be a finite number between 0 and 1.')
def systemd(prop):
    try:
        r = subprocess.run(['systemctl', '--user', 'show', unit, '--property=' + prop, '--value'],
                           capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''
def owner():
    if systemd('ActiveState') != 'active':
        return None
    try:
        owner_pid = int(systemd('MainPID'))
        if owner_pid <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
            return None
        if host_mode == '1' and (type(s.get('hostPid')) is not int or s.get('hostPid') != owner_pid):
            return None
        return s
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
def next_revision(target):
    try:
        data = json.loads(target.read_text())
        old = data.get('revision') if isinstance(data, dict) else None
        if (type(old) not in (int, float) or
                (type(old) is float and (not math.isfinite(old) or not old.is_integer())) or
                old < 0 or old >= (1 << 53) - 1):
            return 1
        return int(old) + 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
if owner() is None:
    raise SystemExit('The native Workspace viewer is stopped or not ready; use Show Workspace again.')
lock_fd = os.open(p / 'control.lock', os.O_CREAT | os.O_RDWR, 0o600)
with os.fdopen(lock_fd, 'r+') as lock:
    os.fchmod(lock.fileno(), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    target = p / 'control.json'
    revision = next_revision(target)
    tmp = p / ('control.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps({'revision': revision, 'curvature': curvature}))
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        s = owner()
        if s is None:
            raise SystemExit('The native Workspace owner stopped before reporting curvature.')
        try:
            result_revision = s.get('controlResultRevision')
            if (type(result_revision) in (int, float) and math.isfinite(result_revision) and
                    result_revision == revision):
                result = s['curvatureResult']
                value = result.get('value')
                response = {'ready': True, 'ok': result.get('applied') is True,
                            'state': s['state'], 'overlayKey': key,
                            'curvature': {'supported': result.get('supported') is True,
                                          'applied': result.get('applied') is True,
                                          'requested': float(result['requested']),
                                          'value': value,
                                          'errorCode': int(result['errorCode'])}}
                print(json.dumps(response, allow_nan=False))
                raise SystemExit(0)
        except (KeyError, TypeError, ValueError):
            pass
        time.sleep(0.1)
    raise SystemExit('The native Workspace owner did not report a curvature result.')
PY
    exit $?
    ;;
  placeholders)
    # FW-15: placeholder-only panels for the windows still waiting to launch.
    [ "$#" -eq 2 ] && [ "${FRAME_NATIVE_HOST:-}" = 1 ] && [ -z "$panel" ] || {
      echo 'placeholders takes one JSON list and needs FRAME_NATIVE_HOST=1 without --panel.' >&2; exit 2; }
    host_panel placeholders "" "$uid" "" "$runtime_base/host" "" "" "" "" window \
      "${XDG_RUNTIME_DIR:-/run/user/$uid}" "$2"
    exit $?
    ;;
  start) ;;
  *) echo 'usage: workspace-native-on-frame.sh [--panel ID [--name TITLE] [--initial-pose JSON] [--pose JSON]] start URL WIDTH HEIGHT TAG [software|v4l2] [cpu|gpu] [0|1|gaze foveation] [U,V|scan diagnostic gaze] | focus | adjust WIDTH DISTANCE | pose RIGHT UP CLOSER YAW PITCH ROLL | curvature VALUE | stop' >&2; exit 2 ;;
esac
[ "$#" -ge 5 ] && [ "$#" -le 9 ] || exit 2
url=$2
w=$3
h=$4
tag=$5
decoder=${6:-software}
case "$decoder" in software|v4l2) ;; *) echo 'Decoder must be software or v4l2.' >&2; exit 2 ;; esac
foveation=${8:-0}
case "$foveation" in 0|1|gaze) ;; *) echo 'Foveation must be 0, 1 or gaze.' >&2; exit 2 ;; esac
foveation_args=()
if [ "$foveation" = 1 ]; then foveation_args=(--foveation); fi
# gaze: send eye gaze for gaze-directed detail on an ordinary stream.
if [ "$foveation" = gaze ]; then foveation_args=(--gaze); fi
diagnostic_gaze=${9:-}
if [ -n "$diagnostic_gaze" ]; then
  [ "$foveation" = 1 ] || [ "$foveation" = gaze ] || { echo 'Diagnostic gaze requires foveation or gaze.' >&2; exit 2; }
  diagnostic_gaze=$(python3 - "$diagnostic_gaze" <<'PYGAZE'
import math, sys
value = sys.argv[1]
if value != 'scan':
    try:
        uv = list(map(float, value.split(',')))
        assert len(uv) == 2 and all(math.isfinite(v) and 0 <= v <= 1 for v in uv)
    except (ValueError, AssertionError):
        raise SystemExit('Diagnostic gaze must be scan or finite U,V in [0,1].')
    print(f'{uv[0]:.17g},{uv[1]:.17g}')
else:
    print('scan')
PYGAZE
  )
  foveation_args+=(--diagnostic-gaze "$diagnostic_gaze")
fi
conversion=${7:-cpu}
case "$conversion" in cpu|gpu) ;; *) echo 'Conversion must be cpu or gpu.' >&2; exit 2 ;; esac
case "$w:$h" in *[!0-9:]*) exit 2 ;; esac
[ "${#w}" -le 4 ] && [ "${#h}" -le 4 ] || exit 2
[ "$w" -ge 320 ] && [ "$h" -ge 200 ] && [ "$w" -le 5120 ] && [ "$h" -le 5120 ] || exit 2
[ "$((10#$w * 10#$h))" -le 16777216 ] || exit 2
case "$tag" in fc????????) ;; *) exit 2 ;; esac
case "$tag" in *[!0-9a-f]*) exit 2 ;; esac
case "$url" in http://127.0.0.1:*/view\?*) ;; *) echo 'Native Workspace requires its loopback SSH tunnel.' >&2; exit 2 ;; esac
binary="$install/frame-workspace-native"
[ -x "$binary" ] || {
  echo 'The native Workspace viewer is not installed. Install the native build, or restart the companion with FRAME_WORKSPACE_VIEWER=browser.' >&2
  exit 3
}

if [ "${FRAME_NATIVE_HOST:-}" = 1 ] && [ -n "$panel" ]; then
  if [ -n "$diagnostic_gaze" ]; then
    echo 'Diagnostic gaze is not supported by the native host panel route.' >&2
    exit 2
  fi
  host_panel start "$panel" "$uid" "$legacy_unit" "$runtime_base/host" "$runtime" "$pose_file" "$key" "" "$role" \
    "$url" "$w" "$h" "$tag" "$decoder" "$conversion" "$panel_name" "$initial_pose" \
    "$pose_override" "$foveation" "$diagnostic_gaze" "$binary" "${XDG_RUNTIME_DIR:-/run/user/$uid}" "$wall_slot" "$session" \
    "$bundle_id" "$points"
  exit $?
fi

if [ -n "$session" ]; then session_command begin; fi

# Same reserved service as the browser route. Confirm complete shutdown before
# replacing it, including an orphan left by a previous companion process.
stopped() {
  # systemd returns nonzero for an unloaded transient unit even though its
  # LoadState is explicitly not-found. That is a confirmed stopped owner.
  load=$(systemctl --user show "$unit" --property=LoadState --value 2>/dev/null) || {
    [ "$load" = not-found ] && return 0
    return 1
  }
  [ "$load" = not-found ] && return 0
  active=$(systemctl --user show "$unit" --property=ActiveState --value) || return 1
  pid=$(systemctl --user show "$unit" --property=MainPID --value) || return 1
  case "$active" in inactive|failed) [ "$pid" = 0 ] ;; *) return 1 ;; esac
}
if ! stopped; then
  # Show again closes the old stream first, so the viewer may exit and its
  # transient unit unload before this stop runs ("Unit ... not loaded").
  # The loop below is what confirms shutdown either way.
  systemctl --user --no-block stop "$unit" >/dev/null 2>&1 || true
  for _ in $(seq 1 25); do stopped && break; sleep 0.2; done
  stopped || { echo 'The previous Workspace service has not stopped.' >&2; exit 1; }
fi

umask 077
if [ "$pose_override_set" -eq 1 ]; then
  # mkstemp creates a private same-directory file; os.replace makes the
  # replacement atomic. This happens only after shutdown has been confirmed.
  python3 - "$pose_file" "$pose_override" "$panel" "$bundle_id" "$panel_name" "$session" <<'PYWRITEPOSE'
import json, os, pathlib, sys, tempfile, time
target = pathlib.Path(sys.argv[1])
if sys.argv[3]:
    # A window panel's restored pose is its user's own placement (FW-15).
    pose = json.loads(sys.argv[2])
    pose.update(userSet=True, bundleID=sys.argv[4], title=sys.argv[5], session=sys.argv[6], savedAt=int(time.time()))
    sys.argv[2] = json.dumps(pose, separators=(',', ':'), allow_nan=False)
target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(sys.argv[2])
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
PYWRITEPOSE
fi

mkdir -p "$runtime"
chmod 700 "$runtime"
secret="$runtime/view-$tag.url"
state="$runtime/state.json"
control="$runtime/control.json"
printf '%s\n' "$url" > "$secret"
rm -f "$state" "$control"
trap 'rm -f "$secret"' EXIT
pose="$pose_file"
mkdir -p "$(dirname "$pose")"
# Close in the headset asks the companion through this private folder.
requests="${XDG_RUNTIME_DIR:-/run/user/$uid}/frame-workspace/requests"
mkdir -p "$requests"
chmod 700 "$requests"
panel_args=(--request-dir "$requests")
if [ -n "$panel_name" ]; then panel_args+=(--overlay-name "$panel_name"); fi
if [ -n "$bundle_id" ]; then panel_args+=(--bundle-id "$bundle_id"); fi
if [ -n "$initial_pose" ]; then panel_args+=(--initial-pose "$initial_pose"); fi
vrcmd=/opt/steamvr/bin/linuxarm64/vrcmd
native_start=(--user --unit="$unit" --description='Frame Workspace native viewer' --collect \
  --property=Type=exec --property=KillMode=control-group --property=Restart=no --property=TimeoutStopSec=3s \
  --setenv="HOME=$HOME" --setenv="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$uid}" \
  --setenv=DISPLAY=:0 --setenv=LC_ALL=C.UTF-8 \
  "$binary" --url-file "$secret" --overlay-key "$key" --control-file "$control" \
    --state-file "$state" --pose-file "$pose" --decoder "$decoder" \
    --conversion "$conversion" "${foveation_args[@]}" "${panel_args[@]}")
if [ -n "$session" ]; then
  launch_status=0
  python3 "$exit_helper" guarded-run --session "$session" -- systemd-run "${native_start[@]}" >/dev/null 2>&1 || launch_status=$?
elif systemd-run "${native_start[@]}" >/dev/null 2>&1; then
  launch_status=0
else
  launch_status=1
fi
if [ "$launch_status" -ne 0 ]; then
  if [ -n "$session" ] && ! session_command check; then
    echo 'Workspace Stop cancelled the native viewer start.' >&2
    exit 1
  fi
  if [ -n "$session" ]; then
    python3 "$exit_helper" guarded-run --session "$session" -- systemctl --user --no-block stop "$unit" >/dev/null 2>&1 || true
  fi
  echo 'Could not start the native Workspace service.' >&2
  exit 1
fi

for _ in $(seq 1 80); do
  if stopped; then
    echo 'The native Workspace viewer exited during startup. Inspect its scoped user-service journal.' >&2
    exit 1
  fi
  if [ -s "$state" ] && python3 - "$state" "$key" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    valid = s.get('ready') is True and s.get('overlayKey') == sys.argv[2]
    valid = valid and 0 < int(s.get('textureWidth', 0)) <= 5120 and 0 < int(s.get('textureHeight', 0)) <= 5120
    sys.exit(0 if valid else 1)
except (OSError, ValueError, TypeError):
    sys.exit(1)
PY
  then
    if "$vrcmd" --overlays 2>/dev/null | grep -Fq -- "'$key' -- '"; then
      echo "Native Workspace ready: registered OpenVR key $key in $unit"
      exit 0
    fi
  fi
  sleep 0.5
done
systemctl --user stop "$unit" >/dev/null 2>&1 || true
echo 'The native Workspace did not submit a decoded texture and register its overlay within 40 seconds; its service was stopped.' >&2
exit 1
