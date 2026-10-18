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
