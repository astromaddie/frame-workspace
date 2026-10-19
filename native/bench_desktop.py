#!/usr/bin/env python3
"""Measure native desktop updates with an owned scrolling fixture.

Start Workspace first. Obtain off-head consent and the shared Frame booking
before running. This does not create displays or restart the viewer. Close other
benchmarks/builds for a comparison; desktop updates are not compositor FPS.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
import frame_mcp  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:60339")
    parser.add_argument("--frame", default="frame", help="SSH alias")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 120:
        parser.error("seconds must be 10..120")
    client = frame_mcp.Client(args.url, os.environ.get("FRAME_UI_KEY", "1"))
    workspace = client.request("/api/macview")["workspace"]
    if workspace.get("state") != "running":
        raise RuntimeError("Start an owned Workspace before benchmarking; no physical-display fallback.")
    bounds = workspace["bounds"]
    spec = importlib.util.spec_from_file_location("workspace_benchmark", ROOT / "scripts/macview-bench.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    samples = []
    mac_frames = {}
    mac_start = mac_end = None
    fixture_size = None
    fixture_rate = None
    with tempfile.TemporaryDirectory(prefix="frame-workspace-native-motion-") as profile:
        fixture = bench.chrome_window("scroll.html", profile=profile,
                                      size=(int(bounds["width"]), int(bounds["height"])),
                                      position=(int(bounds["x"]), int(bounds["y"])))
        try:
            time.sleep(5)
            owned = [w for w in client.request("/api/macview").get("windows", [])
                     if str(w.get("title", "")).startswith("fmv-bench scroll")]
            if len(owned) != 1:
                raise RuntimeError("Expected exactly one owned scrolling fixture; close other benchmarks.")
            fixture_size = [int(owned[0]["w"]), int(owned[0]["h"])]
            if any(actual < expected * .8 for actual, expected in
                   zip(fixture_size, [bounds["width"], bounds["height"]])):
                raise RuntimeError("The owned fixture is too small for this Workspace; check its display placement.")
            cadence = re.search(r"(\d+) fps", owned[0].get("title", ""))
            fixture_rate = int(cadence.group(1)) if cadence else None
            streams = client.request("/api/macview")["streams"]
            sources = [s for s in streams if s.get("title") == "Frame Workspace"]
            if len(sources) != 1:
                raise RuntimeError("Expected one owned Workspace stream.")
            stream_id = sources[0]["id"]
            def mac_snapshot(since=0):
                response = client.request("/api/macview", {"action": "stream_stats", "id": stream_id,
                                                          "since": since, "settle": 0})
                stream = next(s for s in response["streams"] if s["id"] == stream_id)
                return response["now"], stream
            mac_start = mac_snapshot()
            first_seq = max((r["s"] for r in mac_start[1]["frames"]), default=0)
            last_seq = first_seq
            collector_stop = threading.Event()
            collector_errors = []
            def collect():
                nonlocal last_seq
                try:
                    while not collector_stop.wait(1):
                        _, stream = mac_snapshot(max(first_seq, last_seq - 120))
                        for row in stream["frames"]:
                            mac_frames[row["s"]] = row
                            last_seq = max(last_seq, row["s"])
                except Exception as error:
                    collector_errors.append(error)
            collector = threading.Thread(target=collect, daemon=True)
            collector.start()
            # Deliberately omit placement, credentials and window inventories.
            remote = """import json,pathlib,time
p=pathlib.Path('/run/user/1000/frame-workspace/state.json')
for _ in range(COUNT):
 s=json.loads(p.read_text())
 s={k:v for k,v in s.items() if k.endswith('Ms') or k in (
  'ready','connected','textureWidth','textureHeight','encodedWidth','encodedHeight',
  'foveationActive','foveationNegotiated','diagnosticGaze','gazeState','received','decoded','uploaded',
  'errors','networkErrors','serverErrors','decoderSendErrors','decoderReceiveErrors',
  'decoderFatalErrors','submitErrors','droppedEncoded','droppedDecoded','decoderActive','decoderCodec',
  'queueOverflows','packetQueueLimit','packetQueueHighWater','emptyReceives',
  'droppedAwaitingIdr','overflowRecoveryIdrs','lastOverflowRecoverySeq',
  'staleDriverFramesDropped','decoderTimingHighWater','decoderOpenAttempts',
  'decoderInitialOpens','decoderReconnectOpens','decoderFormatReopens','decoderOpenFailures',
  'decoderOpenTotalMs',
  'conversionRequested','conversionActive','conversionFallbacks',
  'decoderPixelFormat','decoderColorSpace','decoderColorRange')}
 s['sampleMonotonic']=time.monotonic()
 print(json.dumps(s),flush=True)
 time.sleep(1)
""".replace("COUNT", str(args.seconds + 1))
            try:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", args.frame, "python3 -"],
                    input=remote, text=True, capture_output=True, timeout=args.seconds + 35, check=True)
            finally:
                collector_stop.set()
                collector.join(timeout=15)
            if collector.is_alive() or collector_errors:
                raise RuntimeError("Could not collect Mac stream statistics.") from (collector_errors or [None])[0]
            mac_end = mac_snapshot(max(first_seq, last_seq - 120))
            for row in mac_end[1]["frames"]:
                mac_frames[row["s"]] = row
            # Let asynchronous frame-drawn reports settle without extending the
            # measurement window or retaining credentials/raw frame records.
            time.sleep(1)
            for row in mac_snapshot(max(first_seq, last_seq - 120))[1]["frames"]:
                mac_frames[row["s"]] = row
            samples = [json.loads(line) for line in result.stdout.splitlines()]
        finally:
            fixture.terminate()
            try:
                fixture.wait(timeout=8)
            except subprocess.TimeoutExpired:
                fixture.kill()
                fixture.wait()
            # Chromium may leave children after the launcher exits. Target only
            # the unique disposable profile; never the user's Chrome profile.
            for _ in range(50):
                result = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={profile}"],
                                        text=True, capture_output=True)
                if result.returncode == 1:
                    break
                if result.returncode:
                    raise RuntimeError("Could not confirm owned Chrome fixture cleanup.")
                time.sleep(.1)
            else:
                raise RuntimeError("Owned Chrome fixture did not exit.")
    if len(samples) < 2 or not all(s.get("ready") for s in samples):
        raise RuntimeError("Native viewer was not ready throughout the measurement.")
    first, last = samples[0], samples[-1]
    duration = last["sampleMonotonic"] - first["sampleMonotonic"]
    counters = ("received", "decoded", "uploaded", "errors", "networkErrors", "serverErrors",
                "decoderSendErrors", "decoderReceiveErrors", "decoderFatalErrors", "submitErrors",
                "droppedEncoded", "droppedDecoded", "queueOverflows", "droppedAwaitingIdr",
                "overflowRecoveryIdrs", "staleDriverFramesDropped", "decoderOpenAttempts",
                "decoderInitialOpens", "decoderReconnectOpens", "decoderFormatReopens",
                "decoderOpenFailures", "decoderOpenTotalMs")
    counters += tuple(k for k in ('emptyReceives', 'emptyReceiveTotalMs') if k in first and k in last)
    delta = {key: last[key] - first[key] for key in counters}
    if any(value < 0 for value in delta.values()):
        raise RuntimeError("Native viewer restarted during measurement.")
    timings = {}
    for key in last:
        if key.endswith("Ms") and key not in ("decoderOpenTotalMs", "emptyReceiveTotalMs", "emptyReceiveMaxMs"):
            values = sorted(float(s[key]) for s in samples[1:] if key in s)
            timings[key] = {"p50": statistics.median(values),
                            "p95": values[round(.95 * (len(values) - 1))]}
    report = {"label": args.label, "evidence": "off-head desktop texture updates; not compositor or optical FPS",
              "duration": duration, "workspacePreset": workspace["preset"],
              "target": workspace["stream"],
              "fixtureWindowSize": fixture_size,
              "fixtureReportedFpsAtWarmup": fixture_rate,
              "receivedFps": delta["received"] / duration,
              "decodedFps": delta["decoded"] / duration,
              "uploadedFps": delta["uploaded"] / duration,
              "counters": delta, "timings": timings,
              "textureSizes": sorted({(s["textureWidth"], s["textureHeight"]) for s in samples}),
              "encodedSizes": sorted({(s.get("encodedWidth"), s.get("encodedHeight")) for s in samples}),
              "foveationStates": sorted({s.get("foveationActive", False) for s in samples}),
              "gazeStates": sorted({s.get("gazeState", "unknown") for s in samples}),
              "diagnosticGaze": last.get("diagnosticGaze", False),
              "decoder": last.get("decoderCodec"),
              "decoderPixelFormat": last.get("decoderPixelFormat"),
              "decoderColorSpace": last.get("decoderColorSpace"),
              "decoderColorRange": last.get("decoderColorRange"),
              "conversionRequested": last.get("conversionRequested"),
              "conversionActive": last.get("conversionActive"),
              "conversionFallbacks": last.get("conversionFallbacks"),
              "lastOverflowRecoverySeq": last.get("lastOverflowRecoverySeq"),
              "decoderTimingHighWater": last.get("decoderTimingHighWater"),
              "packetQueueLimit": last.get("packetQueueLimit"),
              "packetQueueHighWater": last.get("packetQueueHighWater"),
              "emptyReceiveMaxMs": last.get("emptyReceiveMaxMs"),
              "note": "Stage percentiles summarize one-second observations, not every frame."}
    start_us, start_stream = mac_start
    end_us, end_stream = mac_end
    mac_duration = (end_us - start_us) / 1_000_000
    rows = sorted((r for r in mac_frames.values() if start_us <= r["snd"] < end_us), key=lambda r: r["s"])
    seen = {(r["cap"], r["arr"]) for r in start_stream["frames"]}
    unique, repacks = [], []
    for row in rows:
        capture = (row["cap"], row["arr"])
        (repacks if capture in seen else unique).append(row)
        seen.add(capture)
    # A capture's first sent copy can be dropped while a later gaze repack is
    # drawn. Count its first actual draw exactly once across all sent copies.
    shown = {}
    for row in sorted((r for r in rows if r["drw"] > 0), key=lambda r: r["drw"]):
        if row["cap"] >= start_us:
            shown.setdefault((row["cap"], row["arr"]), row)
    def latency(records):
        values = sorted((r["drw"] - r["cap"]) / 1000 for r in records
                        if r["cap"] >= start_us and r["drw"] >= r["cap"] > 0)
        return {"n": len(values), **({"p50": statistics.median(values),
                 "p95": values[round(.95 * (len(values) - 1))]} if values else {})}
    def stages(records):
        # Per-frame stage durations on the Mac clock (the Frame's receive/decode/
        # draw times arrive clock-mapped), so slow stages and jitter are visible.
        names = (("captureDelivery", "cap", "arr"), ("waitForEncoder", "arr", "e0"),
                 ("encode", "e0", "e1"), ("queueToSend", "e1", "snd"), ("socket", "snd", "wire"),
                 ("network", "wire", "rx"), ("frameDecode", "rx", "dec"), ("frameDrawSubmit", "dec", "drw"))
        out = {}
        for name, a, b in names:
            values = sorted((r[b] - r[a]) / 1000 for r in records if r[a] > 0 and r[b] > 0)
            if values:
                out[name] = {"p50": round(statistics.median(values), 2),
                             "p95": round(values[round(.95 * (len(values) - 1))], 2)}
        return out
    def cadence(records, key):
        # Gaps between consecutive frames, in 120 Hz headset refreshes. Even
        # 60 fps pacing is all 2s; 1s and 3s are visible judder.
        times = sorted(r[key] for r in records if r[key] > 0)
        gaps = [round((b - a) / 8333.3) for a, b in zip(times, times[1:])]
        counts = {str(n): gaps.count(n) for n in sorted(set(min(g, 6) for g in gaps))}
        return {"refreshGaps": counts, "evenShare": round(gaps.count(2) / len(gaps), 3) if gaps else None}
    report["mac"] = {"duration": mac_duration,
                     "desktopCaptureFps": (end_stream["captured"] - start_stream["captured"]) / mac_duration,
                     "uniqueSentCaptureFps": len(unique) / mac_duration,
                     "uniqueDrawnCaptureFps": len(shown) / mac_duration,
                     "sentFps": len(rows) / mac_duration,
                     "repeatedCaptureSubmissions": len(repacks),
                     "h264Mbps": sum(r["b"] for r in rows) * 8 / mac_duration / 1_000_000,
                     "contentToDrawMs": latency(shown.values()), "repeatContentAgeMs": latency(repacks),
                     "stageMs": stages(shown.values()),
                     "captureCadence": cadence(shown.values(), "cap"),
                     "receiveCadence": cadence(shown.values(), "rx"),
                     "drawCadence": cadence(shown.values(), "drw"),
                     "gazeCounters": {k: v - start_stream.get("summary", {}).get("gaze", {}).get("counts", {}).get(k, 0)
                                      for k, v in end_stream.get("summary", {}).get("gaze", {}).get("counts", {}).items()},
                     "note": "Mac host-clock window brackets the native sampling window; H264 payload only. "
                             "Unique captures are separate from repeats caused by gaze or backlog. "
                             "Clock-synchronized cap-to-draw excludes compositor and optical latency."}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
