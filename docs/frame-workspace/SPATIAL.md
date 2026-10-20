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

## Still to test

- Mirroring the laptop screen to the headset display (public API; changes the Mac's displays briefly).
- A drag from one window's display to another's (moves the real cursor).
- Detecting new windows as they open (Accessibility observer).
- Gaze picking the active window, and the cursor memory (needs the headset worn).
