# FW-15 capacity sweep: show the spike fixture's windows
# (mac/frame-mac-view/Diagnostics/SpatialFixture.swift) on displays of their
# own, as native panels, and record per-window delivery on the Frame plus CPU
# and memory on both machines at 1, 2, 4, 6, 8, 10 and 12 windows. Needs the
# companion on port 60339 started with FRAME_WINDOW_DISPLAYS=1 and the
# Workspace running. Usage: python3 scripts/spatial-capacity-sweep.py LOG
import json, re, subprocess, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "ui"))
import frame_mcp
c = frame_mcp.Client("http://127.0.0.1:60339", "1")
out = open(sys.argv[1], "a")
def log(*a):
    print(*a, file=out, flush=True)
def frame(cmd, timeout=60):
    return subprocess.run(["ssh", "frame", cmd], capture_output=True, text=True, timeout=timeout).stdout
ws = sorted([w for w in c.request("/api/macview")["windows"] if str(w.get("title", "")).startswith("Spatial Spike")],
            key=lambda w: w["title"])
shown = []
for target in (1, 2, 4, 6, 8, 10, 12):
    while len(shown) < target:
        w = ws[len(shown)]
        t0 = time.time()
        try:
            c.request("/api/macview", {"action": "show", "src": "separate:" + w["src"].split(":")[1], "quality": "sharp"})
            log(f"shown {w['title']} in {time.time() - t0:.1f}s")
        except Exception as e:
            log(f"show {w['title']} failed: {e}")
        shown.append(w)
    time.sleep(22)  # two vr-perf periods
    units = frame("systemctl --user list-units 'frame-workspace-panel-*' --no-legend | awk '{print $1}'").split()
    stats = []
    for u in units:
        line = frame(f"journalctl --user -u {u} --no-pager -o cat -n 40 | grep vr-perf | tail -1")
        m = dict(re.findall(r"(\w+)=([\d.]+)", line))
        stats.append((float(m.get("upload_fps", 0)), float(m.get("queue_age_ms", 0)), int(float(m.get("dropped", 0)))))
    top = frame("top -bn1 | head -4 | tail -3")
    mem = frame("free -m | sed -n 2p")
    mac = subprocess.run("ps -A -o %cpu,rss,comm | grep -E 'frame-mac-view|WindowServer' | grep -v grep", shell=True,
                         capture_output=True, text=True).stdout
    fps = [s[0] for s in stats]
    log(f"== {target} windows ({len(units)} panels): fps min {min(fps, default=0):.1f} mean "
        f"{sum(fps) / max(len(fps), 1):.1f}; age max {max((s[1] for s in stats), default=0):.0f} ms; "
        f"dropped {[s[2] for s in stats]}")
    log("frame:", " | ".join(l.strip() for l in top.splitlines()), "| mem", mem.strip())
    log("mac:", " | ".join(l.strip() for l in mac.splitlines()))
log("done")
