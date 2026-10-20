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

## Still to test

- A longer mirrored session, and the Mac's behaviour on sleep and wake while mirrored.
