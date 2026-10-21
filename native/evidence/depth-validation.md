# Window Depth And Pressed Menu Title Validation — 2026-10-21

Source base `1126e0e2e7c4488f1dbcab95ce74d06c5482fb8e`, branch `fw15b/depth`.
Local macOS / Apple M4 accelerated OpenGL, no headset or helper runtime session.

## Implementation

The picture meets the bezel at the 15 mm front plane. Four wall overlays extend
back to the saved placement plane. Each bend-following edge packs 1–4 coplanar
facets into one planar overlay; asymmetric bends preserve the centre seam and
vertical bends transpose the geometry. The rear/side footprint is a closed rectangular shell recessed under the rounded
bezel lip, with exact planar joins shared by rendering and picking. Rounded
material normals shape its edge lighting; alpha cutouts and overlap tabs are
absent at wall junctions. This is not an exact extruded quarter-cylinder corner.
It preserves the rounded front outline while keeping the overlay budget. Wall hits reveal
frame handles; picture intersections take priority. The production shader's
Palladium palette, light, tip light and pressed reflection functions are shared.

One session-owned cursor serves the host's focused window. Content, frame and
control half overlays are allocated only for unequal bends and destroyed when
halves match again. The menu strip likewise allocates its second half only when
its region crosses an unequal bend seam. Curved layer texel aspect preserves the
physical height while the front-plane radius changes the arc width.

The helper emits optional global-point `selectedRect` only while an app dropdown
is open. AXSelected wins, with dropdown-to-item frame matching as fallback.
Native parsing, clipping and point-scale/inset mapping keep the pressed title on
the same curved strip. Background sampling preserves title ink from transparent,
light, accent-blue and dark captures, rendered as dark ink on the pressed metal.

## Overlay Accounting

Counts include allocated hidden menu slots, four shelf apps, shelf backing,
status, and the SteamVR bar panel plus its thumbnail. Other applications consume additional system overlays.

| Configuration | Before / Window | After / Window | Before 6 / 9 | After 6 / 9 |
| --- | ---: | ---: | ---: | ---: |
| Flat | 7 | 7 | 56 / 77 | 56 / 77 |
| Even Curve, Horizontal Or Vertical | 7 | 7 | 56 / 77 | 56 / 77 |
| Unequal Bend, Horizontal Or Vertical | 7 | 10 | 56 / 77 | 74 / 104 |

Before: picture, frame, controls, per-window cursor and three eagerly reserved
halves. Fixed UI was 14: shelf/apps 5, status 1, menu slots 4, menubar 2, bar panel
and thumbnail 2. After: flat/even window has picture/frame/controls plus 4 walls;
unequal bends add 3 live halves. Fixed UI is also 14: shelf/apps 5, status 1,
menu slots 4, menubar 1, cursor 1, bar panel/thumbnail 2. A split menubar adds 1,
so the extreme all-nine-split case is 105. The ordinary nine-window wall is 77,
below 100; the extreme split case is below the 128 system-wide ceiling.

`SpatialRenderer` retains its largest allocated tile vector even after closing
UI. The actual `spatialLayout` caps switcher thumbnails at seven and can add
three app-menu rows. Tests construct its four-app/nine-window layout and count
15 retained tiles (5 base + 7 switcher + 3 menu), then combine them with actual
nine `wallPlacement` window counts. This raises nine flat/even to **87**, and
nine split with a split menubar to **115**. Six-window high-water is 65 ordinary
or 83 split (84 with split menubar). These count hidden retained overlays too.
The same high-water increment applied before the depth change.

## Checks And Source Identity

- Final native adapter build and automatic/explicit `--self-test` passed without
  warnings. Binary SHA-256:
  `adf34b21ccc6ca54ec259b5681cae2b638e677fea9d40f422debff2a905f6410`
  at `scratchpad/depth/build-closed/frame-workspace-native`. All 40 C/C++ source hashes
  match the final tree; inputs were frozen before this build. Final render and integration evidence uses this
  closed-shell version. Prior build
  `08aeffaad024904d746d912c45ec08b2fe6cad52ab44a89564ab4b5c0ce886b2`
  established the earlier shader/host behavior; its geometry was superseded by
  the exact planar-join repair and retained-UI budget self-tests.
- Native self-tests cover flat/even/split/vertical wall geometry, front/rear
  vertex depth, facets, recessed silhouette, picture hit priority, cursor ownership,
  production half creation/destruction policy, counts, selectedRect validation
  and curved-strip pressed-title UV. Exact wall-join continuity is checked
  through the wall thickness at every corner. Existing host gaze distances now assert the
  picture's new physical front plane.
- Pure Swift policy compile/run passed all 24 checks, including AX selection,
  geometric fallback, closed menu reset, context/status exclusion and unmatched
  dropdown rejection.
- Full helper build passed with no warnings; the helper was never launched.
  SHA-256: `9fab3a4ececfb830b1eec7916c2af70dbe3a46f432de483fcc293990bc5400b0`.
- Unchanged launcher suite passed: 22 tests in 13.466 seconds.
- `python3 native/tests_scene.py "$bin"` passed on the final binary in about
  10 minutes 22 seconds: flat/even/split/vertical picture and menu orientation,
  camera changes, foreground ordering, premultiplied alpha, wall slots and
  invalid inputs.
- Unmodified `native/tests_host.py` hit its 8-second first-panel readiness
  deadline and then its 5-second cleanup wait on this Mac. Its exact process
  was confirmed gone. A scratch-only copy passed both host and four-role
  surface scenarios on the final binary in about two minutes. The copy changes
  only two readiness limits from 8 to 90 seconds and two host lifetimes from
  60 to 240 seconds; its exact diff was inspected. Assertions, fixtures and
  stream sequences are unchanged. This is supplementary integration evidence;
  the original short-deadline run did not pass. No test processes remained.

## Reproduction

Use the macOS `gl.pc`, compiler wrapper and offscreen CGL interpose documented in
`shelf-menubar-validation.md` and `spatial-scene-validation.md`. All task scratch:
`/private/tmp/claude-501/-Users-mads-code-frame-workspace/d6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/depth`.
No window or virtual display is created by the CGL bootstrap. Check
`memory_pressure | tail -1` before each compile, waiting if below 15% free.
Observed implementation build checks were 45–51% free.

```sh
scratch=/private/tmp/claude-501/-Users-mads-code-frame-workspace/d6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/depth
memory_pressure | tail -1
PKG_CONFIG_PATH="$scratch" CXX="$scratch/cxx" scripts/build-workspace-native.sh "$scratch/build-closed"
bin="$scratch/build-closed/frame-workspace-native"
export DYLD_INSERT_LIBRARIES="$scratch/cgl-bootstrap.dylib"
export FRAME_NATIVE_TEST_TMP="$scratch" TMPDIR="$scratch" PYTHONDONTWRITEBYTECODE=1
"$bin" --self-test
python3 native/tests_host.py "$bin"
python3 native/tests_scene.py "$bin"
python3 native/tests_depth_render.py "$bin"
python3 native/scene/generate-depth-scenes.py "$scratch/scenes"
python3 native/scene/generate-wall-scenes.py "$scratch/wall"
"$bin" --render-scene native/scene/example.json native/evidence/depth-side.png --eye 1.1,.15,-.9 --look -75,-6 --fov 60 --size 1600x1000
"$bin" --render-scene "$scratch/scenes/depth-above.json" native/evidence/depth-above.png --eye .35,1.25,-1.1 --look -35,-65 --fov 70 --size 1600x1000
"$bin" --render-scene "$scratch/scenes/depth-flat-corner.json" native/evidence/depth-flat-corner.png --eye .95,.9,-1.4 --look -48,-54 --fov 25 --size 1200x900
"$bin" --render-scene "$scratch/scenes/menubar-pressed.json" native/evidence/menubar-pressed.png --eye 0,.65,0 --look 0,-10 --fov 48 --size 1400x700
"$bin" --render-scene native/scene/example.json native/evidence/spatial-scene.png
"$bin" --render-scene native/scene/example-slim.json native/evidence/spatial-scene-slim.png
"$bin" --render-scene "$scratch/wall/wall-6.json" native/evidence/spatial-wall-6.png
```

## Retained Renders

All seven images were rendered with the final binary above and inspected after
the closed-joint repair.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `depth-side.png` | 261546 | `dc55da66580a25bbc3ff68531045c164ea5b38b761ee2c6a9aa68102d7a1b9a5` |
| `depth-above.png` | 315849 | `fb4dd30712e2f542e09ced3314624bc78fb123228b5fa83d43f534ac23e17513` |
| `depth-flat-corner.png` | 128943 | `0f8d0d4e5bb9950b6317426691d7245d2524132de2f2d992ba8c1c903d88d0eb` |
| `menubar-pressed.png` | 215277 | `4fb39b065c30472b8548595e93340d6e5d9816a7a554285d9f3f2b50e6fd100a` |
| `spatial-scene.png` | 249396 | `c22851c07b7ad7eb54f0147d0906264cfd518714974646e6124cc8e9c7caac9a` |
| `spatial-scene-slim.png` | 239541 | `c86fd8db00306b262c718c71beba963f2a9ad28f9f3ed2c7bc00421111c34c60` |
| `spatial-wall-6.png` | 220952 | `e05e9ddead971f7ddcb990819d5cbb349ee021fa5d22e122d5f5524a55a9d725` |

The first visual pass caught square wall ends outside the rounded front bezel
and an opaque black selection caused by treating transparent background pixels
as ink. Both were corrected locally; these are superseded observations, not the
retained render state. All seven retained renders were opened and inspected. The side comparison
closes the original inner air gap; front views preserve picture and text
orientation; the above/corner views show the recessed side profile beneath the
rounded lip; File has readable dark ink and a blue inward bevel. A focused correction removed rounded alpha cutouts at the wall junctions.
The final rear shell planes meet exactly beneath the rounded front lip, without
openings or protruding overlap tabs. Continuity self-tests cover all four corners
at 4, 7.5 and 11 mm depth across flat, curved, unequal and vertical bends.

The pressed-title regression passed for all three captures. In its fixed
2,346-pixel title ROI, counts (metal / ink / blue reflection) were transparent
2151 / 138 / 1312, opaque accent 2156 / 136 / 1314, opaque dark 2153 / 137 / 1315.
It renders transparent black
ink, opaque blue with white ink and opaque dark with white ink through the actual
scene compositor, checking visible metal, dark glyph coverage and inward blue
reflection.

These are synthetic monoscopic production-renderer checks, not SteamVR overlay
presentation, physical controller/gaze interaction, real AX menu discovery or
worn acceptance. No headset, virtual display, user windows or system settings
were changed. Only source, this report and the seven requested evidence renders
are retained from this task; generated build/test scratch is removed after
recording the results above.

## Cleanup

Removed the exact task scratch directory after confirming no process was using
it: 67 generated files, 29,881,394 logical bytes and 30,167,040 allocated bytes
(28.77 MiB). This removed temporary binaries/debug information, build metadata,
GL adapters, scene inputs, test copies, logs and aborted-test directories.
The scratch directory and `native/build` are absent. Source, feedback images,
existing evidence and the seven requested final renders were preserved.
