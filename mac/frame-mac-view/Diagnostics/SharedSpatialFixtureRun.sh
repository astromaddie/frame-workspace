#!/bin/sh
# Real-helper acceptance, restricted to owned fixture windows. No headset use.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
scratch=/private/tmp/claude-501/-Users-mads-code-frame-workspace/d6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/wp-a
mkdir -p "$scratch"
work=$(mktemp -d "$scratch/fixture.XXXXXX")
preferences=shared-spatial-fixture
preferences_path="$HOME/Library/Preferences/$preferences.plist"
had_preferences=0
if [ -f "$preferences_path" ]; then
  cp "$preferences_path" "$work/preferences-before.plist"
  had_preferences=1
fi
cleanup_done=0
cleanup() {
  [ "$cleanup_done" -eq 0 ] || return 0
  cleanup_done=1
  if [ "$had_preferences" -eq 1 ]; then
    defaults import "$preferences" "$work/preferences-before.plist" >/dev/null 2>&1 || true
  else
    defaults delete "$preferences" >/dev/null 2>&1 || true
    rm -f "$preferences_path"
  fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM
memory_gate() {
  tries=0
  while [ "$tries" -lt 5 ]; do
    line=$(memory_pressure | tail -1)
    echo "$line" >&2
    free=$(echo "$line" | sed 's/.*: //;s/%//')
    [ "$free" -ge 15 ] && return 0
    tries=$((tries + 1))
    [ "$tries" -ge 5 ] && break
    sleep 60
  done
  echo 'Free memory remained below 15%; stopped.' >&2
  return 1
}
memory_gate
if [ "$#" -gt 0 ]; then helper=$1; else
  helper="$work/frame-mac-view"
  sh "$here/../build.sh" "$helper" >&2
fi
memory_gate
xcrun swiftc -O -swift-version 5 -module-cache-path "$work/modules" \
  "$here/SharedSpatialFixture.swift" -o "$work/shared-spatial-fixture"
memory_gate
cd "$root"
PYTHONDONTWRITEBYTECODE=1 python3 "$here/SharedSpatialFixtureRun.py" \
  --work "$work" --helper "$helper" --fixture "$work/shared-spatial-fixture"
