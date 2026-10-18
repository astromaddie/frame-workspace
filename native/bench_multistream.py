#!/usr/bin/env python3
"""Measure one native Workspace plus browser side panels on the actual Frame.

Requires off-head consent and an active shared Frame booking. Start a 9:8 native
Workspace first. No display is created, and only this run's disposable Chrome
sources and side viewers are closed. Desktop updates are not optical FPS.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
from frame_mcp import Client

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

REMOTE = """import fcntl,hashlib,json,os,pathlib,time
p=pathlib.Path('/run/user/%d/frame-workspace'%os.getuid())
s=json.loads((p/'state.json').read_text())
assert s.get('ready') and s.get('overlayKey')=='com.frame-workspace.native.%d.workspace'%os.getuid()
if FOCUS:
 with (p/'control.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  target=p/'control.json'
  old=int(json.loads(target.read_text()).get('revision',0)) if target.exists() else 0
  revision=old+1 if 0<=old<9007199254740991 else 1
  tmp=p/('bench-control-%d.tmp'%os.getpid())
  tmp.write_text(json.dumps({'revision':revision,'focus':True}))
  os.chmod(tmp,0o600);tmp.replace(target)
pose=pathlib.Path.home()/'.local/share/frame-workspace/workspace-pose.json'
print(json.dumps({'sampleMonotonic':time.monotonic(),**{k:s.get(k) for k in ('ready','connected','received','decoded','uploaded','errors','networkErrors','serverErrors','decoderFatalErrors','submitErrors','textureWidth','textureHeight','conversionActive')},'poseSha256':hashlib.sha256(pose.read_bytes()).hexdigest()}))
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:60339")
    ap.add_argument("--frame", default="frame")
    ap.add_argument("--holder", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--counts", default="1,2,4,6")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    counts = [int(x) for x in args.counts.split(",")]
    if not counts or any(n not in (1, 2, 4, 6) for n in counts) or not 10 <= args.seconds <= 60:
        ap.error("counts must be selected from1,2,4,6; seconds must be10..60")
    lock = Path.home() / ".codex/skills/steam-frame-vr-port/scripts/frame-lock.sh"
    booking = subprocess.check_output([str(lock), "status"], text=True,
        env={**os.environ, "FRAME_LOCK_DIR": str(Path.home() / "code/.frame-lock")})
    if not booking.startswith("HELD by " + args.holder + " for "):
        raise RuntimeError("This benchmark requires its named active Frame booking.")
    client = Client(args.url, os.environ.get("FRAME_UI_KEY", "1"))

    def remote(focus=False):
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                 args.frame, "python3 -"], input=REMOTE.replace("FOCUS", str(focus)),
                                capture_output=True, text=True, timeout=15, check=True)
        return json.loads(result.stdout)

    initial = client.request("/api/macview")
    workspace = initial["workspace"]
    if workspace.get("state") != "running" or workspace.get("preset") != "2880x2560":
        raise RuntimeError("Start the owned 2880x2560 Workspace first; no physical-display fallback.")
    src = workspace["captureSrc"]
    if len(initial.get("streams", [])) != 1 or initial["streams"][0]["src"] != src:
        raise RuntimeError("Only the native Workspace may be open before the benchmark; unrelated streams are preserved.")
    primary_id = initial["streams"][0]["id"]
    primary_pose = remote()["poseSha256"]
    reports = []
    template = (ROOT / "bench/pages/scroll.html").read_text()

    def stats(session, since=0):
        response = client.request("/api/macview", {"action": "stream_stats", "id": session,
            "since": since, "settle": 0})
        return {**response["streams"][0], "sampleHostUs": response["now"]}

    def percentiles(values):
        values = sorted(values)
        return ({"n": len(values), "p50": statistics.median(values),
                 "p95": values[round(.95 * (len(values) - 1))]} if values else {"n": 0})

    for count in counts:
        children, shown = [], []
        directory = tempfile.mkdtemp(prefix="frame-workspace-multi-")
        tmp = Path(directory)
        try:
            sources = [src]
            # One full-size moving native desktop; additional independent
            # moving windows on the built-in desktop, with unique profiles.
            for i in range(count):
                title = f"fmv-multi-{os.getpid()}-{i}"
                page = tmp / f"scroll-{i}.html"
                page.write_text(template.replace("fmv-bench scroll", title))
                profile = tmp / f"profile-{i}"
                b = workspace["bounds"]
                width, height = (int(b["width"]), int(b["height"])) if i == 0 else (576, 432)
                x, y = (int(b["x"]), int(b["y"])) if i == 0 else (30 + ((i-1) % 3)*586, 60 + ((i-1)//3)*452)
                proc = subprocess.Popen([CHROME, f"--user-data-dir={profile}", "--no-first-run",
                    "--no-default-browser-check", f"--window-size={width},{height}",
                    f"--window-position={x},{y}", "--app=" + page.as_uri()],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                children.append((proc, profile))
                deadline = time.monotonic() + 20
                while True:
                    windows = [w for w in client.request("/api/macview").get("windows", [])
                               if str(w.get("title", "")).startswith(title)]
                    if len(windows) == 1:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Could not identify the unique owned Chrome fixture.")
                    time.sleep(.3)
                if i:
                    side = f"window:{windows[0]['id']}"
                    shown.append(side)
                    client.request("/api/macview", {"action": "show", "src": side, "quality": "sharp",
                        "w": windows[0]["w"], "h": windows[0]["h"]})
                    sources.append(side)
            deadline = time.monotonic() + 25
            while True:
                state = client.request("/api/macview")
                sessions = {s["src"]: s for s in state["streams"] if s["src"] in sources}
                if len(sessions) == count and all(s.get("stats", {}).get("synced") for s in sessions.values()):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Not all owned viewers synchronized before measurement.")
                time.sleep(.5)
            remote(True)
            time.sleep(4)
            first = {source: stats(sessions[source]["id"]) for source in sources}
            since = {source: max([f["s"] for f in value["frames"]] or [0]) for source, value in first.items()}
            native_first = remote()
            began = time.monotonic(); last_focus = began
            while time.monotonic() - began < args.seconds:
                if time.monotonic() - last_focus >= 10:
                    remote(True); last_focus = time.monotonic()
                time.sleep(.5)
            elapsed = time.monotonic() - began
            native_last = remote()
            streams = []
            for index, source in enumerate(sources):
                data = stats(sessions[source]["id"], since[source])
                duration = (data["sampleHostUs"]-first[source]["sampleHostUs"])/1e6
                frames = [f for f in data["frames"] if f["snd"] >= first[source]["sampleHostUs"]]
                durations = {}
                for stage, a, b in (("captureMs", "cap", "arr"), ("encodeMs", "e0", "e1"),
                    ("wireMs", "snd", "wire"), ("networkMs", "e1", "rx"),
                    ("decodeMs", "rx", "dec"), ("contentToDrawMs", "cap", "drw")):
                    durations[stage] = percentiles([(f[b]-f[a])/1000 for f in frames if f[a] and f[b]])
                streams.append({"label": "native-workspace" if index == 0 else f"browser-side-{index}",
                    "measurementSeconds": duration, "frameCount": len(frames), "sentFps": len(frames)/duration,
                    "captureFps": (data["captured"]-first[source]["captured"])/duration,
                    "mbps": sum(f["b"] for f in frames)*8/duration/1e6,
                    "pictureSizes": sorted({(f["w"], f["h"]) for f in frames}),
                    "controller": data["controller"], "timings": durations,
                    "viewer": data["summary"].get("decoder")})
            reports.append({"count": count, "durationSeconds": elapsed, "route": state.get("route"),
                "streams": streams, "nativeWindowSeconds": native_last["sampleMonotonic"]-native_first["sampleMonotonic"],
                "nativeUploadedFps": (native_last["uploaded"]-native_first["uploaded"])/(native_last["sampleMonotonic"]-native_first["sampleMonotonic"]),
                "nativeErrorsDelta": {k: native_last[k]-native_first[k] for k in
                    ("errors", "networkErrors", "serverErrors", "decoderFatalErrors", "submitErrors")},
                "posePreserved": native_last["poseSha256"] == primary_pose})
            args.out.write_text(json.dumps({"label": args.label, "evidence": __doc__, "conditions": reports}, indent=2)+"\n")
            print(json.dumps({"count": count, "nativeUploadedFps": reports[-1]["nativeUploadedFps"],
                              "sentFps": [round(s["sentFps"], 2) for s in streams]}), flush=True)
        finally:
            cleanup_errors = []
            for source in shown:
                try:
                    client.request("/api/macview", {"action": "stop", "src": source})
                except Exception as error:
                    cleanup_errors.append(str(error))
            for proc, profile in children:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={profile}"], capture_output=True).returncode == 0:
                    if time.monotonic() >= deadline:
                        cleanup_errors.append("Owned Chrome child remains for " + str(profile))
                        break
                    time.sleep(.1)
            if cleanup_errors:
                raise RuntimeError("; ".join(cleanup_errors) + "; fixture preserved at " + str(tmp))
            shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
