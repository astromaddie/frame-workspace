#!/usr/bin/env bash
# The "Frame Workspace" entry on the SteamVR bar (FW-08). Runs on the Frame,
# sent by the Mac companion once a second while it is connected.
#   sync STATE_JSON  save the companion's state for the panel, start the panel
#                    if it isn't running, then print (and remove) each request
#                    made in the headset, one JSON object per line
#   stop             remove the panel from the bar
# The panel itself only ever writes fixed requests (start/stop the Workspace,
# recenter, close a window); the companion decides what to do with them.
set -eu
uid=$(id -u)
unit="frame-workspace-dash-$uid.service"
runtime="${XDG_RUNTIME_DIR:-/run/user/$uid}/frame-workspace"
requests="$runtime/requests"
images="$runtime/dash"
state="$runtime/dash-state.json"
binary="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/frame-workspace-dash"

case "${1:-}" in
  stop)
    [ "$#" -eq 1 ] || exit 2
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
    rm -rf "$images" "$state"
    echo "Stopped $unit"
    exit 0
    ;;
  sync)
    [ "$#" -eq 2 ] && [ "${#2}" -le 16384 ] || exit 2
    ;;
  *)
    echo 'usage: workspace-dash-on-frame.sh sync STATE_JSON | stop' >&2
    exit 2
    ;;
esac

umask 077
mkdir -p "$requests" "$images"
chmod 700 "$runtime" "$requests" "$images"
printf '%s\n' "$2" > "$state.tmp"
mv -f "$state.tmp" "$state"

if ! systemctl --user is-active --quiet "$unit"; then
  if [ ! -x "$binary" ]; then
    echo 'DASH_MISSING'
  else
    # A panel that keeps failing is retried every 10 s, not every second.
    stamp="$runtime/dash-started"
    now=$(date +%s)
    last=$(cat "$stamp" 2>/dev/null || echo 0)
    case "$last" in *[!0-9]*|'') last=0 ;; esac
    if [ "$((now - last))" -ge 10 ]; then
      echo "$now" > "$stamp"
      systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
      if systemd-run --user --unit="$unit" --description='Frame Workspace SteamVR bar panel' --collect \
        --property=Type=exec --property=Restart=no --property=TimeoutStopSec=3s \
        --setenv="HOME=$HOME" --setenv="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$uid}" \
        --setenv=LC_ALL=C.UTF-8 \
        "$binary" --state "$state" --request-dir "$requests" --images "$images" >/dev/null 2>&1; then
        echo 'DASH_STARTED'
      else
        echo 'DASH_FAILED'
      fi
    fi
  fi
fi

for f in "$requests"/*.json; do
  [ -f "$f" ] || continue
  head -c 512 "$f"
  echo
  rm -f "$f"
done
