#!/bin/sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/frame-capture-recovery.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
xcrun swiftc -swift-version 5 -module-cache-path "$tmp/ModuleCache" -o "$tmp/window-close-recovery-tests" \
  "$here/Sources/WindowCloseRecovery.swift" \
  "$here/tests/WindowCloseRecoveryTests.swift"
"$tmp/window-close-recovery-tests"
