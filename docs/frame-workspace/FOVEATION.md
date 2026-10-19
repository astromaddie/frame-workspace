# Native direct foveated capture (opt-in)

The current experiment captures a low-resolution whole display and a detailed
region directly with ScreenCaptureKit, then combines them at their original
screen positions in one fixed-size NV12 picture. It does not pack an atlas or
warp the desktop. The ordinary H.264 decoder and one OpenVR overlay receive the
same dimensions throughout gaze movement and tracking loss. H.264 remains the
transport; this change does not optimize the codec.

Public ScreenCaptureKit exposes rectangular cropping and output dimensions,
not a per-region density/rasterization-rate map. This reduces the pixels delivered
by the capture streams, not the rendering work inside other Mac applications.
A full-size GPU composition and full-size encode/decode remain necessary. See
Apple's [SCStreamConfiguration](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration)
and [sourceRect](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/sourcerect).

## Fixed dimensions and centre fallback

New native clients request `foveation=1&direct=1`; the companion must explicitly
answer `foveation:1,direct:1`. The existing `FRAME_WORKSPACE_FOVEATION=1` gate is
required on both ends. The feature remains opt-in. An unsupported peer or source
ends the direct session explicitly; there is no automatic packing or full-quality
capture fallback. Ordinary sessions without the opt-in retain their existing path.

Wire mode3 uses the existing 20-byte extension. Source and encoded width/height
are equal. The even-aligned half-width by half-height detailed region describes
pixels already in their ordinary positions; native rendering must not reconstruct
it. The parser rejects unsupported modes, reduced encoded geometry, out-of-bounds
regions, packets before negotiation and geometry changes within a connection.
Legacy mode0/1/2 parser support remains for bounded historical tests, but new
production `--foveation` connections require direct mode3 throughout.

Missing, invalid, stale or uncalibrated gaze chooses a detailed **centre** crop.
It does not select an unfoveated encoded fallback. Session nonce, sample ordering,
150ms freshness and 50ms future limits remain in force. Gaze coordinates stay in
session memory and are not logged. Source capture failure is an explicit error.

## Crop provenance and costs

The low-resolution whole-display stream delivers one quarter of the baseline
pixels; the full-detail half-width by half-height crop delivers another quarter.
Their normal steady capture output is therefore 50% of the baseline pixels,
while the composed and encoded picture remains full size.

Each detailed crop stream has an immutable source rectangle and layout. Moving
it creates a bounded replacement generation, with coalesced gaze targets; a late
callback can never be placed using a newer requested origin. This deliberately
avoids treating `updateConfiguration` completion as a queued-frame flush: Apple
does not document that guarantee, and `SCStreamFrameInfo` does not document a
configuration-generation echo. Replacement startup, temporary overlap and
cross-stream synchronization can outweigh the capture reduction, so fewer
capture pixels alone is not evidence of improved throughput.

## Independent gaze producer

A bounded producer samples eye tracking independently of the render loop and
posts into the existing newest-sample network slot. Rendering reads status; no
render/GL work runs on the producer. Geometry synchronization excludes long
uploads and `glFinish`, and shutdown joins the producer before destroying OpenVR
resources. The required `glFlush(); glFinish()` compositor handoff is retained.

## Host verification of the direct path

`mac/frame-mac-view/test-direct-capture.sh --host /tmp/frame-direct-evidence`
creates and removes its own 644×484 non-16:9 display and pattern window. It never
replaces the active Workspace, installed companion or headset viewer. The checks
cover actual ScreenCaptureKit source rectangles, all NV12 luma/chroma samples in
both ranges, fixed dimensions under attempted size adaptation, loss/reentry
callback generations, and centre fallback. The real Session test additionally
covers unsupported/disabled requests, startup failure, direct hello only after
successful capture, VideoToolbox framing and gaze invalidation/expiry.

The native direct-sequence test runs that actual SCK/VT output through the strict
handshake, decoder, and ordinary GL texture. CPU RGB uses the FFmpeg CLI oracle.
GPU RGB uses independently decoded YUV and the BT.709 video-range equations,
matching the existing shader contract. The first GPU comparison against CLI RGB
failed at 3/255 (mean 1.77): libswscale's integer RGB conversion differs from the
floating-point conversion. The independent equation check matched all 935,088
channel values exactly; CPU output matched CLI exactly. The test preserves the
separate CLI deviation and tightens the GPU equation tolerance to 1/255; it does
not hide the failed comparison by increasing a tolerance.

Temporary-sequence native commands (use the local no-VR GL display on macOS;
Linux defaults to Xvfb):

```sh
clang++ -std=c++17 -O2 -isystem /opt/homebrew/include \
  native/tests_direct_protocol.cpp -o /tmp/direct-protocol-test
/tmp/direct-protocol-test
python3 native/tests_direct_negotiation.py /tmp/native-candidate --use-display
python3 native/tests_direct_capture.py /tmp/native-candidate \
  /tmp/frame-direct-evidence/direct-host.sequence --use-display --conversion gpu
python3 native/tests_direct_capture.py /tmp/native-candidate \
  /tmp/frame-direct-evidence/direct-host.sequence --use-display --conversion cpu
```

Current direct support is **display-only**. Independent windows, Separate and
TestSource are rejected rather than substituted. Crop replacement is capped at
10Hz even though eye acquisition runs near 60Hz. A replacement must start a new
SCK stream, so this is a correctness prototype with material crop-following
latency, not an accepted 60fps or gaze-to-photon optimization. A mostly static
fixture does not measure moving-desktop throughput; long overall output gaps
include intentionally unchanged periods. Device/worn evidence is separate.

### Initial off-head comparison

The first installed candidate (Mac `394c0c25…`, native `42f9c8ad…`) was compared
with one ordinary/full, one centre and one moving-diagnostic 30-second scrolling
run, in that order. Source/coded dimensions stayed 2880×2560; each used the same
2880×2530 Chrome fixture, 8-second startup warmup, requested 60fps, V4L2 and GPU
conversion, with no concurrent builds/tests. These are desktop texture updates,
not compositor or optical FPS, and one ordered trial is not a sustained benchmark.

| Mode | Fresh drawn/s | H264 payload Mbps | Errors | Format reopens |
| --- | ---: | ---: | ---: | ---: |
| Ordinary full |41.85|3.11|0|0|
| Direct centre |41.80|4.69|0|0|
| Direct moving diagnostic |38.27|6.28|0|0|

Source capture/sending remained approximately 55/s in the first two cases; the
loss is later in the pipeline. Full-size encoding/decoding remains unchanged,
so this capture prototype has not demonstrated a route to 60fps. The moving run
accepted 2000 diagnostic samples but also recorded 36 stale arrivals, 15 expiry
checks and 4 fallback transitions; there were no layout-driven decoder resets.
No actual valid-eye/worn claim follows from synthetic gaze.

Ordinary streaming was restored with the saved pose hash unchanged. The native
receiver completed about 55 packets/s, decoded about 50 pictures/s and uploaded
about 41/s in the full/centre runs. The latest-only picture slot deliberately
replaces unconsumed decoded pictures; received/decoded/uploaded counts need not
match. Both TCP endpoints already disable Nagle. The synchronous GL handoff and
render-loop scheduling/bursts need per-frame profiling to explain the remaining
loss; low decoder API timings alone do not identify the limiting stage.

A subsequent review flagged the 150ms cross-stream age gate: without a newer ROI
confirmation it intentionally holds composition. Public SCK idle events have no
guaranteed heartbeat cadence. Targeted tests with an unchanged centre and a
separately animated window wholly outside it nevertheless progressed correctly
on this Mac: 118 outputs in 2.6s, all 8 peripheral levels, unchanged centre,
78.58ms maximum output gap and no skew holds. ROI idle callbacks carried current
displayTime/PTS. A later latency check observed a 387.75ms output gap even with
fresh ROI idle events and no skew holds: the base callback itself was 342.5ms
old. The matched full-capture baseline also exceeded 150ms (189.46ms). An
absolute output-gap limit is therefore different from the 150ms emitted-pair
skew contract. The regression checks actual pair skew, correct pixels and
continued late-stage progression; output gaps remain reported latency limits.
An animated-centre control also progressed. No normal-operation
freeze was reproduced, so the capture behavior was retained and the case added
as a regression. The screenshot-sample alternative returned correct 322×242 NV12
pixels but cost 51.7ms median/81.75ms p95/115.25ms maximum; it was not adopted.

The final regression delivered all eight peripheral animation levels in 67
outputs over 2.6 seconds while preserving the exact unchanged focal pixels.
The animated-centre control delivered 100 outputs and all eight levels in both
regions. Maximum emitted pair skew was 33.334ms; observed output gaps were
177.458ms and 112.458ms. The final moving-crop probe checked 194 outputs at 32
positions with zero pixel errors (maximum luma difference 1/255).

The final installed Mac binary is `48a3e139…`; the native ARM64 binary remains
`42f9c8ad…`. The follow-up only adds optional diagnostics and the regression;
capture behavior is unchanged. Its installed direct path negotiated successfully,
kept 2880×2560 and selected centre detail with unavailable off-head eye tracking.
Ordinary streaming is the restored default. Latest pre-change executables and
pose/metadata are retained in `/tmp/frame-workspace-direct-rollback` on each
machine until wearer acceptance. Exact hashes and aggregate evidence are in
[FOVEATION-VALIDATION.json](FOVEATION-VALIDATION.json). Temporary worktrees, builds,
logs and captures were removed after consolidation (82.57 MB of files across
this task; local free space increased 67.10 MB during final cleanup). Current
executables, the active companion and latest rollback were preserved.

### Gaze producer verification

The standalone lifecycle test passes under ThreadSanitizer: bounded cadence,
main/render thread blocked300ms, provider exceptions, original acquisition
stamp on delayed calls, stop/join, restart and exception unwinding. Production
WS→decode→GL with three injected stalls measured301.09/301.05/301.06ms,
15 fresh socket-received samples in each, maximum gaps31.89/28.92/27.09ms and
mapped arrival ages−0.17…17.40ms. The matched previous render-loop producer,
using the same warmup and direct protocol, received zero fresh samples in a stall.

Reconnect/clock-jitter and partial-message regressions also pass. This proof does
not establish global startup freshness: before first-GL warmup, a separate
observation found eight arrivals up to283.27ms old (mapped age283.24ms). None
occurred during/after the measured stall windows. The exact startup scheduling
cause remains unresolved; sender100ms and receiver150ms freshness checks are
unchanged, so stale arrivals still select centre fallback. OpenVR calls have no
cancellation API: shutdown joining depends on an in-flight call returning.
Real eye sampling, runtime geometry/curvature and wearer acceptance remain open.

For local macOS checks with the existing Homebrew dependencies (no native
pkg-config installation), build the same source with:

```sh
host_flags=(-std=c++17 -O1 -g -pthread -Wno-unused-function
  -I/opt/homebrew/opt/sdl2/include/SDL2 -I/opt/homebrew/opt/json-c/include
  -I/opt/homebrew/opt/ffmpeg/include -I/opt/homebrew/opt/openssl/include
  -DMSG_NOSIGNAL=0 -L/opt/homebrew/opt/sdl2/lib -L/opt/homebrew/opt/json-c/lib
  -L/opt/homebrew/opt/ffmpeg/lib -L/opt/homebrew/opt/openssl/lib
  -lSDL2 -ljson-c -lavcodec -lavutil -lswscale -lssl -lcrypto -framework OpenGL)
clang++ "${host_flags[@]}" native/main.cpp native/gaze_bridge.cpp -o /tmp/native-candidate
/tmp/native-candidate --self-test
```

`MSG_NOSIGNAL=0` is only the macOS build adaptation; the program already ignores
SIGPIPE. Linux ARM64 uses `scripts/build-workspace-native.sh` unchanged.

Reproduce the producer checks using the normal native build flags, adding
`-DglFinish=fwTestGlFinish native/tests_gaze_render_stall.cpp` for a separate
test binary only, then run `native/tests_gaze_render_stall.py`. Run
`native/tests_gaze_producer.cpp` with `-fsanitize=thread` for lifecycle coverage.
No timing injection enters the production executable; GL synchronization stays.

## Historical packed-stream experiment (through fc2cfb9)

The following measurements and reproduction commands describe the previous
axis/atlas implementation and its then-current binaries. They are retained as
negative performance evidence, not as instructions to enable the current direct
path. In particular, the old native packing fixture runner needs that historical
binary; a current direct client intentionally refuses that server handshake.

One Mac H.264 stream and one reconstructed OpenVR overlay texture. Default
behavior remains ordinary full-frame H.264. This is implemented and tested through
Metal → VideoToolbox → native software and V4L2 decoders → actual Frame GL
for the supported cases below, but has **not been accepted while worn**.
Installed Workspace measurements are recorded separately below; desktop texture
updates do not establish compositor refresh or optical latency.

**Runtime decision:** keep this experimental and opt-in. Final matched runs did
not show a sustained frame-rate or bandwidth benefit. Ordinary full-quality
Workspace remains the normal mode; this is not an accepted 60fps optimization.

## Enable and fallback

Start the companion with `FRAME_WORKSPACE_FOVEATION=1` and its existing native
backend settings. `ui/frame_macview.py` forwards the capability to the owned native
launcher, which adds `--foveation`. No UI, placement, quality, or capture-size
controls change. Both the Mac opt-in and the native `foveation=1` request are
required. Peers without foveation support retain the 17-byte H.264 framing;
peers with axis-only support retain mode1 without atlas negotiation.

The optional eye provider queries `FnTable:IVRSystem_026` from the **already
initialized** OpenVR library. New SDK types live in a private namespace and a
separate translation unit; the pinned System022/Overlay027 ABI is unchanged.
Its per-eye NDC centers are unprojected through the matching eye projection and
eye-to-head matrices and a valid standing HMD pose. Both rays must intersect the
owned overlay. Their normalized UVs are averaged. Before use, geometry-only rays
calibrate runtime UV orientation against known local left/right/upper/lower
positions; failure keeps full quality. Calibration is invalidated by texture or
geometry changes; failed attempts retry at most once a second. These rays never
substitute for eye tracking. Runtime curvature uses the runtime's own intersection.

The bridge requires the center API's validity result, finite eye/pose/projection
values, connected valid HMD tracking, and in-bounds intersections. There is no
head-direction or centered fallback. The center API does not expose a sensor
sample timestamp: its returned validity is the available tracking freshness
signal. Real eye UV orientation, tracking loss, curvature, and gaze-to-photon
behavior still require runtime/worn verification.

Native state reports only `gazeState`, `diagnosticGaze`, negotiated/active booleans,
and source/encoded dimensions; no gaze coordinates. UV messages and layouts are
session memory only and are not saved in telemetry or logs.

## Atlas, legacy warp and color

New native viewers request `foveation=1&atlas=1`; an atlas-capable server echoes
`atlas:1`. Atlas mode packs a stable half-resolution whole desktop on the left
and a half-width × half-height 1:1 gaze ROI on the right. At 2880×2560 the encoded
picture is **2880×1280, exactly 50% of the original pixels**. The base tile never
moves, while the ROI tile follows gaze by translation. Original overlay size,
aspect, mouse scale, texture bounds and placement stay unchanged. Both tiles are
reconstructed into one final full-size texture; there is one H.264 reference chain
and decoder. The ROI is selected inside its source rectangle and the base outside.
Sampling clamps independently to each tile, preventing filtering across the seam.

Atlas source dimensions must be divisible by four; exact half-sized ROI dimensions
and origins are even for NV12. This includes 644×484 → 644×242 with a 322×242 ROI.
Origins clamp at source edges and corners, and encoded dimensions stay fixed during
gaze motion. Invalid/stale/unavailable gaze retains the existing full-quality
fallback, acquisition and cooldown policy. Unsupported dimensions use full quality.

Old native viewers requesting only `foveation=1` still receive mode1 axis warp.
New viewers also accept that mode from old servers. It retains approximately half
of each axis at 1:1 and compresses the periphery 2:1 (2880×2560 → 2160×1920,
56.25% pixels), with four-pixel-aligned origins and cores. Atlas is accepted only
after its explicit capability handshake.

A retained-core hysteresis experiment was rejected and removed after matched
moving-scan60 evidence: 33.52 uploads/s, 32.58 fresh drawn/s, 12.55 Mbps and 37
encoded queue overflows versus continuous axis recentering's 49.43 uploads/s,
45.85 fresh drawn/s, 8.71 Mbps and 2 overflows. Large recentering jumps worsened
compression bursts despite reducing layout changes. Atlas performance remains a
separate runtime acceptance question; pixel reduction alone does not prove it.

Metal samples NV12 planes independently without RGB conversion. Base pixels average
2×2 source samples; focal luma and chroma copy exact sample centers. Color attachments
propagate. Diagnostic BGRA uses the same GPU mapping. H.264 remains lossy: 1:1 means
no spatial downsampling in the focal region, not lossless encoding.

The native CPU path converts the two compact atlas tiles separately using existing
swscale color settings, avoiding cross-tile chroma filtering, then reconstructs
RGBA. GPU YUV conversion fuses reconstruction and range/matrix conversion. Legacy
axis/full conversion is unchanged. Both submit exactly one full-size texture.
Existing `glFlush(); glFinish()` before OpenVR handoff remains in place.

## Per-frame protocol and bounds

A negotiated server hello adds `foveation:1` and a fresh `gazeSession` nonce;
`atlas:1` additionally authorizes mode2 only when requested. Every
negotiated binary frame sets flag `0x80`; bit 0 remains the keyframe flag. After
the existing 17 bytes comes a **20-byte** extension:

| Offset | Field |
| --- | --- |
| 17 | Version, exactly 1 |
| 18 | Mode: 0 full quality, 1 axis warp, 2 base+ROI atlas |
| 19–20 | Reserved, zero |
| 21–36 | Eight big-endian u16 values: source width/height, encoded width/height, core left/top/width/height |
| 37 onward | Annex-B H.264 access unit |

Identity mode has matching source/encoded dimensions and zero core fields. The
parser rejects unknown flags/version/mode, truncated headers, nonzero reserved
bytes, dimensions above 4096, invalid alignment/ranges/core size, unnegotiated
extensions, and missing negotiated layouts. Decoded dimensions must match the
packet's encoded dimensions. `Encoder.encode` captures the immutable layout in
that frame's VideoToolbox callback. `PacketTiming` retains it under the exact
submitted decoder PTS token; `Picture` takes that entry only. Existing bounded
64-entry timing history, 8-packet queue, one-newest-picture handoff, IDR barriers,
reconnect resets and strict unknown/stale PTS handling also govern layouts.

For negotiated V4L2 pictures, `h264_crop.hpp` additionally follows each slice's
PPS→SPS mapping and validates progressive 8-bit 4:2:0 crop bounds against FFmpeg's
visible/coded dimensions. Crop origin, extent and VUI range/matrix travel in that
same PTS entry. QCOM exposes coded height (e.g. 368 for visible360), with zero
AVFrame crop fields, and omits its color tags. Only an exact coded rectangle or
an already-cropped visible rectangle is accepted. `av_frame_apply_cropping`
selects the verified visible plane origins; no pixels are padded or synthesized.
Conflicting crop tags, dimensions or layout fail closed. Validated SPS VUI tags
supply missing hardware color metadata. Legacy unnegotiated behavior is unchanged.

Gaze backchannel messages are `t:gaze`, `version:1`, `session:<hello nonce>`,
`source:eye|diagnostic`, Boolean `valid`, numeric finite `u,v` in [0,1], and
`time` in synchronized **Mac host-clock microseconds**, plus `sampleTime` in
native monotonic microseconds. Freshness uses host time; replay and sample-gap
ordering use the separate native sample clock, which is bound to the session
nonce. This avoids treating a clock-estimate correction as a replay. A connection
cannot change clock kinds; old native senders without `sampleTime` retain their
prior ordering behavior. The Mac rejects replayed,
wrong-session, invalid, out-of-range, >150 ms old or >50 ms future samples.
Any invalid/stale/lost gaze restores full quality. Reentry requires 500 ms of
continuous fresh samples and a two-second cooldown after an active loss. This
bounds full/compact encoder and decoder transitions; gaze movement itself never
changes codec dimensions. Packing failure disables packing for that session and
retains full quality.

The native provider samples at at most 60 Hz, keeps only its newest sample and
sends unique sample timestamps. Opt-in socket idle polling is 8 ms (legacy stays
100 ms), so a static desktop does not delay the backchannel until video arrives.
When foveation is requested, clock conversion uses the lowest-RTT observation
from a fixed32-entry,60-second window. Only RTT≤80ms observations are eligible:
midpoint uncertainty is then at most40ms, inside the unchanged50ms future bound.
A congested pong cannot replace a better recent estimate. With no eligible
observation, gaze waits for clock synchronization and full quality remains in
use. Legacy feature-off clock behavior is unchanged.
The Mac checks the newest capture at up to 60 Hz. Healthy capture carries the
newest gaze layout itself; gaze-only refresh waits until no capture has arrived
for two current frame periods. Restoring full quality bypasses this quiet check.
All capture and repack submissions share the requested
nominal FPS limit and any lower adaptive/background cap, preserving the original
capture timestamp; a static desktop does not retain an obsolete sharp
region indefinitely. No extra captured-picture queue is created.

Authenticated `/stats` (`stream_stats`) and `/status` summaries expose
`gaze.active`, `gaze.lastReason` and fixed session-local counters under
`gaze.counts`. Accepted samples, activations, fallbacks, validation reasons,
replays, sample gaps and expiry are counted. No UV, sample timestamp, sample
history or new UI is stored/exposed by these diagnostics.

## Reproduce checks

Run from the repository root; use temporary outputs, not the active Mac binary.

```sh
mac/frame-mac-view/build.sh /tmp/frame-foveation-mac-view
xcrun swiftc -O -swift-version 5 \
  mac/frame-mac-view/Sources/Foveation.swift \
  mac/frame-mac-view/Sources/Encoder.swift \
  mac/frame-mac-view/tests/FoveationTests.swift -o /tmp/test-foveation
/tmp/test-foveation /tmp/foveation-fixtures
/tmp/test-foveation /tmp/foveation-fixtures-atlas --atlas
/tmp/test-foveation /tmp/foveation-fixtures-atlas-production --atlas --production-sized
python3 mac/frame-mac-view/tests/test_foveation_session.py /tmp/frame-foveation-mac-view
python3 mac/frame-mac-view/tests/test_foveation_session.py /tmp/frame-foveation-mac-view --atlas
python3 mac/frame-mac-view/tests/test_foveation_clock.py /tmp/frame-foveation-mac-view
c++ -std=c++17 -O2 native/tests_clock_sync.cpp -o /tmp/test-foveation-clock
/tmp/test-foveation-clock
PYTHONPATH=tests python3 -m unittest test_macview.Helpers test_macview.WorkspaceLifecycle
```

On Linux ARM64 with the existing native build dependencies and FFmpeg, copy those
fixtures and build with `scripts/build-workspace-native.sh /tmp/foveation-build`.
The foveated fixture oracle also accepts `--decoder v4l2`; run that only when
the coordinator has stopped the installed hardware decoder, and keep the strict
decoded-dimension/image checks. The following tests create hidden GL windows and do not initialize OpenVR:

```sh
DISPLAY=:0 python3 native/tests_foveation.py /tmp/foveation-build/frame-workspace-native /tmp/foveation-fixtures --use-display --conversion cpu
DISPLAY=:0 python3 native/tests_foveation.py /tmp/foveation-build/frame-workspace-native /tmp/foveation-fixtures --use-display --conversion gpu
DISPLAY=:0 python3 native/tests_gaze_transport.py /tmp/foveation-build/frame-workspace-native --use-display --scan --clock-jitter
DISPLAY=:0 python3 native/tests_gaze_slow_read.py /tmp/foveation-build/frame-workspace-native
DISPLAY=:0 python3 native/tests_gpu_conversion.py /tmp/foveation-build/frame-workspace-native --use-display
DISPLAY=:0 python3 native/tests_stream.py /tmp/foveation-build/frame-workspace-native --use-display --reconnect --size-change
```

Use the normal Frame booking/wearer-consent rules for any device execution. These
commands do not replace installed binaries or stop the active viewer.

The default generator retains the 512×384 source/384×288 encoded case, including
its observed QCOM failure. For separate supported-size hardware crop proof:

```sh
/tmp/test-foveation /tmp/foveation-fixtures-large --hardware-sized
DISPLAY=:0 python3 native/tests_foveation.py /tmp/foveation-build/frame-workspace-native /tmp/foveation-fixtures-large --use-display --decoder v4l2 --conversion cpu --no-resize --case frame-0 --case frame-1
DISPLAY=:0 python3 native/tests_foveation.py /tmp/foveation-build/frame-workspace-native /tmp/foveation-fixtures-large --use-display --decoder v4l2 --conversion gpu --no-resize --case frame-0 --case frame-1
```

`--hardware-sized` uses source768×576 instead of512×384 for the second size.
`--no-resize` explicitly starts at picture4's IDR; gaze/layout still changes at
picture7 within that same VT reference chain. The default test retains the
picture4 size transition and every fixture. Hardware invocations wait three
seconds between isolated processes; no delay is added inside a stream.

## Atlas acceptance evidence (2026-10-19)

Image-oracle Mac candidate SHA256 `d92f46b8c65375ac9e4a0db7bfc169574fc12b1c8da0fcbb035cccfd4016a1c0`;
native SHA256 `3b26594e8e82bc98ba29a4fe1803aa14739f1f13e156fda7ec560637ff99be74`.
These identify the image-check artifacts; final installation and measurements
are recorded below. The later Mac scheduling correction does not change the
pixel packer or encoder.

- Metal atlas NV12: 14 VT fixtures plus actual2880×2560 input, maximum packed-plane
  error0/255, focal Y/UV byte-exact. Separate644×484 corner/center tests prove
  base bytes invariant during ROI motion. Opposed UV halves expose seam mistakes.
- Frame software decode/GL: all14 GPU atlas cases passed max2/255; four CPU cases
  spanning both ranges and644×484 passed max1/255. Source resize and immutable
  per-picture layout associations remain strict. Chroma uses the existing nearest
  filter, with an atlas-only independent tile clamp; luma/RGBA use linear filtering.
- Actual-sized Frame V4L2: a180-picture/6-second sequence uses2 settled seconds each
  of full2880×2560, atlas2880×1280, then full2880×2560. ROI movement occurs inside
  the middle encoder reference chain. CPU and GPU each passed with exactly2 decoder
  format reopens, zero errors, acknowledgements in all3 stages, and final full mode.
  Reconstruction max error1/255 for both; CPU mean0.015444, GPU mean0.019300.
- Compatibility: the real Mac Session test without `atlas=1` receives mode1;
  with it receives mode2. Both pass static-source gaze motion, invalid/stale loss,
  cooldown, reconnect nonce/full fallback and shared30fps capture/repack pacing.
  The browser probe retains17-byte framing. A new native viewer requesting atlas
  also passes two axis-only mock-server VT/GL cases whose hello omits atlas
  (max2/255). Self-test rejects mode2 without explicit negotiation and malformed
  bounds/alignment, including divisible-by-four but not-eight dimensions.
- Clock correction/reconnect regression passed with no harmless-resync fallback;
  duplicate ordering and actual250ms loss still fail closed. These tests do not
  establish worn gaze placement, latency or improved live throughput/bitrate.

To reproduce the settled actual-size hardware oracle (after ordinary device
booking/off-head coordination, with no other hardware decoder active):

```sh
/tmp/test-foveation /tmp/foveation-fixtures-atlas-fallback --atlas --production-sized --fallback-chain
DISPLAY=:0 python3 native/tests_foveation.py /tmp/foveation-build/frame-workspace-native /tmp/foveation-fixtures-atlas-fallback --use-display --decoder v4l2 --conversion cpu
DISPLAY=:0 python3 native/tests_foveation.py /tmp/foveation-build/frame-workspace-native /tmp/foveation-fixtures-atlas-fallback --use-display --decoder v4l2 --conversion gpu
```

Unlike `--no-resize`, `--fallback-chain` requires both in-process format transitions
and acknowledgements in all stages. It cannot be combined with `--no-resize`.

## Explicit diagnostics and matched measurements

For synthetic gaze only, the Mac process additionally requires
`FRAME_MAC_VIEW_DIAGNOSTIC_GAZE=1`. Run the native stream with `--foveation
--diagnostic-gaze 0.5,0.5` for a fixed focal point, or `--foveation
--diagnostic-gaze scan` for a four-second circle within UV 0.2–0.8. Both print an
explicit synthetic diagnostic notice and report diagnostic state. Ordinary
companion launches never add these flags unless explicitly configured. For the
normal authenticated companion workflow, set all three startup variables:

```sh
FRAME_WORKSPACE_FOVEATION=1
FRAME_MAC_VIEW_DIAGNOSTIC_GAZE=1
FRAME_WORKSPACE_DIAGNOSTIC_GAZE=scan  # or 0.5,0.5
```

The launcher validates the diagnostic choice and prerequisites before display
creation/restart, then forwards the CLI option to the owned native service. No
manual ticket or service reconstruction is needed. Missing diagnostic configuration
preserves ordinary launcher arguments. To return to real eye mode, remove the
FRAME_WORKSPACE_DIAGNOSTIC_GAZE and FRAME_MAC_VIEW_DIAGNOSTIC_GAZE variables (or
remove the direct CLI diagnostic flag); retain the common
`FRAME_WORKSPACE_FOVEATION=1` opt-in.

Compare the **same final Mac/native binaries**, real 2880×2560 source, scene,
content motion, bitrate/quality, conversion, decoder, transport and duration.
Use native `--decoder v4l2` only in an authorized isolated decoder window.
For a matched off-head full-quality baseline, retain `--foveation` with true eye
mode: unavailable eye data selects identity framing with the same crop/color
metadata and pacing. Fixed and moving runs add the diagnostic flags above.
Feature-off legacy regression is checked separately. Use the
existing `--url-file` single-use/reconnect credential path and `--state-file`.
Never put tickets in diagnostic logs. Native state reports original `textureWidth`
/ `textureHeight`, `encodedWidth` / `encodedHeight`, `foveationActive`, decoder
opens, queue and stage timing. Mac authenticated `/stats` retains existing capture,
encode, receive, decode and draw timings by sequence, including GPU packing in
encode duration. Static content capture age is not eye-to-photon latency.

Repeat at requested 30 and 60 fps and distinguish decode/upload fps from compositor
fps. Do not rank settings until the runs are comparable. Real eye-mode off-head
usually falls back to full quality and is not a useful foveated benchmark.

## Final installed Workspace measurements

Actual Frame, off-head, final Mac/native binaries recorded below, Sharp H.264 at 0.14 bpp,
V4L2 with GPU conversion, adaptation disabled. The logical/captured source remains
2880×2560. An owned Chrome `scroll.html` fixture covers2880×2530 at(1800,30),
verified through public window bounds; its animation reports60Hz. No builds or
other benchmarks ran concurrently. Each60fps case samples60seconds;30fps controls
sample30seconds. The Mac statistics window brackets native sampling by about
6–7seconds, with the same fixture active throughout.

| Requested fps / mode | Fresh captures drawn/s | H.264 Mbps | Native format reopens |
| --- | ---: | ---: | ---: |
| 60 / full quality | 46.00 | 2.17 | 0 |
| 60 / fixed synthetic gaze | 40.99 | 4.07 | 10 |
| 60 / moving synthetic gaze | 38.93 | 7.16 | 13 |
| 60 / full quality recheck | 48.40 | 2.23 | 0 |
| 30 / full quality | 26.22 | 1.43 | 0 |
| 30 / moving synthetic gaze | 24.89 | 4.42 | 5 |

Fresh rate deduplicates actual captures across all sent copies and counts their
first actual draw; it does not count gaze-only resubmissions as new desktop
content. Native upload and capture rates, stage percentiles, latency, drop/queue
counters and earlier rejected trials are retained in
[FOVEATION-VALIDATION.json](FOVEATION-VALIDATION.json). Capture delivery was roughly
54–55/s at a60fps request and28/s at30. All final runs had zero decoder/network/
submit errors, but queue recovery and decoded-frame drops still occurred.

**Result:** reconstruction and fallback work, but sustained60fps was not achieved.
The repeated full-quality control rules out a general throughput decline as the
sole explanation for the slower foveated runs. Stale/expired gaze still causes
codec-size transitions; moving ROI content also costs more bits. Halving encoded
pixels does not guarantee lower bitrate or faster display delivery. Keep the
feature opt-in and leave ordinary full-quality Workspace running after testing.
Real eye activation, orientation and worn acceptance remain pending.

The ordinary feature-off viewer was subsequently checked for30seconds at the
60fps target:45.37 fresh/s,45.66 uploads/s,2.77Mbps, zero errors and no format
reopens. It is left running with GPU conversion, Sharp, Coding2880×2560 and no
synthetic-gaze or foveation flags. Original overlay placement is preserved.

Reproduce an individual run after starting the selected mode, using the normal
Frame booking/off-head procedure:

```sh
python3 -B native/bench_desktop.py --seconds 60 --label MODE-gpu-60 --out /tmp/MODE-gpu-60.json
```

## Evidence from this implementation

- Mac ARM64 optimized complete build passed. Metal tests covered seven gaze
  positions/sizes (center, four corners, movement and resize), both NV12 ranges:
  maximum packed-plane error 1/255 versus the independent box-sampling oracle;
  focal luma and chroma byte-exact. Fourteen real VideoToolbox H.264 fixtures, each with a 40-picture reference
  chain and periodic IDR (560 pictures total). Revised chains now use the same
  Encoder object throughout: source resize at picture4 and gaze/layout-only
  change at7. They never splice independent same-size encoder sessions.
- Actual Frame, hidden GL and **software decoder**: all fourteen fixtures passed
  CPU reconstruction (max channel error 2/255, mean 0.12590) and fused GPU
  reconstruction (max 2/255, mean 0.14474) against decoded-image numerical oracles.
  Revised continuous-chain CPU rerun also passed all14 cases with source resize,
  max2/255 and mean0.123112. Frame/layout association, moving core and source resize exercised; zero decoder
  or submit errors. No performance comparison or worn test was performed.
- Corrective hardware validation: QCOM's 480×368 coded frame was normalized to
  visible480×360 using per-PTS SPS crop metadata. VUI correction reduced observed
  CPU/GPU image errors from max24–25/255 (driver matrix/range unspecified) to the
  unchanged max2/255 oracle. CPU supported cases0/1 (both ranges) passed with
  mean0.133191. GPU supported cases0/1 passed mean0.156246, and cases0–6
  passed max2/255; these use continuous
  same-encoder gaze movement and one reconstructed texture. See limitations below.
- Live Mac Session test: one captured static BGRA picture produced six layout
  transitions; center-to-corner, invalid fallback, cooldown/reentry, stale fallback,
  legacy framing, stable compact size and fresh nonce/full fallback on reconnect
  all passed. A second live test combined continuous 30 fps capture and moving
  gaze: 67 encoded pictures in 2.6 seconds, including 58 warped pictures, below
  the shared nominal ceiling; repacks preserved the single original capture
  timestamp in the static test.
- Native idle backchannel test: 45 valid unique diagnostic messages on each of
  two connections, max interarrival 24.50 ms, max sample-delta vs arrival-delta
  difference 4.48 ms. No incoming video; fresh nonce and clock handshake required
  after reconnect. Moving scan also passed: 45 messages per connection, max
  interarrival 24.43 ms and max delta difference 5.08 ms. No gaze values retained
  in the report.
- Existing GPU suite: 16 NV12/YUV420P × range/matrix/pattern shader cases and seven
  real H.264 socket cases passed, including unsupported-format CPU fallback,
  reconnect and resize. Legacy CPU reconnect+resize and 38 companion
  helper/lifecycle tests passed. The legacy live Mac Agent suite also passed
  (12 tests, one permission-dependent skip). Deliberate reconnect tests report one expected
  network disconnect; no decoder or submit failures.
- Isolated new-ABI bridge and native self-tests passed on ARM64. The actual runtime
  exposes System026 and the eye input action. Off-head samples remain invalid, as
  expected. The installed overlay passes geometry-only UV calibration and reaches
  `eye-unavailable`; real eye UV orientation and worn sharpness remain unverified.
  V4L2 image and performance evidence is recorded separately. The UV test image
  was inspected: one full rectangle, sharp central
  region and compressed peripheral detail, with no missing edge/corner bands.

### Clock-jitter correction

A real continuously scanning30fps Workspace session exposed repeated full-quality
fallbacks. The counters-only build confirmed3 replay rejections,35 stale and75
future rejections during the observed interval (plus2 real expiry and2 invalid
messages). The old estimator replaced its offset with every pong midpoint;
queued responses biased freshness and subsequent correction could reverse host
timestamps. The new low-RTT estimator and separate monotonic ordering address
these measured causes without clamping or fabricating sample age.

- Standalone native clock tests passed asymmetric70/500ms queued observations,
  best-sample selection, exact freshness,60-second expiry and reconnect reset.
- Actual Frame hidden native WebSocket jitter/reconnect test passed257 moving
  diagnostic messages per connection. Each connection had one backward
  host-time correction while native sample ordering remained strict. Sample
  host age stayed−30.39…9.91ms through240ms delayed pongs; maximum interarrival
  was40.30ms and maximum sample-vs-arrival delta difference4.57ms. No incoming
  video, OpenVR or hardware decoder was used in this focused test.
- Live Mac Session test accepted118 and41 samples in two sessions, each with one
  activation and zero fallback through±20ms clock corrections. Duplicate native
  timestamps, stale/future messages and wrong nonce were rejected; reconnect
  reset ordering and a real250ms producer stall still restored full quality.
- Existing static fallback/reconnect test retained six outputs from one capture.
  Continuous30fps capture plus gaze produced66 outputs in2.6seconds,58 warped.
  Full Mac ARM64 and native ARM64 builds/self-tests passed.

### Backchannel progress during a partial message

Clock correction alone did not eliminate fallbacks. `WebSocket::readBytes`
previously called the idle callback only on a poll timeout. A large message
arriving in chunks faster than that timeout could starve gaze updates until the
entire message arrived. Foveated connections now service the callback every
8 ms while assembling messages as well; feature-off behavior is unchanged.

The independent live reader in `native/tests_gaze_slow_read.py` records arrival
times while the server writes a 128 KiB harmless JSON message in 1 KiB chunks.
On the actual Frame, the same approximately 525 ms transfer produced a maximum
gaze gap of **530.55 ms before / 27.03 ms after**. Chunk intervals stayed below
4.33 ms. The original diagnostic probe buffered its timestamps until after the
write and was discarded; these numbers come from the corrected independent
reader. No decoder, OpenVR, gaze-coordinate logs or image-oracle relaxation was
involved. The strict regression fails on the previous binary and passes on the
installed candidate.

### Prefer fresh captures over gaze-only refresh

The first atlas moving-scan60 run sent270 repeats in66.33seconds; unique sent
captures fell to49.78/s versus54.30/s for full quality. The gaze timer could take
a paced slot just before the next capture arrived. It now waits for two current
frame periods without capture delivery; warped-to-full fallback bypasses that
wait. Admitting a fresh foveated capture also clears an older pending-refresh
flag so completion cannot resend that same picture unnecessarily.

The final live Session regression models55Hz capture at a60fps target and27Hz
at30fps. Fixed and moving cases each captured165/81 pictures respectively;
the measured steady windows contained120/121 and59/60 unique submissions, with
zero repeats in each case. The six-transition static test, original capture
timestamps, prompt invalid fallback during healthy capture, cooldown, reconnect
and clock/replay/freshness tests still pass. The optional capture-rate override
is restricted to TestSource and cannot alter ScreenCaptureKit/window/separate
capture. These focused tests establish scheduling behavior; desktop throughput
is measured separately below.

### Hardware limitations observed during corrective checks

The 384×288 VT fixture produced a zero/unknown-PTS hardware picture; stock
FFmpeg h264_v4l2m2m also produced an all-zero NV12 picture and then stalled on
that sample. A supported-size case7 similarly failed at its first unknown PTS.
These are explicit failing cases, not skipped/accepted output. Their scope is
these generated streams on the current QCOM runtime, not a general minimum-size
claim. Every unknown/zero PTS still terminates the stream safely.

A size change after only three pictures (100ms) returned EBUSY on the reopened
hardware decoder's first send. EBUSY also occurred on a fresh initial open after
short process runs; a three-second inter-process interval reduced that separate
startup issue. The rapid fixture resize remains unaccepted. The coordinator
subsequently verified actual2880×2560↔2160×1920 Workspace transitions, including
350ms viewer suspension→full-quality fallback→compact reacquisition, with zero
decoder errors on the crop/color build. The pixel path is unchanged by the later
clock correction; sustained post-correction scan measurements remain separate. No oracle threshold,
PTS rule, dimensions or image padding were relaxed to pass hardware checks.

Bulky generated fixtures, logs and raw captures are temporary test inputs and are
removed after the coordinator's integration handoff. Retain this report and the
reproducible tests, not generated captures, in source control.

Final atlas build identities (source based on `406d56cc632cb1c480cc9a8bdb30a2d753f976e0`
plus this feature): Mac ARM64 SHA-256
`f09de9fe5a1c73ddf29d09f6b1827eea051fba0b62dad916fc5bfc4e46adc04a`;
native ARM64 SHA-256
`3b26594e8e82bc98ba29a4fe1803aa14739f1f13e156fda7ec560637ff99be74`.
Both are installed. Actual Workspace testing was performed by the coordinator;
normal full-quality mode is retained after the tests.
The native build was repeated from the same working directory and reproduced
its binary hash. Build metadata matches all 36 native source files and excludes
generated Python bytecode; invoking the debug build from another directory changes
DWARF build paths and therefore its artifact hash.

Local regression passed: 50 MacView tests (zero skips), shell/Python syntax and
owned-source diff checks, followed by the focused final scheduling/clock tests.
Earlier unchanged panel (10) and JavaScript (69) checks also passed. The vendored
SDK is byte-identical to its recorded upstream SHA256, including six upstream
trailing-space lines and a terminal blank line; owned-source whitespace checks
exclude only that hash-verified header.
MacView retains its existing socket ResourceWarnings; the suite exits successfully.
The launcher also now recognizes an explicitly `not-found` transient systemd
unit as stopped even when `systemctl show` returns nonzero, verified through real
companion restarts. Uncertain or inaccessible owner state still fails closed.

Cleanup removed **159,165,582 bytes (159.17 MB)** of task-generated artifacts
across Mac and Frame, including obsolete binaries, synthetic VT/GL fixtures,
probe/build copies, raw captures and superseded logs. The final pass removed
55,882,460 file bytes / 56,057,856 allocated bytes; earlier passes recorded
103,283,122 file bytes. The active companion log was trimmed, not removed.
Current installed deliverables, user data, the active companion runner and the
requested awake assertion are preserved.

One small pre-task rollback remains until worn verification:

- Mac helper: `/tmp/frame-workspace-foveation-mac-rollback/frame-mac-view`,
  SHA256 `d6bccf857ff48e2de99400e16e15e82b981bb340bc903644460b96b3aa969edc`.
- Frame binary, build metadata and original pose:
  `/tmp/frame-workspace-foveation-rollback`, native SHA256
  `93f3497538e89d99fb25e7460fbb1c868a6b06cc1756a33e23bba20d7964684f`.

Rollback restores the helper to `mac/bin/frame-mac-view`, and the Frame binary
plus metadata to `~/.local/share/frame-workspace/native/`, with the owned viewer
stopped. Keep foveation disabled when running the older native binary. These
`/tmp` backups are temporary. The original installed pose still hashes to
`1dc2bd39102c1103f0bd83e4657edfac51b953646321fafd1f065328d2b6887c`.
