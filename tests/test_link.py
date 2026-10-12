"""frame_link: finding a headset among its addresses and following each stage of
connecting, with a stand-in ssh (tests/fakessh/ssh) and real sockets on this computer.
Also the server's /api/connection, its event stream, and /api/devices.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import http.client
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_devices as fd  # noqa: E402
import frame_link as fl  # noqa: E402
import frame_network as fn  # noqa: E402

FAKESSH = ROOT / "tests" / "fakessh"
NET = {"id": "n-test", "gateway": "192.168.1.1", "gateway_mac": "aa:bb:cc:dd:ee:ff", "interface": "en0",
       "ssid": None, "wifi": True, "local_ip": "192.168.1.9", "tailscale": {"up": False, "installed": False}}


def explain(msg):
    """A cut-down server.unreachable, so this needs no server import."""
    if "Could not resolve" in msg:
        return "Can't find the Frame on the network."
    if "refused" in msg:
        return "The Frame refused the connection."
    if "timed out" in msg.lower():
        return "The Frame isn't answering."
    if "Permission denied" in msg:
        return "The Frame didn't accept this computer's SSH key."
    return None


class FailureDetail(unittest.TestCase):
    def test_an_address_that_failed_says_why_in_a_few_words(self):
        # #87: the banner put each address's detail after the headline, and with the whole
        # message as the detail it said everything twice ("...may be answering here..").
        self.assertEqual(fl.failure_detail({"message": "A long sentence.", "short": "SSH identity changed"}),
                         "SSH identity changed")
        self.assertEqual(fl.failure_detail({"message": "The connection to the Frame dropped. Try again."}),
                         "The connection to the Frame dropped. Try again.")
        self.assertEqual(fl.failure_detail(None), "SSH failed")


class Probe(unittest.TestCase):
    def test_answers_refusals_and_unknown_names(self):
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            port = srv.getsockname()[1]
            seen = []
            res = fl.probe("127.0.0.1", port, update=lambda **f: seen.append(f["state"]))
            self.assertEqual(res["state"], "answered")
            self.assertEqual(res["ip"], "127.0.0.1")
            self.assertIsInstance(res["rtt_ms"], float)
            self.assertEqual(seen, ["resolving", "trying"])
        # Closed now. Windows retries a refused connect for about 2 s before saying so.
        self.assertEqual(fl.probe("127.0.0.1", port, timeout=6 if os.name == "nt" else 2)["state"], "refused")
        self.assertEqual(fl.probe("frame-control-test.invalid", 22, timeout=2)["state"], "unresolved")

    def test_failed_probes_read_like_ssh(self):
        # So the server's UNREACHABLE table words them like any other ssh failure.
        self.assertIn("Could not resolve hostname x", fl.probe_raw("x", 22, {"state": "unresolved"}))
        self.assertIn("port 22: Connection refused", fl.probe_raw("x", 22, {"state": "refused"}))
        self.assertIn("Operation timed out", fl.probe_raw("x", 22, {"state": "timeout"}))


class Pick(unittest.TestCase):
    def pick(self, results, tried=()):
        return fl.Link.pick(results, set(tried), threading.Condition(), time.monotonic() + 5)

    def test_best_ranked_answer_wins(self):
        now = time.monotonic()
        ok = lambda t=now: {"state": "answered", "t": t}
        no = {"state": "timeout", "t": now}
        self.assertEqual(self.pick([no, ok(), ok()]), 1)
        self.assertEqual(self.pick([no, ok(), ok()], tried=[1]), 2)
        self.assertIsNone(self.pick([no, no]))
        # A worse-ranked answer waits PREFER for a better one still trying, then goes.
        t0 = time.monotonic()
        self.assertEqual(self.pick([None, ok(time.monotonic())]), 1)
        self.assertGreaterEqual(time.monotonic() - t0, fl.PREFER - 0.05)

    def test_gives_up_on_slow_lookups_at_the_deadline(self):
        t0 = time.monotonic()
        results = [None]
        self.assertIsNone(fl.Link.pick(results, set(), threading.Condition(), time.monotonic() + 0.3))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(results[0]["state"], "timeout")


@unittest.skipIf(os.name == "nt", "the stand-in ssh is a POSIX script")
class Connecting(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="frame-link-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "ssh").mkdir()
        self.log = self.dir / "calls.jsonl"
        env = {"FRAME_CONTROL_SSH_DIR": str(self.dir / "ssh"), "FAKESSH_LOG": str(self.log),
               "FAKESSH_DIR": str(self.dir), "PATH": f"{FAKESSH}{os.pathsep}{os.environ['PATH']}"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("current_network", lambda *a, **k: dict(NET)), ("fingerprint", lambda: ("192.168.1.1", "en0", "aa:bb:cc:dd:ee:ff"))):
            p = mock.patch.object(fn, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.addCleanup(self.srv.close)
        self.port = self.srv.getsockname()[1]
        self.reg = fd.Registry(self.dir / "devices.json")
        self.routes = []
        self.link = fl.Link(self.reg, env_alias=None, mux_base=["ssh", "-o", "BatchMode=yes", "-o", "ControlPath=x"],
                            control="x", apply=lambda alias, opts: self.routes.append((alias, list(opts))),
                            explain=explain)
        self.addCleanup(self.link.stop)

    def test_start_routes_to_the_saved_headset_before_serving(self):
        a = self.reg.add_device("frame", hosts=["192.0.2.1"])
        b = self.reg.add_device("frame-2", hosts=["192.0.2.2"])
        self.reg.set_active(b["id"])
        with mock.patch.object(self.link, "run", lambda: None):  # no connector: only what start() applies
            self.link.start()
        self.assertEqual(self.routes[0][0], "frame-2")
        self.assertIn("HostName=192.0.2.2", self.routes[0][1])
        self.assertNotEqual(a["id"], b["id"])

    def test_headset_removed_elsewhere_mid_install_reaches_nothing(self):
        a = self.reg.add_device("frame", hosts=["192.0.2.1"])
        self.reg.add_device("frame-2", hosts=["192.0.2.2"])
        self.reg.set_active(a["id"])
        other = fd.Registry(self.dir / "devices.json")  # another Frame Control server
        other.remove_device(a["id"])
        self.link.work = lambda: 1
        self.assertTrue(self.link.active_device().get("none"))
        self.link.work = lambda: 0
        self.assertEqual(self.link.active_device()["alias"], "frame-2")  # idle: move on

    def test_nothing_elsewhere_moves_a_running_install(self):
        """A bare alias in use, then another server sets up and picks a headset."""
        self.link.override = None
        self.hosts({"localhost": "ok"})
        with mock.patch.object(fl, "ssh_g", return_value=("localhost", self.port, "tester", False)):
            self.link.connect(["start"])
        started = self.routes[-1]
        other = fd.Registry(self.dir / "devices.json")
        other.set_active(other.add_device("frame-2", hosts=["192.0.2.2"])["id"])
        self.link.work = lambda: 1
        with mock.patch.object(fl, "ssh_g", return_value=("localhost", self.port, "tester", False)):
            self.link.close_master()
            self.link.connect(["dropped"])
        self.assertEqual(self.routes[-1][0], started[0])
        self.assertTrue(self.link.deferred)

    def listen6(self):
        """A "different device": the same port on IPv6 loopback."""
        try:
            six = socket.socket(socket.AF_INET6)
            self.addCleanup(six.close)
            six.bind(("::1", self.port))
            six.listen(4)
        except OSError:
            self.skipTest("no IPv6 loopback")

    def pin(self, device_id):
        fd.known_hosts(device_id).parent.mkdir(parents=True, exist_ok=True)
        fd.known_hosts(device_id).write_text(f"frame-control-{device_id} ssh-ed25519 AAAA\n")

    def hosts(self, mapping):
        # An IPv4 answer is what ssh is pointed at, so localhost arrives as 127.0.0.1.
        if "localhost" in mapping:
            mapping = dict({"127.0.0.1": mapping["localhost"]}, **mapping)
        os.environ["FAKESSH_HOSTS"] = json.dumps(mapping)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def device(self, *hosts):
        d = self.reg.add_device("frame-t", port=self.port, hosts=[])
        for h in hosts:
            self.reg.add_address(d["id"], h, kind="lan")
        return d

    def test_falls_through_to_the_address_that_is_really_the_headset(self):
        # Tried in this order: a name that doesn't resolve, a different device, the headset.
        self.listen6()
        d = self.device("nothing.invalid", "::1", "127.0.0.1")
        self.hosts({"::1": "wrong", "127.0.0.1": "ok"})
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertEqual(s["via"]["host"], "127.0.0.1")
        self.assertEqual([st["state"] for st in s["stages"]], ["done"] * 5)
        rows = {p["host"]: p for p in s["probes"]}
        self.assertEqual(rows["nothing.invalid"]["state"], "unresolved")
        self.assertEqual(rows["::1"]["state"], "sshfailed")
        # Its row says why in a few words: the banner's headline already has the whole sentence (#87).
        self.assertEqual(rows["::1"]["detail"], "SSH identity changed")
        # Every ssh command was pointed at the winner, with the host key pinned per device.
        alias, opts = self.routes[-1]
        self.assertEqual(alias, "frame-t")
        self.assertIn("HostName=127.0.0.1", opts)
        self.assertIn(f"HostKeyAlias=frame-control-{d['id']}", opts)
        self.assertIn(f"Port={self.port}", opts)
        master = [c for c in self.calls() if "ControlMaster=yes" in c][-1]
        self.assertIn("StrictHostKeyChecking=accept-new", master)  # first connection: nothing pinned yet
        self.assertNotIn("StrictHostKeyChecking=yes", master)
        # This stand-in ssh saved no key, so commands may still save the first one (accept-new);
        # test_a_first_connection_pins_and_then_checks_strictly has real pins.
        self.assertIn("StrictHostKeyChecking=accept-new", opts)
        self.assertTrue(fd.known_hosts(d["id"]).parent.is_dir())  # where ssh saves the key it accepts
        # It learned: 127.0.0.1 works on this network.
        learned = {a["host"]: a for a in self.reg.get(d["id"])["addresses"]}
        self.assertEqual(learned["127.0.0.1"]["networks"], ["n-test"])
        self.assertEqual(learned["::1"]["networks"], [])
        self.assertTrue(self.link.alive())
        self.link.close_master()
        self.assertFalse(any(p.name.startswith("master-") for p in self.dir.iterdir()))

    def test_stages_are_published_as_they_happen(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        steps, versions = [], []
        real = self.link.stage

        def stage(sid, state, detail=None):
            real(sid, state, detail)
            steps.append((sid, state))
            versions.append(self.link.snapshot()["version"])
        self.link.stage = stage
        before = self.link.snapshot()["version"]
        self.assertIsNone(self.link.wait(before, 0.05))  # nothing new yet
        self.link.connect(["start"])
        started = [sid for sid, state in steps if state == "active"]
        self.assertEqual(list(dict.fromkeys(started)), ["network", "find", "ssh", "identity", "login"])
        self.assertEqual([sid for sid, state in steps if state == "done"][-3:], ["ssh", "identity", "login"])
        self.assertEqual(versions, sorted(versions))  # every step is a new version for the page
        self.assertEqual(self.link.wait(before, 1)["phase"], "connected")

    def test_nothing_answers(self):
        self.device("nothing.invalid", "also-nothing.invalid")
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "failed")
        self.assertEqual(s["error"]["stage"], "find")
        self.assertEqual(s["error"]["message"], "Can't find the Frame on the network.")
        self.assertGreater(s["retry_at"], time.time())
        self.assertEqual([st["state"] for st in s["stages"]][:2], ["done", "failed"])

    def test_refused_key_stops_at_login(self):
        self.device("localhost")
        self.hosts({"localhost": "denied"})
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["error"]["stage"]), ("failed", "login"))
        self.assertIn("SSH key", s["error"]["message"])
        self.assertEqual(s["error"]["auth"], "denied")  # the page offers Set Up Connection
        self.assertEqual(s["error"]["short"], "This computer's key refused")  # the header pill's words (#87)
        self.assertEqual(s["probes"][0]["detail"], "This computer's key refused")

    def test_pinned_identity_is_checked_strictly(self):
        d = self.device("localhost")
        self.pin(d["id"])
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        master = [c for c in self.calls() if "ControlMaster=yes" in c][-1]
        self.assertIn("StrictHostKeyChecking=yes", master)

    def pins(self):
        """The stand-in ssh checks host keys as OpenSSH does (tests/fakessh/ssh)."""
        p = mock.patch.dict(os.environ, {"FAKESSH_PINS": "1"})
        p.start()
        self.addCleanup(p.stop)

    def command(self, device, host="127.0.0.1"):
        """One ssh command as the server runs it, with the route the connector applied."""
        return subprocess.run(["ssh", *self.link.host_opts(device, host), device["alias"], "true"],
                              capture_output=True, text=True, timeout=20)

    def test_a_first_connection_pins_and_then_checks_strictly(self):
        # 0.4.2 on Windows: "No ED25519 host key is known for frame-control-<id> and you have
        # requested strict checking." Every command said "yes" before anything was saved, so one
        # that ran before the connector's handshake saved the key (or without it) failed.
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        r = self.command(d)
        self.assertEqual(r.returncode, 0, r.stderr)  # the first contact saves the key...
        self.assertIn("Permanently added", r.stderr)
        self.assertTrue(fd.pinned(d["id"]))
        self.assertIn("StrictHostKeyChecking=yes", self.link.host_opts(d, "127.0.0.1"))  # ...then it's checked
        self.assertEqual(self.reg.get(d["id"])["identity"], [k[1] for k in fd.pinned_keys(d["id"])])  # and recorded
        # The connector's first handshake, for another new headset.
        self.reg.remove_device(d["id"])
        d = self.device("localhost")
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertTrue(fd.pinned(d["id"]))
        self.assertTrue(self.reg.get(d["id"])["identity"])
        alias, opts = self.routes[-1]
        self.assertIn("StrictHostKeyChecking=yes", opts)  # every command after it checks strictly
        self.assertNotIn("StrictHostKeyChecking=accept-new", opts)

    def lose_pin(self, device_id, how):
        f = fd.known_hosts(device_id)
        if how == "deleted":
            f.unlink()
        elif how == "empty":
            f.write_text("")
        elif how == "unreadable":
            f.chmod(0)
            self.addCleanup(lambda: f.exists() and f.chmod(0o600))
        elif how == "another alias":
            f.write_text(f.read_text().replace(f"frame-control-{device_id}", "frame-control-someone-else"))
        elif how == "not utf-8":
            f.write_bytes(b"\xff\xfe" + f.read_bytes())

    def test_a_lost_or_unusable_pin_fails_closed(self):
        # Review pr85-review-r1: accept-new can't tell a new headset from one whose saved key
        # went missing; real OpenSSH then trusted a replacement key. Once a headset had an
        # identity, nothing is trusted again until the person checks it.
        self.pins()
        cases = ["deleted", "empty", "another alias", "not utf-8"]
        if os.name == "posix" and os.geteuid() != 0:
            cases.append("unreadable")  # root reads anything
        for n, how in enumerate(cases):
            with self.subTest(how=how):
                self.hosts({"localhost": "ok"})
                d = self.reg.add_device(f"frame-l{n}", port=self.port, hosts=[])
                self.reg.add_address(d["id"], "localhost", kind="lan")
                self.link.override = None
                self.reg.set_active(d["id"])
                self.link.connect(["switch"])
                self.assertEqual(self.link.snapshot()["phase"], "connected")
                self.link.close_master()
                d = self.reg.get(d["id"])
                self.lose_pin(d["id"], how)
                self.hosts({"localhost": "wrong"})  # another device, or a reinstalled headset
                r = self.command(d)
                self.assertEqual(r.returncode, 255, r.stderr)
                self.assertIn("Host key verification failed", r.stderr)
                self.assertNotIn("Permanently added", r.stderr)
                self.link.connect(["dropped"])
                s = self.link.snapshot()
                self.assertEqual((s["phase"], s["error"]["stage"]), ("failed", "identity"))
                self.assertEqual(s["error"]["hostkey"]["state"], "missing")
                self.assertIn("missing or can't be read", s["error"]["message"])
                self.assertEqual(s["error"]["short"], "Saved SSH identity missing")
                view = next(x for x in fl.devices_view(self.link)["devices"] if x["id"] == d["id"])
                self.assertTrue(view["identity_lost"])
                scan = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)["scan"]
                self.assertFalse(scan["matches"] or scan["recorded_match"])  # not the key it had
                self.hosts({"localhost": "ok"})
                scan = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)["scan"]
                self.assertTrue(scan["recorded_match"])  # its own key, as recorded
                self.assertFalse(scan["matches"])  # but not usable from the file: trusting restores it
                fl.devices_action(self.link, {"action": "identity-trust", "id": d["id"],
                                              "fingerprint": scan["fingerprint"]}, None)
                self.link.connect(["switch"])
                self.assertEqual(self.link.snapshot()["phase"], "connected", self.link.snapshot()["error"])
                self.link.close_master()

    def test_a_key_check_only_reads_the_key(self):
        # Review pr85-review-r1: with ForwardAgent yes in ~/.ssh/config, a device that accepts
        # "none" got the agent and ran a command before anyone trusted it.
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "evil"})
        os.environ["FAKESSH_CONFIG"] = json.dumps({"forwardagent": "yes", "remotecommand": "touch /tmp/x",
                                                   "permitlocalcommand": "yes", "requesttty": "force"})
        self.addCleanup(os.environ.pop, "FAKESSH_CONFIG", None)
        for proxied in (False, True):
            route = ("jump", "me@jumpy:2222") if proxied else (None, None)
            with self.subTest(proxied=proxied), mock.patch.object(fl, "proxy_route", return_value=route):
                t0 = time.monotonic()
                scan = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)["scan"]
                self.assertLess(time.monotonic() - t0, 10)  # stopped once it had the key, not left open
                self.assertTrue(scan["fingerprint"].startswith("SHA256:"))
                self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith("evil-")], [])
                call = [c for c in self.calls() if "PreferredAuthentications=none" in c][-1]
                self.assertEqual(call[call.index("-F") + 1], "none")  # never ~/.ssh/config for the device
                self.assertIn("-N", call)
                hop = [c for c in call if c.startswith("ProxyCommand=")]
                if proxied:
                    # Review pr85-review-r2: ProxyJump's own ssh inherits the config (ForwardAgent,
                    # LocalCommand); the scan builds the jump host's ssh itself, hardened.
                    self.assertEqual(len(hop), 1)
                    words = shlex.split(hop[0][len("ProxyCommand="):])
                    self.assertEqual(words[-7:], ["-l", "me", "-p", "2222", "-W", "[%h]:%p", "jumpy"])
                    for o in fl.Link.HOP_OPTS:
                        self.assertIn(o, words)
                    self.assertLess(call.index(hop[0]), call.index("HostKeyAlias=frame-control-" + d["id"]))
                else:
                    self.assertEqual(hop, [])
                for o in ("ForwardAgent=no", "IdentityAgent=none", "ClearAllForwardings=yes", "RemoteCommand=none",
                          "PermitLocalCommand=no", "RequestTTY=no", "PubkeyAuthentication=no"):
                    self.assertIn(o, call)
                self.assertLess(call.index("StrictHostKeyChecking=accept-new"), call.index("HostKeyAlias=frame-control-"
                                                                                          + d["id"]))
        self.assertFalse(fd.pinned(d["id"]))
        self.assertEqual(sorted(p.name for p in fd.known_hosts(d["id"]).parent.iterdir()), [])  # no scan files

    @unittest.skipIf(os.name != "posix", "the stand-in ssh runs ProxyCommand with /bin/sh")
    def test_the_jump_hosts_command_line_is_quoted_whatever_the_names(self):
        # Review pr85-review-r3: the hop's words went into the ProxyCommand line unquoted, so an
        # ssh at /tmp/x/bin$(:>INJECTED)/ssh ran shell code; and an IPv6 jump host broke.
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        for n, name in enumerate(["with space", "bin$(touch INJECTED-0)", "back`touch INJECTED-1`tick",
                                  "quote'it\"s", "per%cent %h %%", "semi;touch INJECTED-2"]):
            with self.subTest(name=name):
                bindir = self.dir / f"b{n}" / name
                bindir.mkdir(parents=True)
                (bindir / "ssh").symlink_to(FAKESSH / "ssh")
                for spec, at in (("me@jumpy:2222", "jumpy"), ("[::1]:2200", "::1")):
                    before = len(self.calls())
                    with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}), \
                            mock.patch.object(fl, "proxy_route", return_value=("jump", spec)):
                        scan = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)["scan"]
                    self.assertTrue(scan["fingerprint"].startswith("SHA256:"))
                    hops = [c for c in self.calls()[before:] if "-W" in c]
                    self.assertEqual(len(hops), 1, self.calls()[before:])
                    self.assertEqual(hops[0][-1], at)  # the jump host, bare
                    self.assertEqual(hops[0][hops[0].index("-W") + 1], "[localhost]:%d" % self.port)
                    outer = [c for c in self.calls()[before:] if "PreferredAuthentications=none" in c][-1]
                    line = next(c for c in outer if c.startswith("ProxyCommand="))
                    self.assertTrue(line[len("ProxyCommand="):].startswith(shlex.quote(str(bindir / "ssh")
                                                                                       .replace("%", "%%"))))
                self.assertEqual([p.name for p in self.dir.rglob("INJECTED*")], [])
                self.assertFalse(list(Path.cwd().glob("INJECTED*")))

    def test_a_key_check_refuses_routes_it_cant_harden(self):
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        before = len(self.calls())
        for route in (("command", "nc %h %p"), ("jump", "a,b"), ("jump", "-oProxyCommand=x"), ("jump", "ssh://a"),
                      ("jump", "a b"), ("jump", "a%h"), ("jump", "jumpy:99999")):
            with self.subTest(route=route), mock.patch.object(fl, "proxy_route", return_value=route):
                with self.assertRaises(fd.DeviceError) as caught:
                    fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)
                self.assertIn("can't make safe", str(caught.exception))
        self.assertFalse(any("PreferredAuthentications=none" in c for c in self.calls()[before:]))

    def test_a_key_read_before_the_address_or_port_changed_isnt_trusted(self):
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        for change in ({"action": "update", "id": d["id"], "port": self.port + 1},
                       {"action": "address-remove", "id": d["id"], "host": "localhost"}):
            with self.subTest(change=change["action"]):
                scan = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)["scan"]
                fl.devices_action(self.link, change, None)
                with self.assertRaises(fd.DeviceError):
                    fl.devices_action(self.link, {"action": "identity-trust", "id": d["id"],
                                                  "fingerprint": scan["fingerprint"]}, None)
                self.assertFalse(fd.pinned(d["id"]))
                self.reg.update_device(d["id"], port=self.port)
                if not self.reg.get(d["id"])["addresses"]:
                    self.reg.add_address(d["id"], "localhost", kind="lan")

    def test_terminals_judge_identity_by_the_registry_not_a_frozen_route(self):
        # Review pr85-review-r2: named_route used the device as routed (before its first key was
        # recorded), so a Terminal window fell back to accept-new once the pin was deleted.
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        self.assertEqual(self.link.snapshot()["phase"], "connected")
        self.assertFalse(self.link.routed_device.get("identity"))  # the frozen copy knows nothing of it
        fd.known_hosts(d["id"]).unlink()
        alias, opts = self.link.named_route()
        self.assertIn("StrictHostKeyChecking=yes", opts)
        self.assertNotIn("StrictHostKeyChecking=accept-new", opts)
        self.hosts({"localhost": "wrong"})
        r = subprocess.run(["ssh", *opts, alias, "true"], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 255, r.stderr)
        self.assertFalse(fd.pinned(d["id"]))

    def test_a_key_saved_before_a_refused_login_is_kept_and_checked(self):
        # Review pr85-review-r2: ssh saves the key during the key exchange; if the login is then
        # refused, it was neither recorded nor protected, and the route stayed accept-new.
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "denied"})
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["error"]["stage"]), ("failed", "login"))
        got = self.reg.get(d["id"])
        self.assertEqual(got["identity"], [f for _, f in fd.pinned_keys(d["id"])])
        self.assertTrue(got["identity_established"])
        if os.name == "posix":
            self.assertEqual(fd.known_hosts(d["id"]).stat().st_mode & 0o777, 0o600)
        self.assertIn("StrictHostKeyChecking=yes", self.routes[-1][1])
        fd.known_hosts(d["id"]).unlink()
        self.hosts({"localhost": "wrong"})
        r = self.command(self.reg.get(d["id"]))
        self.assertEqual(r.returncode, 255)
        self.assertNotIn("Permanently added", r.stderr)

    def test_a_key_a_command_saved_tightens_the_route(self):
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "denied"})
        with self.link.route_lock:
            self.link.routed_device = d
            self.link.apply(d["alias"], self.link.host_opts(d, "127.0.0.1"))
        opts = self.routes[-1][1]
        self.assertIn("StrictHostKeyChecking=accept-new", opts)
        r = subprocess.run(["ssh", *opts, d["alias"], "true"], capture_output=True, text=True, timeout=20)
        self.assertIn("Permanently added", r.stderr)
        self.assertEqual(r.returncode, 255)  # saved the key, then refused the login
        self.link.settle_pin()  # what the connector's loop does every tick while accept-new is applied
        self.assertIn("StrictHostKeyChecking=yes", self.routes[-1][1])
        self.assertNotIn("StrictHostKeyChecking=accept-new", self.routes[-1][1])
        self.assertTrue(self.reg.get(d["id"])["identity"])

    def test_editing_the_address_that_worked_keeps_the_identity_history(self):
        # Review pr85-review-r2: last_ok lives on the address, so editing or removing it made a
        # headset whose pin was gone (and had no fingerprint recorded) new again.
        path = self.dir / "legacy.json"
        path.write_text(json.dumps({"version": 1, "active": "abcd1234", "networks": {}, "devices": [
            {"id": "abcd1234", "name": "Old", "alias": "frame-old", "user": "steamos", "port": 22,
             "identity_files": [], "addresses": [{"host": "10.0.0.5", "kind": "lan", "label": "",
                                                  "networks": [], "last_ok": 1790000000.0, "last_rtt_ms": 3}]}]}))
        reg = fd.Registry(path)
        self.assertTrue(reg.get("abcd1234")["identity_established"])  # migrated from last_ok
        reg.update_address("abcd1234", "10.0.0.5", new_host="10.0.0.6")
        reg.remove_address("abcd1234", "10.0.0.6")
        reg.add_address("abcd1234", "10.0.0.7")
        d = fd.Registry(path).get("abcd1234")  # saved, too
        self.assertFalse(any(a.get("last_ok") for a in d["addresses"]))
        self.assertTrue(fd.identity_established(d))
        self.assertEqual(self.link.strictness(d), "yes")
        # A connection that works sets it as well, for a headset added since.
        n = self.reg.add_device("frame-new", hosts=["10.0.0.8"])
        self.reg.record_success(n["id"], "10.0.0.8", None, 1)
        self.reg.remove_address(n["id"], "10.0.0.8")
        self.assertTrue(self.reg.get(n["id"])["identity_established"])

    def test_ssh_last_words_arrive_even_when_the_reader_lags(self):
        # Review pr85-review-r2: ssh's exit with nothing queued yet was taken as the end, and a
        # late reader lost "Permission denied" ("ssh stopped").
        self.pins()
        link = fl.Link(self.reg, env_alias=None, mux_base=["ssh", "-o", "BatchMode=yes"], control=None,
                       apply=lambda *a: None, explain=explain)
        self.addCleanup(link.stop)
        real = threading.Thread

        class Late(real):
            def __init__(self, *a, target=None, **kw):
                if target is not None and getattr(target, "__name__", "") == "tail":
                    inner = target

                    def target():
                        time.sleep(1)
                        inner()
                super().__init__(*a, target=target, **kw)
        self.device("localhost")
        self.hosts({"localhost": "denied"})
        with mock.patch.object(fl.threading, "Thread", Late):
            link.connect(["start"])
        s = link.snapshot()
        self.assertEqual((s["error"]["stage"], s["error"].get("auth")), ("login", "denied"), s["error"])
        self.assertIn("SSH key", s["error"]["message"])

    def test_forgetting_an_identity_is_refused(self):
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        saved = fd.known_hosts(d["id"]).read_text()
        with self.assertRaises(fd.DeviceError) as caught:
            fl.devices_action(self.link, {"action": "forget-identity", "id": d["id"]}, None)
        self.assertIn("identity-scan", str(caught.exception))
        self.assertEqual(fd.known_hosts(d["id"]).read_text(), saved)

    def test_without_a_shared_connection_ssh_writes_to_a_file_not_a_pipe(self):
        # Windows: no ControlMaster, and its OpenSSH can block writing to a stderr pipe.
        self.pins()
        tmp = self.dir / "tmp"
        tmp.mkdir()
        link = fl.Link(self.reg, env_alias=None, mux_base=["ssh", "-o", "BatchMode=yes"], control=None,
                       apply=lambda *a: None, explain=explain)
        self.addCleanup(link.stop)
        real = subprocess.Popen
        seen = []

        def popen(argv, **kw):
            if "-v" in argv:  # the handshake (run_ssh calls go through Popen too)
                seen.append(kw.get("stderr"))
            return real(argv, **kw)
        with mock.patch.object(tempfile, "tempdir", str(tmp)), mock.patch.object(fl.subprocess, "Popen", popen):
            d = self.device("localhost")
            self.hosts({"localhost": "ok"})
            link.connect(["start"])
            s = link.snapshot()
            self.assertEqual(s["phase"], "connected", s["error"])
            self.assertEqual([st["state"] for st in s["stages"]], ["done"] * 5)
            self.assertIn("saved this headset's identity", next(st for st in s["stages"] if st["id"] == "identity")["detail"])
            self.reg.update_device(d["id"], user="nobody")
            self.hosts({"localhost": "denied"})
            link.connect(["switch"])
            s = link.snapshot()
            self.assertEqual((s["phase"], s["error"]["stage"], s["error"].get("auth")), ("failed", "login", "denied"))
        self.assertTrue(seen and all(isinstance(x, int) for x in seen))  # a file descriptor, never PIPE
        self.assertNotIn(subprocess.PIPE, seen)
        for _ in range(50):  # the reader removes its file once ssh has exited
            if not list(tmp.iterdir()):
                break
            time.sleep(0.05)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_a_changed_key_is_shown_never_accepted_and_trusted_on_request(self):
        # 0.4.2 on Linux and Windows: "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", with
        # "Run Set Up Connection again" as the advice, which can't replace the saved key.
        self.pins()
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        self.assertEqual(self.link.snapshot()["phase"], "connected")
        saved = fd.known_hosts(d["id"]).read_text()
        old = fd.pinned_keys(d["id"])
        self.link.close_master()
        self.hosts({"localhost": "wrong"})  # SteamOS reinstalled: a new host key
        self.link.connect(["dropped"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["error"]["stage"]), ("failed", "identity"))
        shown = s["error"]["hostkey"]
        self.assertEqual((shown["state"], shown["type"], shown["host"]), ("changed", "ssh-ed25519", "localhost"))
        self.assertTrue(shown["fingerprint"].startswith("SHA256:"))
        self.assertIn("Devices tab", s["error"]["message"])
        self.assertEqual(s["error"]["short"], "SSH identity changed")
        self.assertEqual(fd.known_hosts(d["id"]).read_text(), saved)  # never accepted on its own
        r = self.command(d)
        self.assertEqual(r.returncode, 255)
        self.assertIn("REMOTE HOST IDENTIFICATION HAS CHANGED", r.stderr)  # nor by any command
        # Trusting needs the key read first, and exactly the fingerprint shown.
        with self.assertRaises(fd.DeviceError):
            fl.devices_action(self.link, {"action": "identity-trust", "id": d["id"],
                                          "fingerprint": shown["fingerprint"]}, None)
        out = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)
        scan = out["scan"]
        self.assertEqual(scan["fingerprint"], shown["fingerprint"])
        self.assertFalse(scan["matches"])
        self.assertEqual(scan["saved"], [{"type": t, "fingerprint": f} for t, f in old])
        self.assertEqual(fd.known_hosts(d["id"]).read_text(), saved)  # reading it saves nothing
        self.assertEqual(sorted(p.name for p in fd.known_hosts(d["id"]).parent.iterdir()), [d["id"]])  # no scan files
        call = [c for c in self.calls() if "PreferredAuthentications=none" in c][-1]  # no login offered
        self.assertLess(call.index("StrictHostKeyChecking=accept-new"), call.index("StrictHostKeyChecking=yes"))
        self.assertNotIn(f"UserKnownHostsFile={fd.known_hosts_opt(d['id'])}",
                         call[:call.index("StrictHostKeyChecking=yes")])  # a throwaway file, first
        with self.assertRaises(fd.DeviceError):
            fl.devices_action(self.link, {"action": "identity-trust", "id": d["id"], "fingerprint": "SHA256:other"}, None)
        self.link.scans[d["id"]]["at"] -= fl.SCAN_TTL + 1
        with self.assertRaises(fd.DeviceError):  # shown too long ago
            fl.devices_action(self.link, {"action": "identity-trust", "id": d["id"],
                                          "fingerprint": shown["fingerprint"]}, None)
        fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)
        with mock.patch.object(self.link, "kick") as kick:
            out = fl.devices_action(self.link, {"action": "identity-trust", "id": d["id"],
                                                "fingerprint": shown["fingerprint"]}, None)
        kick.assert_called_with("switch")
        self.assertEqual(fd.pinned_keys(d["id"]), [("ssh-ed25519", shown["fingerprint"])])  # the old key is gone
        self.assertEqual(next(x for x in out["devices"] if x["id"] == d["id"])["keys"],
                         [{"type": "ssh-ed25519", "fingerprint": shown["fingerprint"]}])
        self.link.connect(["switch"])
        self.assertEqual(self.link.snapshot()["phase"], "connected", self.link.snapshot()["error"])
        out = fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)
        self.assertTrue(out["scan"]["matches"])

    def test_test_now_never_saves_a_key(self):
        self.pins()
        d = self.device("127.0.0.1")
        self.hosts({"127.0.0.1": "ok"})
        self.link.test(d["id"])
        rows = self.link.snapshot()["tests"][d["id"]]["rows"]
        self.assertEqual(rows[0]["ssh"], "unpinned")
        self.assertFalse(fd.pinned(d["id"]))

    def test_reading_a_key_from_a_headset_that_doesnt_answer_says_why(self):
        self.pins()
        d = self.device("nothing.invalid")
        with self.assertRaises(fd.DeviceError) as caught:
            fl.devices_action(self.link, {"action": "identity-scan", "id": d["id"]}, None)
        self.assertIn("Can't find the Frame", str(caught.exception))
        self.assertEqual([p.name for p in fd.known_hosts(d["id"]).parent.iterdir()], [])

    def test_ssh_goes_to_the_ipv4_address_that_answered(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["via"]["host"], s["via"]["ip"]), ("connected", "localhost", "127.0.0.1"))
        self.assertIn("HostName=127.0.0.1", self.routes[-1][1])

    def test_no_headset_after_removing_them_all(self):
        d = self.device("localhost")
        self.reg.remove_device(d["id"])
        self.link.connect(["switch"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["device"]["id"], s["retry_at"]), ("failed", "none", None))
        self.assertIn("No headset", s["error"]["message"])
        self.assertEqual(self.routes[-1], ("frame-control-no-headset", ["-o", "HostName=no-headset.invalid"]))

    def test_a_headset_without_addresses_reaches_nothing(self):
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        fl.devices_action(self.link, {"action": "address-remove", "id": d["id"], "host": "localhost"}, None)
        self.assertIn("HostName=no-address.invalid", self.routes[-1][1])  # at once, not after a retry
        self.link.connect(["switch"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["retry_at"]), ("failed", None))
        self.assertIn("no addresses", s["error"]["message"])

    def test_switching_back_to_the_frame_alias_the_server_started_with(self):
        self.link.override = self.link.session_alias = "frame-bare"
        other = self.device("localhost")
        ids = [d["id"] for d in fl.devices_view(self.link)["devices"]]
        self.assertEqual(ids, ["alias-frame-bare", other["id"]])
        fl.devices_action(self.link, {"action": "use", "id": other["id"]}, None)
        self.assertIn("alias-frame-bare", [d["id"] for d in fl.devices_view(self.link)["devices"]])  # still there
        fl.devices_action(self.link, {"action": "use", "id": "alias-frame-bare"}, None)
        self.assertEqual(self.link.active_device()["alias"], "frame-bare")
        self.assertEqual(self.routes[-1][0], "frame-bare")

    def test_removing_the_headset_frame_alias_named_doesnt_bring_it_back_bare(self):
        d = self.device("localhost")
        self.link.override = self.link.session_alias = "frame-t"
        fl.devices_action(self.link, {"action": "remove", "id": d["id"], "config": True}, None)
        self.assertNotIn("alias-frame-t", [x["id"] for x in fl.devices_view(self.link)["devices"]])

    def test_setup_changing_the_login_waits_for_installs(self):
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        cfg = self.dir / "ssh" / "config"
        cfg.write_text("# >>> steam-frame (frame-t) >>>\nHost frame-t\n  HostName localhost\n  User steamos\n"
                       f"  Port {self.port}\nHost *\n# <<< steam-frame (frame-t) <<<\n")
        self.link.watch_config()  # the block's login is recorded
        routes = len(self.routes)
        cfg.write_text(cfg.read_text().replace("User steamos", "User deck"))
        running = [1]
        self.link.work = lambda: running[0]
        self.link.config_mtime = None
        self.link.watch_config()
        self.assertEqual(len(self.routes), routes)  # an install is running: not yet
        self.link.connect(["dropped"])  # a reconnect meanwhile keeps the login it started with
        self.assertIn("User=steamos", self.routes[-1][1])
        running[0] = 0
        self.link.watch_config()
        self.assertIn("User=deck", self.routes[-1][1])

    def test_a_rename_leaves_the_login_in_the_config_alone(self):
        d = self.device("localhost")
        cfg = self.dir / "ssh" / "config"
        cfg.write_text("# >>> steam-frame (frame-t) >>>\nHost frame-t\n  HostName localhost\n  User deck\n"
                       "Host *\n# <<< steam-frame (frame-t) <<<\n")  # setup wrote a new user, not yet imported
        fl.devices_action(self.link, {"action": "update", "id": d["id"], "name": "Desk"}, None)
        self.assertIn("User deck", cfg.read_text())
        out = fl.devices_action(self.link, {"action": "update", "id": d["id"], "port": 2200}, None)
        self.assertIn("User deck", cfg.read_text())  # changed meanwhile: left as it is
        self.assertIn("left as it is", out["message"])

    def test_a_late_failure_from_the_last_headset_is_ignored(self):
        self.link.state["phase"] = "connected"
        self.link.gen = 3
        self.link.lost("ssh: connect to host a port 22: Operation timed out", 2)  # sent before the switch
        self.assertEqual(self.link.kicks, [])
        self.link.lost("ssh: connect to host b port 22: Operation timed out", 3)
        self.assertEqual(len(self.link.kicks), 1)

    def test_a_jump_hosts_login_isnt_the_headsets(self):
        opts = ["-o", "HostName=10.0.0.5"]
        self.assertFalse(fl.Link.is_target('Authenticated to bastion ([1.2.3.4]:22) using "publickey".', opts, "frame"))
        self.assertTrue(fl.Link.is_target('Authenticated to 10.0.0.5 ([10.0.0.5]:22) using "publickey".', opts, "frame"))
        self.assertTrue(fl.Link.is_target('Authenticated to frame.local ([10.0.0.5]:22) using "publickey".',
                                          ["-o", "HostName=FRAME.LOCAL"], "frame"))

    def test_a_forward_the_jump_host_couldnt_open_tries_the_next_address(self):
        said = ["Authenticated to bastion ([1.2.3.4]:22) using \"publickey\".",
                "channel 0: open failed: connect failed: Connection refused", "stdio forwarding failed"]
        self.link.state["stages"] = [{"id": i, "state": "pending", "started": None, "ended": None, "detail": ""}
                                     for i, _ in fl.STAGES]
        self.assertEqual(self.link.failed("login", said, False, "frame"), "next")
        self.assertEqual(self.link.failed("login", ["steamos@frame: Permission denied (publickey)."], False, "frame"),
                         "stop")

    def test_a_reconnect_being_started_isnt_a_live_connection(self):
        self.link.state["phase"] = "connected"
        self.assertTrue(self.link.alive())
        self.link.busy = True  # the loop took a Retry off the queue and is about to reconnect
        self.assertFalse(self.link.alive())  # so ensure() waits instead of starting work on it

    def test_probes_from_an_earlier_attempt_leave_the_new_rows_alone(self):
        self.link.state.update(attempt=2, probes=[{"host": "b", "state": "waiting"}])
        self.link.probe_update(0, 1, state="answered", ip="10.0.0.2")
        self.assertEqual(self.link.state["probes"][0], {"host": "b", "state": "waiting"})

    def test_a_bare_alias_lets_ssh_config_decide(self):
        self.link.override = "frame-bare"
        self.hosts({"localhost": "ok"})
        with mock.patch.object(fl, "ssh_g", return_value=("localhost", self.port, "tester", False)):
            self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertTrue(s["device"]["transient"])
        # Where ~/.ssh/config sends it, pinned down for the connection (ssh's own known_hosts).
        alias, opts = self.routes[-1]
        self.assertEqual((alias, opts[2:]), ("frame-bare", ["-o", "HostName=localhost", "-o", f"Port={self.port}",
                                                            "-o", "User=tester"]))
        self.assertEqual(opts[:2], ["-o", "ControlPath=" + fl.frame_host.control_path(fl.Link.control_tag(s["device"]))])

    def test_each_headset_has_its_own_shared_connection(self):
        """ssh's %C hashes only address, user and port: two headsets at one address
        (one moved) must still never share a ControlMaster."""
        a, b = (dict(fl.Link.bare("x"), id=i, transient=False, user="steamos", port=22) for i in ("aaaa1111", "bbbb2222"))
        pa, pb = (next(o for o in self.link.host_opts(d, "192.0.2.5") if o.startswith("ControlPath=")) for d in (a, b))
        self.assertNotEqual(pa, pb)
        self.assertIn("aaaa1111", pa)
        long_alias = fl.Link.bare("x" * 64)
        path = next(o for o in self.link.host_opts(long_alias, None) if o.startswith("ControlPath="))
        self.assertLess(len(path) - len("ControlPath=") - len("%C") + 40 + 17, 104)  # fits a macOS socket path

    def test_a_bare_alias_keeps_its_pinned_route_for_reconnects_and_terminals(self):
        self.link.override = "frame-bare"
        self.hosts({"localhost": "ok"})
        with mock.patch.object(fl, "ssh_g", return_value=("localhost", self.port, "tester", False)):
            self.link.connect(["start"])
        pinned = self.routes[-1][1]
        # ~/.ssh/config now sends the alias elsewhere, but an install is running.
        self.link.work = lambda: 1
        with mock.patch.object(fl, "ssh_g", return_value=("elsewhere.invalid", 2222, "other", False)):
            self.link.close_master()
            self.link.connect(["dropped"])
            self.assertEqual(self.link.snapshot()["probes"][0]["host"], "localhost")  # probed where commands go
            self.assertEqual(self.link.snapshot()["phase"], "connected")
            self.assertEqual(self.link.named_route(), ("frame-bare", pinned))  # terminals go there too

    def test_a_set_up_headset_behind_a_jump_host_is_left_to_ssh(self):
        d = self.device("10.99.99.98", "10.99.99.99")  # neither answers directly
        self.hosts({"10.99.99.98": "wrong", "10.99.99.99": "ok"})
        with mock.patch.object(fl, "ssh_g", return_value=("frame-t", 22, "steamos", True)):
            self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["via"]["host"]), ("connected", "10.99.99.99"), s["error"])
        self.assertIn(f"HostKeyAlias=frame-control-{d['id']}", self.routes[-1][1])  # still pinned per headset

    def test_a_bare_alias_behind_a_jump_host_is_left_to_ssh(self):
        self.link.override = "frame-jump"
        self.hosts({"10.99.99.99": "ok"})  # ssh's ProxyJump would get there
        with mock.patch.object(fl, "ssh_g", return_value=("10.99.99.99", 22, "tester", True)):
            self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertEqual(s["via"]["why"], "through a jump host")

    def test_changing_the_port_reroutes_even_if_the_attempt_fails(self):
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        self.reg.update_device(d["id"], port=1)  # nothing listens there
        self.link.connect(["switch"])
        self.assertEqual(self.link.snapshot()["phase"], "failed")
        self.assertIn("Port=1", self.routes[-1][1])

    def test_test_now_checks_every_address_without_touching_the_connection(self):
        self.listen6()
        d = self.device("::1", "127.0.0.1", "nothing.invalid")
        self.pin(d["id"])
        self.hosts({"::1": "wrong", "127.0.0.1": "ok"})
        self.link.test(d["id"])
        rows = {r["host"]: r for r in self.link.snapshot()["tests"][d["id"]]["rows"]}
        self.assertEqual(rows["127.0.0.1"]["ssh"], "ok")
        self.assertEqual(rows["::1"]["ssh"], "wrong")
        self.assertEqual(rows["nothing.invalid"]["state"], "unresolved")
        self.assertEqual(self.routes, [])
        self.assertTrue(all("ControlPath=none" in c for c in self.calls() if "-G" not in c))

    def test_switching_to_a_headset_that_never_answers_stops_using_the_last_one(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        other = self.reg.add_device("frame-other", port=self.port)
        self.reg.add_address(other["id"], "nothing.invalid")
        self.link.use(other["id"])
        self.assertEqual(self.routes[-1][0], "frame-other")  # at once, before any attempt
        self.assertEqual(self.link.snapshot()["phase"], "connecting")
        self.assertFalse(self.link.alive())  # so ensure() waits instead of using the old master
        self.link.connect(["switch"])
        self.assertEqual(self.link.snapshot()["phase"], "failed")
        alias, opts = self.routes[-1]
        self.assertEqual(alias, "frame-other")
        self.assertIn("HostName=nothing.invalid", opts)
        self.assertIn(f"HostKeyAlias=frame-control-{other['id']}", opts)

    def test_an_attempt_overtaken_by_a_switch_routes_nothing_back(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        other = self.reg.add_device("frame-other", port=self.port)
        self.reg.add_address(other["id"], "nothing.invalid")
        pick = fl.Link.pick

        def switch_then_pick(*args):
            if not getattr(self, "switched", False):
                self.switched = True
                self.link.use(other["id"])  # the user switches while A is being found
            return pick(*args)
        with mock.patch.object(fl.Link, "pick", staticmethod(switch_then_pick)):
            self.link.connect(["start"])
        self.assertEqual(self.routes[-1][0], "frame-other")
        self.assertEqual(self.link.snapshot()["phase"], "connecting")
        self.assertFalse(self.link.alive())
        self.assertIsNone(self.link.master)

    def test_no_switching_while_something_is_installing(self):
        d = self.device("localhost")
        other = self.reg.add_device("frame-other")
        for body in ({"action": "use", "id": other["id"]}, {"action": "remove", "id": d["id"]},
                     {"action": "update", "id": d["id"], "port": 2222},
                     {"action": "address-remove", "id": d["id"], "host": "localhost"},
                     {"action": "address-update", "id": d["id"], "host": "localhost", "newHost": "127.0.0.1"},
                     {"action": "identity-trust", "id": d["id"], "fingerprint": "SHA256:x"}):
            with self.assertRaises(fd.DeviceError, msg=body):
                fl.devices_action(self.link, body, open_setup=None, busy=lambda: 1)
        # Renaming, or changing another headset, is fine.
        fl.devices_action(self.link, {"action": "update", "id": d["id"], "name": "Desk"}, None, busy=lambda: 1)
        fl.devices_action(self.link, {"action": "update", "id": other["id"], "port": 2222}, None, busy=lambda: 1)
        self.assertEqual(self.reg.get(d["id"])["name"], "Desk")

    def test_no_reconnecting_under_a_running_install(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        with self.assertRaises(fd.DeviceError):
            fl.devices_action(self.link, {"action": "retry"}, None, busy=lambda: 1)
        fl.devices_action(self.link, {"action": "retry"}, None)  # fine when nothing runs

    def test_terminals_get_the_address_by_name(self):
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        alias, opts = self.link.named_route()
        self.assertEqual(alias, "frame-t")
        self.assertIn("HostName=localhost", opts)
        self.assertIn(f"HostKeyAlias=frame-control-{d['id']}", opts)

    def test_renaming_during_an_install_is_fine(self):
        d = self.device("localhost")
        # The page sends the user and port along with the name, unchanged.
        fl.devices_action(self.link, {"action": "update", "id": d["id"], "name": "Desk", "user": "steamos",
                                      "port": str(self.port)}, None, busy=lambda: 1)
        self.assertEqual(self.reg.get(d["id"])["name"], "Desk")

    def test_a_rename_shows_at_once(self):
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        fl.devices_action(self.link, {"action": "update", "id": d["id"], "name": "Desk"}, None)
        self.assertEqual(self.link.snapshot()["device"]["name"], "Desk")

    def test_stopping_mid_handshake_leaves_no_ssh_behind(self):
        self.device("localhost")
        self.hosts({"localhost": "slow"})
        t = threading.Thread(target=self.link.connect, args=(["start"],), daemon=True)
        t.start()
        for _ in range(100):
            if self.link.pending:
                break
            time.sleep(0.05)
        proc = self.link.pending
        self.assertIsNotNone(proc)
        self.link.stop()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.link.master)

    def test_test_now_goes_through_a_jump_host(self):
        d = self.device("10.99.99.99")
        self.pin(d["id"])
        self.hosts({"10.99.99.99": "ok"})
        with mock.patch.object(fl, "ssh_g", return_value=("frame-t", 22, "steamos", True)):
            self.link.test(d["id"])
        row = self.link.snapshot()["tests"][d["id"]]["rows"][0]
        self.assertEqual(row["ssh"], "ok", row)
        self.assertIn("jump host", row["detail"])

    def test_devices_api_checks_everything(self):
        d = self.device("localhost")
        bad = [{"action": "address-add", "id": d["id"], "host": "-oProxyCommand=touch /tmp/x"},
               {"action": "address-add", "id": d["id"], "host": "a\nHost *"},
               {"action": "address-add", "id": d["id"], "host": "frame.local", "kind": "wifi"},
               {"action": "update", "id": d["id"], "user": "root; id"},
               {"action": "update", "id": d["id"], "port": 0},
               {"action": "address-move", "id": d["id"], "host": "localhost", "delta": 5},
               {"action": "setup", "alias": "-F/etc/passwd"},
               {"action": "setup", "alias": "frame-9", "host": "$(id)"},
               {"action": "use", "id": "nope"},
               {"action": "explode"}]
        for body in bad:
            with self.assertRaises(fd.DeviceError, msg=body):
                fl.devices_action(self.link, body, open_setup=lambda *a: self.fail("setup ran"))
        # Removing every headset leaves none in use, rather than falling back to the `frame` alias.
        spare = self.reg.add_device("frame-spare")
        fl.devices_action(self.link, {"action": "remove", "id": spare["id"]}, None)
        # The only headset, whose ssh alias would stay: not without removing that too.
        (self.dir / "ssh" / "config").write_text("# >>> steam-frame (frame-t) >>>\nHost frame-t\n  HostName localhost\n"
                                                "Host *\n# <<< steam-frame (frame-t) <<<\n")
        with self.assertRaises(fd.DeviceError):
            fl.devices_action(self.link, {"action": "remove", "id": d["id"]}, None)
        opened = []
        out = fl.devices_action(self.link, {"action": "setup", "alias": "frame-9", "host": "192.168.1.50"},
                                open_setup=lambda alias, host: opened.append((alias, host)) or "a terminal")
        self.assertEqual(opened, [("frame-9", "192.168.1.50")])
        self.assertIn("frame-9", out["message"])
        self.assertEqual(out["active"], d["id"])
        self.assertEqual(fl.next_alias(self.link), "frame")
        (self.dir / "ssh" / "config").write_text("Host frame lab-*\n  HostName 10.0.0.7\n")  # someone's own `frame`
        self.assertEqual(fl.next_alias(self.link), "frame-2")

    # ---- what a failure leaves for a problem report ----
    def stderr(self):
        return quiet_log(self)

    def report(self):
        import frame_report as fr
        done = threading.Thread(target=lambda: None)
        done.start()
        done.join()
        with mock.patch.object(fr, "link", self.link), \
                mock.patch.dict(fr._ssh, {"thread": done, "line": "SSH: system OpenSSH, OpenSSH 9.9p1, LibreSSL 3.3.6"}):
            return fr.diagnostics()

    def test_each_failure_goes_to_the_server_log_scrubbed(self):
        err = self.stderr()
        self.device("steamdeck-jane.invalid", "localhost")
        self.hosts({"localhost": "denied"})
        self.link.connect(["start"])
        line = err.getvalue()
        self.assertIn("frame_link: login failed: The Frame didn't accept this computer's SSH key.", line)
        self.assertIn("addresses: hostname unresolved; hostname->ipv4", line)
        for leaked in ("steamdeck-jane", "127.0.0.1", "localhost"):
            self.assertNotIn(leaked, line)
        self.assertEqual(self.link.last_failure["stage"], "login")

    def test_reports_carry_a_connection_summary_in_fixed_words(self):
        self.stderr()
        self.device("steamdeck-jane.invalid", "frame-t.invalid")
        (self.dir / "ssh" / "config").write_text(
            f"{fd.begin_mark('frame-t')}\nHost frame-t\n  HostName 192.168.1.50\n  User steamos\n{fd.end_mark('frame-t')}\n"
            "Host frame-t\n  HostName 10.0.0.7\n")
        self.link.connect(["start"])
        text = self.report()
        self.assertIn("Connection: failed at find, attempt 1 (Starting up)", text)
        self.assertIn("Headsets: 1 saved; active alias custom (saved, 2 address(es))", text)
        self.assertIn("Error: find, frame_not_set_up", text)
        self.assertRegex(text, r"Last failure: find, frame_not_set_up, 0 min \d+ s ago")
        self.assertIn("Addresses tried: hostname unresolved; hostname unresolved", text)
        self.assertIn("Network: gateway yes, Tailscale not installed", text)
        self.assertIn("SSH: system OpenSSH, OpenSSH 9.9p1", text)
        self.assertIn("~/.ssh/config: managed block for the active alias yes (1 managed in all); "
                      "hand-written Host for it yes", text)
        # No free text from the error or ssh at all, and not the custom alias.
        for leaked in ("steamdeck-jane", "Could not resolve", "Can't find", "192.168", "10.0.0.7", "steamos",
                       "frame-t", str(self.dir)):
            self.assertNotIn(leaked, text)

    def test_a_failure_on_the_last_headset_leaves_nothing_of_it_after_switching(self):
        self.stderr()
        a = self.device("steamdeck-jane.invalid")
        self.link.connect(["start"])
        b = self.reg.add_device("frame-2", port=self.port, hosts=[])
        self.reg.add_address(b["id"], "localhost", kind="lan")
        self.hosts({"localhost": "ok"})
        self.reg.set_active(b["id"])
        self.link.connect(["switch"])
        self.assertEqual(self.link.snapshot()["phase"], "connected")
        text = self.report()
        self.assertIn("Connection: connected via hostname->ipv4", text)
        self.assertRegex(text, r"Last failure: find, frame_not_set_up, ")
        self.assertNotIn("steamdeck-jane", text)
        self.assertNotEqual(a["id"], b["id"])

    def test_the_headsets_alias_and_name_stay_out_of_the_log(self):
        import frame_report as fr
        err = self.stderr()
        d = self.reg.add_device("jane-office.example.com", name="Jane Doe's work headset", hosts=[])
        self.reg.set_active(d["id"])
        self.link.connect(["start"])
        self.assertIn("find failed: The active headset has no addresses.", err.getvalue())
        # A message that names it anyway (any case) goes too, in the log and so in a report's log.
        self.link.note_failure("ssh", "JANE-OFFICE.EXAMPLE.COM: jane doe's work headset stopped answering")
        log = self.dir / "server.log"
        log.write_text(err.getvalue())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": str(log)}):
            with mock.patch.object(fr, "link", self.link), mock.patch.dict(fr._ssh, {"thread": None, "line": "SSH: x"}):
                text = fr.diagnostics(include_logs=True)
        self.assertIn("frame_link: ssh failed", text)
        for leaked in ("jane", "Jane", "JANE"):
            self.assertNotIn(leaked, err.getvalue())
            self.assertNotIn(leaked, text)


def quiet_log(test):
    """Catch frame_link's log lines, starting with nothing remembered."""
    fl._logged.clear()
    err = io.StringIO()
    p = mock.patch.object(sys, "stderr", err)
    p.start()
    test.addCleanup(p.stop)
    return err


class FailureLog(unittest.TestCase):
    def test_the_same_failure_isnt_logged_every_retry(self):
        err = quiet_log(self)
        for _ in range(3):
            fl.log_failure("find", "The Frame isn't answering.", "", [{"host": "frame.local", "state": "timeout"}])
        self.assertEqual(err.getvalue().count("frame_link:"), 1)
        for v in fl._logged.values():
            v["at"] -= fl.LOG_REPEAT_EVERY + 1
        fl.log_failure("find", "The Frame isn't answering.", "", [{"host": "frame.local", "state": "timeout"}])
        self.assertIn("(and 2 more times)", err.getvalue())
        self.assertNotIn("frame.local", err.getvalue())

    def test_interleaved_failures_are_throttled_too(self):
        err = quiet_log(self)
        for _ in range(4):
            fl.log_failure("find", "The Frame isn't answering.")
            fl.log_failure("login", "The Frame didn't accept this computer's SSH key.")
        self.assertEqual(err.getvalue().count("frame_link:"), 2)
        for i in range(fl.LOG_REMEMBER + 10):  # many different failures: memory stays bounded
            fl.log_failure("ssh", f"failure number {i}")
        self.assertLessEqual(len(fl._logged), fl.LOG_REMEMBER)

    def test_hosts_ssh_names_go_known_or_not_and_whatever_the_case(self):
        err = quiet_log(self)
        fl.log_failure("ssh", "ssh stopped", "ssh: Could not resolve hostname bastion-jane: Name or service not known")
        fl.log_failure("ssh", "ssh stopped", "channel 0: open failed: connect to host jane-office.example.com port 22")
        fl.log_failure("ssh", "ssh stopped", "kex_exchange_identification: Connection reset by steamdeck-jane",
                       hosts=["STEAMDECK-JANE"])
        fl.log_failure("ssh", "Timed out talking to frame-jane", "")
        out = err.getvalue()
        self.assertIn("hostname <host>", out)
        self.assertIn("connect to host <host> port 22", out)
        for leaked in ("bastion-jane", "jane-office", "steamdeck-jane", "frame-jane"):
            self.assertNotIn(leaked, out)

    def test_headsets_named_like_sshs_words_dont_shield_the_real_host(self):
        err = quiet_log(self)
        for saved in ("host", "hostname", "to"):
            fl.log_failure("ssh", "ssh stopped", "ssh: connect to host bastion-jane port 22: Connection refused",
                           hosts=[saved])
            fl.log_failure("ssh", "ssh stopped", "ssh: Could not resolve hostname jane-office.example.com: not known",
                           hosts=[saved])
        out = err.getvalue()
        self.assertIn("connect to host <host> port 22", out)
        self.assertIn("hostname <host>", out)
        for leaked in ("bastion-jane", "jane-office"):
            self.assertNotIn(leaked, out)
        self.assertEqual(fl.hide_hosts("<host> and frame", ["host", "frame"]), "<host> and <host>")

    def test_whole_names_go_before_sshs_words_and_only_real_tokens_are_spared(self):
        for name, said in (("Jane Doe's work headset", "Timed out talking to Jane Doe's work headset"),
                           ("Jane to Doe headset", "Jane to Doe headset stopped answering"),
                           ("<Jane Doe>", "<Jane Doe> stopped answering")):
            out = fl.scrub_failure(said, [name])
            self.assertTrue(out.startswith("<host>") or out == "Timed out talking to <host>", out)
            for leaked in ("Jane", "Doe"):
                self.assertNotIn(leaked, out)
        self.assertEqual(fl.scrub_failure("Timed out talking to Jane Doe's work headset", ["Jane Doe's work headset"]),
                         "Timed out talking to <host>")
        self.assertEqual(fl.hide_hosts("<user>@<host>: <ip>", ["user", "host", "ip"]), "<user>@<host>: <ip>")

    def test_windows_user_names_with_spaces_go_whole(self):
        err = quiet_log(self)
        with mock.patch.object(fl.frame_telemetry, "_user_names", return_value=set()):
            fl.log_failure("ssh", r"Bad owner or permissions on C:\Users\Jane Doe/.ssh/config", "")
            fl.log_failure("login", "The Frame didn't accept this computer's SSH key.",
                           "Jane Doe@frame: Permission denied (publickey).")
            fl.log_failure("login", "refused", r"debug | ssh said: CORP\jane@frame's password: denied")
        out = err.getvalue()
        self.assertIn(r"C:\Users\<user>/.ssh/config", out)
        self.assertIn("<user>@<host>: Permission denied", out)
        for leaked in ("Jane", "Doe", "jane", "CORP"):
            self.assertNotIn(leaked, out)

    def test_whole_ssh_user_at_host_fields_and_home_folders_go(self):
        import frame_telemetry as tm
        with mock.patch.object(tm, "_user_names", return_value=set()):
            self.assertEqual(tm.scrub(r"CORP\Jane Doe@jane-office.example.com: Permission denied (publickey)."),
                             "<user>@<host>: Permission denied (publickey).")
            self.assertEqual(tm.scrub("jane@jane-office.example.com's password: "), "<user>@<host>'s password: ")
            self.assertEqual(tm.scrub(r"Bad owner on C:\Users\O'Brien/.ssh/config"), r"Bad owner on C:\Users\<user>/.ssh/config")
            self.assertEqual(tm.scrub(r"c:\users\Zoë Smith.Jr\.ssh\config"), r"c:\users\<user>\.ssh\config")
            self.assertEqual(tm.scrub("/users/O'Brien/x and /HOME/jane doe/y"), "/users/<user>/x and /HOME/<user>/y")
            self.assertEqual(tm.scrub("write to me@example.com today"), "write to <email> today")


class ConnectionDiagnostics(unittest.TestCase):
    def test_address_kinds_never_the_address(self):
        self.assertEqual(fl.address_kind("frame.local", "fe80::1%eth0"), ".local->ipv6 link-local")
        self.assertEqual(fl.address_kind("frame.local", "192.168.1.5"), ".local->ipv4")
        self.assertEqual(fl.address_kind("frame.local"), ".local")
        self.assertEqual(fl.address_kind("fe80::1%5"), "ipv6 link-local")
        self.assertEqual(fl.address_kind("2001:db8::1"), "ipv6")
        self.assertEqual(fl.address_kind("192.168.1.5", "192.168.1.5"), "ipv4")
        self.assertEqual(fl.address_kind("100.101.102.103"), "tailscale")
        self.assertEqual(fl.address_kind("frame.tail1234.ts.net", "100.101.102.103"), "tailscale")
        self.assertEqual(fl.address_kind("steamdeck"), "hostname")
        self.assertEqual(fl.probes_summary([{"host": "frame", "label": "from ~/.ssh/config", "state": "timeout"}]),
                         "alias hostname timeout")

    def test_hidden_hosts_leave_the_rest(self):
        self.assertEqual(fl.hide_hosts("frame_link: Could not resolve hostname frame", ["frame"]),
                         "frame_link: Could not resolve hostname <host>")
        self.assertEqual(fl.hide_hosts("ssh frame to frame.local", ["frame.local", "frame"], keep=("frame",)),
                         "ssh frame to <host>")
        self.assertEqual(fl.hide_hosts("reset by SteamDeck", ["steamdeck"]), "reset by <host>")
        self.assertEqual(fl.hide_operands("The headset took too long to answer."), "The headset took too long to answer.")

    def test_ssh_version_is_rebuilt_from_its_numbers(self):
        import frame_report as fr
        for said, want in (("OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2\n", "OpenSSH for Windows 9.5p1, LibreSSL 3.8.2"),
                           ("OpenSSH_9.9p1, LibreSSL 3.3.6\n", "OpenSSH 9.9p1, LibreSSL 3.3.6"),
                           ("OpenSSH_8.9p1 Ubuntu-3ubuntu0.10, OpenSSL 3.0.2 15 Mar 2022\n", "OpenSSH 8.9p1"),
                           ("OpenSSH_9.6p1, OpenSSL 3.0.13 30 Jan 2024\n", "OpenSSH 9.6p1, OpenSSL 3.0.13"),
                           ("OpenSSH_9.9p1-Jane-Doe-Laptop, LibreSSL 3.3.6\n", "unknown"),
                           ("OpenSSH_9.9p1, OpenSSL jane-laptop\n", "OpenSSH 9.9p1"),
                           (r"C:\Users\Jane\OpenSSH-portable\ssh.exe: not found", "unknown"),
                           ("", "unknown")):
            with mock.patch.object(fr.frame_host, "run_ssh", return_value=subprocess.CompletedProcess([], 0, "", said)):
                self.assertEqual(fr.ssh_version("ssh"), want, said)

    def test_only_the_default_alias_is_named(self):
        import frame_report as fr
        self.assertEqual(fr.alias_kind("frame"), 'default ("frame")')
        self.assertEqual(fr.alias_kind("jane-office.example.com"), "custom")

    def test_a_slow_path_never_holds_up_a_report(self):
        import frame_report as fr
        release = threading.Event()

        def slow():
            release.wait(5)
            return []
        with mock.patch.dict(fr._ssh, {"thread": None, "line": None}), mock.patch.object(fr, "_ssh_on_path", slow), \
                mock.patch.object(fr, "link", None), mock.patch.object(fr.shutil, "which", return_value="/usr/bin/ssh"), \
                mock.patch.object(fr.frame_host, "run_ssh",
                                  return_value=subprocess.CompletedProcess([], 0, "", "OpenSSH_9.9p1, LibreSSL 3.3.6\n")):
            t0 = time.monotonic()
            self.assertIn("Connection: no connector", fr.diagnostics())
            self.assertIn("SSH: still being checked", fr.ssh_line())
            self.assertLess(time.monotonic() - t0, 2)
            thread = fr._ssh["thread"]
            release.set()
            thread.join(5)
            self.assertTrue(fr._ssh["line"].startswith("SSH: "))
            self.assertNotEqual(fr.ssh_line(), "SSH: still being checked")

    def test_ssh_kinds(self):
        import frame_report as fr
        for path, kind in ((r"C:\WINDOWS\System32\OpenSSH\ssh.exe", "Windows OpenSSH (System32)"),
                           (r"C:\Program Files\OpenSSH\ssh.exe", "OpenSSH in Program Files"),
                           (r"C:\Program Files\Git\usr\bin\ssh.exe", "Git for Windows"),
                           ("/usr/bin/ssh", "system OpenSSH"), ("/opt/homebrew/bin/ssh", "Homebrew or /usr/local"),
                           ("/home/jane/bin/ssh", "other"), (None, "not found")):
            self.assertEqual(fr.ssh_path_kind(path), kind)

    def test_no_connector_says_so(self):
        import frame_report as fr
        with mock.patch.object(fr, "link", None):
            self.assertIn("Connection: no connector", fr.diagnostics())


@unittest.skipIf(os.name == "nt", "the stand-in ssh is a POSIX script")
class ServerConnection(unittest.TestCase):
    """The real server, a Set Up Connection block in a stand-in ~/.ssh, and the stand-in ssh."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="frame-link-server-"))
        ssh_dir = cls.dir / "ssh"
        ssh_dir.mkdir()
        cls.srv = socket.socket()  # the "headset's" port 22
        cls.srv.bind(("127.0.0.1", 0))
        cls.srv.listen(16)
        (ssh_dir / "config").write_text("# >>> steam-frame (frame) >>>\nHost frame\n  HostName localhost\n"
                                        f"  Port {cls.srv.getsockname()[1]}\n"
                                        "  User steamos\nHost *\n# <<< steam-frame (frame) <<<\n")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "FRAME_CONTROL_SSH_DIR": str(ssh_dir),
               "FRAME_CONTROL_DATA_DIR": str(cls.dir / "data"), "FAKESSH_LOG": str(cls.dir / "calls.jsonl"),
               "FAKESSH_DIR": str(cls.dir), "FAKESSH_HOSTS": json.dumps({"localhost": "ok", "127.0.0.1": "ok"}),
               "PATH": f"{FAKESSH}{os.pathsep}{os.environ['PATH']}"}
        env.pop("FRAME_ALIAS", None)
        cls.log = tempfile.TemporaryFile()
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "ui" / "server.py"), "--port", "0"], env=env,
                                    stdout=subprocess.PIPE, stderr=cls.log, text=True)
        cls.port = int(cls.proc.stdout.readline().split("127.0.0.1:")[1].split()[0])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=15)
        cls.proc.stdout.close()
        cls.log.close()
        cls.srv.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def request(self, method, path, body=None, key="1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"X-Frame-UI": key, "Content-Type": "application/json"})
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def wait_connected(self):
        for _ in range(100):
            status, s = self.request("GET", "/api/connection")
            if s.get("phase") in ("connected", "failed"):
                return s
            time.sleep(0.1)
        self.fail(f"never connected: {s}")

    def test_imports_the_headset_and_connects_through_its_port(self):
        s = self.wait_connected()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertEqual(s["via"]["host"], "localhost")
        self.assertEqual(s["device"]["alias"], "frame")
        self.assertEqual(s["device"]["name"], "Steam Frame")
        self.assertEqual(s["probes"][0]["host"], "localhost")
        status, devices = self.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertEqual([d["alias"] for d in devices["devices"]], ["frame"])
        self.assertEqual(devices["nextAlias"], "frame-2")

    def test_events_stream_the_state(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", "/api/connection/events", headers={"X-Frame-UI": "1"})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        line = r.fp.readline()
        self.assertTrue(line.startswith(b"data: "), line)
        self.assertIn("stages", json.loads(line[6:]))
        conn.close()

    def test_changes_meant_for_another_headset_are_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", "/api/launch", body=b'{"appid": "620"}',
                     headers={"X-Frame-UI": "1", "Content-Type": "application/json", "X-Frame-Device": "someoneelse"})
        r = conn.getresponse()
        self.assertEqual(r.status, 409)
        self.assertIn("switched headsets", json.loads(r.read())["error"])
        conn.close()

    def test_guards_and_validation(self):
        self.assertEqual(self.request("GET", "/api/connection", key="")[0], 403)
        self.assertEqual(self.request("GET", "/api/devices", key="nope")[0], 403)
        self.assertEqual(self.request("POST", "/api/devices", {"action": "address-add", "id": "x", "host": "a;b"})[0], 400)
        self.assertEqual(self.request("POST", "/api/devices", {"action": "setup", "alias": "-oProxyCommand=x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/devices", {"action": "nope"})[0], 400)


if __name__ == "__main__":
    unittest.main()
