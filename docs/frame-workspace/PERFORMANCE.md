# FW-09 active-stream policy and Frame measurements

Explicit viewer focus/input selects a stream for a 30-second in-memory lease.
With multiple live streams, background sources retain their dimensions but cap
capture and encoder pacing at 10 fps. The selected stream retains its normal
quality target. New input/focus switches selection; blur/release does not steal
it. Expiry or disconnect releases the cap. With one stream or no reliable
selection, the existing rates remain unchanged. `FRAME_MAC_VIEW_PRIORITY=0`
disables this policy for comparison or rollback.

This does not infer visibility or eye gaze. Physical Mac keyboard activity is
not observed by the viewer protocol; its priority lease can expire. Controller
statistics report role, reason, cap and remaining lease separately from normal
congestion adaptation. `FRAME_MAC_VIEW_ADAPT=0` disables congestion adaptation,
not this priority policy.

## Matched actual-Frame scrolling trial

One native 2880×2560 Workspace plus browser-backed side windows was measured
with priority off, then on. The native path used V4L2 and CPU conversion at a
30 fps target. Each side window was 576×432 Mac points / 1152×864 video pixels,
using Sharp at a 60 fps target and the Frame browser's software H.264 decoder.
The native stream received an explicit focus request every ten seconds. Both
runs used the same network route, fixture sizes and settings, with congestion
adaptation disabled. This is one sequential pair, without repeated trials or
confidence intervals.

Values are **off → on**. Latency is capture timestamp to viewer draw/upload,
reported as p50/p95, and includes capture, encoding, transport and decode. It is
not input-to-photon latency. Native update rates use the Frame upload counter;
background rates use Mac sent-frame records. Protocol byte rates exclude SSH
and TCP overhead.

| Total streams | Native desktop updates/s | Mean background sent fps | Native content-to-draw ms p50/p95 | Total video Mbit/s |
|---:|---:|---:|---:|---:|
|1|22.5 → 21.8|— → —|112/183 → 112/188|1.36 → 1.61|
|2|21.3 → 22.2|40.4 → 9.2|114/193 → 116/192|3.13 → 2.18|
|4|21.0 → 21.6|35.3 → 9.3|123/220 → 123/198|5.79 → 3.77|
|6|21.9 → 21.7|31.9 → 9.2|131/219 → 126/220|7.98 → 5.70|

All conditions preserved the exact picture dimensions and saved pose, with zero
native network, server, decoder-fatal or submission errors during their sample
windows. Background work fell substantially. The selected native stream stayed
around 22 updates/s; this run does **not** demonstrate a large foreground-rate
improvement or 60 fps. Nominal pauses were 30 seconds; SSH/HTTP sampling makes
the per-metric windows longer and slightly different. Exact durations, p50/p95
stage timings, per-stream bitrate/capture rate and error deltas are retained in
[compact device evidence](evidence/multistream-frame.json).

## Decode-burst follow-up, 2026-10-19

Fresh off-head runs isolated the single full-resolution 2880×2560 Workspace,
with the same scrolling fixture, 30 fps target, V4L2, CPU conversion and disabled
adaptation. The Mac remained on AC; builds and other browser tests were paused.
The display stayed registered while only its viewer was replaced. These are
sequential trials with natural capture/network variance, not repeated statistical
estimates or a remeasurement of the earlier multi-stream matrix.

| Queue bound | Window | Received/s | Uploaded/s | Encoded drops | Overflows | Receive-to-upload ms p50/p95 |
|---:|---:|---:|---:|---:|---:|---:|
|3, original|30.02 s|29.64|23.32|149|15|53/193|
|8, candidate|30.02 s|29.14|29.04|0|0|54/61|
|3, reverse comparison|30.02 s|29.15|27.28|46|5|53/83|
|8, longer run|60.04 s|28.35|27.70|0|0|52/61|

The original three-packet bound discarded otherwise valid H.264 reference
chains during delivery bursts, then dropped dependent pictures until an IDR.
The new eight-packet bound absorbed an observed peak of six queued packets.
Both candidate windows had zero encoded drops, IDR recoveries or errors; their
sampled queue-wait p95 was below 4 ms. The newest-picture-only presentation rule
still discarded 4 and 39 decoded pictures respectively, favoring freshness.
Empty decoder receives were short (maximum below 0.16 ms), rather than a long
blocking drain. Queue/recovery and timestamp bounds remain enforced.

The longer run still did **not** sustain 30 uploads/s: only 28.35 packets/s
arrived, and 27.70 textures/s were uploaded. The fix removes the observed
keyframe-recovery loss; it does not establish 30/60 fps, capture/delivery
regularity, worn comfort or compositor refresh. Delay percentiles above are
one-second state observations of receive-to-upload age, not per-frame
capture-to-photon latency. Exact results and compiler-input/binary identities
are in [the four-run evidence](../../native/evidence/queue-bursts.json).

Reproduce a single-desktop trial with:

```sh
python3 native/bench_desktop.py --seconds 60 --label queue8-cpu30 \
  --out /tmp/queue8-cpu30.json
```

Keep resolution, conversion, source fixture, power and concurrency matched.
The larger queue is finite; its default overflow/IDR recovery must continue to
pass the strict production socket/decoder/image test. On the actual Frame the
new binary passed both the normal 4-second and forced-overload 8-second hidden
GL/socket tests. The latter deliberately overflowed seven times, recovered at
an IDR without reopening the decoder, and retained valid sequence timing and
an RGB difference of at most 2 against FFmpeg's reference. Both tests reported
zero errors; their generated image dumps were removed automatically.

After restarting the companion, restoring Coding and verifying actual inline
placement clicks, a final 20.01-second motion check received 28.73 and uploaded
28.53 frames/s, with zero encoded drops or overflows during that window. The
native owner's lifetime queue gauges include launch/warmup before the window;
they are retained separately from measurement deltas. Final health verified one
connected, ready native stream, the full 2880×2560 texture, zero errors and the
original saved pose. The workspace and bounded Mac awake assertion remain active.

Compact evidence and the inspected UI captures were retained. Removed the
temporary UI worktree and Frame source/build/older rollback trees: **33.74 MiB
allocated**, measured with `du -sk`. The UI worker also removed 13.7 MiB of
temporary dependencies/caches, and native tests automatically removed 42.19 MiB
of logical PPM data. The current installed viewer, companion/profile data and
one preceding verified rollback at `/tmp/frame-workspace-30fps-rollback` remain.

## 60 fps single Workspace, 2026-10-19

The single full-resolution 2880×2560 Workspace now delivers about 60 fresh
desktop updates/s to the Frame at a 60 fps target. The same scrolling fixture
was used throughout, with V4L2, GPU conversion and adaptation disabled; the
first row is a baseline taken the same day with the previously installed
binaries. These are off-head desktop texture updates, not compositor or
optical FPS.

| Configuration | Mac capture/s | Decoded/s | Fresh drawn/s | Overflows | Encoded drops | Capture-to-draw p50/p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (glFinish handoff, 8-packet queue, 1/fps interval, 60 Hz) | 52.6 | 47.1 | 38.2 | 6 | 133 | 65/196 |
| Fence handoff only | 54.8 | 41.2 | 42.3 | 14 | 351 | 73/231 |
| + 16-packet queue, 2 ms capture jitter allowance (two runs) | 58.4, 57.8 | 57.9, 57.4 | 57.1, 56.5 | 0, 0 | 0, 0 | 44/124, 44/140 |
| + Workspace display at 120 Hz (two runs) | 62.1, 62.2 | 62.2, 62.0 | 60.9, 60.7 | 0, 0 | 0, 0 | 64/139, 66/148 |
| Final configuration, 60 s | 60.5 | 60.3 | 59.0 | 0 | 0 | 68/152 |

There were three separate limits, each hidden behind the one before it:

1. **The Frame's GL owner was serial.** Each picture was uploaded, converted,
   then waited on with `glFinish()` before `SetOverlayTexture`, followed by a
   fixed 4 ms sleep. The viewer now issues each picture into one of three
   texture slots, places a GL fence and submits the texture once that fence
   has signalled. SteamVR therefore still never imports an incomplete texture,
   but uploading the next picture overlaps the GPU's conversion of the
   previous one. The loop wakes when a decoded picture arrives instead of on a
   timer. Upload time fell from 3.9 to 0.9 ms p50, and decoded pictures
   replaced before upload fell from 284 to 24–36 per 30 s. Reusing a slot after
   submission is safe because vrclient blits the submitted texture into its own
   shared texture inside our GL context (`CVRSharedGLTexture`, with a Vulkan/GL
   interop semaphore). `--handoff finish` or `FRAME_WORKSPACE_HANDOFF=finish`
   restores the previous serial path.
2. **Wi-Fi delivers in bursts.** New per-second gauges show packet arrival gaps
   of about 100 ms every second and 140–220 ms at p95, while one decoder step
   (parse, send, receive, plane copy) peaks at 15–20 ms. A 200 ms gap at 60 fps
   is about 12 packets, which overflowed the 8-packet queue; each overflow
   discards the reference chain and waits for an IDR. The bound is now 16, and
   the observed high water in these runs was 9–13. Frame power saving is off;
   the link was 5 GHz channel 36 at −58 dBm. The Frame hosts its own 6 GHz
   access point on the same radio, and the Mac's AWDL interface was active.
   Either may cause the gaps (**inferred**, not isolated).
3. **Capture lost early frames.** ScreenCaptureKit's `minimumFrameInterval` was
   exactly 1/fps, so a 60 Hz display frame landing slightly early was rejected.
   That held capture near 54.6/s in every earlier 60-target run. The interval
   now allows 2 ms of jitter, which leaves 30 and 10 fps caps on the same vsync
   multiples. That alone reached about 58/s. At 60 Hz every late app frame still
   costs a streamed frame, so the Workspace display now defaults to 120 Hz
   (`FRAME_MAC_VIEW_VD_HZ=60` restores 60 Hz); capture can then take the next
   vsync.

The H.264 stream is about 2.2 Mbit/s, so bandwidth is not a limit. Capture-to-
draw latency read about 20 ms higher at 120 Hz, but the Frame-local
receive-to-upload age was unchanged (28 ms p50). The cross-device clock
estimate uses a link with 4.5–95.6 ms ping, so whether that difference is real
is unresolved. All conditions kept the exact dimensions and saved pose, with
zero errors. On the Frame, the new binary passed the V4L2 default, reconnect,
size-change, forced-overload and GPU BT.601 stream gates, plus the GPU
conversion and decoder suites. Exact runs, hashes and gates are in
[the 60 fps evidence](../../native/evidence/sixty-fps.json).

Worn presentation, judder and readability at 60 fps are not established.

Reproduce with the Workspace running, off-head and with no other benchmarks:

```sh
python3 native/bench_desktop.py --seconds 60 --label sixty --out /tmp/sixty.json
```

For an A/B comparison, the Frame launcher does not forward viewer environment
variables. Install a build of the previous viewer (or pass `--handoff finish` in
a direct test run), then Show Workspace again. Alternatively, restart the
companion with `FRAME_MAC_VIEW_VD_HZ=60`. Keep everything else matched. A pass shows fresh drawn updates within about
2/s of Mac capture, zero queue overflows, zero encoded drops and zero errors.

## Remaining acceptance and reproduction

This mixed route exercises one native overlay and real Frame browser viewers.
It does not establish six native overlays, tiled-native alignment, six equally
large video streams, sustained worn coding, focus switching/typing latency,
static-code/IME behavior or USB-C performance. FW-09 remains open for that broader
acceptance. The earlier [synthetic loopback evidence](evidence/stream-priority-loopback.json)
is a short functional wiring check, not a substitute for this device trial.

After off-head consent and the shared Frame booking, restore Coding, keep only
the Workspace open, and run:

```sh
python3 native/bench_multistream.py --holder <booking-holder> \
  --label priority-off --seconds 30 --out /tmp/priority-off.json
```

Restart the companion with `FRAME_MAC_VIEW_PRIORITY=1`, restore Coding and repeat
with `--label priority-on`. Keep all other settings matched. The harness closes
only its disposable fixture windows/profiles and side viewers; it leaves the
Workspace running. It refuses to reuse unrelated active streams.
