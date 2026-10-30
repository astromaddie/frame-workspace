#!/usr/bin/env python3
"""Fixture-only acceptance through the real helper's HTTP/WebSocket paths."""
import argparse
import http.client
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("shared_agent_harness", ROOT / "tests/test_spatial_shared_agent.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def wait_for(read, predicate=lambda value: bool(value), timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last):
            return last
        time.sleep(.05)
    raise AssertionError(f"Timed out waiting for fixture state: {last}")


class Stream:
    def __init__(self, port, token, source):
        self.ws = harness.WS(port, "/stream?" + urlencode(dict(k=token, src=source, codec="h264", max=1600, fps=60)))
        assert self.ws.status == 101, (source, self.ws.status)
        self.ws.s.settimeout(.3)
        self.frames = []
        self.messages = []
        self.errors = []
        self.active = True
        self.thread = threading.Thread(target=self.receive, daemon=True)
        self.thread.start()

    def receive(self):
        import socket
        while self.active:
            try:
                op, data = self.ws.recv()
            except socket.timeout:
                continue
            except (EOFError, OSError):
                return
            if op == 2:
                self.frames.append(time.monotonic())
            elif op == 1:
                value = json.loads(data)
                self.messages.append(value)
                if value.get("t") == "hello":
                    self.send(dict(t="ack"))
                if value.get("t") == "error":
                    self.errors.append(value)

    def send(self, value):
        self.ws.send_text(json.dumps(value))

    def click(self, u, v):
        self.send(dict(t="m", e="move", x=u, y=v, b=0))
        time.sleep(.12)
        self.send(dict(t="m", e="down", x=u, y=v, b=0))
        time.sleep(.08)
        self.send(dict(t="m", e="up", x=u, y=v, b=0))

    def close(self):
        self.active = False
        self.ws.close()
        self.thread.join(timeout=2)


def run(options):
    helper, fixture, streams = None, None, []
    report = dict(verified={}, unverified=[], errors=[])
    fixture_state = Path(options.work) / "fixture-state.json"
    token = os.urandom(24).hex()
    port = None
    before = json.loads(subprocess.check_output([options.fixture, "--inventory"], text=True))
    original_cursor = before["cursor"]

    def state():
        try:
            return json.loads(fixture_state.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def command(name, **fields):
        fixture.stdin.write(json.dumps(dict(command=name, **fields)) + "\n")
        fixture.stdin.flush()

    def call(path, method="GET", **query):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        try:
            connection.request(method, path + "?" + urlencode(dict(k=token, **query)))
            response = connection.getresponse()
            data = response.read()
            try:
                body = json.loads(data)
            except json.JSONDecodeError:
                body = data.decode()
            return response.status, body
        finally:
            connection.close()

    try:
        fixture = subprocess.Popen([options.fixture, str(fixture_state), "--60hz"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        wait_for(state)
        command("count", value=3)
        initial = wait_for(state, lambda value: sum(w["visible"] for w in value.get("windows", [])) == 3)
        pid = initial["pid"]
        with open(Path(options.work) / "helper-stderr.txt", "w") as errors:
            helper = subprocess.Popen([options.helper, "serve", "--port", "0", "--exit-on-eof"],
                env={**os.environ, "FRAME_MAC_VIEW_TOKEN": token, "FRAME_SPATIAL_ONLY_PIDS": str(pid),
                     "FRAME_MAC_VIEW_ADAPT": "0", "FRAME_SPATIAL_DIAGNOSTICS": "1"}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=errors, text=True)
            assert select.select([helper.stdout], [], [], 15)[0], "Helper did not listen"
            port = int(helper.stdout.readline().rsplit(":", 1)[1])
            permissions = call("/status")[1]
            report["verified"]["permissions"] = {k: permissions.get(k) for k in ("screen", "accessibility")}
            if not permissions.get("screen") or not permissions.get("accessibility"):
                report["unverified"].append("Live run blocked by helper Screen Recording/Accessibility preflight; no prompt requested")
                return report
            code, spatial = call("/spatial/start", "POST", policy="legacy-all")
            assert code == 200, spatial
            assert len(spatial["windows"]) == 3, spatial
            assert {w["pid"] for w in spatial["windows"]} == {pid}, spatial
            report["verified"]["display"] = spatial["display"]
            target = min(spatial["windows"], key=lambda value: value["id"])
            source = f"spatial:{target['id']}"
            for key in (source, "menubar", "status", "menu:0"):
                streams.append(Stream(port, token, key))
            window, menubar, status, menu = streams
            wait_for(lambda: window.frames)
            # Raising the fixture activates its app menus before strip acceptance.
            window.send(dict(t="focus", u=.31, v=.12))
            for stream in (menubar, status):
                wait_for(lambda: stream.frames)
            # Window zero was placed first and is covered by its siblings.
            window.send(dict(t="focus", u=.31, v=.12))
            wait_for(lambda: call("/spatial")[1], lambda value: value["focusedSrc"] == source)
            window.click(.4, .5)
            window.send(dict(t="k", e="down", code="KeyA", key="a", mods=[]))
            window.send(dict(t="k", e="up", code="KeyA", key="a", mods=[]))
            events = wait_for(state, lambda value: any(e["kind"] == "key" for e in value.get("events", [])))["events"]
            target_index = next(w["index"] for w in initial["windows"] if w["id"] == target["id"])
            landed = [e for e in events if e["kind"] in ("down", "up", "key")]
            assert {e["index"] for e in landed} == {target_index}, landed
            report["verified"]["coveredWindowInput"] = landed
            # Try a physical pointer move into the shared wallpaper, beyond all windows.
            bounds = spatial["display"]["bounds"]
            command("cursor", point=[bounds[0] + 3000, bounds[1] + 1700])
            time.sleep(.4)
            point = state()["cursor"]
            report["fenceDiagnostics"] = call("/spatial")[1].get("cursorFence")
            report["fenceAttemptedCursor"] = point
            allowed = [[target["x"], target["y"], target["w"], target["h"]]]
            for strip in (menubar, status):
                allowed.append(next(m["rect"] for m in reversed(strip.messages) if m.get("t") == "surface" and m.get("visible")))
            assert any(x <= point[0] < x+w and y <= point[1] < y+h for x, y, w, h in allowed), point
            assert report["fenceDiagnostics"]["enabled"], report["fenceDiagnostics"]
            report["verified"]["fenceCursor"] = point
            time.sleep(1)
            start = time.monotonic()
            time.sleep(5)
            end = time.monotonic()
            frames = sum(start <= frame <= end for frame in window.frames)
            report["verified"]["focusedFrames"] = dict(frames=frames, seconds=end-start, fps=frames/(end-start), redrawHz=60, targetFps=60)
            command("menu-info")
            info = wait_for(state, lambda value: any(e["kind"] == "menu-info" for e in value.get("events", [])))["events"]
            probe = next(e for e in info if e["kind"] == "menu-info" and e["title"] == "Probe")
            bar = next(m for m in reversed(menubar.messages) if m.get("t") == "surface" and m.get("visible"))
            # AX may report the built-in's copy; translate its local menu x.
            built_in = next(d for d in before["displays"] if d["main"])
            x = probe["point"][0]
            local_x = x - bar["rect"][0] if bar["rect"][0] <= x < bar["rect"][0] + bounds[2] else x - built_in["bounds"][0]
            menubar.click((local_x + 15) / bar["rect"][2], .5)
            wait_for(lambda: menu.frames)
            surface = wait_for(lambda: [m for m in menu.messages if m.get("t") == "surface" and m.get("visible")])[-1]
            report["verified"]["menuSurface"] = surface
            window.send(dict(t="k", e="down", code="Escape", key="Escape", mods=[]))
            window.send(dict(t="k", e="up", code="Escape", key="Escape", mods=[]))
            for name, stream in zip((source, "menubar", "status", "menu:0"), streams):
                assert not stream.errors, stream.errors
                report["verified"].setdefault("streamFrames", {})[name] = len(stream.frames)
            code, stopped = call("/spatial/stop", "POST")
            assert code == 200 and stopped["removed"], stopped
            time.sleep(.5)
            final = state()
            restored = sum(a["frameAppKit"] == b["frameAppKit"] for a, b in zip(initial["windows"][:3], final["windows"][:3]))
            assert restored == 3, (initial["windows"][:3], final["windows"][:3])
            report["verified"]["restoredFixtureFrames"] = restored
            # Crash only this owned helper, then exercise startup restoration
            # with the same fixture PID and persisted originals.
            code, restarted = call("/spatial/start", "POST", policy="legacy-all")
            assert code == 200, restarted
            helper.kill()
            helper.wait(timeout=10)
            helper.stdin.close(); helper.stdout.close()
            helper = subprocess.Popen([options.helper, "serve", "--port", "0", "--exit-on-eof"],
                env={**os.environ, "FRAME_MAC_VIEW_TOKEN": token, "FRAME_SPATIAL_ONLY_PIDS": str(pid)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
            assert select.select([helper.stdout], [], [], 15)[0], "Recovery helper did not listen"
            port = int(helper.stdout.readline().rsplit(":", 1)[1])
            recovered = wait_for(lambda: call("/spatial")[1], lambda value: value["state"] == "stopped")
            time.sleep(.5)
            recovered_frames = sum(a["frameAppKit"] == b["frameAppKit"] for a, b in zip(initial["windows"][:3], state()["windows"][:3]))
            assert recovered_frames == 3, state()["windows"][:3]
            report["verified"]["startupRecoveryFixtureFrames"] = recovered_frames
    except Exception as error:
        report["errors"].append(str(error))
    finally:
        report["streamDiagnostics"] = [dict(frames=len(stream.frames),
            messages=[{k: v for k, v in message.items() if k not in ("r", "gazeSession")} for message in stream.messages if message.get("t") != "stats"],
            lastStats=next((message for message in reversed(stream.messages) if message.get("t") == "stats"), {}), errors=stream.errors) for stream in streams]
        log = Path(options.work) / "helper-stderr.txt"
        if log.exists(): report["helperDiagnostics"] = log.read_text()[-8000:]
        for stream in streams:
            stream.close()
        if helper:
            if port:
                try:
                    report["cleanupStop"] = call("/spatial/stop", "POST")
                except Exception as error:
                    report["errors"].append(f"Stop: {error}")
            helper.stdin.close()
            try:
                helper.wait(timeout=20)
            except subprocess.TimeoutExpired:
                helper.terminate()
                helper.wait(timeout=15)
            helper.stdout.close()
        if fixture:
            command("cursor", point=original_cursor)
            time.sleep(.2)
            command("quit")
            fixture.stdin.close()
            fixture.wait(timeout=10)
            fixture.stderr.close()
        after = json.loads(subprocess.check_output([options.fixture, "--inventory"], text=True))
        report["verified"]["displayLayoutMatches"] = before["displays"] == after["displays"]
        def nonempty(window):
            return window["frame"]["Width"] > 0 and window["frame"]["Height"] > 0
        report["verified"]["ignoredEmptySurfaces"] = sum(not nonempty(w) for w in before["windows"])
        # CursorUIViewService is the system cursor canvas, not an application
        # window; moving the pointer can create or remove it by design.
        report["verified"]["systemCursorSurfaceCount"] = sum(w["app"] == "CursorUIViewService" for w in before["windows"])
        before["windows"] = [w for w in before["windows"] if nonempty(w) and w["app"] != "CursorUIViewService"]
        current = {w["id"]: w for w in after["windows"] if nonempty(w) and w["app"] != "CursorUIViewService"}
        changed = [dict(before=w, after=current.get(w["id"])) for w in before["windows"] if current.get(w["id"]) != w]
        report["verified"]["otherAppWindowCount"] = len(before["windows"])
        report["verified"]["otherAppWindowsChanged"] = changed
        if changed or before["displays"] != after["displays"]:
            report["errors"].append("Non-fixture windows or display layout differ after cleanup")
    return report


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise InterruptedError("Fixture Run Interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--helper", required=True)
    parser.add_argument("--fixture", required=True)
    options = parser.parse_args()
    result = run(options)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1 if result["errors"] else 2 if result["unverified"] else 0)
