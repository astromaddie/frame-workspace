#!/usr/bin/env bash
# The "Frame Workspace" entry on the SteamVR bar (FW-08). Runs on the Frame.
#   sync STATE_JSON  save companion state, ensure the dashboard is running, and
#                    return the requests made in the headset
#   show             ensure the dashboard is running and open it in SteamVR
#   stop             remove only the dashboard process and its generated images
#
# Only sync writes dash-state.json. The companion owns that file; show never
# changes it, so an offline launch naturally renders the panel's offline state.
set -eu
uid=$(id -u)
unit="frame-workspace-dash-$uid.service"
runtime="${XDG_RUNTIME_DIR:-/run/user/$uid}/frame-workspace"
requests="$runtime/requests"
images="$runtime/dash"
state="$runtime/dash-state.json"
binary="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/frame-workspace-dash"
exit_helper="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/workspace-exit-on-frame.py"

usage() {
  echo 'usage: workspace-dash-on-frame.sh sync STATE_JSON | show | stop' >&2
  exit 2
}

mode=${1:-}
case "$mode" in
  stop) [ "$#" -eq 1 ] || usage ;;
  show) [ "$#" -eq 1 ] || usage ;;
  sync) [ "$#" -eq 2 ] && [ "${#2}" -le 16384 ] || usage ;;
  *) usage ;;
esac

umask 077
mkdir -p "$runtime" "$requests" "$images"
chmod 700 "$runtime" "$requests" "$images"

if [ "$mode" = stop ]; then
  systemctl --user stop "$unit" >/dev/null 2>&1 || true
  rm -rf "$images"
  echo "Stopped $unit"
  exit 0
fi

if [ "$mode" = sync ]; then
  printf '%s\n' "$2" > "$state.tmp"
  mv -f "$state.tmp" "$state"
fi

ensure_dash() {
  if systemctl --user is-active --quiet "$unit"; then
    echo 'DASH_ACTIVE'
    return 0
  fi
  if [ ! -x "$binary" ]; then
    if [ "$mode" = sync ]; then echo 'DASH_MISSING'; return 0; fi
    echo "The Frame Workspace dashboard is missing: $binary" >&2
    return 1
  fi

  # Companion sync retries a failed panel at most every ten seconds. A direct
  # Steam library launch always gets an immediate attempt.
  if [ "$mode" = sync ]; then
    stamp="$runtime/dash-started"
    now=$(date +%s)
    last=$(cat "$stamp" 2>/dev/null || echo 0)
    case "$last" in *[!0-9]*|'') last=0 ;; esac
    if [ "$((now - last))" -lt 10 ]; then return 0; fi
  fi

  date +%s > "$runtime/dash-started"
  systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
  if systemd-run --user --unit="$unit" --description='Frame Workspace SteamVR bar panel' --collect \
      --property=Type=exec --property=Restart=no --property=TimeoutStopSec=3s \
      --setenv="HOME=$HOME" --setenv="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$uid}" \
      --setenv="XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}" \
      --setenv=LC_ALL=C.UTF-8 \
      "$binary" --state "$state" --request-dir "$requests" --images "$images" \
        --exit-helper "$exit_helper" >/dev/null 2>&1; then
    echo 'DASH_STARTED'
  else
    # A concurrent show/sync may have started this same unit after the first
    # active-state check. Accept it only after systemd confirms the unit.
    for _ in $(seq 1 20); do
      if systemctl --user is-active --quiet "$unit"; then
        echo 'DASH_ACTIVE'
        return 0
      fi
      sleep 0.1
    done
    if [ "$mode" = sync ]; then echo 'DASH_FAILED'; return 0; fi
    echo 'Could not start the Frame Workspace dashboard service.' >&2
    return 1
  fi
}

ensure_dash

if [ "$mode" = show ]; then
  vrcmd_bin=${FRAME_WORKSPACE_VRCMD:-/opt/steamvr/bin/linuxarm64/vrcmd}
  if [ ! -x "$vrcmd_bin" ]; then vrcmd_bin=$(command -v "$vrcmd_bin" 2>/dev/null || true); fi
  if [ -z "$vrcmd_bin" ] || [ ! -x "$vrcmd_bin" ]; then
    echo 'The Frame is missing vrcmd, required to open the Workspace dashboard.' >&2
    exit 3
  fi
  for _ in $(seq 1 40); do
    if ! systemctl --user is-active --quiet "$unit"; then
      echo "The Frame Workspace dashboard service exited during startup: $unit" >&2
      exit 1
    fi
    overlays=$("$vrcmd_bin" --overlays 2>/dev/null || true)
    if printf '%s\n' "$overlays" | grep -Fq "'com.frame-workspace.dash' -- 'Frame Workspace'"; then
      if "$vrcmd_bin" --showdashboard com.frame-workspace.dash; then
        echo 'Frame Workspace dashboard opened.'
        exit 0
      fi
      echo 'SteamVR did not open the Frame Workspace dashboard.' >&2
      exit 1
    fi
    sleep 0.25
  done
  echo 'The Frame Workspace dashboard did not register with SteamVR within 10 seconds.' >&2
  exit 1
fi

dock_log() {
  if command -v logger >/dev/null 2>&1; then logger -t frame-workspace -- "dock: $*" >/dev/null 2>&1 || true; fi
}

# A library request can outlive the companion connection that authorized it.
# Drop only our launcher-owned requests after ten seconds; headset panel
# requests retain their established behavior.
now=$(date +%s)
for f in "$requests"/*.json; do
  [ -f "$f" ] || continue
  case "${f##*/}" in
    launcher-*)
      modified=$(python3 -c 'import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))' "$f" 2>/dev/null || echo "$now")
      case "$modified" in *[!0-9]*|'') modified=$now ;; esac
      if [ "$((now - modified))" -gt 10 ]; then rm -f -- "$f"; continue; fi
      ;;
  esac
  case "${f##*/}" in
    spatial-*)
      # A shelf click on its way to the companion: one line in the journal
      # (journalctl --user -t frame-workspace | grep dock:).
      body=$(head -c 512 "$f" | tr -d '\n')
      dock_log "poller picked up ${f##*/} $body"
      ;;
  esac
  head -c 512 "$f"
  echo
  rm -f -- "$f"
  case "${f##*/}" in spatial-*) dock_log "poller forwarded ${f##*/} to the companion" ;; esac
done
