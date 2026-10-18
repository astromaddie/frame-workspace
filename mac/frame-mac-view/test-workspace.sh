#!/bin/sh
# Parser checks never create a display. Termination and full display lifecycle
# checks are opt-in because they attach a virtual monitor to the Mac.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
binary=${1:-$here/../bin/frame-mac-view}
FRAME_MAC_VIEW_BIN="$binary" python3 - <<'PY'
import json
import os
import signal
import select
import time
import subprocess
import sys
import fcntl

binary = os.environ["FRAME_MAC_VIEW_BIN"]

def call(*args):
    return subprocess.run([binary, *args], text=True, capture_output=True, timeout=30)

def displays():
    result = call("displays")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["displays"]

def canonical(rows):
    return sorted((r["id"], r["name"], r["x"], r["y"], r["w"], r["h"], r["pixelW"], r["pixelH"], r["main"], r["mirrored"], r["mirrorDisplayID"])
                  for r in rows if r["name"] != "Frame Workspace")

class JSONEvents:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def next(self):
        deadline = time.monotonic() + 30
        while True:
            stripped = self.buffer.lstrip()
            if stripped:
                try:
                    event, end = self.decoder.raw_decode(stripped)
                    leading = len(self.buffer) - len(stripped)
                    self.buffer = self.buffer[leading + end:]
                    return event
                except json.JSONDecodeError:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.stream], [], [], max(0, remaining))[0]:
                raise AssertionError("timed out waiting for workspace JSON event")
            line = os.read(self.stream.fileno(), 65536).decode("utf-8")
            if not line:
                raise AssertionError("workspace exited before emitting its next JSON event")
            self.buffer += line

def cleanup(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()

for args in (
    ("workspace", "--width", "1279"),
    ("workspace", "--scale", "3"),
    ("workspace", "--duration", "nan"),
    ("workspace", "--width", "7680", "--height", "4320", "--scale", "2"),
    ("workspace", "--recover-orphan", "--capture-probe"),
    ("workspace", "--unknown"),
):
    result = call(*args)
    assert result.returncode == 2, (args, result.returncode, result.stdout, result.stderr)
    event = json.loads(result.stdout)
    assert event["state"] == "error" and event.get("error"), (args, event)
print("PASS workspace argument validation (no display mutation)")

if os.environ.get("FRAME_MAC_VIEW_RUN_TERMINATION_TESTS") == "1":
    lock_path = f"/tmp/frame-workspace-{os.getuid()}.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_acquired = False
    baseline = []
    run_owner_tests = False
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except BlockingIOError:
            print("SKIP termination regression; another Workspace owner holds the lease")
        if lock_acquired:
            baseline = displays()
            if any(row["name"] == "Frame Workspace" for row in baseline):
                print("SKIP termination regression; a Workspace display is already registered")
            else:
                run_owner_tests = True
    finally:
        if lock_acquired:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    if run_owner_tests:
        owner = subprocess.Popen([binary, "workspace", "--duration", "60"], text=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 bufsize=1)
        try:
            owner_events = JSONEvents(owner.stdout)
            ready = owner_events.next()
            assert ready.get("state") == "ready" and ready.get("main") is False and ready.get("mirrored") is False, ready
            active = displays()
            assert any(row["id"] == ready["displayID"] and row["name"] == "Frame Workspace" for row in active), active

            started = time.monotonic()
            rejected = subprocess.run([binary, "workspace", "--duration", "1"], text=True,
                                      capture_output=True, timeout=4)
            elapsed = time.monotonic() - started
            events = [json.loads(line) for line in rejected.stdout.splitlines() if line.strip()]
            assert rejected.returncode == 1, (rejected.returncode, rejected.stdout, rejected.stderr)
            assert len(events) == 1 and events[0].get("state") == "error", events
            assert "live Frame Workspace owner" in events[0].get("error", ""), events
            assert elapsed < 4, elapsed
            assert any(row["id"] == ready["displayID"] and row["name"] == "Frame Workspace" for row in displays())
            print(f"PASS live-owner rejection exits promptly and leaves its display untouched ({elapsed:.2f}s)")

            owner.send_signal(signal.SIGTERM)
            stopped = owner_events.next()
            assert stopped.get("state") == "stopped" and stopped.get("removed") is True, stopped
            rest, stderr = owner.communicate(timeout=10)
            assert owner.returncode == 0, (owner.returncode, rest, stderr)
            assert canonical(displays()) == canonical(baseline), (baseline, displays())
            print(f"PASS owner SIGTERM released its display ({ready['displayID']})")
        finally:
            cleanup(owner)

        started = time.monotonic()
        recovered = subprocess.run([binary, "workspace", "--recover-orphan"], text=True,
                                    capture_output=True, timeout=8)
        elapsed = time.monotonic() - started
        events = [json.loads(line) for line in recovered.stdout.splitlines() if line.strip()]
        assert recovered.returncode == 0, (recovered.returncode, recovered.stdout, recovered.stderr)
        assert len(events) == 1 and events[0].get("state") == "stopped" and events[0].get("removed") is True, events
        assert events[0].get("recoveredOrphan") is False, events
        assert elapsed < 8, elapsed
        assert canonical(displays()) == canonical(baseline), (baseline, displays())
        print(f"PASS no-op orphan recovery exits promptly without a display ({elapsed:.2f}s)")

if os.environ.get("FRAME_MAC_VIEW_RUN_DISPLAY_TESTS") != "1":
    print("SKIP desktop lifecycle tests; set FRAME_MAC_VIEW_RUN_DISPLAY_TESTS=1 to opt in")
    sys.exit(0)

baseline = displays()
for width, height, scale in ((5120, 1440, 1), (5120, 2160, 1), (5120, 1440, 1), (5120, 2160, 1), (2560, 1440, 2)):
    process = subprocess.Popen(
        [binary, "workspace", "--width", str(width), "--height", str(height), "--scale", str(scale),
         "--duration", "1.5", "--capture-probe"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
    )
    try:
        events = JSONEvents(process.stdout)
        ready = events.next()
        assert ready.get("state") == "ready", ready
        actual = ready["actual"]
        assert (actual["logicalWidth"], actual["logicalHeight"]) == (width, height), ready
        assert (actual["bufferWidth"], actual["bufferHeight"]) == (width * scale, height * scale), ready
        assert ready["captureProbe"] is True, ready
        assert ready["main"] is False and ready["mirrored"] is False and ready["mirrorDisplayID"] is None, ready
        assert ready["physicalLayoutUnchanged"] is True, ready
        bounds = ready["bounds"]
        assert bounds["width"] == width and bounds["height"] == height, ready
        for screen in baseline:
            no_area_overlap = (bounds["x"] + bounds["width"] <= screen["x"] or
                               screen["x"] + screen["w"] <= bounds["x"] or
                               bounds["y"] + bounds["height"] <= screen["y"] or
                               screen["y"] + screen["h"] <= bounds["y"])
            assert no_area_overlap, (ready, screen)
        active = displays()
        assert any(row["id"] == ready["displayID"] and row["name"] == "Frame Workspace" for row in active), active
        stopped = events.next()
        assert stopped.get("state") == "stopped" and stopped.get("removed") is True, stopped
        rest, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (process.returncode, rest, stderr)
        assert canonical(displays()) == canonical(baseline), (baseline, displays())
        print(f"PASS {width}x{height}@{scale}x: captured, discoverable, stopped and removed ({ready['displayID']})")
    finally:
        cleanup(process)

def lifecycle_stop(reason, signal_number=None, use_eof=False):
    process = subprocess.Popen(
        [binary, "workspace", "--capture-probe"] + (["--exit-on-stdin-eof"] if use_eof else []),
        text=True, stdin=subprocess.PIPE if use_eof else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
    )
    try:
        events = JSONEvents(process.stdout)
        ready = events.next()
        assert ready.get("state") == "ready" and ready.get("captureProbe") is True, ready
        if use_eof:
            process.stdin.close()
            process.stdin = None
        else:
            process.send_signal(signal_number)
        stopped = events.next()
        assert stopped.get("state") == "stopped" and stopped.get("reason") == reason and stopped.get("removed") is True, stopped
        rest, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (process.returncode, rest, stderr)
        assert canonical(displays()) == canonical(baseline), (baseline, displays())
        print(f"PASS {reason}: capture stopped and display removed ({ready['displayID']})")
    finally:
        cleanup(process)

lifecycle_stop("SIGINT", signal_number=signal.SIGINT)
lifecycle_stop("SIGTERM", signal_number=signal.SIGTERM)
lifecycle_stop("stdin_eof", use_eof=True)
PY
