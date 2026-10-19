#!/usr/bin/env bash
# Owned native Workspace overlay. Runs on the Frame; does not restart SteamVR.
set -eu
uid=$(id -u)
unit="frame-workspace-viewer-$uid.service"
runtime="${XDG_RUNTIME_DIR:-/run/user/$uid}/frame-workspace"
install="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native"
key="com.frame-workspace.native.$uid.workspace"

case "${1:-}" in
  focus)
    [ "$#" -eq 1 ] || exit 2
    python3 - "$runtime" "$unit" "$key" focus <<'PY'
import fcntl, json, math, os, pathlib, subprocess, sys
p = pathlib.Path(sys.argv[1])
unit, key, action = sys.argv[2:]
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
        if int(systemd('MainPID')) <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
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
    python3 - "$runtime" "$unit" "$key" "$2" "$3" <<'PY'
import fcntl, json, math, os, pathlib, re, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
unit, key, raw_width, raw_distance = sys.argv[2:]
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
        if int(systemd('MainPID')) <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
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
    python3 - "$runtime" "$unit" "$key" "$2" "$3" "$4" "$5" "$6" "$7" <<'PY'
import fcntl, json, math, os, pathlib, re, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
unit, key = sys.argv[2:4]
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
        if int(systemd('MainPID')) <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
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
    python3 - "$runtime" "$unit" "$key" "$2" <<'PY'
import fcntl, json, math, os, pathlib, re, subprocess, sys, time
p = pathlib.Path(sys.argv[1])
unit, key, raw = sys.argv[2:]
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
        if int(systemd('MainPID')) <= 0:
            return None
        s = json.loads((p / 'state.json').read_text())
        placement = s.get('placement')
        if (s.get('ready') is not True or s.get('overlayKey') != key or
                not isinstance(s.get('state'), str) or not isinstance(placement, dict)):
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
  start) ;;
  *) echo 'usage: workspace-native-on-frame.sh start URL WIDTH HEIGHT TAG [software|v4l2] [cpu|gpu] [0|1|gaze foveation] [U,V|scan diagnostic gaze] | focus | adjust WIDTH DISTANCE | pose RIGHT UP CLOSER YAW PITCH ROLL | curvature VALUE' >&2; exit 2 ;;
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
mkdir -p "$runtime"
chmod 700 "$runtime"
secret="$runtime/view-$tag.url"
state="$runtime/state.json"
control="$runtime/control.json"
printf '%s\n' "$url" > "$secret"
rm -f "$state" "$control"
trap 'rm -f "$secret"' EXIT
pose="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/workspace-pose.json"
vrcmd=/opt/steamvr/bin/linuxarm64/vrcmd
if ! systemd-run --user --unit="$unit" --description='Frame Workspace native viewer' --collect \
  --property=Type=exec --property=KillMode=control-group --property=Restart=no --property=TimeoutStopSec=3s \
  --setenv="HOME=$HOME" --setenv="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$uid}" \
  --setenv=DISPLAY=:0 --setenv=LC_ALL=C.UTF-8 \
  "$binary" --url-file "$secret" --overlay-key "$key" --control-file "$control" \
    --state-file "$state" --pose-file "$pose" --decoder "$decoder" \
    --conversion "$conversion" "${foveation_args[@]}" >/dev/null 2>&1; then
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
