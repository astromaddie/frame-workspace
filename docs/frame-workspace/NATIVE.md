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

Start the Mac companion with these settings to select the native path:

```sh
FRAME_PRIVATE_SSH=1 FRAME_ALIAS=frame FRAME_CONTROL_TELEMETRY=0 \
FRAME_WORKSPACE_VIEWER=native FRAME_WORKSPACE_DECODER=v4l2 \
FRAME_WORKSPACE_FPS=30 python3 ui/server.py --port 60339
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

The latest installed binary from the initial device verification is
`9108842a9fafe942588198ee9721792740b5cfc4323969591f9357bfad477f00`.
It links the Frame's FFmpeg 61, SDL2 and OpenGL libraries. A host-container
executable linked against FFmpeg 58 was tested locally and was never deployed.
The installed build metadata and compact [device evidence](../../native/evidence/frame-hardware.json)
identify the actual source and binary; the production tree was uncommitted when
that build was produced, so its base Git revision alone is insufficient identity.

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

## Boundaries and next gates

- Native-viewer readability and head-motion behavior remain **headset-unverified**.
  The user's favorable readability feedback applies to the preceding browser
  viewer, not this build.
- Placement is saved locally by the overlay owner and restored within the same
  SteamVR tracking session. A saved matrix is not a room anchor. Runtime reset,
  room changes, multiple panels, game coexistence and curvature need separate
  FW-10 evidence before broader spatial-restore claims.
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
texture submissions. The current software conversion/upload path still copies
full RGBA images; zero-copy NV12 import and GPU color conversion are not claimed.

Eye-tracked foveated streaming is a possible later optimization. For this simple
textured-overlay workload, reduced shading alone is unlikely to address the
measured video conversion/copy work. Reducing peripheral encoded pixel density
could help, whereas a bitrate-only change retains the full decoded image size.
This is an engineering hypothesis, not a measured improvement. Valve documents
[eye tracking and foveation extensions](https://partner.steamgames.com/doc/steamhardware/steamframe/engines/custom)
and [OpenVR eye-tracking actions](https://partner.steamgames.com/doc/steamhardware/steamframe/input).
Their availability in this overlay route and gaze-to-sharp-text delay still need
device and worn verification; the current pinned SDK and viewer do not use them.
