#!/bin/sh
# Removes the AWDL hold service and turns AWDL back on. Run with sudo:
#   sudo mac/awdl-hold/uninstall.sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0" >&2; exit 1; }
label=com.frame-workspace.awdl-hold
plist=/Library/LaunchDaemons/$label.plist
launchctl bootout system "$plist" 2>/dev/null || true
rm -f "$plist" /usr/local/libexec/frame-workspace-awdl-hold /var/run/frame-workspace-awdl-hold
/sbin/ifconfig awdl0 up
echo "Removed. AWDL (AirDrop, Handoff, Universal Control, Sidecar) is back on."
