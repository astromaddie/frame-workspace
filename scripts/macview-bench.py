#!/usr/bin/env python3
"""Benchmark "Mac in the headset" on the real Frame, so every change to the
stream is judged by numbers (docs/mac-in-headset.md, "Measuring").

  scripts/macview-bench.py run [--scenario test,scroll,type] [--net 8] [--label x]
  scripts/macview-bench.py run --mode display --display workspace --quality sharp
  scripts/macview-bench.py compare bench/results/A.json bench/results/B.json
  scripts/macview-bench.py ab --arm off:FRAME_MAC_VIEW_ADAPT=0 --arm on: --repeat 3 --scenario scroll

`run` uses the lab agent (mac/frame-mac-view/lab.sh serve), which has
Screen Recording and Accessibility. It opens a viewer on the Frame the way
Frame Control does, drives a fixed scenario, and reads the agent's per-frame
records: when the Mac composited each frame, when it was encoded and sent,
and when the viewer received, decoded and drew it (the viewer's clock is
synced to the Mac's). "content" is Mac composited -> drawn in the viewer;
what the Frame's compositor adds after that is reported ("present",
"fps_shown") but not graded, because panel scheduling can change with headset
visibility, standby and firmware. These clock-based estimates do not measure
physical photon latency or establish worn text readability. It writes a summary to bench/results/ and exits 1 if a
target is missed ("bad"), or if --baseline is given and a key number got
more than 10% worse.

Wi-Fi changes from minute to minute, so single runs taken at different times
can differ by more than a change does. `ab` restarts the lab agent with each
arm's settings (environment variables read by the agent), runs the arms
interleaved, and prints each metric's median and range per arm.

Nobody needs to wear the headset for these synthetic scenarios. The scroll and
type scenarios open a throwaway Chrome window (a fresh profile in /tmp, removed
after each scenario) on the Mac. Display mode resolves the current main or
Workspace display at each launch; a missing/ambiguous Workspace fails closed.

Network conditions come from a relay on the Mac between the agent and the
tunnel, so they need no sudo:
  --net 8                8 Mbit/s the whole run
  --net 50@0,8@10,50@20  50 Mbit/s, 8 from 10 s, back to 50 from 20 s
  --delay 30             30 ms more one-way delay
The relay holds at most --buffer ms of data at the current rate (default
250 ms, like a bloated Wi-Fi queue), then pushes back on the agent.

Stdlib only.
"""
import argparse
import asyncio
import datetime
try:
    import fcntl
except ImportError:  # compare/report helpers remain importable on Windows
    fcntl = None
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode  # %20, not +: the agent's URLComponents keeps +

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
import frame_macview  # noqa: E402

RESULTS = ROOT / "bench" / "results"
PAGES = ROOT / "bench" / "pages"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LAB_DIR = Path.home() / "Library" / "Caches" / "frame-mac-view-lab"  # lab.sh serve

# The acceptance bar (the handoff's targets): (feels local, acceptable) per
# metric; worse than acceptable is "bad". Latencies in ms.
TARGETS = {
    "content_p50": (25, 60), "content_p95": (40, 100),
    "input_p50": (50, 100), "input_p95": (70, 150),
    "fps": (58, 45),  # higher is better
    "late_pct": (1, 5), "stall_max": (100, 250),
    "adapt_s": (1, 3),
}
HIGHER_IS_BETTER = {"fps"}
# Content that changes every frame, so fps and stalls mean something.
MOVING = {"test", "scroll"}


def ms(a, b):
    return (b - a) / 1000 if a and b else None


def pct(values, p):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return round(v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))], 1)


def dist(values):
    v = [x for x in values if x is not None]
    return {"p50": pct(v, 0.5), "p95": pct(v, 0.95), "p99": pct(v, 0.99), "n": len(v)} if v else {"n": 0}


# ---- the network relay ----

class Relay:
    """Agent <-> tunnel, shaping the agent-to-Frame direction: a bottleneck
    of `rate` Mbit/s with a queue of `buffer_ms`, plus `delay` ms each way."""

    def __init__(self, target_port, schedule, delay_ms=0, buffer_ms=250):
        self.target_port = target_port
        self.schedule = schedule  # [(seconds from start, Mbit/s or None)]
        self.delay = delay_ms / 1000
        self.buffer_ms = buffer_ms
        self.rate = schedule[0][1] if schedule else None
        self.next_free = 0.0
        self.port = None
        self.loop = asyncio.new_event_loop()
        # Connections reset when a run tears the tunnel down; that's expected.
        self.loop.set_exception_handler(lambda loop, ctx: None if isinstance(ctx.get("exception"), ConnectionError)
                                        else loop.default_exception_handler(ctx))
        self.started = None
        self.queued_max = 0

    def start(self):
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            server = self.loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
            self.port = server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)

    def begin(self):
        """Start the schedule's clock (when the measured part begins)."""
        self.started = time.monotonic()
        for at, rate in self.schedule:
            self.loop.call_soon_threadsafe(self.loop.call_later, at, self._set_rate, rate)

    def _set_rate(self, rate):
        self.rate = rate

    def rate_at(self, t):
        r = None
        for at, rate in self.schedule:
            if t >= at:
                r = rate
        return r

    async def _client(self, reader, writer):
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(self._shaped(up_r, writer), self._plain(reader, up_w), return_exceptions=True)
        for w in (writer, up_w):
            w.close()

    async def _plain(self, reader, writer):  # Frame -> agent: delay only
        if not self.delay:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.close()
            return
        # Each chunk leaves `delay` after it arrived, while reading goes on:
        # a delay line, not a pause (which would add up behind a busy stream).
        queue = asyncio.Queue()

        async def send():
            while (item := await queue.get())[1] is not None:
                wait = item[0] - self.loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(item[1])
                await writer.drain()
            writer.close()

        async def pump():
            try:
                while data := await reader.read(65536):
                    await queue.put((self.loop.time() + self.delay, data))
            finally:  # EOF or a reset: either way the sender finishes and closes
                queue.put_nowait((0, None))

        sender, pumper = asyncio.ensure_future(send()), asyncio.ensure_future(pump())
        done, _ = await asyncio.wait({sender, pumper}, return_when=asyncio.FIRST_COMPLETED)
        if sender in done:  # the agent's side went away: stop reading, so the connection closes
            pumper.cancel()
            writer.close()
            return
        await sender  # the viewer's side ended: deliver what's queued first

    async def _shaped(self, reader, writer):  # agent -> Frame
        queue = asyncio.Queue()
        state = {"bytes": 0}
        room = asyncio.Event()
        room.set()

        async def send():
            while True:
                at, data = await queue.get()
                if data is None:
                    return
                wait = at - self.loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()
                state["bytes"] -= len(data)
                room.set()

        sender = asyncio.ensure_future(send())
        while data := await reader.read(16384):
            now = self.loop.time()
            depart = now
            if self.rate:
                depart = max(now, self.next_free) + len(data) * 8 / (self.rate * 1e6)
                self.next_free = depart
            state["bytes"] += len(data)
            self.queued_max = max(self.queued_max, state["bytes"])
            await queue.put((depart + self.delay, data))
            # A full queue pushes back, as a real bottleneck's buffer does.
            while self.rate and state["bytes"] > self.rate * 1e6 / 8 * self.buffer_ms / 1000:
                room.clear()
                await room.wait()
        await queue.put((0, None))
        await sender


# ---- the lab agent, through Frame Control's own code ----

class LabView(frame_macview.MacView):
    """MacView against the already-running lab agent, with the tunnel
    pointing at `tunnel_port` (the relay, or the agent itself)."""

    def __init__(self, tunnel_ssh, run, frame, agent_port, token, tunnel_port):
        super().__init__(tunnel_ssh, run, frame)
        self.agent_port = agent_port
        self.token = token
        self.port = tunnel_port

        class Alive:
            def poll(self):
                return None

        self.agent = Alive()

    def ensure_agent(self):
        pass

    def call(self, path, method="GET", **query):
        url = f"http://127.0.0.1:{self.agent_port}{path}?{urlencode({**query, 'k': self.token}, quote_via=quote)}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)


def ssh_runner(base):
    def run(remote, stdin=None, timeout=30):
        r = subprocess.run([*base, remote], input=stdin, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            e = RuntimeError((r.stderr or r.stdout).strip())
            e.stdout = r.stdout
            raise e
        return r.stdout
    return run


# ---- CPU on both ends ----

FRAME_CPU = r"""
hz=$(getconf CLK_TCK)
while :; do
  t=$(date +%s.%N)
  for g in viewer:'frame-control/mac-view|data/frame-mac-view|frame-workspace/mac-view' tailscaled:tailscaled sshd:'^sshd' gamescope:'^gamescope'; do
    name=${g%%:*} pat=${g#*:} sum=0
    for p in $(pgrep -f "$pat"); do
      s=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null) && sum=$((sum + s))
    done
    echo "$t $name $sum $hz"
  done
  echo "$t total $(awk '/^cpu /{print $2+$3+$4+$6+$7+$8}' /proc/stat) $hz"
  sleep 2
done
"""


class CpuSampler:
    def __init__(self, frame_ssh, agent_pid):
        self.agent_pid = agent_pid
        self.mac = []  # (t, cpu seconds)
        self.frame = {}  # name -> [(t, ticks, hz)]
        self.proc = subprocess.Popen([*frame_ssh, "bash -s"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.proc.stdin.write(FRAME_CPU)
        self.proc.stdin.close()
        threading.Thread(target=self._read, daemon=True).start()
        self.stop_flag = False
        threading.Thread(target=self._mac, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            parts = line.split()
            if len(parts) == 4:
                self.frame.setdefault(parts[1], []).append((float(parts[0]), int(parts[2]), int(parts[3])))

    def _mac(self):
        while not self.stop_flag:
            out = subprocess.run(["ps", "-o", "cputime=", "-p", str(self.agent_pid)], capture_output=True,
                                 text=True).stdout.strip()
            m = re.match(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", out)
            if m:
                h, mnt, s = m.groups()
                self.mac.append((time.monotonic(), int(h or 0) * 3600 + int(mnt) * 60 + float(s)))
            time.sleep(2)

    def stop(self):
        self.stop_flag = True
        self.proc.terminate()

    def summary(self):
        def rate(samples):
            if len(samples) < 2:
                return None
            (t0, a0, *h0), (t1, a1, *_) = samples[0], samples[-1]
            hz = h0[0] if h0 else 1
            return round((a1 - a0) / hz / (t1 - t0) * 100, 1)
        out = {"mac_agent_pct": rate(self.mac)}
        for name, s in self.frame.items():
            out[f"frame_{name}_pct"] = rate(s)
        if "frame_total_pct" in out and out["frame_total_pct"] is not None:
            out["frame_total_pct"] = round(out["frame_total_pct"] / 8, 1)  # 8 cores -> share of the whole
        return out


# ---- scenarios ----

def chrome_window(page, *, profile, size=(1280, 820), position=(80, 80)):
    """Opens a page in a throwaway Chrome app window; returns (proc, title)."""
    if not Path(CHROME).exists():
        raise SystemExit("Google Chrome is needed for the scroll and type scenarios.")
    proc = subprocess.Popen([CHROME, f"--user-data-dir={profile}", "--no-first-run",
                             "--no-default-browser-check", f"--window-size={size[0]},{size[1]}",
                             f"--window-position={position[0]},{position[1]}", f"--app=file://{PAGES / page}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def select_display(displays, selection):
    """Resolve a fresh source, never substitute the private physical desktop."""
    matches = [d for d in displays if
               (d.get("main") if selection == "main" else d.get("name") == "Frame Workspace")]
    if len(matches) != 1:
        raise SystemExit(f"Expected one {selection} display, found {len(matches)}; start Workspace first if selected")
    return matches[0]


def find_window(mv, title, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        for w in mv.call("/windows").get("windows", []):
            if title in (w.get("title") or ""):
                return w
        time.sleep(0.5)
    raise SystemExit(f"window {title!r} didn't appear")


def git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def parse_schedule(net):
    if not net or net == "none":
        return []
    out = []
    for part in net.split(","):
        rate, _, at = part.partition("@")
        out.append((float(at or 0), None if rate in ("", "none", "0") else float(rate)))
    return sorted(out)


def run_scenario(args, scenario, agent_port, token, frame_ssh):
    schedule = parse_schedule(args.net)
    relay = None
    tunnel_port = agent_port
    if schedule or args.delay:
        relay = Relay(agent_port, schedule, args.delay, args.buffer)
        relay.start()
        tunnel_port = relay.port
    tunnel_ssh = list(frame_ssh[:-1]) + args.ssh_opt
    mv = LabView(tunnel_ssh, ssh_runner(frame_ssh), frame_ssh[-1], agent_port, token, tunnel_port)
    mv.prefer_usb = args.usb  # otherwise the path is --host's (or ssh's default)
    if args.browser_flag is not None:
        mv.browser_flags = [f for f in args.browser_flag if f]
    chrome = None
    chrome_profile = None
    src = "test"
    try:
        source_size = (1280, 820)
        if scenario in ("scroll", "type"):
            position = (80, 80)
            display = None
            if args.mode == "display":
                displays = mv.call("/displays").get("displays", [])
                display = select_display(displays, args.display)
                source_size = (display["w"], display["h"])
                position = (int(display.get("x", 0)) + 20, int(display.get("y", 0)) + 40)
            chrome_profile = tempfile.TemporaryDirectory(prefix="fmv-bench-")
            chrome = chrome_window(f"{scenario}.html", size=source_size, position=position, profile=chrome_profile.name)
            w = find_window(mv, f"fmv-bench {scenario}")
            src = display["src"] if display else f"{args.mode}:{w['id']}"
        t = time.time()
        shown = mv.show(src, args.quality, *source_size)
        show_s = round(time.time() - t, 2)
        # Wait for frames, then measure from a clean start.
        stream = None
        end = time.time() + 20
        while time.time() < end and not stream:
            for s in mv.call("/status").get("streams", []):
                if s["src"] == src and s.get("stats", {}).get("fps", 0) > 0:
                    stream = s
            time.sleep(0.5)
        if not stream:
            raise SystemExit(f"{scenario}: no frames reached the viewer within 20 s")
        time.sleep(args.warmup)
        first = mv.call("/stats", id=stream["id"], settle=0)["streams"][0]
        since = max([f["s"] for f in first["frames"]] or [0])
        captured_before = first["captured"]
        start_mac_us = mv.call("/stats", id=stream["id"])["now"]
        agent_pid = int(subprocess.run(["pgrep", "-f", "Frame Mac View Lab.app/Contents/MacOS/frame-mac-view"],
                                       capture_output=True, text=True).stdout.split()[0])
        cpu = CpuSampler(frame_ssh, agent_pid)
        if relay:
            relay.begin()
        expected = 0  # input events the harness asked the viewer for
        if scenario == "type":
            mv.call("/bench", method="POST", src=src, action="click", x=0.5, y=0.3)
            time.sleep(0.5)
            # Steady typing, then slow typing that lets the link go idle.
            fast = "the quick brown fox jumps over the lazy dog "
            n_fast = max(10, int((args.duration * 0.6) / 0.2))
            mv.call("/bench", method="POST", src=src, action="type", text=(fast * (n_fast // len(fast) + 1))[:n_fast],
                    interval=200)
            expected += n_fast
            time.sleep(n_fast * 0.2 + 0.5)
            slow_n = max(3, int(args.duration * 0.4 / 1.5))
            mv.call("/bench", method="POST", src=src, action="type", text=("idle typing test " * (slow_n // 17 + 1))[:slow_n],
                    interval=1500)
            expected += slow_n
            time.sleep(slow_n * 1.5 + 0.5)
        else:
            # Clicks on the test pattern measure the input path; the scroll
            # page just scrolls.
            end = time.time() + args.duration
            while time.time() < end:
                if scenario == "test":
                    mv.call("/bench", method="POST", src=src, action="click", x=0.3, y=0.5)
                    expected += 1
                time.sleep(0.5)
        at_end = mv.call("/stats", id=stream["id"], since=2**32 - 1)
        end_mac_us = at_end["now"]
        captured_end = at_end["streams"][0]["captured"] if at_end["streams"] else None
        time.sleep(2)  # let the viewer report the last frames
        streams = mv.call("/stats", id=stream["id"], since=since)["streams"]
        if not streams:
            raise SystemExit(f"{scenario}: the stream ended during the run (did the Frame go to sleep?)")
        data = streams[0]
        cpu.stop()
        if args.raw:
            Path(f"{args.raw}-{scenario}.json").write_text(json.dumps(data))
        result = summarize(scenario, data, start_mac_us, end_mac_us, args, relay, schedule, expected)
        result["controller"] = data.get("controller", {})
        result["events"] = [{"t": round((e["t"] - start_mac_us) / 1e6, 2), "e": e["e"]}
                            for e in data.get("events", []) if e["t"] >= start_mac_us]
        result["captured"] = (captured_end if captured_end is not None else data["captured"]) - captured_before
        result["source_fps"] = round(result["captured"] / max(result["duration_s"], 1), 1)
        result["cpu"] = cpu.summary()
        result["viewer"] = data["summary"].get("decoder", "")
        result["show_s"] = show_s
        result["panel"] = shown.get("panel")
        result["src"] = src
        result["source_selection"] = args.display if args.mode == "display" else args.mode
        result["source_window"] = {k: w[k] for k in ("w", "h")} if chrome else None
        if args.mode == "display" and chrome:
            result["source_display"] = {k: display.get(k) for k in ("name", "w", "h", "pixelW", "pixelH", "x", "y", "main")}
        result["route"] = mv.route
        return result
    finally:
        cleanup_error = None
        try:
            mv.stop(src)
            mv.finish_browser_cleanup()
        except Exception as e:  # noqa: BLE001 - still release local resources
            cleanup_error = e
        mv.closing = True  # or its supervisor reopens the tunnel
        if mv.tunnel and mv.tunnel.poll() is None:
            mv.tunnel.terminate()
            mv.tunnel.wait(5)
        if relay:
            relay.loop.call_soon_threadsafe(relay.loop.stop)
        if chrome:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=5)
        if chrome_profile:
            chrome_profile.cleanup()
        if cleanup_error:
            raise frame_macview.MacViewError(f"Benchmark cleanup failed: {cleanup_error}") from cleanup_error


def composited(f):
    """When the Mac had the frame: its display time, or when ScreenCaptureKit
    handed it over if that was earlier (display time can be a few ms ahead of
    delivery, which would make latency look lower than it is)."""
    return min(f["cap"], f["arr"]) if f["arr"] else f["cap"]


def summarize(scenario, data, start_us, end_us, args, relay, schedule, expected=0):
    frames = [f for f in data["frames"] if start_us <= f["cap"] <= end_us]
    inputs = [i for i in data["inputs"] if i["tv"] and i["tv"] >= start_us]
    by_seq = {f["s"]: f for f in data["frames"]}
    drawn = [f for f in frames if f["drw"]]
    shown = [f for f in frames if f["vs"]]
    # The whole measured interval, so a stall at either end still counts.
    duration = (end_us - start_us) / 1e6
    stages = {
        "capture": [ms(f["cap"], f["arr"]) for f in frames],
        "queue": [ms(f["arr"], f["e0"]) for f in frames],
        "encode": [ms(f["e0"], f["e1"]) for f in frames],
        "socket": [ms(f["snd"], f["wire"]) for f in frames],
        "network": [ms(f["e1"], f["rx"]) for f in frames],
        "decode": [ms(f["rx"], f["dec"]) for f in frames],
        "draw": [ms(f["dec"], f["drw"]) for f in frames],
        # Waiting for the browser's next frame: set by the Frame's compositor,
        # which throttles panels nobody is looking at (see "Measuring").
        "present": [ms(f["drw"], f["vs"]) for f in frames],
        "content": [ms(composited(f), f["drw"]) for f in frames],
        "content_shown": [ms(composited(f), f["vs"]) for f in frames],
    }
    out = {"scenario": scenario, "frames_sent": len(frames), "frames_drawn": len(drawn),
           "frames_shown": len(shown), "duration_s": round(duration, 1)}
    out["stages_ms"] = {k: dist(v) for k, v in stages.items()}
    # Smoothness: gaps between frames reaching the viewer's canvas.
    # Smoothness counts only what was drawn inside the interval; a frame
    # captured just before the end but drawn after it still counts for latency.
    dr = [start_us] + sorted(f["drw"] for f in drawn if f["drw"] <= end_us) + [end_us]
    gaps = [(b - a) / 1000 for a, b in zip(dr, dr[1:]) if b > a]
    target_fps = frame_macview.QUALITY[args.quality]["fps"]
    out["fps"] = round(sum(f["drw"] <= end_us for f in drawn) / duration, 1) if duration else 0
    out["fps_shown"] = round(sum(0 < f["vs"] <= end_us for f in shown) / duration, 1) if duration else 0
    out["late_pct"] = round(100 * sum(g > 1.5 * 1000 / target_fps for g in gaps) / len(gaps), 2) if gaps else None
    out["stall_max"] = round(max(gaps), 1) if gaps else None
    out["stalls_over_100ms"] = sum(g > 100 for g in gaps)
    out["mbps"] = round(sum(f["b"] for f in frames) * 8 / duration / 1e6, 2) if duration else 0
    out["keyframes"] = sum(f["k"] for f in frames)
    out["size"] = f"{frames[-1]['w']}x{frames[-1]['h']}" if frames else ""
    out["captured"] = data.get("captured")
    out["viewer_never_drawn"] = len(frames) - len(drawn)
    # Input: the viewer's event -> the first frame after it drawn (and shown).
    lat, lat_shown = [], []
    parts = {"uplink": [], "mac": [], "back": []}
    for i in inputs:
        f = by_seq.get(i["frame"])
        if f and f["drw"]:
            lat.append(ms(i["tv"], f["drw"]))
            lat_shown.append(ms(i["tv"], f["vs"]))
            parts["uplink"].append(ms(i["tv"], i["inj"]))
            parts["mac"].append(ms(i["inj"], composited(f)))
            parts["back"].append(ms(composited(f), f["drw"]))
    out["input_ms"] = dist(lat)
    out["input_shown_ms"] = dist(lat_shown)
    out["input_parts_ms"] = {k: dist(v) for k, v in parts.items()}
    out["inputs"] = {"asked": expected, "sent": len(inputs), "seen": len(lat)}
    # A per-second timeline, for adaptation.
    timeline = []
    t0 = start_us
    for sec in range(int(duration) + 1):
        a, b = t0 + sec * 1_000_000, t0 + (sec + 1) * 1_000_000
        fs = [f for f in frames if a <= f["cap"] < b]
        if not fs:
            continue
        timeline.append({
            "t": sec, "fps": sum(1 for f in fs if f["drw"]), "mbps": round(sum(f["b"] for f in fs) * 8 / 1e6, 2),
            "content_p50": pct([ms(composited(f), f["drw"]) for f in fs], 0.5),
            "content_p95": pct([ms(composited(f), f["drw"]) for f in fs], 0.95),
            "bitrate": fs[-1].get("br"), "tier": fs[-1].get("tier"), "w": fs[-1]["w"],
            "net": relay.rate_at(sec) if relay else None,
        })
    out["timeline"] = timeline
    out["adapt"] = adaptation(timeline, schedule, duration)
    out["content_p50"] = out["stages_ms"]["content"].get("p50")
    out["content_p95"] = out["stages_ms"]["content"].get("p95")
    out["input_p50"] = out["input_ms"].get("p50")
    out["input_p95"] = out["input_ms"].get("p95")
    out["grades"] = grade(out, scenario, args.quality)
    return out


def adaptation(timeline, schedule, duration=float("inf")):
    """After each drop in the link's rate: how long until latency was within
    bounds again and stayed there for 3 s. The bound is the acceptable p95
    (100 ms), or 1.5 times the p95 before the drop if that was higher: on a
    slower link each frame takes longer to send, so latency can't return to
    what it was on the fast one."""
    steps = []
    for n, ((at, rate), (_, before)) in enumerate(zip(schedule[1:], schedule)):
        # Settling must happen while this rate lasts, not after the link recovers.
        until = min(schedule[n + 2][0] if n + 2 < len(schedule) else float("inf"), duration)  # or the run ends
        if before is not None and (rate is None or rate >= before):
            continue
        pre = [s["content_p95"] for s in timeline if at - 4 <= s["t"] < at and s["content_p95"]]
        base = max(float(TARGETS["content_p95"][1]), 1.5 * statistics.median(pre) if pre else 0)
        after = [s for s in timeline if s["t"] >= at]
        worst = max([s["content_p95"] or 0 for s in after[:10]] or [0])
        settled = None
        for i, s in enumerate(after):
            window = after[i:i + 3]
            # Three populated seconds in a row: a second with no frames at all
            # is a stall, not a pass.
            if (len(window) == 3 and [w["t"] for w in window] == [s["t"], s["t"] + 1, s["t"] + 2] and s["t"] + 3 <= until
                    and all((w["content_p95"] or 1e9) <= base for w in window)):
                settled = s["t"] - at
                break
        steps.append({"at": at, "to_mbit": rate, "p95_bound": round(base, 1), "worst_p95": worst,
                      "settled_s": settled})
    return steps


def grade(out, scenario, quality="balanced"):
    g = {}
    checks = ["content_p50", "content_p95"]
    asked = max(out["inputs"].get("asked", 0), out["inputs"]["sent"])
    if asked:
        checks += ["input_p50", "input_p95"]
        # Input that never reaches the Mac, or never shows up on screen, is a
        # failure, not a gap in the data.
        seen = out["inputs"]["seen"] / asked
        g["input_replies"] = "local" if seen >= 0.95 else "acceptable" if seen >= 0.8 else "bad"
    if scenario in MOVING:
        checks += ["fps", "late_pct", "stall_max"]
    for k in checks:
        v = out.get(k)
        if v is None:
            g[k] = "missing"
            continue
        good, ok = TARGETS[k]
        if k == "fps":  # the targets are for 60 fps; Light and Compatible ask for fewer
            want = frame_macview.QUALITY[quality]["fps"]
            good, ok = good * want / 60, ok * want / 60
        if k in HIGHER_IS_BETTER:
            g[k] = "local" if v >= good else "acceptable" if v >= ok else "bad"
        else:
            g[k] = "local" if v <= good else "acceptable" if v <= ok else "bad"
    for step in out["adapt"]:
        s = step["settled_s"]
        good, ok = TARGETS["adapt_s"]
        g[f"adapt@{step['at']:g}s"] = "bad" if s is None or s > ok else "local" if s <= good else "acceptable"
    return g


# ---- compare ----

KEYS = [("content_p50", "content p50 ms"), ("content_p95", "content p95 ms"), ("input_p50", "input p50 ms"),
        ("input_p95", "input p95 ms"), ("fps", "fps"), ("late_pct", "late %"), ("stall_max", "max gap ms"),
        ("fps_shown", "fps shown"), ("mbps", "Mbit/s")]
STAGE_KEYS = ["capture", "queue", "encode", "socket", "network", "decode", "draw", "present"]


def load(path):
    return json.loads(Path(path).read_text())


def compare(a, b, threshold=0.10):
    """Prints before/after per scenario; returns the regressions."""
    regressions = []
    print(f"A: {a['label']} ({a['commit']}, {a['date']})\nB: {b['label']} ({b['commit']}, {b['date']})")
    for sc in b["scenarios"]:
        ra = next((x for x in a["scenarios"] if x["scenario"] == sc["scenario"]), None)
        if not ra:
            continue
        print(f"\n{sc['scenario']:<10} {'A':>10} {'B':>10} {'change':>9}")
        rows = [(k, n, ra.get(k), sc.get(k)) for k, n in KEYS]
        rows += [(f"stage.{s}", f"  {s} p50", ra["stages_ms"].get(s, {}).get("p50"), sc["stages_ms"].get(s, {}).get("p50"))
                 for s in STAGE_KEYS]
        for k, name, va, vb in rows:
            change = ""
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va:
                d = (vb - va) / abs(va)
                change = f"{d:+.0%}"
                worse = d < -threshold if k == "fps" else d > threshold
                if worse and k in ("content_p50", "content_p95", "input_p50", "input_p95", "fps") and abs(vb - va) > 2:
                    regressions.append(f"{sc['scenario']} {name}: {va} -> {vb}")
                    change += " worse"
            print(f"{name:<18} {fmt(va):>10} {fmt(vb):>10} {change:>9}")
    return regressions


def fmt(v):
    return "–" if v is None else f"{v:g}" if isinstance(v, (int, float)) else str(v)


# ---- main ----

def add_run_args(r):
    r.add_argument("--scenario", default="test,scroll,type")
    r.add_argument("--duration", type=float, default=20)
    r.add_argument("--warmup", type=float, default=3)
    r.add_argument("--quality", default="balanced", choices=list(frame_macview.QUALITY))
    r.add_argument("--mode", default="separate", choices=["separate", "window", "display"], help="how Mac content is shown")
    r.add_argument("--display", default="main", choices=["main", "workspace"],
                   help="fresh display selection when --mode display is used")
    r.add_argument("--net", default="", help="Mbit/s, or a schedule like 50@0,8@10,50@20")
    r.add_argument("--delay", type=float, default=0, help="extra one-way delay, ms")
    r.add_argument("--buffer", type=float, default=250, help="bottleneck queue, ms at the current rate")
    r.add_argument("--frame", default="frame", help="ssh host of the Frame")
    r.add_argument("--host", default="", help="connect to this address instead (e.g. the Frame's LAN IP)")
    r.add_argument("--usb", action="store_true", help="use the Frame's USB-C network when plugged in, as Frame Control does")
    r.add_argument("--ssh-opt", action="append", default=[], help="extra ssh option for the tunnel, e.g. -oIPQoS=ef")
    r.add_argument("--browser-flag", action="append", default=None,
                   help="Chromium flag for the viewer (replaces Frame Control's defaults; '' for none)")
    r.add_argument("--label", default="")
    r.add_argument("--out", default="")
    r.add_argument("--raw", default="", help="also write the agent's raw records here")


def lab_agent():
    try:
        agent_port = int((LAB_DIR / "port").read_text())
        token = (LAB_DIR / "token").read_text().strip()
        urllib.request.urlopen(f"http://127.0.0.1:{agent_port}/ping", timeout=3)
        return agent_port, token
    except (OSError, ValueError):
        raise SystemExit("Start the lab agent first: mac/frame-mac-view/lab.sh serve")


def restart_lab(env):
    """The lab agent again, without rebuilding, with these experiment switches."""
    subprocess.run(["sh", str(ROOT / "mac" / "frame-mac-view" / "lab.sh"), "serve-only"],
                   env={**os.environ, **env}, check=True, capture_output=True)
    time.sleep(1)


def frame_ssh_for(args):
    frame_ssh = ["ssh", "-o", "BatchMode=yes"]
    if args.host:
        # Another address for the same Frame: its host key must still match.
        alias = next((line.split()[1] for line in subprocess.run(["ssh", "-G", args.frame], capture_output=True,
                      text=True).stdout.splitlines() if line.startswith("hostname ")), args.frame)
        frame_ssh += ["-o", f"HostName={args.host}", "-o", f"HostKeyAlias={alias}"]
    frame_ssh.append(args.frame)
    return frame_ssh


def run_suite(args, frame_ssh, quiet=False):
    # A second run would close/relaunch the first run's viewer and corrupt both
    # measurements. Keep the lease open until all scenario cleanup finishes.
    if fcntl is None:
        raise SystemExit("Running the MacView benchmark requires macOS; compare can run on other platforms.")
    with open(Path(tempfile.gettempdir()) / f"frame-macview-bench-{os.getuid()}.lock", "a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another MacView benchmark is active; wait for its cleanup before starting.")
        return _run_suite(args, frame_ssh, quiet)


def _run_suite(args, frame_ssh, quiet=False):
    agent_port, token = lab_agent()
    results = []
    for sc in args.scenario.split(","):
        if not quiet:
            print(f"-- {sc} ...", flush=True)
        res = run_scenario(args, sc, agent_port, token, frame_ssh)
        if not quiet:
            print(f"   content p50/p95 {res['content_p50']}/{res['content_p95']} ms, input p50/p95 "
                  f"{res['input_p50']}/{res['input_p95']} ms, {res['fps']} fps (shown {res['fps_shown']}), "
                  f"late {res['late_pct']}%, max gap {res['stall_max']} ms, {res['mbps']} Mbit/s, {res['size']}",
                  flush=True)
            print(f"   stages p50: " + ", ".join(f"{k} {v.get('p50')}" for k, v in res["stages_ms"].items()), flush=True)
            print(f"   grades: {res['grades']}  cpu: {res['cpu']}\n   viewer: {res['viewer']}", flush=True)
            for e in res["events"][:12]:
                print(f"   {e['t']:6.2f}s {e['e']}", flush=True)
        results.append(res)
    return results


def document(args, frame_ssh, results, **extra):
    build = subprocess.run([*frame_ssh, "grep -m1 BUILD_ID /etc/os-release"], capture_output=True, text=True).stdout
    # Whether the headset is in standby (it throttles panels even harder then).
    standby = subprocess.run([*frame_ssh, "grep -h standby ~/.local/share/Steam/logs/vrserver.txt | tail -n 1"],
                             capture_output=True, text=True).stdout.strip()
    commit = git("rev-parse", "--short", "HEAD") + ("+dirty" if git("status", "--porcelain", "--", "mac", "ui", "scripts/macview-bench.py") else "")
    return {
        "label": args.label or args.cmd, "date": datetime.datetime.now().isoformat(timespec="seconds"), "commit": commit,
        "config": {"quality": args.quality, "mode": args.mode, "display": args.display, "net": args.net or "none", "delay_ms": args.delay,
                   "buffer_ms": args.buffer, "host": args.host or args.frame, "usb": args.usb, "ssh_opts": args.ssh_opt,
                   "encoder": os.environ.get("FRAME_MAC_VIEW_ENCODER", ""), "duration_s": args.duration,
                   "browser_flags": args.browser_flag if args.browser_flag is not None else frame_macview.BROWSER_FLAGS},
        "frame_build": build.strip().partition("=")[2], "mac": platform.mac_ver()[0], "headset": standby[-40:],
        "scenarios": results, **extra,
    }


def write(doc, args):
    RESULTS.mkdir(parents=True, exist_ok=True)
    name = f"{doc['date'][:10]}-{doc['commit'].replace('+', '-')}-{re.sub(r'[^a-zA-Z0-9_.-]+', '-', doc['label'])}.json"
    out = Path(args.out) if args.out else RESULTS / name
    out.write_text(json.dumps(doc, indent=1))
    print(f"wrote {out}")
    return out


AB_KEYS = ["content_p50", "content_p95", "input_p50", "input_p95", "fps", "late_pct", "stall_max", "mbps"]


def ab(args, frame_ssh):
    """Arms interleaved (A B, B A, ...) so a Wi-Fi link that changes over
    minutes affects both alike; medians and ranges per arm."""
    arms = []
    for a in args.arm:
        name, _, envs = a.partition(":")
        arms.append((name, dict(e.split("=", 1) for e in envs.split(",") if e)))
    runs = {name: [] for name, _ in arms}
    for rep in range(args.repeat):
        for name, env in (arms if rep % 2 == 0 else arms[::-1]):
            print(f"-- repeat {rep + 1}, arm {name} {env}", flush=True)
            restart_lab(env)
            runs[name].append(run_suite(args, frame_ssh, quiet=True))
    table = {}
    for sc in args.scenario.split(","):
        print(f"\n{sc:<14}" + "".join(f"{name:>26}" for name, _ in arms))
        for k in AB_KEYS:
            row = []
            for name, _ in arms:
                vals = [r[k] for suite in runs[name] for r in suite if r["scenario"] == sc and r.get(k) is not None]
                table.setdefault(sc, {}).setdefault(k, {})[name] = vals
                row.append(f"{statistics.median(vals):g} ({min(vals):g}-{max(vals):g})" if vals else "–")
            print(f"{k:<14}" + "".join(f"{c:>26}" for c in row))
    restart_lab({})  # back to the defaults
    return runs, table


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    add_run_args(r)
    r.add_argument("--baseline", default="", help="fail on >10%% regressions against this result")
    a = sub.add_parser("ab", help="compare lab-agent settings, interleaved: --arm name:ENV=V,ENV2=V ...")
    add_run_args(a)
    a.add_argument("--arm", action="append", required=True)
    a.add_argument("--repeat", type=int, default=3)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    args = p.parse_args()
    # The agent keeps the last 4096 frames (Stats.swift) and results are read
    # at the end: at 60 fps that's 68 s, less warm-up and the reporting wait.
    if args.cmd != "compare" and args.host and args.usb:
        p.error("--host and --usb pick the path two ways; use one")
    if args.cmd != "compare" and args.warmup + args.duration > 60:
        p.error("--warmup plus --duration can be at most 60 s (the agent keeps 4096 frames)")

    if args.cmd == "compare":
        regs = compare(load(args.a), load(args.b))
        if regs:
            print("\nworse:\n  " + "\n  ".join(regs))
        sys.exit(1 if regs else 0)

    lab_agent()
    frame_ssh = frame_ssh_for(args)
    # Keep the Mac's screen awake: virtual displays aren't removed while it sleeps.
    runs = 1 if args.cmd == "run" else args.repeat * len(args.arm)
    awake = subprocess.Popen(["caffeinate", "-d", "-u", "-t", str(int(runs * (args.duration * 4 + 60) + 120))])
    try:
        if args.cmd == "ab":
            arm_runs, table = ab(args, frame_ssh)
            write(document(args, frame_ssh, [], arms=args.arm, runs=arm_runs, table=table), args)
            return
        results = run_suite(args, frame_ssh)
    finally:
        awake.terminate()
    doc = document(args, frame_ssh, results)
    write(doc, args)
    bad = [f"{r['scenario']} {k}" for r in results for k, v in r["grades"].items() if v in ("bad", "missing")]
    regs = compare(load(args.baseline), doc) if args.baseline else []
    if bad:
        print("missed targets: " + ", ".join(bad))
    if regs:
        print("worse than baseline:\n  " + "\n  ".join(regs))
    sys.exit(1 if bad or regs else 0)


if __name__ == "__main__":
    main()
