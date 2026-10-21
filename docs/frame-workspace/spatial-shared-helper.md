# Shared Spatial Mac Helper (FW-15 Option B, Package A)

This package replaces Spatial Mode's per-window display pool with one hidden
`CGVirtualDisplay`: vendor `0xF0C2`, product `0x5E9D`, 3600×2250 points at 2×,
60 Hz. `FRAME_SPATIAL_SHARED_SIZE=WxH` overrides the experimental size.
The display sits below the built-in with a 128-point shared edge. The built-in
remains main and uses the existing black cover; Spatial mirroring is removed.

Eligible windows overlap at the usable display's top-left. Window streams use
`spatial:<wid>` and independent window capture with child windows included.
Focus raises the chosen window, restores its remembered pointer, and moves an
event-tap fence around the window, menu bar and visible dropdowns. A focused
window can capture at 60 fps; other live windows are capped at 30 fps. Key-down
and mouse-down raise the spatial target before posting input.

The helper exposes `menubar`, `status`, and `menu:0` through `menu:3`. A watcher
splits the menu strip at the leftmost layer-25 status item and retargets dropdown
slots using current visibility and geometry. Window-capture startup failure, or no first picture within three seconds, falls
back to a display crop. Control Center's layer-22 surface is measured in BGRA and
cropped to opaque bounds. Surface transitions invalidate queued encoded frames
and send versioned `surface` messages before fresh pictures.

When Displays Have Separate Spaces is off, the strip comes from the built-in;
that display capture excludes the helper's cover window. The display-choice
policy has a unit test. The system preference is never changed by verification.

Original window frames are atomically persisted before moving windows in
`~/Library/Application Support/Frame Control/spatial-originals.json`. On server
startup, pending originals are restored under the per-user locks. Stop ends
captures, reacquires AX windows, restores their frames, removes the display, and
verifies physical layout and window restoration. `FRAME_SPATIAL_ONLY_PIDS=pid,...`
restricts both eligibility and startup recovery to the listed processes.

Desktop's `window:`, `display:`, `separate:` and test paths keep their existing
capture, input and display behavior. Desktop separate streams are restored before
Spatial starts. Spatial streams and auxiliary tickets are revoked on Spatial stop.

## Reproduction

Use the capsule scratch directory as `work`. Check `memory_pressure | tail -1`
before every build or live run; below 15%, wait one minute and retry, at most five
times. Ignore swap size.

```sh
work=$(python3 -c 'from tests.test_spatial_shared_agent import SCRATCH; print(SCRATCH)')
mkdir -p "$work"
mac/frame-mac-view/build.sh "$work/frame-mac-view"
FRAME_SPATIAL_TEST_BINARY="$work/frame-mac-view" python3 -m unittest tests/test_spatial_shared_agent.py -v
mac/frame-mac-view/Diagnostics/SharedSpatialFixtureRun.sh "$work/frame-mac-view"
```

`SharedSpatialFixtureRun.sh` compiles an owned fixture with a new `--60hz` mode,
starts the real helper with only that fixture PID eligible, exercises streams,
focus and input, and compares all other application windows and display modes,
origins, main identity and mirroring after teardown. Its temporary binaries,
module cache, state and logs are deleted by its exit trap. It never prompts for
permissions, changes system settings, or accesses a headset. An already-permitted
helper binary may be passed as its first argument.

Pure Swift checks use `xcrun swiftc -swift-version 5 -module-cache-path
"$work/modules" SOURCES TEST -o "$work/TEST"`, then run that executable:

| Test | Source Files (Under `Sources/`) |
| --- | --- |
| SpatialPolicyTests | SpatialPolicy.swift |
| SpatialSharedPolicyTests | SpatialSharedPolicy.swift |
| SpatialCursorFenceTests | SpatialCursorFence.swift |
| SpatialStreamsTests | SpatialStreams.swift |
| StreamPriorityTests | StreamPriority.swift, Controller.swift |
| AppsStateTests | AppsState.swift |
| WindowCloseRecoveryTests | WindowCloseRecovery.swift |
| FoveationTests | Foveation.swift, Encoder.swift, Stats.swift |
| DirectCaptureTests | Foveation.swift, DirectComposite.swift |

## Verification Results

Verified locally on Apple M4, macOS 26.3.1:

| Check | Result |
| --- | --- |
| `mac/frame-mac-view/build.sh` (`-O`, arm64, macOS 14 target) | Pass, no compiler warnings |
| Existing `test_macview.Agent` | 13 run: 12 passed, 1 permission-dependent skip, 0 failures/errors |
| `python3 -m unittest tests/test_spatial_shared_agent.py` | 8 passed, 0 failures/errors |
| Nine Swift executables listed above | 9 passed, 0 failures; final fence source recompiled and passed |
| Fence geometry checks | 5 passed |
| Shared policy checks | 19 passed, including separate-Spaces display choice and alpha cropping |

The existing Agent harness was unchanged. Its temporary-directory request was
redirected into capsule scratch before running the unittest loader, and its
optimized binary was copied before teardown for live verification. Reproduce that
suite with `TMPDIR="$work" PYTHONPATH=tests python3` and this loader:

```python
import tempfile, unittest
import test_macview
from test_spatial_shared_agent import SCRATCH
make_temp = tempfile.mkdtemp

def scratch_temp(*args, **kwargs):
    if kwargs.get("dir") == test_macview.ROOT:
        kwargs["dir"] = SCRATCH
    return make_temp(*args, **kwargs)

tempfile.mkdtemp = scratch_temp
result = unittest.TextTestRunner().run(
    unittest.defaultTestLoader.loadTestsFromTestCase(test_macview.Agent))
raise SystemExit(not result.wasSuccessful())
```

The tested optimized helper's SHA-256 was
`51c3168567e20f2e46b44b0d5a72a1edc7452e1ba0b395b7138a800a78372e77`.
Its binary is temporary; the source, fixture runner, tests and this report are the
deliverables.

The existing Agent suite ran before the final Spatial-only timeout, PID-observer
filter, stable window ordering and error-text changes. The final optimized build
was warning-free, and the shared-helper suite was rerun against the exact binary
used for the final fixture run (8/8 passed).

Verified — final fixture run:

| Measurement | Result |
| --- | --- |
| Shared display | One reported display, 3600×2250 points, scale 2; origin (1672,1169) |
| Independent window stream | 1018 H.264 pictures total; 1280×864 encoded pixels |
| Menu-bar stream | 14 pictures; 1600×16 encoded pixels |
| Status stream | 13 pictures; 1328×60 encoded pixels |
| `menu:0` stream | 2 pictures; 318×68 encoded pixels |
| Covered-window input | Click down/up and `a` arrived only in fixture window 0 |
| Fence | Enabled; forbidden wallpaper movement ended at (1928,1415), inside the focused window |
| Normal stop | 3/3 fixture frames restored; `removed: true` |
| Forced helper crash and new-server startup | 3/3 fixture frames restored from persisted originals |
| Other application windows | 121/121 nonempty frames matched exactly |
| Physical display layout | Exact match, including mode, backing size, refresh, origin, main and mirror state |
| Final cleanup | No fixture/helper process, spatial display or persisted-originals file remained |

The dropdown was opened with normal pointer movement followed by a click through
the menu-bar session. Its global rect was (1876,1200,159,34), anchored to the menu
bar. The final menu capture started as an independent window stream; the fallback
and timeout branches were not forced in that successful run.

Verified — rate limitation: 100 encoded pictures arrived over 5.00568 seconds,
**19.98 fps**, with the fixture's 60 Hz timer mode and the focused source requesting
60 fps. The **60 fps target was not achieved**. This measures H.264 pictures at a
local WebSocket receiver; capture-only cadence was not separately timed. Earlier
runs with different code/load were not matched baselines. The reason for the
shortfall remains unresolved; no sustained 60 fps or headset throughput is claimed.

The inventory excludes zero-sized surfaces and the system cursor canvas
(`CursorUIViewService`), which is created/removed as the cursor moves. The final
baseline contained six empty surfaces and ten cursor surfaces. Earlier attempts
also observed two Quick Look windows disappearing after fixture activation; those
attempts failed the all-window comparison. No AX mutation targeted their processes,
but their disappearance prevents a general claim that adding/focusing a hidden
display can never affect transient UI on a concurrently used Mac. The final 121
application-window comparison passed without such differences.

Verified by source and pure tests, not a live setting change: separate-Spaces-off
selects the built-in menu strip and excludes the cover from display capture.
Control Center's opaque-bound geometry is unit-tested; real Control Center
capture, a forced menu fallback, multiple simultaneous dropdown slots, and a
physical trackpad session remain unverified. Child-window inclusion is configured
on macOS 14.2 and later; live sheet/popover imagery was not rechecked here.

No companion or native-host file changes were needed inside this package. Those
packages must consume the shared contract's source keys, roles and surface messages.
Headset input, decode/render, panel placement, focus comfort and worn acceptance
are outside this local fixture result. No headset was accessed.

Temporary binaries, module caches, fixture state, inventories, diagnostic samples,
logs and intermediate reports were removed after their results were condensed
here. The measured scratch high-water was 54,488 KiB (53.21 MiB), reduced to zero;
the final cleanup removed 6,968 KiB in 29 remaining files after earlier module-cache
cleanup. Two generated Python caches (41,569 bytes) and an empty fixture preference
file (42 bytes) were also removed. No binary, raw capture or bulk log is retained.
The runner now preserves/restores an existing fixture preference file and removes
new fixture preferences on exit; its final cleanup/signal-handling edits passed
shell syntax and Python compilation checks.
