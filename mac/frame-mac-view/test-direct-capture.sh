#!/bin/sh
# Unit/GPU checks by default. --host also creates and removes its own small
# non16:9 display, checks actual crop pixels and exercises production SCK/VT/WS.
# Requires Screen Recording permission for --host. No installed binary/device use.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d /tmp/frame-direct-check.XXXXXX)
trap 'rm -rf "$work"' EXIT HUP INT TERM
xcrun swiftc -O -swift-version 5 "$here/Sources/Foveation.swift" "$here/Sources/DirectComposite.swift" \
  "$here/tests/DirectCaptureTests.swift" -o "$work/tests"
"$work/tests"
if [ "${1:-}" = --host ]; then
  xcrun swiftc -O -swift-version 5 -import-objc-header "$here/Sources/CGVirtualDisplay.h" \
    "$here/Sources/Capture.swift" "$here/Sources/Foveation.swift" "$here/Sources/DirectComposite.swift" \
    "$here/Sources/DirectCapture.swift" "$here/Sources/Stats.swift" "$here/Sources/WindowCloseRecovery.swift" \
    "$here/tests/DirectCaptureHostProbe.swift" -o "$work/probe"
  evidence=${2:-$work/evidence}
  mkdir -p "$evidence"
  "$work/probe" --report "$evidence/direct-crop.json"
  "$work/probe" --continuity --separate-periphery --report "$evidence/direct-continuity.json"
  "$here/build.sh" "$work/companion"
  python3 "$here/tests/test_direct_session.py" "$work/companion" "$work/probe" "$evidence"
fi
