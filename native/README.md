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
sample a pose every video frame. The current HMD pose is sampled only when
initially placing or explicitly recentering the screen.

The visible content remains the approved Mac workspace UI. Keyboard and trackpad
input stay on the Mac. There is no browser window, keyboard capture, new menu,
scene environment, or MSAA pass.

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
show. The state contains texture size, received/decoded/uploaded counts, measured
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
2026-10-20), on two layers in front of it so each eye sees real depth:

- **Bezel** (`<key>.frame`, 1.5 cm in front): the F2 machined bezel, about
  1% of the window's width (2–3.5 cm), with an inward chamfer, a chiseled line
  round its face and a soft shadow on the window's edge.
- **Controls** (`<key>.handles`, 3 cm in front): a move pill and a separate
  close disc with an engraved X, 3 cm below the bezel and always there; and
  four corner Ls and two side grips, 6 mm Palladium rods that slide out when
  the laser leaves the window towards them, widen to 15 mm with grip texture
  under the laser (at 45° across a corner's bend), and press in and glow from
  inside, brightest along the recess's walls, while dragged.

`chrome.hpp` draws both with one GLSL shader into textures (premultiplied
alpha), lit from the upper left, and redraws a layer only when something on it
changes. The laser works as in Stargate: no dot, a small white-hot core where
it lands (`<key>.cursor`, on whichever layer it meets), and the metal within
about 5 cm brightening in the laser's blue, most on faces turned towards it.
The layers follow the window's curvature on concentric cylinders, and the
laser is intersected with each layer's own cylinder. The pill carries the
window rigidly with the controller, corners resize it about its centre, and
the side grips curve it as they are pulled towards the viewer; a finished drag
saves the pose. The measurements live in `handles.hpp`, shared by the hit
zones and the shader. `--render-chrome SCENE OUT.ppm` draws the chrome flat
for checking it without the headset. `--action-manifest PATH|none` overrides
or disables input.

Verified 2026-10-20 on the Frame (not worn): the shader compiles and renders
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
centre line and overlapping by 2 mm; a top-to-bottom bend turns each overlay a
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
`focus` with a window panel key and `(u,v)`. The companion ignores other actions. A state file
older than 6 s shows "Mac Not Connected", so the entry stays on the bar after
the companion quits, with the buttons off. `FRAME_WORKSPACE_DASH=0` turns the
sync off (the MCP adapter's backend does).

```bash
frame-workspace-dash --state FILE --request-dir DIR [--images DIR] [--layout tiles|list]
frame-workspace-dash --render-mock OUT.png list|tiles running|stopped|offline
```

Verified 2026-10-20 (Frame, SteamOS as installed that day), with the panel not
yet looked at worn: the panel builds and starts on the Frame, renders the
offline, stopped, running and one-window states, and requests written to the
folder start the Workspace, recenter it, close a window panel and stop the
Workspace. Not yet verified: the bar thumbnail, and laser hover and clicks on
the dashboard overlay.

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
