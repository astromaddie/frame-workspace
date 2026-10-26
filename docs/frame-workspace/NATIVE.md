# Native viewer proof — 2026-10-19

The Frame now has a native ARM64 Workspace viewer. It receives the existing
authenticated Mac H.264 stream and submits its own full-resolution OpenVR texture.
SteamVR keeps that texture in the world between desktop updates. There is no
Chrome page or nested Gamescope compositor in this rendering path. This removes
those dependencies from head tracking; a worn test is still required to establish
whether the reported head-turn judder is resolved.

## Use and rollback

The existing companion's Workspace Start, Stop and Show again controls are reused.
No new control design or image assets were added. Show again brings the owned
native panel in front of the current headset orientation. An ordinary viewer
process restart retains the saved transform instead.

Build on the Frame against its system development libraries using
`scripts/build-workspace-native.sh`, then install the executable and its
`BUILD-METADATA.json` in `~/.local/share/frame-workspace/native/`. The launcher
fails clearly if the executable is missing. Device installation/testing requires
the shared Frame booking and explicit off-head consent.

### Matching the Mac app (2026-10-26)

The build hashes its inputs (`native/*.cpp`, `*.hpp`, `input/*.json`, `vendor/`,
the build script and `scripts/native_source_identity.py`) into one source
identity, compiles it in, and records it as `source_identity` in
`BUILD-METADATA.json`. `frame-workspace-native --capabilities` prints it with
the protocol version and capabilities (`roles`, `surfaces`, `wall-slots`,
`idle-auxiliary`). An older viewer rejects the flag. The companion computes the
same identity from its bundled sources and compares on connect and before each
start. `build-workspace-native.sh -j 2` builds the viewer and dashboard together.

**Update Headset Viewer** on the Mac page, with no Workspace running on either
side: the companion copies the bundled sources over the existing SSH link to
`~/.cache/frame-workspace/native-update/` and starts
`scripts/workspace-native-update-on-frame.py` in the user unit
`frame-workspace-native-update-UID.service`. That checks the sources against the
identity, builds with `-j2`, runs `--self-test` and `--capabilities`, checks
again that no Workspace unit is running, then replaces the viewer, dashboard,
`input/` bindings and metadata one file at a time (the viewer last). The
previous build is kept in `~/.local/share/frame-workspace/native-previous/`;
poses, the Stop helper and other state are not touched. It stops the dashboard
unit so the companion restarts it on the new binary. Progress and failures
(with the end of the build log) show on the Mac page. To roll back, stop the
Workspace and copy the files from `native-previous/` back.

Start the Mac companion with these settings to select the native path:

```sh
FRAME_PRIVATE_SSH=1 FRAME_ALIAS=frame FRAME_CONTROL_TELEMETRY=0 \
FRAME_WORKSPACE_VIEWER=native FRAME_WORKSPACE_DECODER=v4l2 \
FRAME_WORKSPACE_CONVERSION=cpu FRAME_WORKSPACE_FPS=30 FRAME_MAC_VIEW_ADAPT=0 \
python3 ui/server.py --port 60339
```

Use **9:8, 2880×2560 at 1×** for unattended display tests. On this Mac, creating
the 3840×2160 display opens a macOS mirror/extend prompt. Synthetic 16:9 video
does not create a display and was used only for the isolated decoder tests below.

`FRAME_WORKSPACE_VIEWER=browser` selects the earlier browser viewer; it remains
the default when the environment variable is absent. `FRAME_WORKSPACE_DECODER`
accepts `software` (default) or `v4l2`, with no silent fallback. Stop affects only
the reserved Workspace viewer unit and owned Mac display. SteamVR and the main
Gamescope session are not restarted. The original installed Frame Control app is
preserved.

Independent native window panels and Spatial menu/status panels use the separate
`FRAME_WINDOW_DECODER` setting. It defaults to `software`; set it to `v4l2` only
when explicitly testing hardware decoding on those panels. Invalid values are
rejected before switching to Spatial mode or changing the owned display.

The current installed binary SHA-256 is
`9ebd783d7d90f5b695189d094d41502fd3c861583674a47fd552db0d66191545`;
its build metadata SHA-256 is
`2f7c52d90fca48b24abb6ba9f0934c77e5e252a46eb096814ed982f0630ab1a8`.
It links the Frame's FFmpeg 61, SDL2 and OpenGL libraries. A host-container
executable linked against FFmpeg 58 was tested locally and never deployed.
The [current control-build record](evidence/native-controls-live.json) includes
exact production-source and test-script hashes because the tree was uncommitted
when built; the base Git revision alone is insufficient identity. The earlier
[pipeline record](../../native/evidence/frame-pipeline.json) identifies the builds
used for those decoder and performance results. The initial
`9108842a…` build in [earlier device evidence](../../native/evidence/frame-hardware.json)
is historical.

The active preview companion stores its Coding profile under
`~/Library/Application Support/Frame Workspace Preview`, in private directories
with a mode 0600 profile file. It leaves the installed Frame Control app unchanged.
The preview process itself is not installed as a login service. The current native
binary and saved pose are installed on the Frame; the latest previous build
and metadata remain in `/tmp/frame-workspace-controls-rollback-20261019/`
for a bounded rollback. Stop the owned viewer before atomically restoring both
files, then use the existing Workspace controls to reopen it.

## Device evidence

The direct overlay accepted a **2880×2560** application texture from the real
Mac workspace. OpenVR getters returned the same texture dimensions, a 2.4 m width,
2.1333 m coordinate height and 2880×2560 mouse scale, matching 9:8. All queried
getters returned success. This is API geometry and texture evidence, not an
optical measurement.

A bounded native-process restart retained the same Mac display and overlay key.
Every getter succeeded afterward; the maximum absolute matrix difference was
**0.0**, and the 2880×2560 texture and 2.4 m width were unchanged. The test used
the same running SteamVR tracking session. The compact
[placement record](../../native/evidence/frame-placement.json) deliberately
omits the room transform itself.

The isolated paced test streamed a synthetic H.264 High Annex B file over the
same WebSocket framing and a reverse SSH tunnel. It used no virtual Mac display
or user application. The journal and state identified active
`h264_v4l2m2m` decoding to NV12, with successful OpenVR texture submissions.

| Target | Actual sender rate | Received / decoded / uploaded | Encoded / decoded drops | Errors |
|---|---:|---:|---:|---:|
| 4K, 30 fps | 30.07 fps, 450 frames / 14.967 s | 450 / 420 / 402 | 22 / 18 | 0 |
| 4K, 60 fps | 54.62 fps, 900 frames / 16.477 s | 900 / 322 / 309 | 492 / 13 | 0 |

The 30 fps run's final sampled upload rate was 27.96 fps. The higher-rate run
fell far short of its target and shed many encoded frames. These results prove
hardware-path operation, **not sustained 60 fps**. Desktop upload rate and
SteamVR compositor refresh are separate quantities; compositor presentation
timestamps are not invented in the telemetry.

For comparison, the earlier real 9:8 scrolling run using software decode uploaded
17.19 fps over 30 seconds at 2880×2560, with 359 encoded drops and no decoder
errors. The synthetic and real-desktop workloads are different; they do not
establish a matched hardware speedup.

A subsequent real 2880×2560 scrolling run with V4L2 active uploaded **25.75 fps**
over 30.017 seconds at the 30 fps target, with five encoded drops, five decoded
drops and zero errors. Decode API time was p50/p95 1.58/1.72 ms, conversion
5.46/11.65 ms, upload including synchronization 4.26/4.97 ms, and queue age
53.35/136.14 ms. These are one-second sampled stage values and desktop-update
counts, not GPU timestamps or physical latency. The fixture's observed cadence
also differed from the software run; see the compact
[real-desktop record](../../native/evidence/frame-desktop.json).

## Matched desktop pipeline follow-up

The first optional-GPU comparison exposed repeated hardware-decoder opens during
queue overflow. The repaired path keeps the hardware context for a validated
same-size recovery IDR and discards delayed older output using bounded timestamp
metadata. Reconnect and parsed dimension changes still reopen cleanly. Software
reconnects flush their context and retire prior timing metadata. PTS zero,
negative, missing and unknown tokens remain explicit failures.

All following runs used the same 2880×2560 1× Workspace, a 2880×2530 owned
scrolling fixture reporting about 61 fps, V4L2, fixed source targets and disabled
adaptation. Each measurement lasted 30.02 seconds, with no concurrent builds or
decoder tests.

| Conversion / target | Received / decoded / uploaded fps | Queue overflows | Errors |
|---|---:|---:|---:|
| CPU / 30 | 28.11 / 28.11 / **28.05** | 0 | 0 |
| GPU / 30 | 29.18 / 28.18 / **27.18** | 1 | 0 |
| CPU / 60 | 58.92 / 35.44 / **33.71** | 49 | 0 |
| GPU / 60 | 58.56 / 43.57 / **39.41** | 32 | 0 |

There were no decoder reopens during those measurement windows. The stable
active mode remains **CPU conversion at 30 fps**; GPU remains opt-in. The 60 fps
modes still drop substantial content and have timing spikes. These rates include
queue recovery and IDR waits and do not establish the decoder's silicon limit.
They are desktop texture updates, not compositor refresh or optical FPS.

The [pipeline evidence](../../native/evidence/frame-pipeline.json) also preserves
the earlier matched baseline and strict production-path normal, reconnect,
forced-overload and GPU image-oracle tests. Full-resolution V4L2 tests passed
with maximum RGB difference 2 versus FFmpeg and zero decoder/server/submit errors.
Reconnect tests record one intentional network disconnect separately. Forced
overload recovered with one initial decoder open, 15 queue overflows, one recovery
IDR and one discarded stale driver frame. A previous dimension-change gate
covered 2880×2560 → 2816×2560; same-size SPS reparameterization was not tested.

The final build adds software-reconnect cleanup to the build used for the table;
its V4L2 recovery and GPU conversion paths are unchanged. Focused gates were run
on the exact final binary and test script; desktop benchmarks were not repeated.
Hardware reports NV12 with unspecified color range/space. The strict hardware
GPU oracle establishes the BT.601 limited fixture only, without inventing
BT.709/full-range metadata. The unchanged shader separately passed 16 raw-plane
layout/range/matrix cases on the Frame, with maximum RGB difference 1 against
production libswscale. Legacy 320×180 V4L2 output was padded to 320×192; general
hardware cropping support is not claimed.

A later unattended state check at about 05:23 JST still showed the full-resolution
viewer ready and connected on CPU/V4L2. It had seven network retry attempts in
one 04:00–04:02 JST interval and recovered without submission errors. The generic
redacted retry log does not establish the interruption's cause. This longer idle
observation is separate from the error-free short benchmark windows.

## Boundaries and next gates

FW-10 verdict: **go for an owned-overlay prototype and same-runtime process
recovery; conditional for a complete saved room layout**.

| Capability | Source/device result |
|---|---|
| Set a world transform on the native owner's overlay | Successful direct owner call; read-only getters confirm absolute standing-space placement |
| Restart the native owner and retain placement | Verified; same display/texture/key, matrix difference 0.0 |
| Mutate Valve-owned dashboard overlays | Earlier PermissionDenied result retained; no ownership bypass attempted |
| Show/recenter without restarting the viewer | Owner control JSON is implemented; existing Show again routes there |
| Change native width/distance | Owner API implemented; [two control mockups](NATIVE-CONTROLS.md) await approval |
| Native curvature | Set/readback 0.25 succeeded on Frame; restored flat; worn appearance unverified |
| Multiple native surfaces | Not implemented |
| SteamVR dashboard/game coexistence and runtime reload | Not verified by the bounded placement test |
| Restore across room/tracking-origin changes | Not established; logical profiles explicitly report spatial placement as manual |

- Native-viewer readability and head-motion behavior remain **headset-unverified**.
  The user's favorable readability feedback applies to the preceding browser
  viewer, not this build.
- Placement is saved locally by the overlay owner and restored within the same
  SteamVR tracking session. A saved matrix is not a room anchor. Runtime reset,
  room changes, multiple panels and game coexistence need separate FW-10
  evidence before broader spatial-restore claims; curvature has API readback
  evidence only.
- Keyboard and trackpad input stay on the Mac. Native controller pointer input is
  disabled. This does not establish Japanese IME, multi-panel focus or FW-12
  acceptance.
- Native full-screen capture has a separate [FW-06 record](FULLSCREEN.md).
  Automatic detachment remains gated by that evidence.

## Reproduce and interpret

The [native README](../../native/README.md) documents the production decoder,
queue bounds, URL allowlist, control protocol and local GL/WebSocket tests.
The build's self-test runs before metadata is written. The paced device record
identifies the synthetic fixture dimensions, byte count, hash, access-unit and
IDR counts, sender cadence and achieved rates. Use a loopback-only test server,
wait for reverse-forward readiness before launching, and pace whole access units
without catch-up bursts. An unready tunnel caused an excluded startup failure
with zero received packets; it was a harness failure, not a decoder result.

Do not infer video throughput from a static desktop or compositor refresh from
texture submissions. CPU conversion still copies full RGBA images. Optional
`FRAME_WORKSPACE_CONVERSION=gpu` instead copies owned NV12/YUV420P planes and
converts them to the same full-resolution RGBA texture through a shader. It is
not zero-copy NV12 import; CPU remains the source default. The
[host validation record](../../native/evidence/host-gpu-validation.json) retains
the strict sharp-edge and ramp comparisons rather than relying on throughput
alone. `gpuConvertMs` is CPU wall time issuing GL work; `syncMs` includes the
completion wait, and neither is an optical latency measurement.

Eye-tracked foveated streaming is a possible later optimization. For this simple
textured-overlay workload, reduced shading alone is unlikely to address the
measured video conversion/copy work. Reducing peripheral encoded pixel density
could help, whereas a bitrate-only change retains the full decoded image size.
This is an engineering hypothesis, not a measured improvement. Valve documents
[eye tracking and foveation extensions](https://partner.steamgames.com/doc/steamhardware/steamframe/engines/custom)
and [OpenVR eye-tracking actions](https://partner.steamgames.com/doc/steamhardware/steamframe/input).
Their availability in this overlay route and gaze-to-sharp-text delay still need
device and worn verification; the current pinned SDK and viewer do not use them.
The user identified it as a direction for reaching 60 fps; the immediate
decode-queue work takes precedence. No foveated transport or bandwidth saving is
claimed in the present build.

## Decode-burst follow-up

The 2026-10-19 build increases the finite compressed queue from three to eight
access units, retaining newest-picture presentation and the existing strict
IDR/timestamp recovery. In same-resolution sequential trials the original queue
overflowed 15 and 5 times; the new queue absorbed six-packet bursts with no
overflows or encoded drops. It uploaded 29.04 fps over 30 seconds and 27.70 fps
over 60 seconds. The longer run received only 28.35 packets/s, so a reliable
30 fps result is still open. See [the full comparison and limits](PERFORMANCE.md#decode-burst-follow-up-2026-10-19).

## Native control integration

The current build adds owner-scoped placement controls and curvature readback
to the previously validated decoder pipeline. The actual Frame accepted width,
distance and curvature updates while retaining the same native process and Mac
display. Original placement was restored with matrix delta 0.0. The existing
panel switcher now discovers only the validated owned native Workspace in
addition to its dashboard panels. See [controls and limits](NATIVE-CONTROLS.md).
The earlier pipeline measurements above belong to their recorded builds; they
are not reattributed to the new binary.

The subsequent [multi-stream trial](PERFORMANCE.md) uses the current control
build and records the active-stream policy separately from decoder changes.
