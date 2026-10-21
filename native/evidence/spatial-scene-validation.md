# Spatial Scene Validation — 2026-10-21

Source base: `04e837b1bc36910a466915eea7069c51a301e8a9`, branch
`fw15b/scene-render`. Local macOS / Apple M4 OpenGL development build.
Final tested binary SHA-256:
`9fdb622cf3aaab77e005bec633f207e820a189aba9987af00fc9c9cf950df0c9`.

## Checks

- `scripts/build-workspace-native.sh`: passed, no warnings; its `--self-test`
  passed, including existing placement, bend, focus, spatial and host checks.
- `python3 native/tests_host.py native/build/frame-workspace-native`: both host
  and four-role surface integration scenarios passed on the final binary.
- `python3 native/tests_scene.py native/build/frame-workspace-native`: passed
  through the real CLI and GL compositor. Checks four-corner texture orientation
  for flat/even/split horizontal and vertical bends, eye/look/FOV changes,
  selected-window foreground bands, nearest ordering within a band,
  premultiplied transparency, and invalid input rejection.
- Python AST syntax checks for the capture script, fixture generator and image
  tests passed. `git diff --check` passed.
- Native build free-memory checks were 31–38%; no build began below 15%.
  The capture script independently checks memory before Swift compilation.

## Reproduction On macOS

The normal build uses Linux `gl.pc`. For this local verification only, a temporary
adapter supplied the system OpenGL framework and treated Homebrew dependency
headers as system headers (suppressing dependency-only json-c unused functions).
No source warning flags were disabled and no system settings were changed.

```sh
scene_scratch=$(mktemp -d)
cat > "$scene_scratch/gl.pc" <<'PC'
Name: gl
Description: macOS OpenGL
Version: 2.1
Libs: -framework OpenGL
Cflags: -DGL_SILENCE_DEPRECATION
PC
cat > "$scene_scratch/cxx" <<'PY'
#!/usr/bin/env python3
import os, sys
args=[]
for a in sys.argv[1:]:
    args.extend(['-isystem',a[2:]] if a.startswith('-I/opt/homebrew/') else [a])
os.execvp('c++',['c++']+args)
PY
chmod +x "$scene_scratch/cxx"
memory_pressure | tail -1 # Wait and retry if below 15%.
PKG_CONFIG_PATH="$scene_scratch" CXX="$scene_scratch/cxx" scripts/build-workspace-native.sh
FRAME_NATIVE_TEST_TMP="$scene_scratch" TMPDIR="$scene_scratch" PYTHONDONTWRITEBYTECODE=1 python3 native/tests_host.py native/build/frame-workspace-native
FRAME_NATIVE_TEST_TMP="$scene_scratch" PYTHONDONTWRITEBYTECODE=1 python3 native/tests_scene.py native/build/frame-workspace-native
native/build/frame-workspace-native --render-scene native/scene/example.json native/evidence/spatial-scene.png
native/build/frame-workspace-native --render-scene native/scene/example-slim.json native/evidence/spatial-scene-slim.png
python3 scripts/render-spatial-scene.py --output /private/tmp/real-spatial-scene.png
```

Remove the temporary adapter directory and `native/build` after verification.
The capture script refuses existing outputs and requires pre-existing Screen
Recording permission; it never requests permission.

## Render Evidence

Both synthetic PNGs were opened and inspected after the final build. Text is
upright, including the unequal vertical-bend panel; the even and unequal side
bends face the viewer; chrome follows all three surfaces. The focus border,
controls, hot menu/dropdown, resting slim menu, shelf/icons, status strip,
right-hand diagnostic ray and production cursor are present and within frame.
The initial vertical UV mirror was corrected and is covered by the image test.

- `spatial-scene.png`: 242142 bytes, SHA-256
  `00d4b658ec71a1ac598d5aefffc45890d446ca6439924e2bb6d653fbdfab85ce`
- `spatial-scene-slim.png`: 243241 bytes, SHA-256
  `e2c41f298ca22d5d286d46365cdbc9f49d22d4e53bade711d09a99bca9e062bd`

The private capture preflight passed. Six current on-screen window pictures and
running-app icons were captured, with only the built-in menu-bar region captured
from the display. The final menu/status boundary uses the companion's actual
`SpatialSharedPolicy.menuBarSplit` on intersecting layer-25 window bounds,
matching `SpatialShared.refresh()`. Both final private PNGs were opened and
inspected: hot/slim strips, status and shelf fit at yaw 0°, default 100° FOV.
No window/app movement, activation, resizing or settings changes were performed.

Private finals remain only at the requested scratchpad root:
`/private/tmp/claude-501/-Users-mads-code-frame-workspace/d6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/real-spatial-scene.png`
and its `real-spatial-scene-slim.png` sibling. No private content is in this
repository or report. The first pair with an approximate crop was superseded.

## Geometry And Limits

`bendSegment` supplies actual split transforms/UV bounds and the 2 mm overlap;
`ChromeRenderer`, `SpatialRenderer`, `SurfaceRenderer`, and shared cursor pixels
supply appearance. The host's placement functions supply all auxiliary poses.
Only diagnostic camera projection, overlay tessellation/composition, the room
and controller ray are new. The image is monoscopic, with no headset, SteamVR
compositor, lens distortion, or worn verification.

Actual Spatial Mode defaults overlap: `_show_native_window` in
`ui/frame_macview.py` invokes `beside_workspace_pose` only in Desktop Mode.
The Spatial reconcile path passes a pose only when restoring one. `PanelRunner`
then uses `Placement` and `Overlay::recenter` (2.4 m wide, 2 m ahead, flat,
height from stream aspect). The script reproduces those unsaved defaults,
without inspecting saved headset state. The distinct `spatialLayout` arc is
for switcher thumbnails. The fixtures use explicit arranged poses to expose
all bend variants. This call chain was traced across the companion launcher,
Spatial start/reconcile path, native panel constructor and overlay recenter;
it is not inferred from the switcher layout.

## Cleanup

Measured removal: **215267186 bytes (205.29 MiB)**, consisting of intermediate
capture/icon/scene/compiler data from the capture attempts (208485366 bytes),
the superseded private PNG pair (245791 bytes), and the local build plus
adapters (6536029 bytes). Test temporary directories removed themselves.
The task's `scratchpad/render` directory and `native/build` were removed;
source, synthetic fixtures, this compact report, two synthetic evidence PNGs,
and the two private final PNGs remain.
