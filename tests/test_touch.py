"""Touch: the live view's Control (ui/frame_touch.py on the Frame, and the server's checks).

Run: python3 -m unittest discover -s tests
"""
import io
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))


class Mapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frame_touch
        cls.t = frame_touch

    def test_panel_bigger_than_its_display_is_scaled(self):
        # Verified 2026-09-29: a 1920x1080 window on :1 (1280x720) took Accept at
        # window (1828, 1020) as pointer (1219, 680).
        panel = {"root": [1280, 720], "width": 1920, "height": 1080}
        x, y = self.t.to_root(panel, 1828 / 1920, 1020 / 1080)
        self.assertAlmostEqual(x, 1218.7, delta=1)
        self.assertAlmostEqual(y, 679.4, delta=1)

    def test_same_size_is_one_to_one_and_clamped(self):
        panel = {"root": [1280, 720], "width": 1280, "height": 720}
        self.assertEqual(self.t.to_root(panel, 0, 0), (0, 0))
        self.assertEqual(self.t.to_root(panel, 1, 1), (1279, 719))
        self.assertEqual(self.t.to_root(panel, -3, 7), (0, 719))

    def test_other_shapes_are_letterboxed(self):
        panel = {"root": [1280, 720], "width": 800, "height": 800}  # square: bars left and right
        x, y = self.t.to_root(panel, 0, 0.5)
        self.assertAlmostEqual(x, 280, delta=0.5)
        self.assertAlmostEqual(y, 359.5, delta=0.5)

    def test_same_id_on_both_displays_is_told_apart_by_pid(self):
        t = self.t
        saved = t.displays, t.window_info, t.window_pid
        self.addCleanup(lambda: (setattr(t, "displays", saved[0]), setattr(t, "window_info", saved[1]),
                                 setattr(t, "window_pid", saved[2])))
        t.displays = lambda: [":0", ":1"]
        t.window_info = lambda d, w: {"name": f"on {d}", "width": 1280 if w != "root" else 1920, "height": 720}
        t.window_pid = lambda d, w: {":0": 111, ":1": 222}[d]
        self.assertEqual(t.locate(5, 222)["display"], ":1")
        self.assertEqual(t.locate(5, 111)["display"], ":0")
        self.assertEqual(t.locate(5, None)["display"], ":0")

    def test_focus_display_is_decoded(self):
        t = self.t
        saved = t.xprop_root
        self.addCleanup(lambda: setattr(t, "xprop_root", saved))
        for values, want in (([12602, 0, 58], ":1"), ([12346], ":0"), ([], None), ([0x41], None)):
            t.xprop_root = lambda d, n, v=values: v
            self.assertEqual(t.focus_display(), want)

    def test_ascii_table_covers_printable_characters(self):
        for code in range(0x20, 0x7F):
            self.assertIn(chr(code), self.t.ASCII, chr(code))
        self.assertEqual(self.t.ASCII["a"], (30, False))
        self.assertEqual(self.t.ASCII["A"], (30, True))
        self.assertEqual(self.t.ASCII["?"], (53, True))
        self.assertEqual(self.t.ASCII["1"], (2, False))
        self.assertEqual(self.t.ASCII["0"], (11, False))

    def test_events_parsing(self):
        self.assertEqual(self.t.events(b'{"dx": 1}'), [{"dx": 1}])
        self.assertEqual(self.t.events(b'[{"dx": 1}, 5]'), [{"dx": 1}])
        self.assertEqual(self.t.events(b"nope"), [])


class FakeGamescope:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


class Apply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frame_touch
        cls.t = frame_touch

    def setUp(self):
        self.focused = {"window": 7, "display": ":1", "root": [1280, 720], "width": 1280, "height": 720, "name": "x"}
        saved = self.t.focus, self.t.say, self.t.focus_now
        self.t.STALE[0] = False
        self.said = []
        self.t.focus = lambda: dict(self.focused)
        self.t.focus_now = lambda: (self.focused["window"], self.focused["display"])
        self.t.say = lambda state, **more: self.said.append((state, more))
        self.addCleanup(lambda: (setattr(self.t, "focus", saved[0]), setattr(self.t, "say", saved[1]),
                                 setattr(self.t, "focus_now", saved[2])))

    def test_tap_moves_then_clicks_in_order(self):
        gs, panel = FakeGamescope(), None
        for e in ({"fx": 0.5, "fy": 0.5, "window": 7, "display": ":1"}, {"button": "left", "down": True, "window": 7, "display": ":1"},
                  {"button": "left", "down": False}):
            panel = self.t.apply(gs, e, panel)
        self.assertEqual([c[0] for c in gs.calls], ["move_to", "button", "button"])
        self.assertEqual(gs.calls[1][1:], ("left", True))

    def test_a_tap_meant_for_another_panel_goes_nowhere(self):
        # Focus moved on: the position and the press are dropped; the release still goes.
        gs, panel = FakeGamescope(), None
        for e in ({"fx": 0.5, "fy": 0.5, "window": 99, "display": ":1"}, {"button": "left", "down": True, "window": 99, "display": ":1"},
                  {"key": 30, "down": True, "window": 7, "display": ":0"}, {"button": "left", "down": False, "window": 99, "display": ":1"}):
            panel = self.t.apply(gs, e, panel)
        self.assertEqual(gs.calls, [("button", "left", False)])
        self.assertEqual(self.said[0], ("ready", {"focus": 7, "display": ":1", "stale": True}))

    def test_presses_read_focus_afresh(self):
        # A move may use a recent reading; a press checks again, so a panel that just
        # took focus doesn't get a click meant for another.
        gs, calls = FakeGamescope(), []
        self.t.focus = lambda: calls.append(1) or dict(self.focused)
        panel = self.t.apply(gs, {"fx": 0.5, "fy": 0.5, "window": 7, "display": ":1"}, None)
        panel = self.t.apply(gs, {"fx": 0.6, "fy": 0.5, "window": 7, "display": ":1"}, panel)
        self.assertEqual(len(calls), 1)
        self.t.apply(gs, {"button": "left", "down": True, "window": 7, "display": ":1"}, panel)
        self.assertEqual(len(calls), 1)  # same panel still: the quick check was enough
        self.focused["window"] = 8
        self.t.apply(gs, {"button": "left", "down": True, "window": 7, "display": ":1"}, panel)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c[0] for c in gs.calls], ["move_to", "move_to", "button"])

    def test_stale_is_said_once_and_cleared(self):
        gs, panel = FakeGamescope(), None
        for e in ({"fx": 0.5, "fy": 0.5, "window": 99, "display": ":1"}, {"fx": 0.5, "fy": 0.5, "window": 7, "display": ":1"},
                  {"fx": 0.6, "fy": 0.5, "window": 7, "display": ":1"}):
            panel = self.t.apply(gs, e, panel)
        self.assertEqual(self.said, [("ready", {"focus": 7, "display": ":1", "stale": True}),
                                     ("ready", {"focus": 7, "display": ":1"})])

    def test_stale_clears_on_a_trackpad_move_but_not_on_a_stale_release(self):
        gs = FakeGamescope()
        panel = self.t.apply(gs, {"fx": 0.5, "fy": 0.5, "window": 99, "display": ":1"}, None)
        panel = self.t.apply(gs, {"button": "left", "down": False, "window": 99, "display": ":1"}, panel)
        self.assertTrue(self.t.STALE[0])  # that release was still aimed at the old panel
        self.t.apply(gs, {"dx": 3, "dy": 0}, panel)
        self.assertFalse(self.t.STALE[0])
        self.assertEqual(self.said[-1][1].get("stale"), None)

    def test_same_window_id_on_the_other_display_is_another_panel(self):
        gs = FakeGamescope()
        self.t.apply(gs, {"fx": 0.5, "fy": 0.5, "window": 7, "display": ":0"}, None)
        self.assertEqual(gs.calls, [])

    def test_relative_scroll_keys_text(self):
        gs = FakeGamescope()
        for e in ({"dx": 5, "dy": -3}, {"scroll": [0, 120]}, {"key": 30, "down": True}, {"text": "hi"},
                  {"key": True}, {"button": "sideways"}):
            self.t.apply(gs, e, None)
        self.assertEqual([c[0] for c in gs.calls], ["move_by", "scroll", "key", "text"])

    def test_everything_held_is_let_go(self):
        class Held(self.t.Gamescope):
            def __init__(self):
                self.held, self.keys, self.log = set(), set(), []

            def frame(self):
                pass

        g = Held()
        g.L = type("L", (), {"ei_device_button_button": lambda *a: g.log.append(("button",) + a[2:]),
                             "ei_device_keyboard_key": lambda *a: g.log.append(("key",) + a[2:])})()
        g.device = object()
        g.button("left", True)
        g.key(42, True)
        g.release_all()
        self.assertEqual(g.log[-2:], [("button", 0x110, False), ("key", 42, False)])
        self.assertEqual((g.held, g.keys), (set(), set()))

    def test_resuming_after_a_pause_lets_go_of_everything(self):
        t = self.t
        log, queue = [], [1]

        class L:
            def __getattr__(self, name):
                if name == "ei_get_event":
                    return lambda ei: queue.pop(0) if queue else None
                return {"ei_event_get_type": lambda ev: t.EV_DEVICE_RESUMED, "ei_event_get_device": lambda ev: "dev",
                        "ei_device_has_capability": lambda d, c: True,
                        "ei_device_button_button": lambda d, c, down: log.append(("button", c, down)),
                        "ei_device_keyboard_key": lambda d, c, down: log.append(("key", c, down))}.get(name, lambda *a: 0)

        g = t.Gamescope.__new__(t.Gamescope)
        g.L, g.ei, g.fd, g.device, g.sequence, g.alive = L(), None, None, None, 0, True
        g.held, g.keys = {0x110}, {42}
        g.frame = lambda: None
        old = t.select.select
        t.select.select = lambda *a: ([], [], [])
        try:
            g.pump()
        finally:
            t.select.select = old
        self.assertEqual(sorted(log), [("button", 0x110, False), ("key", 42, False)])
        self.assertEqual(g.device, "dev")

    def test_text_uses_shift_for_capitals(self):
        class Keys(self.t.Gamescope):
            def __init__(self):
                self.pressed = []

            def key(self, code, down):
                self.pressed.append((code, down))
        k = Keys()
        k.text("Hié")  # the é has no key on a US layout and is left out
        self.assertEqual(k.pressed, [(42, True), (35, True), (35, False), (42, False), (23, True), (23, False)])


class ServerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.s = server

    def test_touch_event_keeps_known_fields(self):
        ev = self.s.touch_event
        self.assertEqual(ev({"fx": 0.5, "fy": 2, "window": 5, "display": ":1"}), {"fx": 0.5, "fy": 1.0, "window": 5, "display": ":1"})
        self.assertEqual(ev({"button": "left", "window": 5, "display": ":0"}), {"button": "left", "down": True, "window": 5, "display": ":0"})
        self.assertEqual(ev({"button": "right"}), {"button": "right", "down": True})
        self.assertEqual(ev({"key": 30, "down": False}), {"key": 30, "down": False})
        self.assertEqual(ev({"scroll": [0, 1e9]}), {"scroll": [0.0, 5000.0]})
        self.assertEqual(ev({"dx": 3, "other": 1}), {"dx": 3.0})

    def test_touch_event_rejects_bad_ones(self):
        for bad in (None, {}, {"fx": 0.5, "fy": 0.5}, {"fx": "1", "fy": 0, "window": 1, "display": ":1"}, {"button": "side"},
                    {"fx": 0.5, "fy": 0.5, "window": 1}, {"fx": 0.5, "fy": 0.5, "window": 1, "display": ":1;x"},
                    {"window": 1, "display": ":1"},
                    {"key": 0}, {"key": 999}, {"key": True}, {"scroll": [1]}, {"text": ""}, {"text": "x" * 501},
                    {"fx": 0.1, "fy": 0.1, "window": True, "display": ":1"}):
            with self.assertRaises(self.s.Failure, msg=repr(bad)):
                self.s.touch_event(bad)

    def test_panel_stream_is_the_window_and_checked(self):
        cmd = self.s.stream_command("src=panel&window=10485777&display=:1&h=720&fps=30")
        self.assertIn("DISPLAY=:1 ffmpeg", cmd)
        self.assertIn("-window_id 10485777 -i :1", cmd)
        for bad in ("src=panel&window=1;rm&display=:1", "src=panel&window=1&display=:1;x", "src=panel&display=:1"):
            with self.assertRaises(self.s.Failure):
                self.s.stream_command(bad + "&h=720&fps=30")

    def test_touch_agent_runs_the_helper_with_nothing_to_copy(self):
        agent = self.s.TouchAgent()
        self.assertEqual(agent.deliver(lambda m: None), "")
        cmd = agent.command()
        self.assertTrue(cmd.startswith("python3 -u -c '"))
        self.assertIn("frame_touch", cmd)

    def test_batch_limit(self):
        with self.assertRaises(self.s.Failure):
            self.s.remote_touch({"events": [{"dx": 1}] * (self.s.INPUT_BATCH_LIMIT + 1)})


class FindingPanels(unittest.TestCase):
    """panels() with faked headset probes: where gamescope's properties are, and why nothing was found (#73)."""

    @classmethod
    def setUpClass(cls):
        import frame_touch
        cls.t = frame_touch

    def setUp(self):
        t = self.t
        names = ("xprop_root", "window_info", "window_pid", "displays", "desktop_running", "has_gamescope")
        saved = {n: getattr(t, n) for n in names}
        saved_which = t.shutil.which
        self.real_displays = saved["displays"]
        self.real_has = saved["has_gamescope"]

        def restore():
            for n, v in saved.items():
                setattr(t, n, v)
            t.shutil.which = saved_which
            t._ROOT.clear()
        self.addCleanup(restore)
        t._ROOT.clear()
        self.props = {}  # (display, property) -> values
        self.windows = {}  # (display, window) -> info
        t.xprop_root = lambda d, n: list(self.props.get((d, n), []))
        t.has_gamescope = lambda d, timeout: any((d, n) in self.props for n in
                                                 ("GAMESCOPE_FOCUSABLE_WINDOWS", "GAMESCOPE_FOCUSED_WINDOW"))
        t.window_info = lambda d, w: self.windows.get((d, w))
        t.window_pid = lambda d, w: None
        t.displays = lambda: [":0", ":1", ":2"]
        t.desktop_running = lambda: False
        t.shutil.which = lambda tool: "/usr/bin/" + tool

    def plasma_on(self, display):
        # SteamOS 0.4.1: the nested Plasma desktop (kwin_wayland, app id 0) is a window on :0.
        self.props[(display, "GAMESCOPE_FOCUSABLE_WINDOWS")] = [4194307, 0, 2222]
        self.props[(display, "GAMESCOPE_FOCUSED_WINDOW")] = [4194307]
        self.props[(display, "GAMESCOPE_FOCUS_DISPLAY")] = [0x303A]  # ":0"
        self.windows[(":0", 4194307)] = {"name": "KDE Wayland Compositor", "x": 0, "y": 0, "width": 1280, "height": 800}
        self.windows[(":0", "root")] = {"width": 1280, "height": 720}

    def test_desktop_panel_on_display_0(self):
        self.plasma_on(":0")
        out = self.t.panels()
        self.assertEqual([(p["window"], p["display"], p["focused"]) for p in out["panels"]], [(4194307, ":0", True)])
        self.assertNotIn("why", out)

    def test_gamescope_properties_on_another_display_are_found(self):
        # A build that numbers its displays differently: :0 has no gamescope properties.
        self.plasma_on(":1")
        self.assertEqual(self.t.gamescope_root(), ":1")
        self.assertEqual(len(self.t.panels()["panels"]), 1)

    def test_root_search_shares_one_deadline(self):
        # Every display stalls: the search gives up within ROOT_DEADLINE, each probe within
        # ROOT_PROBE_TIMEOUT, and the answer (:0) is remembered rather than searched for again.
        t, now, probes = self.t, [0.0], []
        t.displays = lambda: [":0", ":1", ":2", ":3", ":4", ":5"]

        def stalled(display, timeout):
            probes.append((display, timeout))
            now[0] += timeout
            return False
        t.has_gamescope = stalled
        self.assertEqual(t.gamescope_root(clock=lambda: now[0]), ":0")
        self.assertLessEqual(now[0], t.ROOT_DEADLINE)
        self.assertTrue(all(0 < timeout <= t.ROOT_PROBE_TIMEOUT for _, timeout in probes))
        self.assertLess(len(probes), 6)
        count = len(probes)
        now[0] += 1
        self.assertEqual(t.gamescope_root(clock=lambda: now[0]), ":0")
        self.assertEqual(len(probes), count)  # remembered
        now[0] += t.ROOT_RETRY
        t.gamescope_root(clock=lambda: now[0])
        self.assertGreater(len(probes), count)  # tried again later

    def test_root_found_is_remembered(self):
        self.plasma_on(":1")
        calls = []
        has = self.t.has_gamescope
        self.t.has_gamescope = lambda d, timeout: calls.append(d) or has(d, timeout)
        self.assertEqual(self.t.gamescope_root(), ":1")
        self.t.panels()
        self.assertEqual(calls, [":0", ":1"])  # one search for the whole request

    def test_has_gamescope_reads_xprop_output(self):
        t = self.t
        saved = t.subprocess.run
        self.addCleanup(setattr, t.subprocess, "run", saved)
        for out, want in (("GAMESCOPE_FOCUSABLE_WINDOWS(CARDINAL) = 1, 0, 2\nGAMESCOPE_FOCUSED_WINDOW(CARDINAL) = 1\n", True),
                          ("GAMESCOPE_FOCUSABLE_WINDOWS:  not found.\nGAMESCOPE_FOCUSED_WINDOW(CARDINAL) = 0\n", True),
                          ("GAMESCOPE_FOCUSABLE_WINDOWS:  not found.\nGAMESCOPE_FOCUSED_WINDOW:  not found.\n", False)):
            t.subprocess.run = lambda *a, o=out, **k: subprocess.CompletedProcess(a, 0, o, "")
            self.assertEqual(self.real_has(":0", 1), want, out)

        def hang(*a, **k):
            raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
        t.subprocess.run = hang
        self.assertFalse(self.real_has(":0", 1))

    def test_listed_window_that_isnt_x11_explains_itself(self):
        self.plasma_on(":0")
        del self.windows[(":0", 4194307)]  # say, a desktop drawn as a Wayland window
        out = self.t.panels()
        self.assertEqual(out["panels"], [])
        self.assertIn("can't find its window", out["why"])
        self.assertIn("Headset view", out["why"])

    def test_desktop_running_but_not_listed(self):
        self.t.desktop_running = lambda: True
        out = self.t.panels()
        self.assertEqual(out["panels"], [])
        self.assertIn("desktop is running", out["why"])

    def test_nothing_open(self):
        out = self.t.panels()
        self.assertEqual(out, {"focus": None, "focus_display": None, "panels": [],
                               "why": "No app panel is open in the headset. Open the desktop or an app in "
                                      "the headset, then try again."})

    def test_missing_x_tools_are_named(self):
        self.plasma_on(":0")
        self.t.shutil.which = lambda tool: None if tool == "xwininfo" else "/usr/bin/" + tool
        self.windows.clear()  # xwininfo can't run, so nothing resolves
        self.assertIn("no xwininfo", self.t.panels()["why"])

    def test_displays_include_abstract_sockets(self):
        # /tmp/.X11-unix not visible to the SSH session: the abstract sockets still name the displays.
        import builtins
        t = self.t
        real_open, real_listdir = builtins.open, t.os.listdir

        def fake_open(path, *a, **k):
            if path == "/proc/net/unix":
                return io.StringIO("0000: 00000002 0 ... @/tmp/.X11-unix/X1\n0000: ... @/tmp/.X11-unix/X0\n"
                                   "0000: ... /run/user/1000/wayland-0\n")
            return real_open(path, *a, **k)

        def fake_listdir(path):
            if path == "/tmp/.X11-unix":
                raise FileNotFoundError(path)
            return real_listdir(path)
        builtins.open, t.os.listdir = fake_open, fake_listdir
        try:
            self.assertEqual(self.real_displays(), [":0", ":1"])
        finally:
            builtins.open, t.os.listdir = real_open, real_listdir


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageGestures(unittest.TestCase):
    """Control's gestures and queue (tests/page/ctrl_gestures.mjs runs the real functions from index.html)."""

    def test_gestures(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "page" / "ctrl_gestures.mjs")], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_desktop_panel_list(self):
        # #73: the headset's reason for no panels, and a failed listing, reach Capture and Control.
        r = subprocess.run(["node", str(ROOT / "tests" / "page" / "desk_panels.mjs")], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Script(unittest.TestCase):
    def test_helper_compiles_on_the_frames_python(self):
        # The Frame runs it with its own python3 (3.13 on SteamOS 0.4.1); stdlib and ctypes only.
        src = (ROOT / "ui/frame_touch.py").read_text()
        compile(src, "frame_touch.py", "exec")
        for mod in ("import ctypes", "import json", "import select"):
            self.assertIn(mod, src)
        self.assertNotIn("import requests", src)


if __name__ == "__main__":
    unittest.main()
