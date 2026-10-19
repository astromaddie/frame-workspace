#!/usr/bin/env python3
"""Real WS -> decoder -> upload/glFinish with a test-only 300 ms stall.

Pass a native binary built with -DglFinish=fwTestGlFinish and linked with
tests_gaze_render_stall.cpp. Uses synthetic diagnostic gaze only. No device.
All fixtures, markers and logs are temporary; output contains aggregates only.
The first upload warms up the host GL driver before clock negotiation; the next
three uploads are measured, identically for baseline and candidate binaries.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("binary", type=Path)
p.add_argument("--use-display", action="store_true")
a = p.parse_args()

def receive(c):
    def exact(n):
        result = b""
        while len(result) < n:
            chunk = c.recv(n-len(result))
            if not chunk:
                raise EOFError()
            result += chunk
        return result
    op, length = exact(2)
    assert length & 128
    length &= 127
    if length == 126:
        length = struct.unpack("!H", exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(8))[0]
    mask, raw = exact(4), exact(length)
    assert op & 15 == 1
    return json.loads(bytes(v ^ mask[i % 4] for i, v in enumerate(raw)))

def send(c, value):
    op = 2 if isinstance(value, bytes) else 1
    raw = value if op == 2 else json.dumps(value).encode()
    n = len(raw)
    c.sendall(bytes([0x80 | op, n if n < 126 else 126]) +
              (b"" if n < 126 else struct.pack("!H", n)) + raw)

encoded = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
    "color=c=red:s=320x180:r=30", "-frames:v", "1", "-c:v", "libx264",
    "-preset", "ultrafast", "-tune", "zerolatency", "-f", "h264", "pipe:1"])
with tempfile.TemporaryDirectory(prefix="fw-gaze-render-") as folder:
    folder = Path(folder)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen(); listener.settimeout(10)
    (folder / "url").write_text(f"http://127.0.0.1:{listener.getsockname()[1]}/view?src=test&t=test")
    errors, observations = [], []
    def server():
        try:
            with listener.accept()[0] as c:
                c.settimeout(5)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += c.recv(1)
                key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                           if line.lower().startswith(b"sec-websocket-key:"))
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                send(c, {"t": "hello", "r": "test", "foveation": 1, "direct": 1,
                         "gazeSession": "render-stall"})
                send_lock = threading.Lock()
                warmup_ready, gaze_ready = threading.Event(), threading.Event()
                def send_locked(value):
                    with send_lock:
                        send(c, value)
                def read_backchannel():
                    # Receive independently of fixture marker IO and sending.
                    previous = 0
                    try:
                        while True:
                            m = receive(c)
                            arrival = time.monotonic() * 1000
                            if m["t"] == "ping":
                                # Ignore pings until the first real GL upload has
                                # initialized the driver. A fresh subsequent ping
                                # establishes the measured phase's clock.
                                if warmup_ready.is_set():
                                    send_locked({"t": "pong", "c": m["c"], "a": arrival * 1000})
                            elif m["t"] == "gaze":
                                assert m["session"] == "render-stall"
                                if not m["valid"]:
                                    continue
                                assert m["source"] == "diagnostic"
                                assert m["sampleTime"] > previous
                                previous = m["sampleTime"]
                                observations.append((arrival, m["sampleTime"] / 1000, arrival - m["time"] / 1000))
                                gaze_ready.set()
                    except EOFError:
                        pass
                    except Exception as error:
                        errors.append(repr(error))
                reader = threading.Thread(target=read_backchannel)
                reader.start()
                seq = 0
                began = time.monotonic()
                while time.monotonic() - began < 5:
                    marker = folder / "stalls"
                    completed = len(marker.read_text().splitlines()) if marker.exists() else 0
                    if completed:
                        warmup_ready.set()
                    if seq < 4 and completed == seq and (seq == 0 or gaze_ready.is_set()):
                        seq += 1
                        layout = bytes([1, 3, 0, 0]) + struct.pack("!8H", 320, 180, 320, 180, 80, 44, 160, 90)
                        send_locked(bytes([0x81]) + struct.pack("!QII", 0, seq, 0) + layout + encoded)
                    time.sleep(.003)
                send_locked({"t": "close"})
                reader.join(3)
                assert not reader.is_alive()
        except Exception as error:
            errors.append(repr(error))
        finally:
            listener.close()
    server_thread = threading.Thread(target=server)
    server_thread.start()
    command = [str(a.binary.resolve()), "--no-vr", "--foveation", "--diagnostic-gaze", "scan",
               "--url-file", str(folder / "url"), "--seconds", "7"]
    if not a.use_display:
        command = ["xvfb-run", "-a", *command]
    env = dict(os.environ, FW_TEST_STALL_MARKERS=str(folder / "stalls"))
    run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    server_thread.join(5)
    assert not errors and not server_thread.is_alive(), (errors, run.stderr)
    assert run.returncode == 0 and "uploaded=4" in run.stderr, run.stderr
    stalls = [tuple(map(float, line.split())) for line in (folder / "stalls").read_text().splitlines()]
    assert len(stalls) == 4, stalls
    stalls = stalls[1:] # same one-upload GL warmup for baseline and candidate
    counts, max_gaps = [], []
    for begin, end in stalls:
        assert end - begin >= 300
        during = [arrival for arrival, sample, age in observations if begin <= sample <= arrival <= end]
        # Both sample acquisition and actual socket receipt must occur while
        # glFinish is blocked. Include the boundaries in the freshness check.
        assert len(during) >= 8, len(during)
        points = [begin, *during, end]
        gap = max(y-x for x, y in zip(points, points[1:]))
        assert gap < 100, gap
        counts.append(len(during)); max_gaps.append(round(gap, 2))
    age_range = [round(min(age for _, _, age in observations), 2),
                 round(max(age for _, _, age in observations), 2)]
    assert all(-50 <= age <= 150 for _, _, age in observations), {
        "hostAgeRangeMs": age_range,
        "warmupUploads": 1,
        "maxLocalSampleAgeMs": round(max(arrival-sample for arrival, sample, _ in observations), 2),
        "staleBeforeFirstStall": sum(age > 150 and arrival < stalls[0][0] for arrival, _, age in observations),
        "staleDuringStalls": sum(age > 150 and any(begin <= arrival <= end for begin, end in stalls)
                                 for arrival, _, age in observations),
        "staleAfterLastStall": sum(age > 150 and arrival > stalls[-1][1] for arrival, _, age in observations)}
    print(json.dumps({"result": "PASS", "path": "production WS/decode/GL upload/gaze sender",
        "renderStallsMs": [round(end-begin, 2) for begin, end in stalls],
        "freshMessagesDuringStall": counts, "maxGapIncludingStallBoundariesMs": max_gaps,
        "hostAgeRangeMs": age_range,
        "warmupUploads": 1,
        "syntheticGazeOnly": True, "OpenVR": False}))
