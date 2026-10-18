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
            # Deliberately omit placement, credentials and window inventories.
            remote = """import json,pathlib,time
p=pathlib.Path('/run/user/1000/frame-workspace/state.json')
for _ in range(COUNT):
 s=json.loads(p.read_text())
 s={k:v for k,v in s.items() if k.endswith('Ms') or k in (
  'ready','connected','textureWidth','textureHeight','received','decoded','uploaded',
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
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", args.frame, "python3 -"],
                input=remote, text=True, capture_output=True, timeout=args.seconds + 35, check=True)
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
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
