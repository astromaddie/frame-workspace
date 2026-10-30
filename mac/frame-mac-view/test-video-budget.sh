#!/bin/sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/frame-video-budget.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

swiftc -swift-version 5 -o "$tmp/video-budget-tests" \
  -module-cache-path "$tmp/ModuleCache" \
  "$here/Sources/SharedVideoBudget.swift" \
  "$here/tests/VideoBudgetTests.swift"
"$tmp/video-budget-tests"

swiftc -swift-version 5 -o "$tmp/adapt-disabled-budget-tests" \
  -module-cache-path "$tmp/ModuleCache" \
  "$here/Sources/StreamPriority.swift" \
  "$here/Sources/Controller.swift" \
  "$here/Sources/SharedVideoBudget.swift" \
  "$here/tests/AdaptDisabledBudgetTests.swift"
FRAME_MAC_VIEW_ADAPT=0 "$tmp/adapt-disabled-budget-tests"
