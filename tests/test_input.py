"""Keyboard and pointer: event checks, and the agent's KDE Connect protocol against a fake kdeconnectd.

The fake follows what KDE Connect 24.02 does (read from its source,
core/backends/lan/lanlinkprovider.cpp): it accepts the TCP connection, reads the
identity line, then starts TLS as the *client*.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))


class InputEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def check(self, event):
        return self.server.input_event(event)

    def test_keeps_known_fields(self):
        self.assertEqual(self.check({"dx": 3, "dy": -1.234}), {"dx": 3.0, "dy": -1.23})
        self.assertEqual(self.check({"singleclick": True, "other": 1}), {"singleclick": True})
        self.assertEqual(self.check({"key": "héllo", "shift": True}), {"key": "héllo", "shift": True})
        self.assertEqual(self.check({"specialKey": 12, "ctrl": True}), {"specialKey": 12, "ctrl": True})
        self.assertEqual(self.check({"scroll": True, "dy": 1}), {"scroll": True, "dy": 1.0})

    def test_clamps_movement(self):
        self.assertEqual(self.check({"dx": 1e9})["dx"], self.server.INPUT_MOVE_LIMIT)
        self.assertEqual(self.check({"dy": -1e9})["dy"], -self.server.INPUT_MOVE_LIMIT)

    def test_rejects_bad_events(self):
        bad = [None, [], "a", {}, {"shift": True}, {"dx": "1"}, {"dx": True}, {"dx": float("nan")},
               {"key": ""}, {"key": 5}, {"key": "x" * 501}, {"specialKey": 0}, {"specialKey": 33},
               {"specialKey": True}, {"specialKey": 1.5}, {"singleclick": "yes"}]
        for event in bad:
            with self.assertRaises(self.server.Failure, msg=repr(event)):
                self.check(event)

    def test_send_while_link_is_down_says_not_sent(self):
        # The page keeps unsent keys and clicks and sends them once the agent is ready again.
        from types import SimpleNamespace

        class Probe(self.server.InputAgent):
            def start(self):
                self.proc, self.status = SimpleNamespace(poll=lambda: None, stdin=None), {"state": "starting"}

        agent = Probe()
        agent.status, agent.proc = {"state": "ready"}, SimpleNamespace(poll=lambda: 255)  # ssh has exited
        self.assertEqual(agent.send([{"key": "x"}]), {"state": "starting", "sent": False})

    def test_command_passes_client_quoted(self):
        os.environ["FRAME_CLIENT"] = "test-client-1"
        try:
            cmd = self.server.InputAgent().command()
        finally:
            del os.environ["FRAME_CLIENT"]
        self.assertTrue(cmd.startswith("python3 -u -c '"))
        self.assertIn(" test-client-1 ", cmd)
        cmd = self.server.InputAgent(packages=[("a.pkg.tar.zst", "ab")]).command("~/in/x")
        self.assertTrue(cmd.endswith(""" '~/in/x' '[["a.pkg.tar.zst","ab"]]'"""), cmd)

    def test_batch_limits(self):
        with self.assertRaises(self.server.Failure):
            self.server.remote_input({"events": "dx"})
        with self.assertRaises(self.server.Failure):
            self.server.remote_input({"events": [{"dx": 1}] * (self.server.INPUT_BATCH_LIMIT + 1)})


class Bundled(unittest.TestCase):
    """KDE Connect ships with Frame Control: the manifest, the notice, the copy to the Frame."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.manifest = json.loads((ROOT / "frame/kdeconnect/packages.json").read_text())

    def test_manifest_pins_every_package(self):
        packages = self.manifest["packages"]
        self.assertEqual({p["name"] for p in packages},
                         {"kdeconnect", "kcontacts", "kpeople", "libfakekey", "modemmanager-qt", "pulseaudio-qt"})
        for p in packages:
            self.assertRegex(p["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(p["file"].startswith(f"{p['name']}-{p['version']}-") and p["file"].endswith("-aarch64.pkg.tar.zst"), p)
            self.assertTrue(p["source"].startswith(self.manifest["release"]), p)
            self.assertRegex(p["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(self.manifest["release"].startswith("https://github.com/") and self.manifest["release"].endswith("/"))
        self.assertNotIn("DO_NOT_SHARE", json.dumps(self.manifest))

    def test_notice_names_each_version_and_its_licence_texts_ship(self):
        notice = (ROOT / "frame/kdeconnect/NOTICE.md").read_text()
        for p in self.manifest["packages"]:
            self.assertIn(f"{p['name']} {p['version']}", notice)
            self.assertIn(p["source"], notice)
            self.assertIn(p["upstream"], notice)
            for spdx in p["licenses"]:
                self.assertTrue((ROOT / "frame/kdeconnect/LICENSES" / p["name"] / f"{spdx}.txt").is_file(), spdx)
        self.assertIn("frame/kdeconnect/NOTICE.md", (ROOT / "THIRD_PARTY_NOTICES.md").read_text())

    def test_about_dialog_has_the_licences(self):
        titles = [n["title"] for n in self.server.licenses()]
        self.assertIn("Frame Control (MIT)", titles)
        self.assertIn("KDE Connect for the Frame", titles)
        self.assertIn("kdeconnect: GPL-2.0-only", titles)

    def test_both_apps_bundle_the_packages(self):
        pkg = json.loads((ROOT / "app/package.json").read_text())["build"]["extraResources"]
        kde = next(r for r in pkg if r["from"] == "../frame/kdeconnect")
        self.assertIn("packages/*.pkg.tar.zst", kde["filter"])
        self.assertIn("LICENSES/**/*", kde["filter"])
        bundle = (ROOT / "ios/scripts/make_frame_bundle.py").read_text()
        for pattern in ("frame/kdeconnect/packages/*.pkg.tar.zst", "frame/kdeconnect/LICENSES/*/*", "THIRD_PARTY_NOTICES.md"):
            self.assertIn(pattern, bundle)

    def fake_bundle(self, damaged=False):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        (folder / "packages").mkdir()
        data = {"a-1-1-aarch64.pkg.tar.zst": b"first", "b-2-1-aarch64.pkg.tar.zst": b"second"}
        packages = []
        for name, body in data.items():
            (folder / "packages" / name).write_bytes(b"x" + body if damaged else body)
            packages.append((name, __import__("hashlib").sha256(body).hexdigest()))
        return folder, packages

    def deliver(self, frame_has, damaged=False):
        folder, packages = self.fake_bundle(damaged)
        calls = []

        def ssh(remote, stdin=None, timeout=30, text=True):
            calls.append((remote, stdin))
            answer = "yes\n" if remote.startswith("{ test -x") and frame_has else ""
            return answer if text else answer.encode()
        old = self.server.ssh, self.server.KDECONNECT, self.server.LOCAL
        self.server.ssh, self.server.KDECONNECT, self.server.LOCAL = ssh, folder, False
        try:
            agent = self.server.InputAgent(packages=packages)
            return agent.deliver(lambda m: calls.append(("report", m))), calls, packages
        finally:
            self.server.ssh, self.server.KDECONNECT, self.server.LOCAL = old

    def test_copies_nothing_when_the_frame_has_them(self):
        folder, calls, packages = self.deliver(frame_has=True)
        self.assertEqual(folder, "")
        self.assertEqual(len(calls), 1)
        # Bytes: on Windows a text pipe would send CRLF and never match.
        self.assertEqual(calls[0][1], "".join(f"{sha}  {name}\n" for name, sha in packages).encode())

    def test_copies_each_package_over_ssh(self):
        folder, calls, packages = self.deliver(frame_has=False)
        self.assertTrue(folder.startswith("~/.local/share/frame-control/kdeconnect/incoming/"))
        copies = [c for c in calls if c[0] != "report" and "cat >" in c[0]]
        self.assertEqual([c[1] for c in copies], [b"first", b"second"])
        self.assertIn("a-1-1-aarch64.pkg.tar.zst.part", copies[0][0])

    def test_refuses_a_damaged_bundle(self):
        with self.assertRaises(self.server.Failure):
            self.deliver(frame_has=False, damaged=True)

    def test_a_failed_copy_is_removed(self):
        calls = []

        def ssh(remote, stdin=None, timeout=30, text=True):
            calls.append(remote)
            if "cat >" in remote and len([c for c in calls if "cat >" in c]) == 2:
                raise self.server.Failure("Broken pipe")
            return b"" if not text else ""
        folder, packages = self.fake_bundle()
        old = self.server.ssh, self.server.KDECONNECT, self.server.LOCAL
        self.server.ssh, self.server.KDECONNECT, self.server.LOCAL = ssh, folder, False
        try:
            with self.assertRaises(self.server.Failure):
                self.server.InputAgent(packages=packages).deliver(lambda m: None)
        finally:
            self.server.ssh, self.server.KDECONNECT, self.server.LOCAL = old
        self.assertTrue(calls[-1].startswith("rm -rf .local/share/frame-control/kdeconnect/incoming/"), calls[-1])

    def lifecycle(self, agent_lines, deliver=None):
        """An InputAgent whose ssh and agent are fakes. Returns it, the folders each launch
        used, and the fake agents. A fake agent reports its lines, then keeps running
        (unless its lines end with need-packages) until the test ends."""
        launches, procs, done = [], [], threading.Event()
        self.addCleanup(done.set)

        class Agent(self.server.InputAgent):
            def deliver(self, report, force=False):
                if deliver:
                    deliver()
                return "~/copied" if force else ""

            def command(self, folder=""):
                launches.append(folder)
                return "agent"

        class Proc:
            stdin = None

            def __init__(self, lines):
                self.lines, self.ended = lines, threading.Event()
                procs.append(self)

            @property
            def stdout(self):
                yield from self.lines
                if self.lines and b"need-packages" not in self.lines[-1]:
                    while not (done.is_set() or self.ended.is_set()):
                        self.ended.wait(0.02)
                self.ended.set()

            def wait(self):
                self.ended.wait(5)
                return 0

            def poll(self):
                return 0 if self.ended.is_set() else None

            def terminate(self):
                self.ended.set()

        def popen(*a, **k):
            return Proc(agent_lines.pop(0) if agent_lines else [b'{"state": "ready"}\n'])
        old = self.server.ensure_master, self.server.subprocess.Popen
        self.server.ensure_master, self.server.subprocess.Popen = lambda: None, popen
        self.addCleanup(lambda: (setattr(self.server, "ensure_master", old[0]),
                                 setattr(self.server.subprocess, "Popen", old[1])))
        agent = Agent(packages=[("a.pkg.tar.zst", "0" * 64)])

        def settle():  # nothing of this test may still be launching when the next one starts
            agent.stop()
            for _ in range(250):
                if agent.launching is None and all(p.ended.is_set() for p in procs):
                    return
                time.sleep(0.02)
        self.addCleanup(settle)
        return agent, launches, procs

    def wait_for(self, check):
        for _ in range(250):
            if check():
                return
            time.sleep(0.02)
        self.fail("timed out")

    def test_agent_asking_for_packages_gets_them_and_starts_again(self):
        agent, launches, procs = self.lifecycle([[b'{"state": "need-packages"}\n'], [b'{"state": "ready"}\n']])
        agent.start()
        self.wait_for(lambda: agent.status == {"state": "ready"})
        self.assertEqual(launches, ["", "~/copied"])
        self.assertIs(agent.proc, procs[1])

    def test_asking_twice_is_an_error_not_a_loop(self):
        need = [b'{"state": "need-packages"}\n']
        agent, launches, _ = self.lifecycle([need, list(need)])
        agent.start()
        self.wait_for(lambda: agent.status.get("state") == "error")
        self.assertEqual(launches, ["", "~/copied"])
        self.assertIn("didn't reach", agent.status["message"])

    def test_start_after_stop_during_the_copy_still_starts(self):
        gate, entered = threading.Event(), threading.Event()
        agent, launches, procs = self.lifecycle([], deliver=lambda: (entered.set(), gate.wait(5)))
        agent.start()
        self.assertTrue(entered.wait(5))
        agent.stop()
        entered.clear()
        agent.start()  # while the first launch is still copying
        self.assertTrue(entered.wait(5), "the second start didn't launch")
        gate.set()
        self.wait_for(lambda: agent.status == {"state": "ready"})
        time.sleep(0.1)
        # The stopped launch started no agent; the new one is the one in use.
        self.assertEqual(len(procs), 1)
        self.assertIs(agent.proc, procs[0])
        agent.stop()
        self.wait_for(lambda: all(p.ended.is_set() for p in procs))

    def test_retry_and_a_new_start_never_run_two_agents(self):
        # A start() landing just as an agent that asked for the packages exits: exactly one
        # of it and the retry launches, and stop() ends everything. The new start is held
        # in its copy until the retry has decided, so neither can see the other's agent.
        first, decided = [], threading.Event()
        agent, launches, procs = self.lifecycle([[b'{"state": "need-packages"}\n'], [b'{"state": "ready"}\n']])
        watch, launch, deliver = agent._watch, agent._launch, agent.deliver

        def racing_watch(proc, errors, retry=False):
            wanted, heard = watch(proc, errors, retry)
            if wanted and not first:
                first.append(threading.current_thread())
                agent.start()  # lands between the agent exiting and the retry
            return wanted, heard

        def held_deliver(report, force=False):
            if force:
                decided.set()  # the retry went ahead
            elif first and threading.current_thread() is not first[0]:
                decided.wait(5)  # the new start waits until the retry has decided
            return deliver(report, force)

        def first_launch(generation, force=False):
            try:
                launch(generation, force)
            finally:
                if first and threading.current_thread() is first[0]:
                    decided.set()  # the retry declined
        agent._watch, agent._launch, agent.deliver = racing_watch, first_launch, held_deliver
        agent.start()
        # Both have decided once the new start has launched its agent (it always does).
        self.wait_for(lambda: decided.is_set() and launches.count("") == 2 and len(procs) == len(launches))
        self.wait_for(lambda: agent.status == {"state": "ready"})
        self.assertEqual(sum(not p.ended.is_set() for p in procs), 1, launches)
        agent.stop()
        self.wait_for(lambda: all(p.ended.is_set() for p in procs))

    def test_stopped_during_the_copy_removes_it(self):
        gate, entered = threading.Event(), threading.Event()
        agent, launches, procs = self.lifecycle([], deliver=lambda: (entered.set(), gate.wait(5)))
        removed = []
        agent.discard = removed.append
        agent.deliver = lambda report, force=False: (entered.set(), gate.wait(5), "~/incoming/x")[2]
        self.addCleanup(lambda: self.assertEqual(removed, ["~/incoming/x"]))
        agent.start()
        self.assertTrue(entered.wait(5))
        agent.stop()
        gate.set()
        self.wait_for(lambda: removed == ["~/incoming/x"])
        self.assertEqual(procs, [])  # no agent started for it

    def test_a_copy_whose_agent_never_starts_is_removed(self):
        agent, launches, procs = self.lifecycle([[]])  # the agent dies before saying anything
        removed = []
        agent.discard = removed.append
        agent.deliver = lambda report, force=False: "~/incoming/y"
        agent.start()
        self.wait_for(lambda: removed == ["~/incoming/y"])

    def test_discard_only_touches_copies(self):
        calls = []
        old = self.server.ssh, self.server.LOCAL
        self.server.ssh, self.server.LOCAL = (lambda remote, **k: calls.append(remote)), False
        try:
            agent = self.server.InputAgent(packages=[])
            agent.discard("")
            agent.discard("~/.local/share/frame-control/kdeconnect")
            agent.discard("~/.local/share/frame-control/kdeconnect/incoming/ab12")
            self.server.LOCAL = True
            agent.discard("~/.local/share/frame-control/kdeconnect/incoming/ab12")
        finally:
            self.server.ssh, self.server.LOCAL = old
        self.assertEqual(calls, ["rm -rf .local/share/frame-control/kdeconnect/incoming/ab12"])

    def test_a_launch_error_leaves_it_ready_to_try_again(self):
        # Popen fails (say, out of file handles) and so does removing the copy: the
        # page gets the error and the next start launches.
        agent, launches, procs = self.lifecycle([])
        agent.deliver = lambda report, force=False: "~/.local/share/frame-control/kdeconnect/incoming/z"
        old_popen, old_ssh = self.server.subprocess.Popen, self.server.ssh
        self.server.subprocess.Popen = self.server.ssh = lambda *a, **k: (_ for _ in ()).throw(OSError(24, "Too many open files"))
        try:
            agent.start()
            self.wait_for(lambda: agent.status.get("state") == "error")
        finally:
            self.server.subprocess.Popen, self.server.ssh = old_popen, old_ssh
        self.assertIsNone(agent.launching)
        agent.start()
        self.wait_for(lambda: agent.status == {"state": "ready"})

    def test_any_unexpected_error_still_reports_and_allows_a_retry(self):
        agent, launches, procs = self.lifecycle([])
        old = self.server.tempfile.TemporaryFile
        self.server.tempfile.TemporaryFile = lambda: (_ for _ in ()).throw(OSError(24, "Too many open files"))
        try:
            agent.start()
            self.wait_for(lambda: agent.status.get("state") == "error")
        finally:
            self.server.tempfile.TemporaryFile = old
        self.assertIsNone(agent.launching)
        agent.deliver = lambda report, force=False: (_ for _ in ()).throw(ValueError("something odd"))
        agent.start()
        self.wait_for(lambda: "something odd" in agent.status.get("message", ""))
        self.assertIsNone(agent.launching)

    def test_copies_go_to_a_folder_of_their_own(self):
        first, _, _ = self.deliver(frame_has=False)
        second, _, _ = self.deliver(frame_has=False)
        self.assertNotEqual(first, second)


@unittest.skipIf(sys.platform == "win32", "the agent runs on the Frame (Linux)")
class AgentInstall(unittest.TestCase):
    """The agent unpacks what the server copied, after checking each SHA-256."""

    @classmethod
    def setUpClass(cls):
        import frame_input_agent
        cls.agent = frame_input_agent

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        saved = self.agent.BASE, self.agent.ROOT, self.agent.stop_daemon, self.agent.say
        self.addCleanup(lambda: setattr_all(self.agent, saved))
        self.agent.BASE, self.agent.ROOT = self.dir / "base", self.dir / "base/root"
        self.agent.stop_daemon, self.agent.say = lambda: None, lambda *a, **k: None
        self.agent.BASE.mkdir()

    def package(self):
        src = self.dir / "src"
        (src / "usr/lib").mkdir(parents=True)
        (src / "usr/lib/kdeconnectd").write_text("#!/bin/sh\n")
        out = self.dir / "incoming/kdeconnect-24.02.2-1-aarch64.pkg.tar.zst"
        out.parent.mkdir()
        if subprocess.run(["tar", "--zstd", "-cf", str(out), "-C", str(src), "usr"], capture_output=True).returncode:
            self.skipTest("this tar can't write zstd")
        return out, [(out.name, self.agent.sha256(out))]

    def test_unpacks_and_stamps(self):
        path, packages = self.package()
        self.assertFalse(self.agent.installed(packages))
        self.agent.install(path.parent, packages)
        self.assertTrue((self.agent.ROOT / "usr/lib/kdeconnectd").is_file())
        self.assertTrue(self.agent.installed(packages))
        self.assertFalse(self.agent.installed([(path.name, "0" * 64)]))

    def test_refuses_a_damaged_package(self):
        path, packages = self.package()
        with open(path, "ab") as f:
            f.write(b"!")
        with self.assertRaisesRegex(RuntimeError, "damaged"):
            self.agent.install(path.parent, packages)
        self.assertFalse(self.agent.ROOT.exists())

    def test_missing_package_and_empty_manifest(self):
        with self.assertRaisesRegex(RuntimeError, "didn't reach"):
            self.agent.install(self.dir, [("nope.pkg.tar.zst", "0" * 64)])
        with self.assertRaisesRegex(RuntimeError, "doesn't include"):
            self.agent.install(self.dir, [])

    def test_leaves_another_devices_running_copy_alone(self):
        # A different build is running for another device: use it, don't stop it to reinstall.
        self.agent.listening, old = (lambda: True), self.agent.listening
        self.agent.our_daemons, old_ours = (lambda: [123]), self.agent.our_daemons
        try:
            self.agent.ensure_daemon("", [("new.pkg.tar.zst", "1" * 64)])
        finally:
            self.agent.listening, self.agent.our_daemons = old, old_ours
        self.assertFalse(self.agent.ROOT.exists())

    def test_asks_for_packages_it_was_not_sent(self):
        self.agent.listening, old = (lambda: False), self.agent.listening
        try:
            for folder in ("", str(self.dir / "gone")):  # none sent, or already tidied away
                with self.assertRaises(self.agent.NeedPackages):
                    self.agent.ensure_daemon(folder, [("new.pkg.tar.zst", "1" * 64)])
        finally:
            self.agent.listening = old

    def test_restart_can_still_unpack_its_copy_then_tidies_it(self):
        folder = self.agent.BASE / "incoming/abc"
        folder.mkdir(parents=True)
        seen = []
        stubs = {"ensure_daemon": lambda f, p: seen.append(Path(f).is_dir()),
                 "identity": lambda c: ("id", "cert", "key"), "our_daemons": lambda: [123],
                 "connect": lambda *a: (_ for _ in ()).throw(OSError("no answer"))}
        saved = {k: getattr(self.agent, k) for k in stubs}
        self.addCleanup(lambda: [setattr(self.agent, k, v) for k, v in saved.items()])
        for k, v in stubs.items():
            setattr(self.agent, k, v)
        self.assertEqual(self.agent.run("c", "n", str(folder), []), 1)
        self.assertEqual(seen, [True, True])  # there for the first start and the restart
        self.assertFalse(folder.exists())

    def test_tidy_keeps_copies_in_use(self):
        incoming = self.agent.BASE / "incoming"
        held_dir = incoming / "held"
        held_dir.mkdir(parents=True)
        held = self.agent.hold_incoming(str(held_dir))
        self.addCleanup(held.close)
        os.utime(held_dir, (time.time() - 7200,) * 2)
        self.agent.tidy_incoming("")
        self.assertTrue(held_dir.is_dir())

    def test_tidy_keeps_other_starts_copies(self):
        incoming = self.agent.BASE / "incoming"
        mine, theirs, stale = incoming / "mine", incoming / "theirs", incoming / "stale"
        for d in (mine, theirs, stale):
            d.mkdir(parents=True)
        os.utime(stale, (time.time() - 7200,) * 2)
        self.agent.tidy_incoming(str(mine))
        self.assertEqual(sorted(p.name for p in incoming.iterdir()), ["theirs"])

    def test_server_and_agent_agree_on_the_stamp(self):
        import server
        packages = [("a.pkg.tar.zst", "1" * 64), ("b.pkg.tar.zst", "2" * 64)]
        self.assertEqual(server.kdeconnect_stamp(packages), self.agent.stamp(packages))


def setattr_all(module, saved):
    module.BASE, module.ROOT, module.stop_daemon, module.say = saved


class FakeKdeConnect:
    """Just enough of kdeconnectd: pairs when asked and records remote-input packets."""

    def __init__(self, paired=False):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.paired, self.identity, self.received, self.pair_requests = paired, None, [], 0
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        conn, _ = self.listener.accept()
        line = b""
        while not line.endswith(b"\n"):
            line += conn.recv(1)
        self.identity = json.loads(line)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        tls = ctx.wrap_socket(conn)  # "Starting client ssl (but I'm the server TCP socket)"
        tls.sendall(b'{"id": 1, "type": "kdeconnect.mousepad.keyboardstate", "body": {"state": true}}\n')
        buf = b""
        while True:
            try:
                chunk = tls.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                p = json.loads(raw)
                if p["type"] == "kdeconnect.pair":
                    self.pair_requests += 1
                elif p["type"] == "kdeconnect.mousepad.request" and self.paired:
                    self.received.append(p["body"])


@unittest.skipIf(sys.platform == "win32", "the agent runs on the Frame (Linux)")
@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate")
class AgentProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frame_input_agent
        cls.agent = frame_input_agent
        cls.dir = tempfile.TemporaryDirectory()
        cls.cert, cls.key = Path(cls.dir.name, "cert.pem"), Path(cls.dir.name, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=framecontrol_test", "-keyout", str(cls.key), "-out", str(cls.cert)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def wait_for(self, check):
        for _ in range(100):
            if check():
                return
            time.sleep(0.02)
        self.fail("timed out")

    def test_pairs_then_forwards_events(self):
        fake = FakeKdeConnect()
        link = self.agent.Link("framecontrol_0123456789abcdef01234567", self.cert, self.key, port=fake.port)
        link.pair(lambda: fake.paired, lambda: setattr(fake, "paired", fake.pair_requests > 0), timeout=5)
        self.assertEqual(fake.pair_requests, 1)
        ident = fake.identity["body"]
        self.assertEqual(fake.identity["type"], "kdeconnect.identity")
        self.assertEqual(ident["protocolVersion"], 7)
        self.assertIn("kdeconnect.mousepad.request", ident["outgoingCapabilities"])
        link.read(0.5)
        self.assertTrue(link.keyboard)
        for body in self.agent.events(b'[{"dx": 4, "dy": -2}, {"key": "hi"}]'):
            link.send(body)
        self.wait_for(lambda: len(fake.received) == 2)
        self.assertEqual(fake.received, [{"dx": 4, "dy": -2}, {"key": "hi"}])

    def test_already_paired_sends_no_pair_request(self):
        # A pair request to a device that's already paired makes KDE Connect unpair it.
        fake = FakeKdeConnect(paired=True)
        link = self.agent.Link("framecontrol_0123456789abcdef01234567", self.cert, self.key, port=fake.port)
        link.pair(lambda: True, lambda: self.fail("accepted a pairing that wasn't needed"))
        link.send({"singleclick": True})
        self.wait_for(lambda: fake.received == [{"singleclick": True}])
        self.assertEqual(fake.pair_requests, 0)

    def test_client_args_are_folder_safe(self):
        old = sys.argv
        try:
            sys.argv = ["-c", "../../etc x", "Alex's Mac"]
            self.assertEqual(self.agent.client_args(), ("etcx", "Frame Control (Alex's Mac)", "", []))
            sys.argv = ["-c"]
            self.assertEqual(self.agent.client_args(), ("default", "Frame Control", "", []))
            sys.argv = ["-c", "mac", "Mac", "~/x", '[["a.pkg.tar.zst", "ab"]]']
            self.assertEqual(self.agent.client_args()[2:], (os.path.expanduser("~/x"), [("a.pkg.tar.zst", "ab")]))
        finally:
            sys.argv = old

    def test_events_parsing(self):
        self.assertEqual(self.agent.events(b'{"dx": 1}'), [{"dx": 1}])
        self.assertEqual(self.agent.events(b'[{"dx": 1}, 5, {}]'), [{"dx": 1}])
        self.assertEqual(self.agent.events(b"not json"), [])


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageQueue(unittest.TestCase):
    """The page's input queue (tests/page/pad_queue.mjs runs the real functions from index.html)."""

    def test_queue_rules(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "page" / "pad_queue.mjs")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
