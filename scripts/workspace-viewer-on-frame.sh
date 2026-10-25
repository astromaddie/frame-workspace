#!/usr/bin/env bash
# Own the Mac Workspace viewer in its own Gamescope OpenVR overlay. This path
# runs on the Frame; ordinary Mac window/display viewers use the existing
# global Gamescope launcher.
set -eu

usage() {
  echo "usage: workspace-viewer-on-frame.sh [--session ID] start URL WIDTH HEIGHT TAG [browser flags...] | stop" >&2
  exit 2
}

session=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --session) [ "$#" -ge 2 ] || usage; session=$2; shift 2 ;;
    *) break ;;
  esac
done
if [ -n "$session" ]; then
  case "$session" in *[!0-9a-f]*) usage ;; esac
  [ "${#session}" -eq 32 ] || usage
fi
exit_helper="${XDG_DATA_HOME:-$HOME/.local/share}/frame-workspace/native/workspace-exit-on-frame.py"
session_command() {
  [ -n "$session" ] || return 0
  [ -x "$exit_helper" ] || { echo 'The local Workspace Stop helper is missing.' >&2; return 3; }
  python3 "$exit_helper" "$1" --session "$session"
}

unit_property() {
  property=$1
  systemctl --user show "$unit" "--property=$property" --value
}

unit_is_stopped() {
  load_state=$(unit_property LoadState 2>/dev/null) || return 2
  [ "$load_state" = not-found ] && return 0
  [ "$load_state" = loaded ] || return 2
  active_state=$(unit_property ActiveState 2>/dev/null) || return 2
  main_pid=$(unit_property MainPID 2>/dev/null) || return 2
  case "$active_state" in inactive|failed) ;; *) return 1 ;; esac
  [ "$main_pid" = 0 ] || return 1
  return 0
}

print_redacted_log() {
  [ -r "$log" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "${line//"$url"/[redacted URL]}"
  done < "$log" >&2
}

stop_viewer() {
  uid=$(id -u)
  unit="frame-workspace-viewer-$uid.service"
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "The Frame needs systemctl to stop its Workspace viewer service." >&2
    exit 1
  fi
  if unit_is_stopped; then
    if [ "${1:-}" != quiet ]; then echo "Workspace viewer already stopped ($unit)"; fi
    return 0
  fi
  if ! systemctl --user kill --signal=TERM --kill-whom=all "$unit" >/dev/null 2>&1; then
    if unit_is_stopped; then
      if [ "${1:-}" != quiet ]; then echo "Workspace viewer stopped ($unit)"; fi
      return 0
    fi
  fi
  systemctl --user --no-block stop "$unit" >/dev/null 2>&1 || true
  for _ in $(seq 1 15); do
    if unit_is_stopped; then
      if [ "${1:-}" != quiet ]; then echo "Workspace viewer stopped ($unit)"; fi
      return 0
    fi
    sleep 0.2
  done

  # TimeoutStopSec is three seconds. If the unit is still deactivating after
  # that grace, SIGKILL only this transient service's cgroup.
  systemctl --user kill --signal=KILL --kill-whom=all "$unit" >/dev/null 2>&1 || true
  systemctl --user --no-block stop "$unit" >/dev/null 2>&1 || true
  for _ in $(seq 1 10); do
    if unit_is_stopped; then
      if [ "${1:-}" != quiet ]; then echo "Workspace viewer stopped ($unit)"; fi
      return 0
    fi
    sleep 0.2
  done
  echo "The Frame Workspace viewer service is still active or deactivating: $unit" >&2
  return 1
}

case "${1:-}" in
  stop)
    [ "$#" -eq 1 ] || usage
    stop_viewer
    exit $?
    ;;
  start) ;;
  *) usage ;;
esac
shift
[ "$#" -ge 4 ] || usage
url=$1
w=$2
h=$3
tag=$4
shift 4
case "$w:$h" in
  *[!0-9:]*) echo "Invalid Workspace viewer dimensions." >&2; exit 2 ;;
esac
case "$tag" in fc????????) ;; *) echo "Invalid Workspace viewer window tag." >&2; exit 2 ;; esac
case "$tag" in *[!0-9a-f]*) echo "Invalid Workspace viewer window tag." >&2; exit 2 ;; esac
[ "${#w}" -le 4 ] && [ "${#h}" -le 4 ] || { echo "Workspace viewer dimensions exceed the supported limit." >&2; exit 2; }
[ "$w" -ge 320 ] && [ "$h" -ge 200 ] || { echo "Invalid Workspace viewer dimensions." >&2; exit 2; }
[ "$w" -le 5120 ] && [ "$h" -le 5120 ] || { echo "Workspace viewer long edge exceeds 5120 pixels." >&2; exit 2; }
if [ "$((10#$w * 10#$h))" -gt 16777216 ]; then
  echo "Workspace viewer exceeds the 16,777,216-pixel capture limit." >&2
  exit 2
fi
physical_width=5.34
if [ "$((10#$h))" -gt "$((10#$w))" ]; then
  physical_width=2.67
elif [ "$((10#$w))" -lt "$((2 * 10#$h))" ]; then
  # Keep standard and near-square landscape panels at a manageable width.
  physical_width=3.0
fi

# Gamescope uploads its black backing through a fixed 1920x1080 RGBA
# staging buffer. Keep that backing small and at the client's aspect ratio.
# The nested X display and Chrome surface retain the requested dimensions;
# the OpenVR backend can submit the application's GPU layer directly.
out_w=$((10#$w))
out_h=$((10#$h))
while [ "$((out_w * out_h))" -gt 2073600 ]; do
  out_w=$((out_w / 2))
  out_h=$((out_h / 2))
done

export LC_ALL=C.UTF-8
uid=$(id -u)
unit="frame-workspace-viewer-$uid.service"
display_file="/tmp/frame-workspace-viewer-$uid-$tag.display"
log="/tmp/frame-workspace-viewer-$uid-$tag.log"
profile="$HOME/.local/share/frame-workspace/workspace-viewer"
if ! command -v systemd-run >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
  echo "The Frame needs a user systemd manager to own the Workspace viewer." >&2
  exit 3
fi
if ! command -v gamescope >/dev/null 2>&1; then
  echo "The Frame's Gamescope executable is unavailable; Workspace needs its dedicated OpenVR viewer." >&2
  exit 3
fi
if ! command -v xwininfo >/dev/null 2>&1 || ! command -v xprop >/dev/null 2>&1; then
  echo "The Frame is missing X11 window inspection tools required to verify Workspace startup." >&2
  exit 3
fi
if [ -n "$session" ]; then session_command begin; fi
vrcmd_bin=${FRAME_WORKSPACE_VRCMD:-/opt/steamvr/bin/linuxarm64/vrcmd}
if [ ! -x "$vrcmd_bin" ]; then
  vrcmd_bin=$(command -v "$vrcmd_bin" 2>/dev/null || true)
fi
if [ -z "$vrcmd_bin" ] || [ ! -x "$vrcmd_bin" ]; then
  echo "The Frame is missing vrcmd, required to verify the Workspace OpenVR overlay." >&2
  exit 3
fi

browser_args=(--ozone-platform=x11 --force-device-scale-factor=1 --no-first-run --no-default-browser-check
  --password-store=basic --disable-session-crashed-bubble --noerrdialogs --disable-infobars
  --start-fullscreen --disable-background-mode --disable-features=Translate,MediaRouter --autoplay-policy=no-user-gesture-required
  "--window-size=$w,$h" "$@" "--app=$url")
if [ -x "$HOME/chromium-xr/chrome" ]; then
  browser=("$HOME/chromium-xr/chrome" "--user-data-dir=$profile" "${browser_args[@]}")
elif command -v flatpak >/dev/null 2>&1 && flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  profile="$HOME/.var/app/org.chromium.Chromium/data/frame-workspace/workspace-viewer"
  mkdir -p "$profile"
  browser=(flatpak run org.chromium.Chromium "--user-data-dir=$profile" "${browser_args[@]}")
elif command -v flatpak >/dev/null 2>&1 && flatpak --user info com.google.Chrome >/dev/null 2>&1; then
  profile="$HOME/.var/app/com.google.Chrome/data/frame-workspace/workspace-viewer"
  mkdir -p "$profile"
  browser=(flatpak run --user com.google.Chrome "--user-data-dir=$profile" "${browser_args[@]}")
else
  echo "NO_BROWSER"
  exit 3
fi
mkdir -p "$profile"
rm -f "$display_file" "$log"

# The unit name is reserved for this feature per user. A currently active
# unit can only be the previous Workspace viewer, never a window viewer.
if ! stop_viewer quiet; then
  echo "The previous Workspace viewer did not stop; refusing to replace its OpenVR overlay." >&2
  exit 1
fi

# Gamescope supplies the nested X11 display and auth cookie to its client.
# Record those inside the unit so readiness checks only this viewer's window.
inner=(/bin/bash -c 'display=$1; shift; umask 077; printf "%s\n%s\n" "$DISPLAY" "${XAUTHORITY:-}" > "$display"; unset WAYLAND_DISPLAY; export QT_QPA_PLATFORM=xcb GDK_BACKEND=x11 MOZ_ENABLE_WAYLAND=0; exec "$@"' \
  frame-workspace-viewer "$display_file" "${browser[@]}")
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$uid}"
viewer_start=(--user --unit="$unit" --description="Frame Workspace viewer" --collect \
    --property=Type=exec --property=KillMode=control-group --property=Restart=no \
    --property=TimeoutStopSec=3s \
    --setenv="HOME=$HOME" --setenv="XDG_RUNTIME_DIR=$runtime_dir" --setenv="LC_ALL=C.UTF-8" \
    env -u DISPLAY -u WAYLAND_DISPLAY gamescope --backend openvr \
      --nested-width "$w" --nested-height "$h" --output-width "$out_w" --output-height "$out_h" \
      --vr-overlay-show-immediately \
      --vr-overlay-key "com.frame-control.workspace.$uid.$tag" \
      --vr-overlay-explicit-name "Frame Workspace" --vr-overlay-enable-control-bar \
      --vr-overlay-physical-width "$physical_width" \
      -- "${inner[@]}")
if [ -n "$session" ]; then
  start_status=0
  python3 "$exit_helper" guarded-run --session "$session" -- systemd-run "${viewer_start[@]}" >"$log" 2>&1 || start_status=$?
elif systemd-run "${viewer_start[@]}" >"$log" 2>&1; then
  start_status=0
else
  start_status=1
fi
if [ "$start_status" -ne 0 ]; then
  if [ -n "$session" ]; then
    if python3 "$exit_helper" guarded-run --session "$session" -- systemctl --user --no-block stop "$unit" >/dev/null 2>&1; then
      for _ in $(seq 1 15); do unit_is_stopped && break; sleep 0.2; done
    fi
  else
    stop_viewer quiet || true
  fi
  echo "Could not start the dedicated Frame Workspace OpenVR viewer:" >&2
  print_redacted_log
  rm -f "$display_file" "$log"
  exit 1
fi

for _ in $(seq 1 60); do
  if unit_is_stopped; then
    echo "The Frame Workspace viewer exited during startup:" >&2
    print_redacted_log
    rm -f "$display_file" "$log"
    exit 1
  fi
  if [ -s "$display_file" ]; then
    IFS= read -r display < "$display_file" || true
    auth=$(sed -n '2p' "$display_file")
    case "$display" in :[0-9]*) ;; *) display= ;; esac
    if [ -n "$display" ]; then
      export DISPLAY="$display"
      if [ -n "$auth" ]; then export XAUTHORITY="$auth"; else unset XAUTHORITY; fi
      for win in $(xwininfo -root -children 2>/dev/null | awk '/^ +0x/ {print $1}'); do
        xprop -id "$win" _NET_WM_NAME WM_NAME 2>/dev/null | grep -qF "[$tag]" || continue
        geom=$(xwininfo -id "$win" 2>/dev/null) || continue
        printf '%s\n' "$geom" | grep -q 'Map State: IsViewable' || continue
        actual_w=$(printf '%s\n' "$geom" | awk '/Width:/ {print $2; exit}')
        actual_h=$(printf '%s\n' "$geom" | awk '/Height:/ {print $2; exit}')
        if [ "$actual_w" = "$w" ] && [ "$actual_h" = "$h" ]; then
          overlay_key="com.frame-control.workspace.$uid.$tag"
          if "$vrcmd_bin" --overlays 2>/dev/null | grep -Fq -- "'$overlay_key' -- '"; then
            echo "Workspace viewer ready: ${actual_w}x${actual_h} X11 window and registered OpenVR key $overlay_key in $unit"
            rm -f "$display_file" "$log"
            exit 0
          fi
        fi
      done
    fi
  fi
  sleep 0.5
done
if ! stop_viewer; then
  echo "The dedicated Workspace viewer did not become ready and its service could not be stopped." >&2
else
  echo "The dedicated Workspace service started, but its ${w}x${h} viewer window or registered OpenVR overlay did not become ready; the service was stopped." >&2
fi
print_redacted_log
rm -f "$display_file" "$log"
exit 1
