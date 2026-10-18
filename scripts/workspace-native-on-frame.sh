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
    [ "$(systemctl --user show "$unit" --property=ActiveState --value)" = active ] || {
      echo 'The native Workspace viewer is stopped; use Show Workspace again.' >&2; exit 1;
    }
    python3 - "$runtime" <<'PY'
import fcntl, json, os, pathlib, sys, time
p = pathlib.Path(sys.argv[1])
with (p / 'control.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    target = p / 'control.json'
    revision = time.time_ns()
    if target.exists():
        revision = max(revision, int(json.loads(target.read_text()).get('revision', 0)) + 1)
    tmp = p / ('control.' + str(os.getpid()) + '.tmp')
    # Existing Show again means bring this panel back in front of the wearer.
    # Ordinary process startup restores its saved world transform unchanged.
    tmp.write_text(json.dumps({'revision': revision, 'focus': True, 'recenter': True}))
    os.chmod(tmp, 0o600)
    tmp.replace(target)
print('Native Workspace focus requested')
PY
    exit 0
    ;;
  start) ;;
  *) echo 'usage: workspace-native-on-frame.sh start URL WIDTH HEIGHT TAG [software|v4l2] [cpu|gpu] | focus' >&2; exit 2 ;;
esac
[ "$#" -ge 5 ] && [ "$#" -le 7 ] || exit 2
url=$2
w=$3
h=$4
tag=$5
decoder=${6:-software}
case "$decoder" in software|v4l2) ;; *) echo 'Decoder must be software or v4l2.' >&2; exit 2 ;; esac
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
  load=$(systemctl --user show "$unit" --property=LoadState --value 2>/dev/null) || return 1
  [ "$load" = not-found ] && return 0
  active=$(systemctl --user show "$unit" --property=ActiveState --value) || return 1
  pid=$(systemctl --user show "$unit" --property=MainPID --value) || return 1
  case "$active" in inactive|failed) [ "$pid" = 0 ] ;; *) return 1 ;; esac
}
if ! stopped; then
  systemctl --user --no-block stop "$unit" >/dev/null
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
    --conversion "$conversion" >/dev/null 2>&1; then
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
