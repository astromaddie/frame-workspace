# Spatial Mode spike (FW-15)

Labels as elsewhere: **verified** (run here, with date and versions) or **inferred**.

## How many windows the Frame can show (verified 2026-10-20)

User's M4 MacBook Pro (macOS 26.3.1), Steam Frame on the SteamOS build installed that day, the
Workspace (2880x2560) streaming alongside. Each test window was on a virtual display of its own
(`FRAME_WINDOW_DISPLAYS=1`, `Separate.swift`) and shown as a native panel (one viewer process and
one hardware H.264 decoder session each).

| Windows | Decoded size each | Total decoded, with the Workspace | Result |
|---|---|---|---|
| 3 (1280x800 windows, 2x) | 2560x1744 | 20.8 MP | all streaming |
| 4 | 2560x1744 | 25.2 MP | the 4th viewer exits: `h264_v4l2m2m` send failed -12 (Cannot allocate memory) |
| 9 (640x400 windows, 2x) | 1600x1200 | 24.6 MP | all streaming |
| 10 | 1600x1200 | 26.5 MP | the 10th exits the same way |

- **The limit is the hardware decoder's budget, about 21-25 megapixels decoded at once, not the
  number of windows** (4 sessions failed at 25.2 MP; 10 sessions passed at 24.6 MP).
- **Frame memory is the next limit.** About 1.6 GB was available with one panel, and each viewer
  process held about 280 MB (the Workspace's about 370 MB); with 9 panels about 0.6 GB was left.
- With all windows redrawing 60 times a second (the worst case), each window's delivered rate fell
  as windows were added (28 fps with one, about 17 with two, about 10-17 with three). The fixture's
  constant redrawing also kept the Mac's WindowServer at 60-98% CPU. Ordinary windows mostly sit
  still, which costs nothing to stream; the realistic moving case is one or two windows.
- Each window took 7-8.5 s to show (its display created, the window moved, the viewer started).

**Inferred design consequence:** one full-resolution live stream per window tops out at about five
typical windows. Spatial Mode needs (a) only the windows that matter to be live at full
resolution: the one you look at, plus ones that are changing; (b) others shown as their last frame
with no decoder session open, or at reduced resolution and rate, reopening when they change or you
look at them; and (c) one viewer process for all panels, to save memory and start panels faster.

## Repeat it

1. Start the companion with `FRAME_WINDOW_DISPLAYS=1` (port 60339) and the Workspace.
2. `swiftc -O mac/frame-mac-view/Diagnostics/SpatialFixture.swift -o /tmp/spatial-fixture` then
   `/tmp/spatial-fixture 12` (moving, 1280x800) or `/tmp/spatial-fixture 6 static small`.
3. `python3 scripts/spatial-capacity-sweep.py /tmp/sweep.log`, or show windows one by one with the
   `show` action and `separate:<window id>`.
4. A pass at a given count is every panel's unit active with `[vr-perf]` lines and no `Cannot
   allocate memory` in the user journal. Afterwards stop each `separate:` stream, quit the fixture,
   and remove the test windows' files in `~/.local/share/frame-workspace/panels/`.

## The laptop screen as if the lid were closed (verified 2026-10-20)

`mac/frame-mac-view/Diagnostics/SpatialMirrorProbe.swift` (build with `swiftc -O`, run while a
Workspace display exists) sets the built-in screen to mirror the Frame Workspace display with
`CGConfigureDisplayMirrorOfDisplay`, applied `.forAppOnly` so macOS undoes it when the probe exits,
then undoes it itself after 6 s. Result, with no errors:

- While mirroring, macOS listed **one** screen, the Frame Workspace: it became the main display
  (menu bar and Dock), at 0,0, 2880x2560; the built-in reported `active=0` and mirrored it. This is
  the "lid closed" desktop, through public API.
- After the undo, the layout was exactly as before (built-in main, 1800x1169 at 0,0; Workspace at
  1800,0), the Workspace stream kept running, and all 72 of the user's windows were still on the
  laptop screen.
- Not yet checked: whether the user's windows change size during a longer mirrored session, and
  whether the built-in's backlight turns off or only goes black.

## A drag between windows on different displays (verified 2026-10-20)

`SpatialDragProbe.swift` opens two text windows of its own, one on the Frame Workspace display and
one on the laptop screen, selects a word in the first and posts a mouse drag that jumps straight
from one display to the other (no path across their shared edge), then drops. **Pass:** the word
arrived in the second window. So a drag can follow the laser from one panel's display to another's.
Inferred, not tested: drags between different apps (the same pasteboard mechanism). The helper's
input now has a shared-pointer mode for window displays; cross-panel behaviour still needs a
device check after integration.

## New windows (verified 2026-10-20)

An Accessibility observer on an app saw `AXWindowCreated` (with the window's title) as a test
window opened. Its close was only reported when the app quit (the closed window object stayed
alive), so closes should also be confirmed from the window list, as `WindowCloseRecovery.swift`
already does.

## Mac foundation (built 2026-10-20)

The helper and companion now implement the Mac half of the
[shared spatial protocol](../../native/README.md#spatial-stream-and-focus-contract-fw-15).
This does not add the mode toggle, automatic window panels, or mirroring.

- `POST /streams/live?src=...` chooses live window streams. Other windows send `still`,
  stop encoding and sending pictures, and continue low-cadence content observation. They
  keep their last picture on the viewer. Resuming sends `live` before a forced keyframe
  containing SPS/PPS. Display streams, including the Workspace, stay live.
- `/status` exposes each stream's live pixel dimensions, live state and last content-change
  time (Unix seconds), plus the focused source and shared-pointer mode. Capture observations
  continue while still; decoder allocation cannot be inferred from this Mac-side status.
- With `FRAME_WINDOW_DISPLAYS=1`, the companion's existing one-second supervisor reserves
  running display streams first, then selects the focused window and recently changed
  windows, newest first. `FRAME_SPATIAL_DECODE_MP` defaults to **18 MP**. Optional windows
  need one second of observation before admission, remain eligible for five seconds after
  their last change, and get three seconds of live residency before a newer window can
  replace them. Focus and the capacity limit override residency. Disabling window displays
  restores all current and future window streams to live.
- Unknown window dimensions are not admitted to the live set. New streams deliver one initial
  picture before honoring still, so each panel has a texture; transient startup decoder demand
  needs device verification. If the Workspace plus focused window cannot
  fit, the focused window stays still until its stream size or the configured budget
  changes. The policy never admits optional pixels beyond the remaining capacity; it
  cannot reduce existing display streams that already exceed the budget by themselves.
- The companion accepts atomic `focus` request files with an owned window panel key and
  finite normalized `(u,v)`, coalesces a batch to its newest valid request and dispatches
  at most four per second. `/focus` raises the window and restores its cursor memory;
  `(u,v)` initializes the first visit. Repeated focus on the same window leaves its cursor
  in place. Shared pointer mode carries held mouse buttons across viewer sessions, while
  keys keep their original session ownership.

**Verified locally, 2026-10-20:** fake-agent policy and request tests, pure Swift state tests,
and the isolated synthetic-helper suite. From the repository root:

```sh
mac/frame-mac-view/build.sh mac/frame-mac-view/.fw15-build/frame-mac-view
xcrun swiftc -swift-version 5 mac/frame-mac-view/Sources/SpatialStreams.swift mac/frame-mac-view/tests/SpatialStreamsTests.swift -o mac/frame-mac-view/.fw15-build/SpatialStreamsTests
mac/frame-mac-view/.fw15-build/SpatialStreamsTests
cd tests && python3 -m unittest test_macview test_workspace_profiles test_server
```

The helper built successfully and the pure Swift checks passed. The Python suite ran **142
tests, with one skip and no failures**; the missing-Accessibility rejection test skipped
because this Mac grants Accessibility. The suite emitted socket `ResourceWarning`s. Pure checks
cover stale-frame epochs, resumed keyframe gating, shared button ownership, first-gaze
initialization after viewer focus, and cursor-coordinate round trips without drift. These
do not establish ScreenCaptureKit or headset behaviour. The helper build stayed inside
this worktree; the installed helper, running companion, displays, viewers and Frame were
untouched. Disposable build binaries were removed afterwards, recovering **2,116 KiB**;
the test fixture removed its isolated helper directories.

Device acceptance is still required after merging the Frame worker:

1. Show more windows than the simultaneous decoder limit with the Workspace running.
   Check `still` leaves the last texture visible and releases the decoder, and that `live`
   recreates it from SPS/PPS plus IDR without allocation failures or stale frames, including
   rapid transitions, resizing, opening windows and reconnecting. Check status dimensions
   against actual decoded sizes and confirm the total live pixels respect the budget.
2. Edit a still window, including a small text edit or badge change. Confirm it wakes,
   remains live briefly, and settles without repeatedly evicting other windows. Measure
   capture/encode load and focus latency with ordinary and continuously changing windows.
3. Wear the headset and look between panels. Confirm the intended window becomes key,
   the Mac cursor appears there, the first visit uses the gaze coordinates, and returning
   restores each window's last cursor position after mouse movement and window resizing.
4. Drag from one panel into another, release in the destination, then repeat between apps.
   Confirm the drag survives the panel boundary, keys remain independent, and closing or
   disconnecting the source during a drag releases buttons without stuck input.

## Shared host foundation (FW-15)

The opt-in Frame implementation is `FRAME_NATIVE_HOST=1`. The companion sends
window panels to one `frame-workspace-host-UID.service`; the Workspace can keep
its separate viewer. `frame-workspace-native --host-dir SPEC_FOLDER` watches
atomic per-panel spec files, keeps one GL context/OpenVR client, and reuses the
same per-panel overlay/chrome/placement loop as the standalone path. The default
launcher path is unchanged. See [native host protocol and lifecycle](../../native/README.md#shared-window-host-fw-15-opt-in).

Still/live and focus are the shared worker contracts:

- WebSocket text `{"t":"still"}` keeps the last completed picture and closes
  the decoder session after processing already accepted packets with a bounded
  final-output drain. `{"t":"live"}` recreates the session only from the next IDR with SPS/PPS. Per-panel state
  exposes `streamStill`, `decoderSessionOpen`, `decoderStillReleases` and
  `decoderLiveOpens`, so decoder ownership can be checked separately from a
  visible held texture.
- The host samples eye tracking through `GetEyeTrackedFoveationCenter`, tests
  the nearest front-facing window surface with the existing bend geometry, and
  applies a 180 ms dwell plus edge hysteresis. Uneven and vertical halves use
  original-window UV. Without eye tracking the controller laser picks the
  window. On a switch it writes an atomic mode-0600 request in the existing
  requests folder: `{"action":"focus","key":"OPENVR_KEY","u":0.0,"v":0.0}`
  (u right, v down, top-left origin). The Mac live-set,
  focus delivery, pointer mode and per-window cursor memory require the
  companion integration and end-to-end verification.

### Verification record (2026-10-20)

**Verified locally (Mac, Apple M4 GL):** the extended self-test, production
Pipeline still/live lifecycle, two-stream host integration, ordinary stream and
reconnect checks passed. All 16 YUV shader layout/range/matrix/pattern cases
matched the production swscale oracle to within 1 RGB level. A four-second
BT.601-limited GPU stream passed its existing image, control and timing
assertions with zero errors. The companion/launcher suite passed: 84 tests run,
1 skipped.

**Verified on the Frame:** an isolated AArch64 build using the device's system
libraries (FFmpeg 61.3.100), the build script's self-test and a separate explicit
`--self-test` passed. All 28 copied C/C++ source/header hashes matched the native
source checkpoint. `/tmp/fw-host-worker` was removed afterwards. No viewer,
OpenVR/GL session, hardware decoder, installed artifact, service or SteamVR
configuration was changed or exercised there.

**Known test harness limitation:** `tests_gpu_conversion.py` reaches a short
1.2-second stream test whose control observer deadline starts before process/GL
startup, while the first updated state is periodic. That revision-observer
assertion fails on both baseline `d877eb1` and this change; the same current
stream passes at four seconds. Assertions were preserved. The full short GPU
suite is therefore not a passing result.

Compact source identities, commands and outcomes are recorded in
`native/evidence/spatial-host-validation.json`. Synthetic streams and geometry
do not establish worn focus behavior, OpenVR host interaction, hardware
reclamation, or performance improvements.

Reproduction, on a development machine with the native build dependencies:

```sh
native/build/frame-workspace-native --self-test
python3 native/tests_still.py
python3 native/tests_host.py native/build/frame-workspace-native
python3 native/tests_stream.py native/build/frame-workspace-native --use-display
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --reconnect
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --conversion gpu --case bt601-limited --duration 4
cd tests && python3 -m unittest test_macview test_native_pose_launcher
```

`tests_host.py` uses a hidden GL window with `--no-vr`, two loopback H.264 streams,
CPU and GPU conversion, dynamic add/remove/re-add, still/live, independent
controls and saved bends, and per-stream failure/close isolation. It does not
contact a companion or initialize OpenVR. The tests remove their fixtures.
`--self-test` covers spec generations and terminal bookkeeping, nearest hits,
rotated/asymmetric/vertical surface UV, back-face/invalid/missed rays, dwell,
edge hysteresis and controller fallback source changes without initializing GL
or OpenVR.

### Device acceptance still required

With the integrator's Mac changes and an authorized device session:

- Start multiple host panels beside the separately running Workspace; check one
  host PID/GL/OpenVR session, independent keys and textures, concurrent launch,
  add/remove/replacement, stream close/error, reconnect and clean shutdown.
- Verify every existing chrome layer and half, controller hover/haptics,
  move/resize/bend (both directions and uneven halves), close, control-file
  commands and saved/profile pose restoration. Overlapping windows must route
  a hand to the nearest panel and retain a drag until release.
- Confirm a real V4L2 session closes on `still` while its last picture remains
  visible, another window can use the freed decoder budget, and repeated
  `live`/keyframe cycles reopen cleanly. Check rapid transitions, tunnel
  reconnect and dimension changes. The isolated build/self-test never opens a
  hardware decoder.
- Worn: validate eye-ray direction, nearest-window choice and top-left UV;
  settle on adjacent and overlapping panels without edge flicker; turn eye
  tracking off and verify controller fallback. Confirm focus requests are
  consumed once, the intended Mac window activates, cursor memory works, and
  looking away does not switch to a controller while gaze is valid.
- Measure the same window set with and without the host: resident memory,
  first-picture/startup latency and moving-stream throughput. The spike's
  per-process costs do not prove the host's actual saving or a larger decoder
  budget. No host runtime or worn test was performed in this worker session.

## Both foundations together (verified 2026-10-20)

Merged and run on the user's M4 and the Frame (not worn), with `FRAME_WINDOW_DISPLAYS=1` and
`FRAME_NATIVE_HOST=1`, the Workspace streaming, and eight still 1280x800 test windows (each
2560x1744 on its own display), twice what one-process-per-window could show:

- All eight showed, with no decoder allocation errors; the budget kept the Workspace and two
  windows live and six still.
- **One host process ran all eight panels in about 394 MB** (one viewer per window measured about
  280 MB each); the Frame had about 2.4 GB available, more than with a single panel before.
- A `focus` request for a still window (as gaze would send) made it live within 2 s, its decoder
  reopening at 2560x1744; the previously live windows, unchanged, went still.
- Rebuilding the Mac helper kept Screen Recording and Accessibility (the companion reported both).
- Showing a window still took 6.5-12 s, now mostly the Mac creating its display and moving it.

## Still to test

- A longer mirrored session, and the Mac's behaviour on sleep and wake while mirrored.
