"""frame_devices: the headset registry, importing ~/.ssh/config, address order, pinned
host keys and input checks. Everything works in temporary folders.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_devices as fd  # noqa: E402
import frame_host  # noqa: E402

CONFIG = """Host lxso1
  HostName 192.168.1.109

# >>> steam-frame (frame) >>>
Host frame
  HostName frame.tail1234.ts.net
  User steamos
  IdentityFile ~/.ssh/id_ed25519_frame
  IdentityFile ~/.ssh/id_rsa_frame_devkit
  IdentitiesOnly yes
  ServerAliveInterval 30
Host *
# <<< steam-frame (frame) <<<
# >>> steam-frame (frame-2) >>>
Host frame-2
  HostName 192.168.1.60
  Port 2222
  User deck
  IdentityFile ~/.ssh/id_ed25519_frame
Host *
# <<< steam-frame (frame-2) <<<
Host *
  ServerAliveInterval 60
"""
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIID6kdLfZZmdTqS1snKfTESTKEYTESTKEYTESTKEYTESTKE"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="frame-devices-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ssh = self.dir / "ssh"
        self.ssh.mkdir()
        (self.ssh / "config").write_text(CONFIG)
        old = os.environ.get("FRAME_CONTROL_SSH_DIR")
        os.environ["FRAME_CONTROL_SSH_DIR"] = str(self.ssh)
        self.addCleanup(lambda: os.environ.__setitem__("FRAME_CONTROL_SSH_DIR", old) if old
                        else os.environ.pop("FRAME_CONTROL_SSH_DIR", None))
        self.reg = fd.Registry(self.dir / "devices.json")


class Validation(unittest.TestCase):
    def test_hosts(self):
        for good in ("frame.local", "192.168.1.40", "fd7a:115c:a1e0::1234:5678", "fe80::1%en0", "frame-2.tail1234.ts.net"):
            self.assertEqual(fd.check_host(good), good)
        for bad in ("", " ", "-oProxyCommand=sh", "a b", "frame;id", "frame\nHost *", "frame..local", "$(id)",
                    "frame%en0", "x" * 300, None, 5, "frame/../x"):
            with self.assertRaises(fd.DeviceError, msg=repr(bad)):
                fd.check_host(bad)

    def test_names(self):
        self.assertEqual(fd.check_alias("frame-2"), "frame-2")
        for bad in ("", "-F", "frame 2", "frame\n", "a" * 65, None):
            with self.assertRaises(fd.DeviceError):
                fd.check_alias(bad)
            with self.assertRaises(fd.DeviceError):
                fd.check_user(bad)
        for bad in ("0", "65536", "x", None, "22; id"):
            with self.assertRaises(fd.DeviceError):
                fd.check_port(bad)
        self.assertEqual(fd.check_port("2222"), 2222)
        with self.assertRaises(fd.DeviceError):
            fd.check_text("line\nbreak", "label")
        with self.assertRaises(fd.DeviceError):
            fd.check_kind("wifi")

    def test_ipv6_zone_is_escaped_for_ssh(self):
        self.assertEqual(fd.ssh_host("fe80::1%en0"), "fe80::1%%en0")


class Migration(Base):
    def test_blocks_are_parsed(self):
        blocks = fd.parse_blocks(CONFIG)
        self.assertEqual([b["alias"] for b in blocks], ["frame", "frame-2"])
        self.assertEqual(blocks[0]["hostname"], "frame.tail1234.ts.net")
        self.assertEqual(blocks[0]["identity_files"], ["~/.ssh/id_ed25519_frame", "~/.ssh/id_rsa_frame_devkit"])
        self.assertEqual((blocks[1]["port"], blocks[1]["user"]), (2222, "deck"))

    def test_existing_headsets_are_imported_once(self):
        self.assertTrue(self.reg.sync_from_config(seed=False))
        devices = self.reg.devices()
        self.assertEqual([d["alias"] for d in devices], ["frame", "frame-2"])
        frame, second = devices
        self.assertEqual(frame["name"], "Steam Frame")
        self.assertEqual(frame["addresses"][0]["host"], "frame.tail1234.ts.net")
        self.assertEqual(frame["addresses"][0]["kind"], "tailscale")
        self.assertEqual((second["user"], second["port"]), ("deck", 2222))
        self.assertEqual(self.reg.active(), frame["id"])
        self.assertFalse(self.reg.sync_from_config(seed=False))  # nothing new
        # It's all on disk, in the documented shape.
        data = json.loads((self.dir / "devices.json").read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["devices"]), 2)
        self.assertEqual(fd.Registry(self.dir / "devices.json").devices(), self.reg.devices())

    def test_first_import_keeps_using_frame(self):
        # Set Up Connection puts each new block first; the app used `frame` before.
        blocks = CONFIG.split("# >>> steam-frame (frame-2) >>>")
        head, first = blocks[0].split("# >>> steam-frame (frame) >>>")
        second, tail = blocks[1].split("# <<< steam-frame (frame-2) <<<")
        (self.ssh / "config").write_text(head + "# >>> steam-frame (frame-2) >>>" + second + "# <<< steam-frame (frame-2) <<<\n"
                                         + "# >>> steam-frame (frame) >>>" + first + tail)
        self.reg.sync_from_config(seed=False)
        self.assertEqual([d["alias"] for d in self.reg.devices()], ["frame-2", "frame"])
        self.assertEqual(self.reg.active(), self.reg.by_alias("frame")["id"])

    @unittest.skipUnless(shutil.which("ssh"), "needs ssh")
    def test_a_port_inherited_from_another_host_entry_is_kept(self):
        (self.ssh / "config").write_text(CONFIG.replace("Host *\n  ServerAliveInterval 60", "Host *\n  Port 2222"))
        self.reg.sync_from_config(seed=False)
        self.assertEqual(self.reg.by_alias("frame")["port"], 2222)  # what ssh itself would use
        self.assertEqual(self.reg.by_alias("frame-2")["port"], 2222)  # its own Port line
        # Saving 22 must then say so in the block, or ssh would go on inheriting 2222.
        self.assertTrue(fd.rewrite_block("frame", port=22))
        self.assertEqual(fd.effective_port("frame", self.ssh / "config"), 22)
        self.assertFalse(fd.rewrite_block("frame", port=22))  # and only once

    def test_setup_finding_a_new_address_adds_it(self):
        self.reg.sync_from_config(seed=False)
        (self.ssh / "config").write_text(CONFIG.replace("HostName frame.tail1234.ts.net", "HostName 192.168.1.237"))
        self.assertTrue(self.reg.sync_from_config(seed=False))
        hosts = [a["host"] for a in self.reg.by_alias("frame")["addresses"]]
        self.assertEqual(hosts, ["192.168.1.237", "frame.tail1234.ts.net"])  # the new one first

    def test_setup_changing_the_login_updates_the_headset(self):
        self.reg.sync_from_config(seed=False)
        (self.ssh / "config").write_text(CONFIG.replace("  User steamos\n", "  User deck\n  Port 2200\n", 1))
        self.assertTrue(self.reg.sync_from_config(seed=False))
        d = self.reg.by_alias("frame")
        self.assertEqual((d["user"], d["port"]), ("deck", 2200))

    def test_removed_headset_stays_removed_until_setup_changes_it(self):
        self.reg.sync_from_config(seed=False)
        second = self.reg.by_alias("frame-2")
        self.reg.remove_device(second["id"])
        self.reg.sync_from_config(seed=False)
        self.assertIsNone(self.reg.by_alias("frame-2"))
        self.reg.undismiss("frame-2")  # Set Up Connection run for it from the Devices tab
        self.reg.sync_from_config(seed=False)
        self.assertIsNotNone(self.reg.by_alias("frame-2"))

    def test_corrupt_registry_is_ignored(self):
        (self.dir / "bad.json").write_text("{not json")
        self.assertEqual(fd.Registry(self.dir / "bad.json").devices(), [])
        (self.dir / "evil.json").write_text(json.dumps({"devices": [
            {"id": "x1", "alias": "-oProxyCommand=id", "addresses": []},
            {"id": "x2", "alias": "ok", "addresses": [{"host": "a b", "kind": "lan"}, {"host": "frame.local", "kind": "mdns"}]}]}))
        devices = fd.Registry(self.dir / "evil.json").devices()
        self.assertEqual([d["alias"] for d in devices], ["ok"])
        self.assertEqual([a["host"] for a in devices[0]["addresses"]], ["frame.local"])



class SharedFile(Base):
    """The desktop app and a standalone server can share devices.json."""

    def test_one_server_never_saves_over_anothers_change(self):
        self.reg.sync_from_config(seed=False)
        other = fd.Registry(self.dir / "devices.json")  # a second server, loaded now
        d = self.reg.by_alias("frame")
        self.reg.add_address(d["id"], "100.101.1.2", "tailscale")
        other.record_success(d["id"], d["addresses"][0]["host"], "net-1", 12)  # works from its older copy
        hosts = [a["host"] for a in fd.Registry(self.dir / "devices.json").get(d["id"])["addresses"]]
        self.assertIn("100.101.1.2", hosts)
        self.assertIn("100.101.1.2", [a["host"] for a in other.get(d["id"])["addresses"]])  # and it sees it

    def test_another_servers_choice_of_headset_doesnt_move_this_one(self):
        self.reg.sync_from_config(seed=False)
        other = fd.Registry(self.dir / "devices.json")
        mine, theirs = self.reg.by_alias("frame")["id"], self.reg.by_alias("frame-2")["id"]
        self.reg.set_active(mine)
        other.set_active(theirs)
        self.assertEqual(self.reg.active(), mine)  # after a refresh
        self.reg.add_address(mine, "192.0.2.9")  # and after a change reloads the file
        self.assertEqual(self.reg.active(), mine)
        self.assertEqual(fd.Registry(self.dir / "devices.json").active(), mine)  # last to save: next start

class ConfigRewrite(Base):
    def test_hostname_user_and_port_change_only_inside_the_block(self):
        cfg = self.ssh / "config"
        self.assertTrue(fd.rewrite_block("frame", hostname="192.168.1.237"))
        text = cfg.read_text()
        self.assertIn("  HostName 192.168.1.237\n", text)
        self.assertEqual(text.replace("192.168.1.237", "frame.tail1234.ts.net"), CONFIG)  # nothing else moved
        self.assertFalse(fd.rewrite_block("frame", hostname="192.168.1.237"))  # no change, no write
        self.assertTrue(fd.rewrite_block("frame", port=2200, user="deck"))
        block = fd.parse_blocks(cfg.read_text())[0]
        self.assertEqual((block["port"], block["user"], block["hostname"]), (2200, "deck", "192.168.1.237"))
        self.assertTrue(fd.rewrite_block("frame-2", port=22))  # back to the default: said explicitly
        self.assertEqual(fd.parse_blocks(cfg.read_text())[1]["port"], 22)
        self.assertIn("  Port 22\n", cfg.read_text())
        self.assertIn("HostName 192.168.1.109", cfg.read_text())  # other hosts untouched
        if os.name != "nt":
            self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)

    def test_concurrent_edits_all_land(self):
        import threading
        def edit(alias, prefix):
            for n in range(15):
                fd.rewrite_block(alias, hostname=f"{prefix}.{n}")
        threads = [threading.Thread(target=edit, args=("frame", "10.0.0")),
                   threading.Thread(target=edit, args=("frame-2", "10.0.1"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        blocks = fd.parse_blocks((self.ssh / "config").read_text())
        self.assertEqual([b["hostname"] for b in blocks], ["10.0.0.14", "10.0.1.14"])
        self.assertEqual([p.name for p in self.ssh.iterdir() if "frame-control." in p.name and not p.name.endswith(".lock")], [])  # no temp files left

    def test_learning_skips_a_block_someone_changed(self):
        # The connector learned an address, but Set Up Connection moved the block meanwhile.
        self.assertFalse(fd.rewrite_block("frame", hostname="10.0.0.9",
                                          expect={"hostname": "old.example", "user": None, "port": None}))
        self.assertIn("HostName frame.tail1234.ts.net", (self.ssh / "config").read_text())
        self.assertTrue(fd.rewrite_block("frame", hostname="10.0.0.9",
                                         expect={"hostname": "frame.tail1234.ts.net", "user": "steamos", "port": 22}))

    def test_zone_is_escaped_and_read_back(self):
        fd.rewrite_block("frame", hostname="fe80::1%en0")
        self.assertIn("HostName fe80::1%%en0", (self.ssh / "config").read_text())
        self.assertEqual(fd.parse_blocks((self.ssh / "config").read_text())[0]["hostname"], "fe80::1%en0")

    def test_missing_block_is_left_alone(self):
        self.assertFalse(fd.rewrite_block("frame-9", hostname="10.0.0.1"))
        self.assertFalse(fd.remove_block("frame-9"))
        self.assertTrue(fd.remove_block("frame-2"))
        self.assertEqual([b["alias"] for b in fd.parse_blocks((self.ssh / "config").read_text())], ["frame"])


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class Pins(Base):
    def test_seed_copies_the_trusted_key_under_the_device_alias(self):
        (self.ssh / "known_hosts").write_text(f"frame.tail1234.ts.net {KEY}\nother.example {KEY}X\n")
        self.assertFalse(fd.pinned("d1"))
        self.assertTrue(fd.seed_pin("d1", ["frame.tail1234.ts.net"]))
        self.assertTrue(fd.pinned("d1"))
        self.assertEqual(fd.known_hosts("d1").read_text(), f"frame-control-d1 {KEY}\n")
        self.assertTrue(fd.seed_pin("d1", ["frame.tail1234.ts.net"]))  # idempotent
        self.assertEqual(fd.known_hosts("d1").read_text().count("\n"), 1)
        self.assertFalse(fd.seed_pin("d2", ["never-seen.example"]))
        self.assertTrue(fd.forget_pin("d1"))
        self.assertFalse(fd.pinned("d1"))
        self.assertFalse(fd.forget_pin("d1"))

    def test_a_trusted_key_replaces_the_saved_one_and_fingerprints_match_ssh_keygen(self):
        key = self.dir / "hostkey"
        frame_host.run_ssh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], capture_output=True,
                           stdin=subprocess.DEVNULL, check=True, timeout=20)
        kind, blob = key.with_suffix(".pub").read_text().split()[:2]
        want = frame_host.run_ssh(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key.with_suffix(".pub"))],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10).stdout.split()[1]
        self.assertEqual(fd.fingerprint(blob), want)
        self.assertIsNone(fd.fingerprint("not base64!"))
        (self.ssh / "known_hosts").write_text(f"frame.local {KEY}\n")
        fd.seed_pin("d1", ["frame.local"])
        old = fd.pinned_keys("d1")
        fd.pin_key("d1", kind, blob)
        self.assertEqual(fd.known_hosts("d1").read_text(), f"frame-control-d1 {kind} {blob}\n")  # old key gone
        self.assertEqual(fd.pinned_keys("d1"), [(kind, want)])
        self.assertNotEqual(old, fd.pinned_keys("d1"))
        self.assertTrue(fd.pinned("d1"))
        for bad in (("ssh-ed25519\nHost *", blob), ("ssh-ed25519", "AAAA AAAA"), ("evil", blob)):
            with self.assertRaises(fd.DeviceError):
                fd.pin_key("d1", *bad)
        # Only plain entries for this headset's alias count.
        self.assertIsNone(fd.key_entry(f"frame-control-d2 {kind} {blob}", "frame-control-d1"))
        self.assertIsNone(fd.key_entry(f"# frame-control-d1 {kind} {blob}", "frame-control-d1"))

    def test_each_headset_has_its_own_file(self):
        (self.ssh / "known_hosts").write_text(f"a.local {KEY}\nb.local {KEY}\n")
        fd.seed_pin("da", ["a.local"])
        fd.seed_pin("db", ["b.local"])
        fd.forget_pin("da")
        self.assertTrue(fd.pinned("db"))  # forgetting one can't touch another
        self.assertNotEqual(fd.known_hosts("da"), fd.known_hosts("db"))

    def test_hashed_and_non_default_port_entries(self):
        kh = self.ssh / "known_hosts"
        kh.write_text(f"[frame.local]:2222 {KEY}\n")
        frame_host.run_ssh(["ssh-keygen", "-H", "-f", str(kh)], capture_output=True,
                           stdin=subprocess.DEVNULL, check=True, timeout=10)
        self.assertFalse(fd.seed_pin("d3", ["frame.local"]))  # port 22: not that entry
        self.assertTrue(fd.seed_pin("d3", ["frame.local"], port=2222))
        self.assertIn(f"frame-control-d3 {KEY}", fd.known_hosts("d3").read_text())

    def test_hashed_pins_are_found_and_forgotten(self):
        target = fd.known_hosts("d4")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"frame-control-d4 {KEY}\n")
        frame_host.run_ssh(["ssh-keygen", "-H", "-f", str(target)], capture_output=True,
                           stdin=subprocess.DEVNULL, check=True, timeout=10)
        self.assertNotIn("frame-control-d4", target.read_text())
        self.assertTrue(fd.pinned("d4"))
        self.assertTrue(fd.forget_pin("d4"))
        self.assertFalse(fd.pinned("d4"))

    def test_known_hosts_option_uses_the_override(self):
        self.assertEqual(fd.known_hosts_opt("d5"), str(self.ssh / "frame-control-hosts" / "d5"))
        os.environ.pop("FRAME_CONTROL_SSH_DIR")
        self.assertEqual(fd.known_hosts_opt("d5"), "~/.ssh/frame-control-hosts/d5")  # no spaces to split on


class Registry(Base):
    def test_address_editing(self):
        d = self.reg.add_device("frame-3", hosts=["192.168.1.40"])
        a = self.reg.add_address(d["id"], "frame-3.local", label="mDNS")
        self.assertEqual(a["kind"], "mdns")
        self.reg.add_address(d["id"], "100.100.1.1", kind="tailscale", label="Tailscale")
        with self.assertRaises(fd.DeviceError):
            self.reg.add_address(d["id"], "frame-3.local")  # already there
        with self.assertRaises(fd.DeviceError):
            self.reg.add_address(d["id"], "frame-3.local; id")
        self.reg.move_address(d["id"], "100.100.1.1", -1)
        self.reg.move_address(d["id"], "100.100.1.1", -1)
        self.reg.move_address(d["id"], "100.100.1.1", -1)  # already first: stays
        hosts = lambda: [x["host"] for x in self.reg.get(d["id"])["addresses"]]
        self.assertEqual(hosts(), ["100.100.1.1", "192.168.1.40", "frame-3.local"])
        self.reg.record_success(d["id"], "192.168.1.40", "n-home", 3.2)
        self.reg.update_address(d["id"], "192.168.1.40", label="Home")
        self.assertEqual(self.reg.get(d["id"])["addresses"][1]["networks"], ["n-home"])  # a label keeps what it learned
        with self.assertRaises(fd.DeviceError):
            self.reg.update_address(d["id"], "192.168.1.40", new_host="192.168.1.41", label="bad\nlabel")
        self.assertEqual(self.reg.get(d["id"])["addresses"][1]["networks"], ["n-home"])  # rejected: unchanged
        self.reg.update_address(d["id"], "192.168.1.40", new_host="192.168.1.41")
        moved = self.reg.get(d["id"])["addresses"][1]
        self.assertEqual((moved["host"], moved["networks"], moved["last_ok"]), ("192.168.1.41", [], None))
        self.reg.remove_address(d["id"], "192.168.1.41")
        self.assertEqual(hosts(), ["100.100.1.1", "frame-3.local"])
        with self.assertRaises(fd.DeviceError):
            self.reg.remove_address(d["id"], "nope")

    def test_devices(self):
        a = self.reg.add_device("frame")
        b = self.reg.add_device("frame-2", name="Office")
        self.assertEqual(self.reg.active(), a["id"])
        with self.assertRaises(fd.DeviceError):
            self.reg.add_device("frame")
        self.reg.set_active(b["id"])
        self.assertEqual(self.reg.update_device(b["id"], name="Desk", user="deck", port="2222")["port"], 2222)
        with self.assertRaises(fd.DeviceError):
            self.reg.update_device(b["id"], user="bad user")
        with self.assertRaises(fd.DeviceError):
            self.reg.update_device(b["id"], user="steam", port="bad")
        self.assertEqual(self.reg.get(b["id"])["user"], "deck")  # a rejected edit changes nothing
        self.reg.remove_device(b["id"])
        self.assertEqual(self.reg.active(), a["id"])
        self.assertFalse(self.reg.emptied())
        self.reg.remove_device(a["id"])
        self.assertTrue(self.reg.emptied())  # the connector then uses no headset at all
        self.reg.add_device("frame-4")
        self.assertFalse(self.reg.emptied())
        with self.assertRaises(fd.DeviceError):
            self.reg.get(b["id"])

    def test_networks_get_names(self):
        net = {"id": "n-1", "gateway": "192.168.1.1", "gateway_mac": "aa:bb:cc:dd:ee:ff", "ssid": None, "wifi": True}
        self.assertEqual(self.reg.network_name(net), "Wi-Fi via 192.168.1.1")
        self.reg.record_network(net)
        self.reg.name_network("n-1", "Home Wi-Fi")
        self.assertEqual(self.reg.network_name(net), "Home Wi-Fi")
        self.assertEqual(self.reg.network_name(dict(net, id="n-2", ssid="Cafe")), "Cafe")
        self.assertEqual(self.reg.network_name(None), "No network")
        with self.assertRaises(fd.DeviceError):
            self.reg.name_network("n-unknown", "x")


class Order(unittest.TestCase):
    def addr(self, host, kind, networks=()):
        return {"host": host, "kind": kind, "networks": list(networks)}

    def test_known_here_then_mdns_then_tailscale_then_the_rest(self):
        addrs = [self.addr("10.1.1.5", "lan", ["n-office"]), self.addr("192.168.1.40", "lan"),
                 self.addr("100.64.1.2", "tailscale"), self.addr("frame.local", "mdns"),
                 self.addr("192.168.1.237", "lan", ["n-home"])]
        order = [a["host"] for a, _ in fd.order_addresses(addrs, "n-home", True)]
        self.assertEqual(order, ["192.168.1.237", "frame.local", "100.64.1.2", "192.168.1.40", "10.1.1.5"])
        # Tailscale off: its addresses go last.
        order = [a["host"] for a, _ in fd.order_addresses(addrs, "n-home", False)]
        self.assertEqual(order[-1], "100.64.1.2")
        # On an unknown network nothing has worked yet; the user's order breaks ties.
        ranked = fd.order_addresses(addrs, None, True)
        self.assertEqual([a["host"] for a, _ in ranked], ["frame.local", "100.64.1.2", "192.168.1.40", "10.1.1.5", "192.168.1.237"])
        self.assertEqual(ranked[0][1], "mDNS name")


if __name__ == "__main__":
    unittest.main()


class RoutingLongLivedSsh(unittest.TestCase):
    """The keyboard agent and the Mac view keep their own ssh open: switching headset
    must end or retarget them, or input would go on reaching the old headset."""

    def test_switching_headset_stops_input_and_retargets_the_mac_view(self):
        import server
        from unittest import mock
        before = (server.FRAME, list(server.HOST_OPTS))
        self.addCleanup(lambda: server.route(*before))
        with mock.patch.object(server._input, "stop") as stop, \
                mock.patch.object(server.macview, "retarget") as retarget:
            server.route(server.FRAME, ["-o", "HostName=192.0.2.9"])  # same headset, new address
            stop.assert_not_called()
            server.route("frame-2", ["-o", "HostName=192.0.2.2"])
            stop.assert_called_once()
            retarget.assert_called_with("frame-2", ["-o", "HostName=192.0.2.2"])
