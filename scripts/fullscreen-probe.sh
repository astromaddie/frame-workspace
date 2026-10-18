#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
out="${FRAME_FULLSCREEN_PROBE_BIN:-/tmp/frame-workspace-fullscreen-probe}"
module_cache="${FRAME_FULLSCREEN_PROBE_MODULE_CACHE:-/tmp/frame-workspace-swift-module-cache}"
mkdir -p "$module_cache"
xcrun swiftc -O -swift-version 5 -target arm64-apple-macos14.0 \
  -module-cache-path "$module_cache" \
  "$repo/mac/frame-mac-view/Sources/Stats.swift" \
  "$repo/mac/frame-mac-view/Sources/Capture.swift" \
  "$repo/mac/frame-mac-view/Diagnostics/FullscreenProbe.swift" \
  -o "$out"
exec "$out" "$@"
