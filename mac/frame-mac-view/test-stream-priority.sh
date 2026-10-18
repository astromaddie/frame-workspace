#!/bin/sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/frame-stream-priority.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

swiftc -swift-version 5 -o "$tmp/stream-priority-tests" \
  "$here/Sources/StreamPriority.swift" \
  "$here/Sources/Controller.swift" \
  "$here/tests/StreamPriorityTests.swift"
"$tmp/stream-priority-tests"
