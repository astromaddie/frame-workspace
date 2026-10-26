# Native Workspace overlay

Experimental `--foveation` now requires the companion's **direct capture**
capability. It keeps the video dimensions fixed and uses centre foveation when
eye tracking is unavailable. It does not negotiate the older packed stream with
an incompatible companion. The eye producer runs independently of GL rendering;
eye data and geometry still require headset verification. See the current
[capture design and validation record](../docs/frame-workspace/FOVEATION.md).

Small Linux ARM64 viewer for the existing Mac `/view` URL and H.264 stream.
It owns an OpenVR overlay, not a SteamVR scene application. The last submitted
picture remains world-locked in the compositor between desktop updates. Head
tracking is handled by SteamVR; this program does not render eye images or
sample a pose every video frame. Window placement samples the HMD when placing
or recentering the screen; Spatial Mode also samples the head once to place its shelf.

Keyboard and trackpad input stay on the Mac. Native chrome and the approved
Spatial Mode shelf and switcher surround the streamed Mac windows.

## Build

On a Linux ARM64 system with C++17, pkg-config, SDL2, desktop OpenGL, FFmpeg
(libavcodec/libavutil/libswscale), json-c and OpenSSL development packages:

```sh
scripts/build-workspace-native.sh
native/build/frame-workspace-native --self-test
```

Use the Frame's system development libraries for the device build. Ubuntu 22.04
container builds are useful host tests but link FFmpeg 58, whereas the verified
Frame headers expose FFmpeg 61. Do not copy a container executable without
checking its dependency closure. The build writes `BUILD-METADATA.json` with
source and binary hashes, architecture and dependency versions. It does not
install, start SteamVR, or mutate the headset.

OpenVR's runtime is loaded from `/opt/steamvr/bin/linuxarm64/libopenvr_api.so`
(with the alternative `lib/` path and system loader as fallbacks). The exact
`IVROverlay_027` and `IVRSystem_022` function tables are required; another ABI
is never cast to these layouts. Vendored headers are Valve OpenVR v2.5.1,
commit `ae46a8dd0172580648c8922658a100439115d3eb`, BSD-3-Clause; see
`vendor/openvr/README.md` and `LICENSE`. System shared libraries are not copied:
FFmpeg is LGPL/GPL depending on its distribution build, SDL2 is zlib, json-c is
MIT, OpenSSL 3 is Apache-2.0, and Mesa OpenGL uses its system distribution license.

## Launch and control

The coordinator's launcher supplies a mode-0600 URL file. It must contain an HTTP
URL with an exact `localhost` or `127.0.0.1` host, `/view` path and `src`/`t` query
parameters. `codec=h264`, `max=320..4096`, `fps=1..120`, and `bpp=.01..2` are
supported. No URL or received text is printed to logs or state. The file is read
once; the caller may remove it after readiness. Reconnect credentials remain in
memory. The viewer validates the WebSocket upgrade, masks client frames,
acknowledges `hello`, uses only `r` on reconnect, and sends receipt acknowledgements
so the Mac's capture congestion controller can advance. Ping clock sync, decoded/
uploaded timestamps and Mac focus messages use the existing protocol. Actual
compositor presentation timestamps are unknown and are sent as zero, not invented.

```sh
native/build/frame-workspace-native \
  --url-file /run/user/1000/frame-workspace/url \
  --overlay-key com.frame-workspace.native.1000.workspace \
  --control-file /run/user/1000/frame-workspace/control.json \
  --state-file /run/user/1000/frame-workspace/state.json \
  --pose-file /home/frame/.local/share/frame-workspace/pose.json
```

The caller creates parent directories. Atomically replace the control file with
JSON and change `revision` for each command. Use integer revisions from 1 to
2^53−1 so the native JSON reader represents them exactly; wrap to 1 at the
limit. The companion normalizes legacy nanosecond revisions before sending:

```json
{"revision": 1, "width": 2.4, "distance": 2.0, "recenter": true, "focus": true}
```

Width clamps to 0.4–8 m; distance to 0.4–10 m. Width/distance changes retain the
world orientation. Recenter places the screen ahead of the current HMD yaw at
eye height, then leaves it fixed in standing tracking space. Invalid tracking
leaves recenter pending. A saved valid pose survives ordinary restarts. `focus`
shows the owned overlay and sends the existing Mac window-focus message.
`{"revision":2,"stop":true}` or SIGTERM cleanly stops threads and destroys only
this process's overlay. SteamVR Quit events are acknowledged. State and pose
files are atomically replaced with mode 0600.

`{"revision":4,"move":[0.1,0,0]}` moves the screen in its own frame (meters:
right, up, toward the viewer; each value within 2 m, and never beyond 10 m from
the room origin). `{"revision":5,"rotate":[5,0,0]}` turns it about its own
centre (degrees: yaw, pitch, roll; each within 90°), keeping the rotation
orthonormal. Both apply live and are saved to the pose file. The launcher's
`pose RIGHT UP CLOSER YAW PITCH ROLL` and the companion's `workspace_pose`
action send them.

`{"revision":3,"curvature":0.25}` requests OpenVR curvature 0–1. The owner
sets and reads back the value; `controlResultRevision` acknowledges the exact
request and `curvatureResult` reports supported/applied/value/errorCode.
Unsupported curvature is nonfatal. Width/distance-only requests do not alter
curvature or request focus/recenter. Confirmed curvature is saved in the pose
file and re-applied at startup (verified on the Frame 2026-10-19: 0.15 read back
after a viewer restart); older pose files without it stay flat. The current Frame accepted 0.25 and restored 0.0;
this does not establish worn optical behavior.

`ready` becomes true only after a successful texture submission and overlay
show. Menu Bar, Status and menu panels also report `registered: true` (state
`idle`) once their stream is connected and the first surface message arrived,
since an idle menu has no picture; windows register with `ready`.
`--capabilities` prints the build's source identity, protocol and capabilities
as JSON (see `docs/frame-workspace/NATIVE.md`). The state contains texture size, received/decoded/uploaded counts, measured
decode/upload rates, last decode/conversion/upload/sync/submit durations, dropped
frames, errors, pending recenter and the saved transform. A held static picture
adds no frame count. These are desktop-update rates, **not compositor FPS**.
The aggregate `errors` counter is split into `networkErrors`, `serverErrors`,
`decoderSendErrors`, `decoderReceiveErrors`, `decoderFatalErrors` and
`submitErrors`; fatal decoder code and stage remain in `decoderError` and
`decoderErrorStage`.
`conversionRequested`, `conversionActive`, `conversionFallbacks`,
`decoderPixelFormat`, `decoderColorSpace` and `decoderColorRange` identify the
conversion in use. `convertMs` measures the CPU conversion stage;
for GPU conversion it includes the CPU plane copy, also reported separately as
`planeCopyMs`. `gpuConvertMs` measures CPU wall time spent issuing the plane
uploads and shader pass; GPU completion is included in `syncMs`. Neither timing
alone predicts end-to-end throughput or compositor presentation rate.

Recovery counters include `queueOverflows`, `droppedAwaitingIdr`,
`overflowRecoveryIdrs`, `lastOverflowRecoverySeq`, `staleDriverFramesDropped`
and `decoderTimingHighWater`. Decoder-open attempts, initial/reconnect/format
opens, failures and cumulative open time are separate counters. `decoderOpenMs`
is the last open duration; `decoderOpenTotalMs` is cumulative.
`packetQueueLimit` and `packetQueueHighWater` expose the compressed queue bound
and its lifetime peak. `packetQueueWaitMs` is the most recent dequeued packet's
receive-to-dequeue time. `emptyReceives`, `emptyReceiveTotalMs` and
`emptyReceiveMaxMs` separately measure receive calls returning EAGAIN; they are
not successful decode or GPU execution timings.

## Window panels

With the native Workspace viewer selected, Show for a Mac window starts a native
viewer of its own instead of a Chrome panel. It has its own unit, runtime files,
OpenVR key `com.frame-workspace.native.UID.panel-wID`, saved pose and hardware
decoder session (`--panel ID [--name TITLE] [--initial-pose JSON | --pose JSON]` before the
launcher command, plus `stop`). The window is captured as itself at its own size
and backing scale; it is not moved onto a separate display or resized. Its first
position is beside the Workspace: on the circle around the viewing point at the
Workspace's distance, just past its edge (right, then left, then further out),
turned to face the viewer, and as wide as the window would be at the Workspace's
scale, so text matches. Without a running Workspace it recenters ahead. Closing
the window's stream ends its viewer, and the companion removes its runtime files.
Verified on the Frame 2026-10-19 with a 986×729-point Terminal window: a
1984×1472 texture with V4L2 decoding at ~56 updates/s beside the running
Workspace, with clean removal on Stop. `FRAME_WINDOW_VIEWER=browser` keeps the
older Chrome panels. In-headset movement and sizing are described below.

### Shared window host (FW-15, opt-in)

`FRAME_NATIVE_HOST=1` on the companion selects one window-host user unit,
`frame-workspace-host-UID.service`. The native Workspace keeps its own viewer.
Without the flag, window panels keep the existing per-window units. This reuses
all existing window overlays and halves, Palladium layers, controller handles,
drag/resize/bend/close behavior, control files and saved poses; it adds no UI.

The host owns one SDL/GL context and one OpenVR client. Each panel has the same
bounded network and decoder workers as the standalone path. The shared runtime
pumps SteamVR events and samples both controllers once per loop. Each hand goes
to its nearest panel; an ongoing drag retains that hand until release. A bad or
closed stream ends only that panel. The host stays alive when its last panel is
removed, so another window can reuse the session.

The launcher sends an atomic mode-0600 spec to
`$XDG_RUNTIME_DIR/frame-workspace/host/panels/ID.json`. Its public entry point is
unchanged, with `FRAME_NATIVE_HOST=1` in the remote environment:

```sh
FRAME_NATIVE_HOST=1 scripts/workspace-native-on-frame.sh --panel w42 --name Terminal \
  start "$VIEW_URL" 1280 800 fc12345678 v4l2 gpu
FRAME_NATIVE_HOST=1 scripts/workspace-native-on-frame.sh --panel w42 stop
```

The executable's direct interface is `--host-dir SPEC_FOLDER --state-file HOST_STATE`.
Keep the host summary outside the spec folder. A spec has this shape (absolute
paths, unique window key and files; `initialPose` is an optional pose JSON string):

```json
{
  "version": 1,
  "generation": "fc12345678",
  "role": "window",
  "urlFile": "/run/user/1000/frame-workspace/panel-w42/view-fc12345678.url",
  "overlayKey": "com.frame-workspace.native.1000.panel-w42",
  "overlayName": "Terminal",
  "controlFile": "/run/user/1000/frame-workspace/panel-w42/control.json",
  "stateFile": "/run/user/1000/frame-workspace/panel-w42/state.json",
  "poseFile": "/home/frame/.local/share/frame-workspace/panels/w42-pose.json",
  "requestDir": "/run/user/1000/frame-workspace/requests",
  "decoder": "v4l2",
  "conversion": "gpu"
}
```

Specs are immutable within a `generation`. Removing one stops its panel;
changing its generation first destroys the old panel, then creates the new one.
Clear the previous generation's control file after removal is acknowledged,
before adding a new generation, as the launcher does. An unchanged failed
generation stays terminal. Malformed spec replacements leave an existing panel
running. Duplicate keys or private file paths are rejected.
Per-panel state keeps the existing schema and adds `hostGeneration` and
`hostPid`; the launcher matches both to avoid stale readiness. The launcher
serializes host ownership changes, confirms individual removal before replacing
a saved pose, and removes only that panel after a startup failure. Saved poses
stay in the existing paths. The companion remembers each launch's host choice
and generation for cleanup.

WebSocket text `{"t":"still"}` processes already accepted packets, preserves
the last completed picture/texture, and closes the decoder session on its owning
thread. The hardware final-output drain is bounded; final-picture availability
before release still needs device testing. `{"t":"live"}` waits for an IDR
containing SPS/PPS before reopening.
These controls work on standalone viewers too. State adds `streamStill`,
`decoderSessionOpen`, `decoderStillReleases` and `decoderLiveOpens`. A still
panel retains its display texture and owned picture data, with no open decoder
session. Receiving `live` alone does not allocate one.

Each host loop samples `GetEyeTrackedFoveationCenter` through `gaze_bridge` and
combines the two standing-space eye rays. It intersects every ready panel using
`bend.hpp`, including uneven and vertical bends, and chooses the nearest front
surface. After a 180 ms dwell on a different window, it atomically writes
`{"action":"focus","key":"PANEL_OPENVR_KEY","u":0.5,"v":0.5}` to the existing
requests folder. Coordinates are right/down from the original window's top left,
independent of its bend. The active panel has a 2 cm edge margin; a new target
must be 1 cm inside its edge. Missing eye tracking uses the controller ray (right
hand preferred); looking away with valid eye tracking does not use that fallback.
Mac-side focus delivery, live-set policy and cursor memory require the companion
integration and end-to-end verification.

Verification commands and the exact device acceptance boundary are recorded in
[SPATIAL.md](../docs/frame-workspace/SPATIAL.md#shared-host-foundation-fw-15).

Profile Save now captures the Workspace pose and each uniquely matched native
window's saved pose from the Frame. Schema v2 stores these against the stable
bundle ID/title rule, so Profile Restore can apply them to fresh Mac window IDs.
Files absent at save time leave that entry without a pose; missing, retitled,
ambiguous and duplicate window matches retain the existing skip diagnostics.
Browser profiles remain logical only. Pose-bearing profiles require native
viewers; switching backends does not prevent reading them.

The launcher accepts `--pose JSON` before `start` for either the Workspace or a
window panel. It strictly validates pose version 1, finite numeric values,
width 0.4–8 m, distance 0.4–10 m, curvature 0–1, and a 12-number matrix with
components within ±100 and a right-handed orthonormal rotation (row-dot tolerance
0.02, matching `placement.hpp`). It then atomically replaces the pose file with
mode 0600 after confirming the old viewer stopped and before starting the new
one. This override wins over a stale pose for the current panel ID.
`--pose` cannot accompany `--initial-pose` or a command other than `start`.
Without a saved profile pose, the existing pose-file/initial-beside-Workspace
behavior is unchanged. Restoring a Workspace pose restarts its viewer rather
than taking the Show Again recenter shortcut; its Mac display can be reused.

Local unit tests cover capture through mocked Frame calls, schema v1 migration,
fresh-ID restore, and the actual launcher with stubbed services. **Not yet
verified on the device:** profile save/restore of multiple moved, resized and
curved panels, stale-pose precedence, same-preset restore while the Workspace
is running, and placement after a reconnect or SteamVR restart. Poses use the
SteamVR room origin; preserving the same physical location after room setup or
origin changes is not established.

Native macOS green-button full screen is still manual: **FW-07 automatic
attachment is gated by FW-06**, so `FRAME_WORKSPACE_AUTODETACH` has no effect yet.
The 2026-10-20 macOS 26.3.1 spike verified windowed fixture capture with existing
permissions but could not test native entry/exit or inactive-Space capture while
the console was locked. Earlier active full-screen capture evidence does not
establish capture after switching Spaces. See the
[verified/inferred findings and go/no-go](../docs/frame-workspace/FULLSCREEN.md#current-spike-attempt-2026-10-20).
Before enabling automatic native panels, the integrator must prove changing
capture while the target remains full screen in an inactive Space, stable window
identity, exit/close cleanup and rapid-toggle deduplication. Then verify beside
an active Workspace on the Frame: placement, stream cleanup, manual-close
suppression, no action without a Workspace, opt-out, focus/input and reconnect.
No device verification or installed-agent update was performed in this attempt.

## In-headset placement (FW-14)

SteamVR's own laser only serves its dashboard: a probe overlay with mouse input
and `MakeOverlaysInteractiveIfVisible` received no laser events on the Frame
(2026-10-19). The viewer therefore reads the controllers itself through SteamVR
Input, using `input/actions.json` installed beside the binary: an `aim` pose
(controller tip) and a trigger `grab` per hand, bound for `frame_controller`.
The action set uses the global overlay priority, and SteamVR's event queue is
pumped every loop; without that, the actions stayed inactive. A probe then
received a continuous aim pose and exact trigger presses.

Each window wears Palladium chrome in silver-lavender
(`docs/frame-workspace/evidence/palladium-vr-handles-mockup.png`, approved
2026-10-20), with a solid 30 mm frame and a separate controls layer:

- **Bezel** (`<key>.frame`, at the picture front plane): the F2 machined bezel, about
  1% of the window's width (2–3.5 cm), with an inward chamfer, a chiseled line
  round its face and a soft shadow on the window's edge. The picture and front
  face meet 30 mm forward of the saved placement plane. Four physical side-wall
  overlays extend back to that plane as a closed rectangular rear shell recessed
  beneath the bezel's rounded lip. Curved edges use radius-based coplanar sampling
  (1 mm chord-error target, capped at 32 facets) packed into one overlay per edge,
  with the same construction transposed for vertical bends. Full-depth pillowed
  normals and shared corner normals soften the lighting; the rear shell has
  exact planar joins, not extruded quarter-cylinder corners.
- **Controls** (`<key>.handles`, 32 mm in front): a move pill and a separate
  close disc with an engraved X, 3 cm below the bezel, revealed only by a laser
  in their shared row plus a 4 cm margin; and
  four corner Ls and two side grips, 6 mm Palladium rods that slide out when
  the laser leaves the window towards them, widen to 15 mm with grip texture
  under the laser (at 45° across a corner's bend), and press in and glow from
  inside, brightest along the recess's walls, while dragged.

`chrome.hpp` draws both with one GLSL shader into textures (premultiplied
alpha), lit from the upper left, and redraws a layer only when something on it
changes. The laser works as in Stargate: no dot, a small white-hot core where
it lands (one host-owned cursor, on the focused window's hit layer), and the metal within
about 5 cm brightening in the laser's blue, most on faces turned towards it.
The layers follow the window's curvature on concentric cylinders, and the
laser is intersected with each layer's own cylinder. The pill carries the
window rigidly with the controller, corners resize it about its centre, and
the side grips curve it as they are pulled towards the viewer; a finished drag
saves the pose. The measurements live in `handles.hpp`, shared by the hit
zones and the shader. `--render-chrome SCENE OUT.ppm` draws the chrome flat
for checking it without the headset. `--action-manifest PATH|none` overrides
or disables input.

Historical pre-depth validation, 2026-10-20 on the Frame (not worn): the shader compiles and renders
the same on its GPU (zink on Turnip) as on the Mac, and with the Workspace
running all three layers are visible, sorted in front of the window, 1.5 and
3 cm out, with matching curvature. Curved, the old handles layer stayed flat
behind the window, so its edges (bending towards the viewer) hid the corners
and side bars. `probe_chrome_curvature.c` checks that this is gone: rays cast
from the centre of curvature with SteamVR's own intersection test met the bezel
1.5 cm and the controls 3 cm inside the window's cylinder at every angle, still
in front just past the window's edges, with positions across matching the
window to within 0.001, at curvature 0.168 and 0.45 (the window wrapping 162°;
2026-10-20). Not yet verified: how they look and feel worn.

**Feel** (2026-10-20): a light controller tick when the laser comes onto a
handle, a firmer one when it is taken and a soft one on release
(`/actions/workspace/out/haptic`). A side's handles stay out 0.35 s after the
laser leaves, and the handle under the laser holds until the laser is 1.5x the
pick-up distance away, so neither flickers at a boundary.

**Bending** (FW-14, `bend.hpp`): a side grip pulled with the trigger bends only
its own half; pulled with the grip button (`/actions/workspace/in/squeeze`) it
bends both evenly. Top and bottom grips bend top to bottom the same way;
changing direction starts from flat. SteamVR only curves an overlay side to
side about its middle, so halves that differ are two overlays (the window's
`<key>.half`, and `.half` copies of the bezel and controls), arcs tangent at the
centre line and overlapping by 2 mm. These three extra overlays are created only
while halves differ and destroyed when they match again; an even curved window
uses the same count as a flat one. A top-to-bottom bend turns each overlay a
quarter turn on its face and draws its texture transposed (the last picture is
redrawn at once, so a still desktop turns too). The pose file keeps the bend as
`"bend":{"vertical":..,"a":..,"b":..}` (a: left or bottom half, b: right or
top), and the control file accepts the same object. The Mac page's curvature
sets an even side-to-side bend. Gaze sharpening pauses while a window is bent
unevenly or top to bottom.

Verified 2026-10-20 on the Frame (not worn) with `probe_bends.cpp`, which
checks SteamVR's own ray intersections against the expected surfaces: even,
right half only, uneven halves, top to bottom (even, top only, uneven) and back,
all within 0.1 mm on the window, bezel and controls. SteamVR's intersection
geometry follows its mouse scale, so every overlay's mouse scale is its
submitted texture's size. With the headset idle, SteamVR applies overlay
changes to its intersection tests a few seconds late; the probe retries. Frame
pacing was unchanged with the window in two halves (no new drops; pictures
12-18 ms old); switching shape drops a few pictures. Not yet verified: how the
bends look and feel worn.

The close disc closes that window (or stops the Workspace): the viewer writes
`{"action":"close","key":KEY}` into `--request-dir` and the Mac companion
acts on it.

### Spatial stream and focus contract (FW-15)

The Mac helper accepts authenticated `POST /streams/live?src=separate:42,separate:7`
(`k` is the companion token). The list selects live window sources; all other
`window:` and `separate:` streams become still. Display streams, including the
Workspace, stay live. An empty `src` selects no windows; `all=1` removes the
selection and restores current and future window streams to live.

On each affected stream, WebSocket text `{"t":"still"}` means no more binary
pictures until `{"t":"live"}`. Keep displaying the last texture; the viewer
may release its hardware decoder. The first picture after `live` is a keyframe
with SPS/PPS, sufficient to recreate the decoder. Other text messages (including
stats) may continue while still. The Mac keeps observing content at a reduced
capture cadence without encoding it. A new stream delivers its initial picture
before honoring still, so the viewer has a texture to retain; startup decoder
headroom remains a device check. `/status` reports per-stream `width` and
`height` in live encoded pixels, `live`, and `lastChanged` in Unix seconds
(zero before content is observed), plus `focusedSrc` and `pointerShared`.

Gaze focus uses the existing request directory,
`$XDG_RUNTIME_DIR/frame-workspace/requests/*.json`. Write a temporary file and
atomically rename it to `.json`, as for Close:

```json
{"action":"focus","key":"com.frame-workspace.native.1000.panel-w42","u":0.25,"v":0.75}
```

`key` is the panel's registered OpenVR key. Coordinates are finite numbers in
`[0,1]`, with `u` increasing right and `v` down from the top-left. With
`FRAME_WINDOW_DISPLAYS=1`, the companion resolves an owned window panel key to
its source and calls `POST /focus?src=separate:42&u=0.25&v=0.75`. Unknown keys
are ignored, batches keep the newest valid focus request, and dispatch is
limited to four per second. The helper raises that window and restores its
remembered window-relative cursor position; the first visit uses `(u,v)`.
Repeated focus on the same window must leave the cursor where it is.

The companion's one-second supervisor selects live windows within
`FRAME_SPATIAL_DECODE_MP` (default **18 MP**), after reserving pixels for display
streams. Focus takes first admission, then recent changes, newest first, with
one second before optional admission, three seconds of residency, and five
seconds of eligibility after a change. Focus and capacity override residency.
Missing dimensions are not admitted; an oversized focused window remains still
until the stream size or budget permits it. Existing display streams are not
stopped if they alone exceed the budget. Disabling window displays restores all
windows to live. `POST /pointer?shared=1|0` synchronizes shared pointer mode:
mouse buttons carry across viewer sessions, while keys retain session ownership.

These are the Mac-side foundation and shared viewer contract. Integration still
needs device verification of decoder release/recreation, gaze, cursor memory,
and cross-panel drags; see [SPATIAL.md](../docs/frame-workspace/SPATIAL.md).

## SteamVR bar panel (FW-08)

`frame-workspace-dash` (`dash.cpp`, drawn by `dash_ui.hpp` with cairo/Pango)
is the single "Frame Workspace" entry on the SteamVR bar for all the virtual
screens. It owns a dashboard overlay, which SteamVR's own laser drives, and
shows the Workspace (Start Workspace, or Stop Workspace and Recenter) and a tile
per floating window with a Close button (up to 9, then "+N more"; `--layout
list` gives one row per window instead). The build script builds it next to
the viewer; install it in the same folder.

The Mac companion runs `scripts/workspace-dash-on-frame.sh sync STATE` once a
second while the Frame is connected. That writes
`$XDG_RUNTIME_DIR/frame-workspace/dash-state.json`, starts the panel's user
unit (`frame-workspace-dash-UID.service`) if it isn't running, and hands back
the requests made since: `workspace_start` (with the settings last used on the
Mac), `workspace_stop`, `recenter`, `close` with a panel key, and Spatial Mode
`focus` with a window panel key and `(u,v)`. Additional Spatial Mode request
shapes are documented below. A state file older than 6 s shows "Mac Not
Connected", so the entry stays on the bar after the companion quits. Start,
Recenter, and Close require the Mac link; Stop remains available for Frame-local
cleanup of the owned Workspace services and can queue window restoration when
the Mac link is available. `FRAME_WORKSPACE_DASH=0` turns the sync off (the MCP
adapter's backend does).

```bash
frame-workspace-dash --state FILE --request-dir DIR [--images DIR] [--layout tiles|list]
frame-workspace-dash --render-mock OUT.png list|tiles running|stopped|offline
```

Verified 2026-10-25 (Frame, SteamOS): the updated panel builds and starts, and its
offline render exposes Stop. With HMD0 in standby, an
OpenVR event-queue probe checked `IsViewingPermitted` for the owned dashboard,
then posted MouseMove/Down/Up at Stop while the synced state exposed
`stopAvailable=true`. The production journal recorded `workspace_stop`; the
bounded `sleep` fixture in the exact Workspace host unit stopped and did not
respawn during a 10-second check. This verifies event routing and unit cleanup
without starting a Mac display. It does not verify worn laser interaction or a
hung-host fallback.
The bar thumbnail and worn hover/click behavior remain unverified.

## Bounded pipeline and synchronization

One network thread acknowledges and reads packets independently of one decoder
thread. At most sixteen compressed access units wait for decoding. This absorbs
Wi-Fi delivery gaps of about 200 ms at 60 fps, which overflowed the earlier
eight-packet bound, without discarding a valid H.264 reference chain. Overflow still discards
that queue and asks for an IDR. Software flushes at recovery; V4L2 keeps its
context for a same-size overflow IDR and retires older timestamp tokens.
Delayed driver output through that barrier is discarded; zero, negative,
missing or unknown timestamps remain errors.
Only the newest completed picture waits for the GL owner: CPU-converted RGBA or
owned copies of supported YUV planes. There are no unbounded work queues. The
initial decoder is explicitly software H.264 with
slice threading; swscale converts at the source's native resolution, honoring
BT.709/BT.601 and full/limited range metadata. This CPU conversion remains the
default. Explicit V4L2 selection is available for controlled device comparison
(below). Optional `--conversion gpu` (or `FRAME_WORKSPACE_CONVERSION=gpu`)
copies supported decoder planes into owned bounded-picture storage, then
converts NV12 or YUV420P to the same full-resolution RGBA8 texture on the GL
owner thread. Color range and matrix metadata feed the conversion shader.
Decoder-owned mapped buffers are released before the picture enters the queue.
Other decoded pixel formats use swscale and report `conversionActive` as
`cpu-fallback`; `conversionFallbacks` counts those frames. A shader setup or GL
conversion error is reported as an error rather than silently changing paths.
The CPU path remains selectable with `--conversion cpu` and is the default for
both the executable and launcher.

For each new picture, the GL owner writes a full-resolution RGBA8 texture in
one of three slots, each with its own source-plane textures. The CPU path uploads RGBA; the GPU path uploads 1.5 bytes per pixel of NV12 or
YUV420P planes and converts them into that texture with a fragment shader. The
GPU path reduces source-plane upload bytes while keeping full-resolution OpenVR
texture dimensions and contents. The default `fence` handoff places a GL fence
after each picture's work, flushes, and calls `SetOverlayTexture` only after
that fence has signalled, oldest first. SteamVR therefore never imports an
incomplete texture (the Frame Zink lesson), while the next picture's upload
overlaps the GPU's conversion of the previous one. A slot is reused only when it
is neither displayed nor in flight; vrclient blits each submitted texture into
its own shared texture inside this GL context. `--handoff finish` (or
`FRAME_WORKSPACE_HANDOFF=finish`) restores the previous serial
`glFlush(); glFinish()` handoff in one slot. The GL owner sleeps until a
decoded picture arrives or in-flight work completes, rather than on a fixed
interval. State reports `handoff`, `gpuLatencyMs` (issue to observed
completion), `handoffBlockingWaits`, and the per-second maxima
`decodeStepMaxMs` and `arrivalGapMaxMs`, which separate decoder stalls from
network bursts.

The default `paced` presentation (`--present paced|immediate`, also
`{"present": ...}` in the control file at runtime) submits finished pictures in
order on headset refresh boundaries, using `GetTimeSinceLastVsync`. At 120 Hz,
60 fps content then shows for an even two refreshes each. When pictures queue
up behind a delivery burst, the next follows after one refresh so the queue
drains without skipping motion; only a backlog beyond three is dropped
(`pacedDrops`). If the refresh counter stalls (headset asleep), it submits
immediately (`pacedFallbacks`). `presentGaps` counts refreshes between
submissions: 1, 2, 3, 4+.

The Frame's iris V4L2 decoder held every picture of the Mac's stream until the
next access unit arrived (output lag 1, ~23 ms hold), even though the SPS
declares no reordering. The cause is `pic_order_cnt_type 0`. x264's POC type 2
streams, and the same Mac stream rewritten to type 2, were released about 5 ms
after queueing at 2880×2560. The V4L2 path therefore rewrites each access unit
to POC type 2 (`h264_poc.hpp`; `--v4l2-poc-rewrite off` disables it). Only SPS
POC fields and slice-header `pic_order_cnt_lsb` change; FFmpeg decodes the
rewrite bit-identically (`native/tests_h264_poc.py BINARY`, macOS). A stream
feature the rewriter does not handle turns it off and restarts at the next IDR.
`decoderHoldMs`, `decoderOutputLag` and their per-second maxima report the hold.
`--rewrite-poc IN OUT` rewrites a file for diagnosis. Texture names change on resolution changes, avoiding stale
SteamVR texture metadata. No per-eye rendering, synchronous steady-state GPU readback or display
refresh loop is involved. `--dump PATH.ppm` performs one explicit diagnostic
readback of the first successfully submitted application texture.

Pictures are stored top row first, but SteamVR reads GL textures bottom-up,
so the overlay's texture bounds flip V (`{0, 1, 1, 0}`). The earlier `{0, 0, 1, 1}`
bounds showed the desktop upside down and mirrored in the lenses, which
application readback could not detect. The flip was confirmed worn on
2026-10-19. Valve's API specifies an app-created GL texture and upper-left-to-lower-right UV
bounds. See [SetOverlayTexture](https://github.com/ValveSoftware/openvr/wiki/IVROverlay%3A%3ASetOverlayTexture)
and the [pinned interface documentation](https://github.com/ValveSoftware/openvr/blob/ae46a8dd0172580648c8922658a100439115d3eb/headers/openvr.h).
The API return plus application readback does not prove the final lens image or
absence of inter-process synchronization artifacts.

## Verification

```sh
# Local headless production GL upload, 2 seconds; no OpenVR runtime:
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 native/build/frame-workspace-native \
  --no-vr --test-pattern --seconds 2 --dump /tmp/native-pattern.ppm
# Real protocol, H.264 decoder, conversion, GL upload/readback, control and timing:
python3 native/tests_stream.py native/build/frame-workspace-native
python3 native/tests_stream.py native/build/frame-workspace-native --reconnect
# Sharp chroma edges and smooth ramps through production GL readback, both
# supported plane layouts, color ranges and matrices, plus stream recovery:
python3 native/tests_gpu_conversion.py native/build/frame-workspace-native
# On an existing device GL display, bypass Xvfb and its software-renderer env:
python3 native/tests_gpu_conversion.py native/build/frame-workspace-native --use-display
python3 native/tests_decoder.py native/build/frame-workspace-native --use-display
# Full-resolution hardware stream gates; serialize against the owned viewer
# unit so another V4L2 decoder is not active. Frame booking/off-head consent
# required; never stop SteamVR or global Gamescope.
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --decoder v4l2
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --decoder v4l2 --reconnect
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --decoder v4l2 --size-change
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --decoder v4l2 --overload
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --decoder v4l2 --conversion gpu --case bt601-limited
# Device diagnostic, only after the coordinator owns the Frame lock:
native/build/frame-workspace-native --test-pattern --seconds 15 \
  --state-file /tmp/native-state.json --dump /tmp/native-pattern.ppm
```

The stream integration test creates temporary synthetic H.264 frames, sends
them through the production WebSocket path, validates acknowledgement/focus/
clock and frame messages, compares GPU readback to FFmpeg output, then removes
its video and capture data. Hardware tests use a finite, valid 61-access-unit
sequence with AUD boundaries and alternating adjacent IDR picture IDs. The
overload case requires a delivered frame at or beyond the recovery IDR, one
hardware open, bounded timing metadata and zero decoder/submission errors.
The YUV layout tests upload synthetic NV12 and
YUV420P planes through the production shader and compare full-frame readback
against a direct libswscale `sws_scale` conversion using the same color settings
as the viewer. A sharp chroma-edge fixture is retained alongside smooth ramps;
the separate FFmpeg CLI `scale` result is reported because its chroma phase can
differ from the viewer's direct swscale API path on sharp NV12 edges.
`--test-pattern` generates an asymmetric four-quadrant
grid with a moving bar; it is diagnostic content, not a product UI.

Current local evidence: self-test passed; 1920x1080 test-pattern GL readback
visually inspected; production stream test decoded and uploaded 72 frames with
zero errors at 320x180, maximum RGB difference 2 versus the FFmpeg oracle.
A forced-disconnect run resumed using only the issued reconnect key (52 frames,
one expected disconnect), and a second process retained the saved pose.
Compact evidence is in `evidence/host-validation.json` and `evidence/host-pattern.png`.
The local renderer was ARM64 Mesa llvmpipe. The later
[device record](../docs/frame-workspace/NATIVE.md) covers real desktop textures
and explicit hardware decoding. Head motion and wearer acceptance remain open.

## Explicit decoder comparison

The software decoder remains the default. Set `FRAME_WORKSPACE_DECODER=v4l2`
when starting the companion with `FRAME_WORKSPACE_VIEWER=native` to request the
Frame's `h264_v4l2m2m` decoder. Invalid values fail before creating the Workspace
display. The launcher accepts optional sixth and seventh start arguments
(`software|v4l2` and `cpu|gpu`) and passes `--decoder` and `--conversion` to the
executable. `FRAME_WORKSPACE_CONVERSION=gpu` selects the optional conversion
through the companion environment; invalid values fail before the display is
created. Decoder selection has no automatic fallback.

State reports `decoderRequested`, `decoderCodec`, and `decoderActive` (empty until
that decoder produces a valid converted picture), plus `decoderError` and its
stage. The existing `decoder` and Mac clock report identify the actual codec and
pending/active state. Normal exits report `exitReason`: timer, signal number,
control stop, server close, SDL Quit or OpenVR Quit. Decoder failures exit nonzero
with their FFmpeg error code and stage, even if an earlier picture was displayed.

V4L2 requests CPU-mappable NV12 and retains the same RGBA texture contract. The
default CPU conversion isolates decoder selection; `--conversion gpu` can be
selected separately for a matched device comparison. Unique packet timestamp tokens map
delayed output back to the correct sequence and receive time. Input EAGAIN drains
output before retrying that packet. First-keyframe startup does not flush an
unused decoder. On overflow, a validated same-size IDR resets the H.264 reference
state without reopening V4L2, whose FFmpeg wrapper has no flush callback. Real
reconnects and parsed visible/coded dimension changes reopen the hardware
context. Software reconnects flush the existing context and retire its timing
metadata. The sixteen-packet and one-newest-picture limits remain in place;
timestamp metadata is separately bounded to 64 entries. Unsupported output formats or missing timestamps
fail explicitly instead of reporting guessed timing or software acceleration.

`sendMs` measures the send-packet API call; `decodeMs` now combines that cost with
the successful receive-frame call. These are decoder API wall times, not GPU
execution time. Queue age uses the output frame's original received timestamp.
The [device pipeline record](evidence/frame-pipeline.json) preserves matched
30/60-target desktop runs and strict recovery/image-oracle tests. These do not
establish sustained 60 fps or worn head-motion acceptance.

For a matched desktop run, start the owned 9:8 Workspace with adaptation
disabled and no concurrent decoder tests/builds, then run
`python3 native/bench_desktop.py --label cpu-30 --out /tmp/workspace-cpu30.json`.
The tool creates and closes only its disposable scrolling Chrome fixture,
measures counter deltas over 30 seconds and omits window inventories and room
transforms from its report. Change rate/conversion between runs and wait for
readiness before recording the next condition.

On the tested Frame, Xvfb lacks GLX: the default Xvfb stream test fails before
native control/video startup. For that device, select the existing GL display
explicitly with `DISPLAY=:0` and `--use-display`, after off-head consent and
stopping the reserved Workspace viewer to free its hardware decoder. The test
uses a hidden SDL window and `--no-vr`; it does not restart SteamVR. An error
return is checked before dependent control acknowledgments so a graphics-startup
failure is reported directly. All control, image, dimension and error assertions
remain in effect.

## Spatial shelf, switcher and focus (FW-15)

The shared host reads `$XDG_RUNTIME_DIR/frame-workspace/spatial-ui.json` every
50 ms, with a 1 MiB file bound for large app/window inventories.
`--spatial-state PATH` overrides it for isolated runs. `--request-dir DIR`
overrides the action destination; otherwise it is `requests/` beside that state
file (or beside the host-spec directory when XDG and the override are absent).
The host never creates a Mac app/window inventory or changes the mode itself.
The companion atomically publishes:

```json
{"mode":"spatial","apps":[{"bundleID":"com.apple.Terminal","name":"Terminal","icon":"com.apple.Terminal.png","running":true,"pinned":true,"hidden":false,"active":true,"windows":[{"key":"PANEL_OPENVR_KEY","title":"zsh"}]}],"switcher":{"open":false,"index":0,"seq":0,"chosen":null}}
```

`apps` and each app's `windows` retain companion order. `index` addresses the
flattened windows array. `icons/` beside the state contains actual 128×128 PNGs;
only filenames are accepted. Missing icons do not prevent interaction; the host
rechecks icon timestamps once a second so icons arriving after JSON appear. Cairo,
already required by the bar-panel executable, supplies PNG decoding and UTF-8
text rasterization through its runtime API; GL draws the Palladium material and
composites the labels, icons and live panel textures. There is no new native link
dependency. `mode != "spatial"` hides every spatial UI layer.

The D1 shelf is placed once and then stays where it is: no head-following. Its
frame sits 1.3 m ahead of the head's horizontal facing, at head height, with the
icon row 0.48 m below it. A saved pose wins; otherwise the first valid head pose
places it there (not saved until it is moved or recentered). The pose lives in
`$XDG_DATA_HOME/frame-workspace/panels/spatial-shelf-pose.json` (`spatial-shelf-pose.json`
beside the state file when `--spatial-state` is given), in the window pose format,
and is checked like one (orthonormal rows, within 10 m) plus handedness. The
status band and the app menus move with it. A pose that fails is ignored.

Move it the way a window moves: the same Palladium move pill slides out under the
shelf when a controller laser comes near it, and a trigger (or grip, while the
switcher is closed) press on the pill carries the shelf rigidly with the
controller until release, which saves the pose. A SteamVR recenter (seated or
standing zero pose reset) or the dashboard's Recenter in Spatial Mode brings the
shelf back in front of the user and saves that. The dashboard request reaches the
host through the companion: it publishes `recenterSeq` in the spatial state, and
the host recenters when that counter grows (the first value it sees only sets the
baseline). The switcher cards keep their own head anchor (0.8 rad snap).

At rest the shelf is 60% of full size. A controller laser or the eye-gaze ray on
it, within 1.5 cm (gaze 3.5 cm) of its edges, enlarges it over 200 ms with a
smootherstep ease, and magnifies the icons like the macOS Dock: the icon under the
pointer reaches 1.5x, falling off as a cosine bell over 2.2 icons, the row
stretching around the pointer so the spot under it stays put, and icons grow upward
from a level base. When the ray leaves, it waits 500 ms, then shrinks back over
200 ms; while enlarged the hit region grows to 6 cm (8 cm for gaze), so a ray near
the edge does not flicker it. Icons relax in 150 ms as soon as the ray leaves the
icons. `spatialLayout` takes the shelf's scale, magnification, pointer and pose, and
its tiles are the geometry both drawn and picked, so clicks land on the icon under
the ray at every size. The shelf consumes gaze like any foreground UI: looking at it
magnifies it but does not change the focused window. The shelf backing is drawn at its
current size (whole 8 px steps) into the corner of a texture that stays the size of
the shelf's largest form (full size, icons fully magnified), and the overlay shows that
corner through its texture bounds, so SteamVR sees one texture size while the shelf
grows, shrinks and magnifies and does not recreate its remote image on every frame.
The texture is reallocated only when the shelf's content changes (its app count or
status band). `tests_spatial_render.cpp` checks this with the renderer's allocation counter.

Its icons keep the
companion's order and show running dots. Trigger release opens an app; a
500 ms uninterrupted hold opens Show All Windows, Hide (Unhide for hidden apps),
and Quit. An outside trigger click dismisses the menu. Dock presses and open
menus bind to the bundle ID, so list changes cannot retarget an action. Holding
the trigger before entering an icon does not click it. The
shader lights only the laser vicinity; the pressed piece glows only while held.

S1 is an eye-level arc of live thumbnails. It shares the existing panel textures,
including still frames; it does not create decoders. Vertical-bend textures are
unturned when sampled. Up to seven cards are visible around the current index;
Cmd-Tab advances the companion index and brings further windows into view.
While the strip is open, left/right grip presses move to the previous/next card,
wrapping at the ends; those grips are consumed before window placement. Local
controller index survives same-sequence app refreshes; a newer Mac sequence wins.
The selected index has a lit edge. The default controller binding is left Y
(`/actions/workspace/in/switcher`) to open/close. A trigger click or 250 ms gaze
selects a visible window. Selection recenters the existing panel at its saved
distance, preserves its width/bend, saves its pose, and sends a switch request.
The selected panel and its chrome use sort orders 51–54; other panels return to
1–4, below the spatial UI at 99/100. Gaze and controller picks follow that
foreground priority even when an obscured panel is physically closer.
Spatial overlays declare premultiplied alpha consistently with window chrome.
Unavailable/closed keys are ignored. A non-null `chosen` is consumed once per
`seq` during a host lifetime, even with `open:false`; local dismissal stays closed
across unrelated state refreshes until acknowledged or a new sequence arrives.

Requests use the existing atomic mode-0600 file writer:

```json
{"action":"app","verb":"open","bundleID":"com.apple.Terminal"}
{"action":"app","verb":"show_windows","bundleID":"com.apple.Terminal"}
{"action":"app","verb":"hide","bundleID":"com.apple.Terminal"}
{"action":"app","verb":"unhide","bundleID":"com.apple.Terminal"}
{"action":"app","verb":"quit","bundleID":"com.apple.Terminal"}
{"action":"switch","key":"PANEL_OPENVR_KEY"}
{"action":"switcher","verb":"open"}
{"action":"switcher","verb":"close"}
```

The visible foreground window receives gaze feedback, otherwise the nearest
bent window, with controller fallback when eye input is unavailable. An
uninterrupted 250 ms builds the existing bezel
halo; successful atomic focus delivery triggers its snap and clears the prior
focused bezel. Short glances fade without selection; residual glow cannot shorten
a later dwell. Spatial UI consumes gaze and controller hits so windows behind it
do not focus or start handle interactions. Still windows keep their usual picture.

Spatial rendering is capped at 30 Hz. Static shelf/menu content is cached until
its visual state changes. Thumbnail content refreshes even if the source reuses
the same GL texture name; texture storage is allocated only on size changes.
No device performance claim follows from these implementation choices.

### Launcher (FW-15 A)

`launcher.hpp` (data, layout, hit testing, gestures), `launcher_render.hpp` (Cairo drawing)
and `launcherTiles` in `spatial.hpp`. The companion adds a `launcher` object to the state:

```json
{"available":true,"open":false,"seq":3,"catalog":1761442810000,"query":"pa","results":["com.example.paint"],"selected":0,"pageStep":0,"keyboard":true}
```

and writes the catalogue it names to `launcher.json` beside the state, only when it changes:

```json
{"catalog":1761442810000,"apps":[{"id":"com.apple.Safari","name":"Safari","v":"26.3-…"}],"items":[{"app":"com.apple.Safari"},{"folder":"folder-1","name":"Games","apps":["com.apple.Chess"]}],"recent":["com.apple.Safari"]}
```

The host reads `launcher.json` when the state names a catalogue it does not have (retrying
every 250 ms until the file matches), and checks it like the writer: valid bundle IDs, each
app once, no empty folders, at most 600 apps; apps the items leave out join the end. With an
available launcher the shelf's first slot is an Applications button (tile kind `Apps`, after
the app tiles in the list so their indices stay put), magnified like the icons and lit while
the launcher is open. Opening is local and immediate, as the switcher's is: the host sends
`{"action":"launcher","verb":"open"}` and keeps it open until the companion's `seq` moves on.
The panel opens 1.15 m ahead of the head's horizontal facing at eye level, 1.1 × 0.68 m with
OpenVR curvature 0.152 (a cylinder round the head); a feathered black backdrop at 45%
(sort order 90, 1.4 m, 72% of a circle) dims the windows behind and not the shelf. The panel
is order 101, a carried icon 102. While it is open, windows take no hands and gaze does not
change focus.

Layout is in the mockup's units (600 × 372): the field at the top, Recent Apps (up to eight),
a groove, then 8 × 3 cells a page and the page dots at y 352. The texture is 1440 × 893. A
search shows `results` in the companion's order (folders included); an open folder shows its
apps in a recessed area with Back. Presses bind to what is under the laser:
`launcher:recent:<id>`, `launcher:cell:<id or folder:id>`, `launcher:dot:<page>`,
`launcher:back`. With the eyes on an icon, a hand aimed at nothing presses it. Clicking an app
sends `{"action":"app","verb":"open","bundleID":…}` and closes the launcher; a trigger aimed at
nothing closes it. Holding 500 ms on a cell (not while searching, not by the eyes) picks it
up; the drop gap is before a cell's left 30%, after its right 30%, nothing over its middle,
and the end past the last icon. Held 650 ms at the left or right edge, the page turns. A drop
that changes the order is applied locally and sent as
`{"action":"launcher","verb":"move","id":…,"before":…|null,"folder":…|null}`. `pageStep`
counts the Mac's Left/Right arrows on an empty field; a new query starts at its first page; the
keyboard selection keeps its page in view. Host state reports `launcherOpen`, `launcherApps`
and `launcherItems`.

Painting happens when what is shown changes (hover, page, query, a carried icon's gap, an icon
file arriving); the laser spot is a GL pass over the cached picture. Labels use Cairo's toy
font API, as the shelf does, so scripts that font lacks fall back to its boxes.

`--render-scene` accepts a `launcher` object: `catalog` (inline, the `launcher.json` format),
`query`, `results`, `selected`, `keyboard`, `folder`, `page`, `hover` (`{"kind":"cell"|"recent"|"dot"|"back","index":N}`)
and `carry` (`{"id":…,"x":…,"y":…}` in panel units). Icons are `<bundle>.png` beside the JSON.
The evidence pictures come from generic fixture icons (no real app artwork) and the
companion's own search:

```sh
python3 native/scene/generate-launcher-scenes.py "$scratch"
for n in open search folder carry; do
  native/build/frame-workspace-native --render-scene "$scratch/launcher-$n.json" \
    native/evidence/launcher-$n.png --look 0,-5 --fov 72 --size 960x600
done
```

`launcher-open.png` (Recent Apps, the grid, the laser on an icon, the button lit),
`launcher-search.png` ("pa": Paint by name, Écran Partagé by word, the first selected),
`launcher-folder.png` (Utilities open, the laser on Back) and `launcher-carry.png` (an icon
carried between two others) were compared with mockup A.

### Shared Display Surfaces (Option B)

The [shared-display contract](../docs/frame-workspace/spatial-shared-contract.md)
adds `role: "window" | "menubar" | "status" | "menu"` to host specs; omitted
roles remain windows. The launcher accepts `--role ROLE` before its command.
Auxiliary roles require `FRAME_NATIVE_HOST=1` and reserved IDs `menubar`, `status`,
and `menu0` through `menu3`. Source names `menu:0` through `menu:3` remain in the
stream URL. Invalid roles, mismatched auxiliary IDs, and auxiliary `--pose` or
`--initial-pose` options fail before service or file mutations. Existing window
launches keep their standalone or opt-in host route. Auxiliary launch specs omit
foveation/gaze negotiation, which applies to window streams.

Auxiliary overlays have no placement input, chrome/handle overlays, initial or
saved pose. Pose controls cannot move them. They are excluded from focus and
switcher selection, but consume gaze and controller rays before underlying
windows or shelf tiles. Desktop Mode hides them. Dropdown textures have a thin,
rounded silver-lavender Palladium rim. The menubar panel and shelf status use
the materials described below. `SurfaceRenderer` uses the same upload path as
production video; there are no separate asset files.

The menubar follows `HostFocus.active` along the top-left bezel's bend tangent.
Its own rounded, chamfered panel uses the bezel's Palladium palette and lighting,
with a 6 mm picture inset. The panel is 8 mm in front of the window, behind the
30 mm bezel, and in sort band 51 below the focused bezel's 52. At rest only a
4 mm lip shows above the bezel at 0.35 of the window width. The picture keeps its
point scale as the visible crop slides upward and widens to `menusWidth` plus
the inset. Controller hits on the lip, companion `hot`, or an uploaded visible
menubar-anchored menu expand it; a 1-second grace period precedes collapse.
The linear 0.12-second slide matches the handles. Covered pixels do not take
host gaze/laser hits; visible hits map to the inset source picture. Held still
pictures redraw with the changing crop as well as streamed pictures.

The shelf extends at least 52 mm below the icon row as one piece. Its width still follows
the icons. Status is right-aligned inside that band, with 6 mm padding, at 1 mm
per point; it shrinks uniformly when too wide. A taller picture extends the band downward
to preserve its natural scale.
It has no separate rim. Status-menu placement uses the fitted scale and the item's
horizontal position, with clearance above the whole shelf. Other menu offsets use
`rect.minX - anchorRect.minX` and `rect.minY - anchorRect.maxY`, at the anchor's
point scale, 1 cm forward. With `anchor: "none"`, the focused window is the fallback
anchor; absent anchors keep the surface hidden.

Move pills and close discs start hidden, including on focused windows. A controller
laser below the bezel in their row plus margin reveals them in 0.12 seconds; they
wait 350 ms after leaving before fading out on that same cadence. Hover, press and
an ongoing pill move hold them fully shown. Hidden controls and transparent padding
pass host laser ownership through to surfaces behind. The existing tip light and
pressed inner glow remain. Both hands contribute reveal zones.

An open dropdown's selected title uses the pressed handle's concave bevel,
Palladium palette and inward blue reflection. The source title's ink is retained
across transparent, light and opaque accent/dark captures; its global
`selectedRect` maps through the picture inset and the strip's existing curve.

A validated version-1 `surface` text message carries `seq`, `visible`, `rect`,
`anchor`, `anchorRect`, optional `menusWidth`, and `hot`. Auxiliary streams start
hidden. A show/re-show waits for a successfully uploaded picture whose sequence
is strictly newer than the fence and which arrived after the message. Hide is
immediate on the next host iteration. Older metadata is ignored, 32-bit sequence
wrap is supported, and same-sequence hot-only updates retain an eligible held
picture. Reconnect invalidates previous metadata. State exposes `role`,
`surfaceVisible`, `surfaceSeq`, `surfaceRevision`, `uploadedSeq`, and
`surfaceHeight` for local diagnostics. A Spatial Mode focus snap writes the
existing companion request and queues `{"t":"focus","u":...,"v":...}` on the
selected window's stream; switcher selection sends centre coordinates.

**Contract limitation:** the current host spec/hello has no window point width.
The host estimates the focused window's metres per point as
`2 * panelWidthMetres / uploadedWidthPixels`, matching uncapped 2x capture. A
stream downscaled by its encoder cap makes that estimate too large. Exact scale
requires the helper/companion packages to carry the actual current window point
width (or point-to-pixel scale) to the native host; those files are outside this
package. Placement helpers accept an exact metres-per-point value already.
No live helper or headset run was performed for this package.

The four compact PNGs in `native/evidence/` are synthetic Mac content rendered
through the production surface material, inspected locally on Apple M4 OpenGL:

```sh
native/build/frame-workspace-native --render-spatial-style menubar-slim native/evidence/menubar-slim.png
native/build/frame-workspace-native --render-spatial-style menubar-wide native/evidence/menubar-wide.png
native/build/frame-workspace-native --render-spatial-style status native/evidence/status.png
native/build/frame-workspace-native --render-spatial-style menu native/evidence/menu.png
```

`menu` now names the streamed dropdown PNG scene; `app-menu` preserves the older
D1 app-action-menu PPM diagnostic. These images establish renderer appearance,
not SteamVR presentation, tracked ray input, real Mac menu discovery/input, focus
latency, or worn comfort.

**Local verification (2026-10-21):** `scripts/build-workspace-native.sh` and its
self-test passed with zero warnings. On macOS the unchanged Linux build script
uses a temporary `gl.pc` with `Libs: -framework OpenGL` and
`Cflags: -DGL_SILENCE_DEPRECATION`; the temporary compiler wrapper marks Homebrew
include directories as system headers to suppress dependency-only json-c
unused-function warnings. This is an Apple M4 development build, not a Linux
ARM64 or headset build. `python3 native/tests_host.py
native/build/frame-workspace-native` passed both integration scenarios: existing
host lifecycle/still/live/pose isolation, and four roles across five loopback
streams with surface fences, hide/re-show, hot updates, focus text and companion
request delivery, absent auxiliary pose files, ignored pose controls and
Desktop Mode isolation. `PYTHONPATH=tests python3 -m unittest
tests.test_native_pose_launcher` passed 20 tests. The self-test additionally
covers role parsing, focus exclusion, placement, sequence wrap and reveal timing.
Temporary binaries and fixtures are removed after verification; retain these
commands and the four small PNGs to reproduce the checks. Use `TMPDIR` and
`FRAME_NATIVE_TEST_TMP` to direct integration/launcher scratch to an isolated
folder; `PYTHONDONTWRITEBYTECODE=1` avoids test bytecode files. Cleanup removed
6.73 MiB of generated binaries, debug symbols, build metadata and scratch.
All loopback processes and sockets were closed; the four PNGs total about 52 KiB
on disk and remain as the current review deliverables.

Local diagnostic scenes use the production renderer:

```sh
native/build/frame-workspace-native --render-spatial-style rest /tmp/spatial.ppm
native/build/frame-workspace-native --render-spatial-style hover /tmp/hover.ppm
native/build/frame-workspace-native --render-spatial-style held /tmp/held.ppm
native/build/frame-workspace-native --spatial-state /tmp/fixture/spatial-ui.json --render-spatial-style app-menu /tmp/menu.ppm
native/build/frame-workspace-native --render-chrome focus-glow /tmp/glow.ppm
native/build/frame-workspace-native --render-chrome focus-snap /tmp/snap.ppm
```

With four shelf apps, the shelf backing, status, four reserved dropdown slots,
menu strip, shared cursor and SteamVR bar panel plus thumbnail, six ordinary
windows use 56 overlays and nine use 77. Each window uses 7 flat/even-curved or
10 with split bends; a split menubar adds one. All-nine-split is 104–105, still
below the system ceiling of 128. Before the wall change each window reserved 7
including its cursor and three idle halves; six/nine totals were also 56/77.
The retained UI high-water adds up to seven switcher tiles and three app-menu
rows: nine ordinary windows then use 87, or 115 with every window and the menu
strip split. Hidden retained UI overlays are included. Other applications consume
additional system overlays.

The shared host now applies a total overlay budget of 96 by default;
`FRAME_NATIVE_OVERLAY_BUDGET` can set a value from 1 through SteamVR's 128-overlay
limit. The dashboard overlay and its thumbnail are owned by the separate
dashboard process, so the host reserves those two slots and reports both
`hostOverlayCount` and `dashboardOverlayReserve`; `overlayCount` includes the
reservation and `overlayBudget` is the configured total. Required pictures,
frames, controls, bends, menus, shelf/status surfaces, spatial tiles and cursor
handles are admitted first. When required overlays need room, the host drops
whole four-overlay wall groups from the windows furthest from gaze, then the
least recently focused among ties; the focused window's walls are protected.
The joined picture and bezel remain, and wall picking falls back to the flat
frame surface while its walls are unavailable. Walls return as whole groups,
focused and nearest first, only when the reported total including the group is
at least four below budget. Required overlays still allow a window to open when
the configured budget is exhausted; the host logs the first such overrun once.
Wall drop/restore transitions are coalesced per window at most once per second,
with the final pending state flushed on shutdown. Focus protection and gaze
priorities refresh before allocations and immediately when focus changes.
The nine-window split/retained-UI stress case drops five wall groups and uses
95 overlays, including the two-slot dashboard reservation, while preserving all
77 required host overlays. Nine flat/even windows retain their walls at 87.

The optional state override supplies fixture icons/labels; thumbnails in this
standalone diagnostic are deliberately asymmetric color fixtures. Alternating
cards use normal and physically quarter-turned textures. These are renderer
checks, not captures of Mac windows or a lens image. See
[evidence/spatial-ui-validation.json](evidence/spatial-ui-validation.json) for
commands, source identity and verification boundaries.

`--self-test` covers layout, hit tests, focus timing, trigger gestures, menu
identity and strip navigation, plus the production overlay-budget allocator's
drop/restore ordering, focused protection, hysteresis, and nine fully split
windows within the 96-overlay default. On a development machine with a GL display,
`tests_host.py` exercises the production host with loopback streams and isolated
state, including an inventory larger than 16 KiB. The renderer regression test
checks late icons and in-place live-texture updates through actual GL readback:

```sh
c++ -std=c++17 -O2 -pthread native/tests_spatial_render.cpp native/gaze_bridge.cpp -o /tmp/fw-spatial-render-test $(pkg-config --cflags --libs sdl2 gl libavcodec libavutil libswscale json-c openssl) -ldl
/tmp/fw-spatial-render-test
rm /tmp/fw-spatial-render-test
```

**Verified 2026-10-20:** local Apple M4 GL build, self-test, host integration and
renderer regression; isolated Frame AArch64 build using
`scripts/build-workspace-native.sh` and a second explicit `--self-test`.
The Frame check exercised no GL/OpenVR session or installed viewer. Real eye
tracking, controller bindings, SteamVR presentation, Mac action delivery and
worn comfort remain acceptance checks. Temporary builds and fixtures were removed.

### Offline Spatial Scene

`--render-scene` composites the native host's overlays into a perspective PNG,
without OpenVR, the headset, or a companion connection:

```sh
native/build/frame-workspace-native --render-scene native/scene/example.json native/evidence/spatial-scene.png
native/build/frame-workspace-native --render-scene native/scene/example-slim.json native/evidence/spatial-scene-slim.png
# Metres; yaw right and pitch up in degrees. Options follow the two paths.
native/build/frame-workspace-native --render-scene native/scene/example.json /tmp/scene.png --eye 0.08,0.04,0 --look 5,-3 --fov 100 --size 1920x1200
```

The default camera is at the origin, looking along -Z, with a **100-degree
horizontal FOV stand-in**: this repository does not document the Frame's lens
FOV. Output defaults to 1920×1200, rendered at twice each dimension and resolved
from four samples per pixel. The dark room and floor grid at -1.6 m are depth
references, not passthrough. This is a monoscopic software composition of the
host's submitted surfaces, not SteamVR compositor, optical, or worn evidence.

The scene lists `panels` (`id`, `role: "window"`, `placement` in the existing
version-1 pose format or a nonnegative wall `slot`, `texture`, optional metre `height`, `focused`, `focusGlow`,
`focusSnap`, `pillLaser`), `apps` (`id`, `name`, 128×128 `icon`), and optional `menubar`,
`status`, `dropdown` entries (`texture` and the existing `surface` message, including optional menubar `selectedRect` in global points).
An optional menubar `reveal` from 0 to 1 forces a transition frame for diagnostics.
An optional `shelf` object pins and draws the shelf as the host does: `scale` (1 full
size, 0.6 at rest), `magnify` (0 to 1), `icon` (the app index under the laser, which
sets the pointer and lights it) and `handle` (true shows the move pill). Without it the
shelf is the old full-size one. The shelf pictures in `evidence/shelf-*.png` come from
`native/scene/shelf-{before,rest,magnified,move}.json`, rendered with
`--render-scene native/scene/shelf-rest.json native/evidence/shelf-rest.png --look 0,-24 --fov 62 --size 960x560`.
`pillLaser` aims the scene ray at that window's pill and reveals its below-edge controls.
PNG paths are relative to the JSON. Input is premultiplied through Cairo.
Only one window may be focused. The fixture poses are explicitly arranged to
show an even bend, unequal side bends, and unequal vertical bends; they are not
a new default layout. `python3 native/scene/generate-fixtures.py` regenerates
all synthetic textures and both manifests using the existing Cairo dependency.
No real window content is included in those fixtures.

The compositor consumes `bendSegment` transforms/widths/UV bounds, including
the 2 mm split seam, curvature radius `width / (2*pi*curvature)`, concentric
layer offsets, and vertical texture orientation. It uses `ChromeRenderer`
with the same picture/bezel front at 30 mm, four rearward walls, 32 mm controls and shared cursor,
`SpatialRenderer`'s rendered D1 shelf/dock textures, `SurfaceRenderer`'s
auxiliary material, and `menuStripPlacement`, `statusStripPlacement`, and
`surfacePlacement`. Overlay sort bands preserve the selected window in front
of other windows and the shelf/menu bands above it. The right-controller ray
is a diagnostic line ending at the production cursor/tip; its pose is synthetic.
The hot fixture shows one dropdown. The slim fixture hides it because the live
`MenuStripReveal` keeps the strip wide while a dropdown is open.

To render current Mac window pictures, with existing Screen Recording permission:

```sh
python3 scripts/render-spatial-scene.py --output /private/tmp/real-spatial-scene.png
```

The script preflights permission without requesting it, lists up to six normal
on-screen windows, captures their pictures, exports running-app icons, and
captures only the built-in display's menu-bar region, split at the leftmost
intersecting layer-25 status-item window. It calls the companion's actual
`SpatialSharedPolicy.menuBarSplit` function and uses the same rectangle rules
as `SpatialShared.refresh()`. It never activates, moves, resizes, or
closes windows or changes settings. It stops if permission or the built-in
display is unavailable. The script retains only the requested private PNG and
its `-slim.png` counterpart; all captured inputs, scene metadata, and compiler
scratch are deleted on success or failure. Existing outputs are not overwritten.
There is no synthetic dropdown in the private capture, since opening a menu
would change the user's desktop. The split uses window bounds only, with no
accessibility reads. If the hot strip extends outside the default view, the
script turns the camera toward the midpoint of the surfaces' horizontal angular
bounds, using the same yaw for both
images. It retains the 100-degree FOV and all production sizes and positions.

**The private capture script reproduces the earlier overlapping defaults.**
It uses flat panels, 2.4 m wide and 2 m ahead of an identity HMD pose, with height
from stream aspect, so the frontmost captured window is focused and occludes much
of the others. It does not read saved headset poses. Current Spatial Mode assigns
the L2 wall slots described below; use the wall fixtures to reproduce that layout.
Desktop Mode still uses `beside_workspace_pose`. The separate `spatialLayout` arc is for switcher thumbnails,
not streamed windows. Menubar scale follows the current host's `2 * panelWidth /
uploadedWidthPixels` estimate; the status strip follows its fixed .001 m/point.

### Spatial Wall (L2)

The companion assigns stable lowest-free `--slot` numbers: focused/frontmost,
left, right, then the same order below; slots 6–9 extend to ±50 degrees and
10–13 to ±75 degrees. Restored poses bypass allocation. The native host computes
placement in `wall.hpp`, shared with `--render-scene`'s panel `slot` field. Content
fits a 0.64 × 0.40 m cell at 1.75 m, preserving aspect, at +11.5/-6.5 degree rows
and 25 degree columns. The smaller content rectangle reserves space for the
production bezel halo, expanded handles, move pill and close disc; nominal
0.70 × 0.50 m content would overlap those controls. Pure tests project the visible
chrome at its actual depth and check adjacent cells through slot 13, plus the D1
shelf and status strip. Transparent raster padding is not visible chrome.

A valid head pose supplies yaw and position once per Spatial session, ignoring
pitch/roll. The Menu Bar panel generation identifies that session even when all
windows close; a new generation resets an otherwise surviving shared host.
Hand moves, resizing, bends, nudges and recentering detach the panel and save its
pose. Generation-scoped release requests free its companion slot. Automatic wall
placement is not written as a manually remembered pose. Pose readers accept
widths down to 0.01 m so narrow portrait cells round-trip; manual resizing retains
its normal 0.4 m minimum except for an already narrower panel.

Reproduce mixed landscape/portrait evidence without captures or headset access:

```sh
# Use the approved task scratch directory for wall_scratch.
PYTHONDONTWRITEBYTECODE=1 python3 native/scene/generate-wall-scenes.py "$wall_scratch"
native/build/frame-workspace-native --render-scene "$wall_scratch/wall-6.json" native/evidence/spatial-wall-6.png
native/build/frame-workspace-native --render-scene "$wall_scratch/wall-9.json" native/evidence/spatial-wall-9.png --fov 125
```

`--self-test` includes wall radius/order/facing, aspect/portrait pose round-trip,
level session anchoring and projected chrome/shelf/status clearance. Host tests
exercise slotted streamed panels, malformed slot isolation, release on recenter,
saved-pose precedence and session generations; scene tests exercise the same
slot geometry through the GL renderer. Companion tests cover focused ordering,
freed-slot reuse, restored poses, release generations and launcher validation.
These are synthetic/local checks; they do not establish worn readability or
headset acceptance. Remove generated scene inputs and local build output after
verification; retain only the six- and nine-window evidence PNGs.

Validation snapshot (2026-10-21, local macOS build): the native build completed
without warnings and both its built-in and explicit `--self-test` passed. Binary
SHA-256 was `b67bf262a277778ec408d597b3a715e52d81c7db873532cc8bddc57ecf9266f7`.
The required companion command ran 177 tests: 164 passed and 13 real-helper HTTP
checks timed out; an earlier run instead encountered the shared helper's
per-user Spatial lease. The native host test timed out waiting for its first
panel, and the scene test stalled on its first render. A standalone SDL-only
program reproduced the startup stall at `SDL_Init` → `NSApplication init` →
SkyLight `SLSMainConnection`, before workspace geometry runs. Process-local
background mode and a temporary LaunchServices app reproduced it; SDL's
offscreen driver could not initialize OpenGL. No system settings or dependencies
were changed. Host/scene integration and both wall PNGs remain pending a working
GUI runtime; no renders or visual acceptance are claimed by this snapshot.
Re-run the commands above and the required host/scene/companion suites after
that blocker clears. Generated build output, fixtures and diagnostic logs are
removed after retaining this compact reproduction record.
Cleanup removed 7,151,477 bytes (6.82 MiB), including the local build, temporary
scene inputs, adapter, standalone diagnostic programs and logs.
