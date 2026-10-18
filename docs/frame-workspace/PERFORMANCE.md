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
