# Native Workspace overlay

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
JSON and change `revision` for each command:

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

`ready` becomes true only after a successful texture submission and overlay
show. The state contains texture size, received/decoded/uploaded counts, measured
decode/upload rates, last decode/conversion/upload/sync/submit durations, dropped
frames, errors, pending recenter and the saved transform. A held static picture
adds no frame count. These are desktop-update rates, **not compositor FPS**.

## Bounded pipeline and synchronization

One network thread acknowledges and reads packets independently of one decoder
thread. At most three compressed access units wait for decoding. Overflow discards
that queue, asks for an IDR and flushes the decoder before accepting that keyframe.
Only the newest completed RGBA picture waits for the GL owner. There are no
unbounded work queues. The initial decoder is explicitly software H.264 with
slice threading; swscale converts at the source's native resolution, honoring
BT.709/BT.601 and full/limited range metadata. The default path does not claim hardware acceleration. Explicit V4L2 selection
is available for controlled device comparison (below); GPU YUV conversion is
not implemented.

The GL owner uploads a full-resolution RGBA8 texture only for a new picture. It
flushes then finishes producer GPU work once before `SetOverlayTexture`, with the
wait measured separately. This conservative synchronization follows the Frame
Zink incomplete-image lesson; it needs device/worn validation for this overlay
path. Texture names change on resolution changes, avoiding stale SteamVR texture
metadata. No per-eye rendering, synchronous steady-state GPU readback or display
refresh loop is involved. `--dump PATH.ppm` performs one explicit diagnostic
readback of the first successfully submitted application texture.

Valve's API specifies an app-created GL texture and upper-left-to-lower-right UV
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
# Device diagnostic, only after the coordinator owns the Frame lock:
native/build/frame-workspace-native --test-pattern --seconds 15 \
  --state-file /tmp/native-state.json --dump /tmp/native-pattern.ppm
```

The stream integration test creates one temporary synthetic H.264 frame, sends it
through the production WebSocket path, validates acknowledgement/focus/clock and
frame messages, compares GPU readback to an FFmpeg reference, then removes its
video and capture data. `--test-pattern` generates an asymmetric four-quadrant
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
display. The launcher accepts an optional sixth start argument (`software` or
`v4l2`) and passes `--decoder` to the executable. There is no automatic fallback.

State reports `decoderRequested`, `decoderCodec`, and `decoderActive` (empty until
that decoder produces a valid converted picture), plus `decoderError` and its
stage. The existing `decoder` and Mac clock report identify the actual codec and
pending/active state. Normal exits report `exitReason`: timer, signal number,
control stop, server close, SDL Quit or OpenVR Quit. Decoder failures exit nonzero
with their FFmpeg error code and stage, even if an earlier picture was displayed.

V4L2 requests CPU-mappable NV12 and retains the same RGBA conversion/upload path,
so the comparison isolates decoder selection. Unique packet timestamp tokens map
delayed output back to the correct sequence and receive time. Input EAGAIN drains
output before retrying that packet. First-keyframe startup does not flush an
unused decoder. On queue overflow/reconnect, the next IDR recreates the V4L2
context because the upstream FFmpeg wrapper has no flush callback. The three
packet and one newest-picture queue limits remain in place; driver timestamp
metadata is separately bounded. Unsupported output formats or missing timestamps
fail explicitly instead of reporting guessed timing or software acceleration.

`sendMs` measures the send-packet API call; `decodeMs` now combines that cost with
the successful receive-frame call. These are decoder API wall times, not GPU
execution time. Queue age uses the output frame's original received timestamp.
V4L2 throughput and device recovery still require the coordinator's matched tests.
