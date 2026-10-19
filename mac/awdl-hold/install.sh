#!/bin/sh
# Installs the AWDL hold service. Run with sudo:
#   sudo mac/awdl-hold/install.sh
# Remove it with mac/awdl-hold/uninstall.sh.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
label=com.frame-workspace.awdl-hold
plist=/Library/LaunchDaemons/$label.plist
mkdir -p /usr/local/libexec
# Root-owned and not writable by the user, since launchd runs it as root.
install -o root -g wheel -m 755 "$here/frame-workspace-awdl-hold" /usr/local/libexec/frame-workspace-awdl-hold
install -o root -g wheel -m 644 "$here/$label.plist" "$plist"
launchctl bootout system "$plist" 2>/dev/null || true
launchctl bootstrap system "$plist"
echo "Installed. AWDL is held down while a Frame Workspace runs."
