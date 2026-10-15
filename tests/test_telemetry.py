"""Anonymous analytics (ui/frame_telemetry.py): what's collected at each level,
what's scrubbed, and that nothing is sent without a key, the notice, or consent.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_compat_db as db  # noqa: E402
import frame_report as fr  # noqa: E402
import frame_host  # noqa: E402
import frame_telemetry as tm  # noqa: E402


def link_error(message, kind=RuntimeError):
    """An error as an ssh helper raises it when ssh couldn't reach the headset."""
    return frame_host.link_failure(kind(message))


class Base(unittest.TestCase):
    """A packaged build with a key, its state in a temp folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        for name, value in (("STATE", state), ("SETTINGS", state / "settings.json"),
                            ("OUTBOX", state / "outbox.jsonl"), ("SENT", state / "sent.jsonl")):
            p = mock.patch.object(tm, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_KEY": "phc_test", "FRAME_CONTROL_PACKAGED": "1",
                                           "FRAME_CONTROL_POSTHOG_HOST": "http://127.0.0.1:9",
                                           "FRAME_CONTROL_VERSION": "9.9.9"})
        env.start()
        self.addCleanup(env.stop)
        for k in ("DO_NOT_TRACK", "FRAME_CONTROL_TELEMETRY"):
            os.environ.pop(k, None)
        tm._seen_errors.clear()

    def queued(self):
        return tm._read_lines(tm.OUTBOX)


class Gates(Base):
    def test_blocked_without_key_or_in_a_checkout_or_by_do_not_track(self):
        self.assertIsNone(tm.blocked())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_KEY": ""}), \
                mock.patch.object(tm, "HERE", Path(tempfile.gettempdir()) / "no-config-here"):
            self.assertIn("key", tm.blocked())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_PACKAGED": ""}):
            self.assertIn("source checkout", tm.blocked())
        with mock.patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertIn("DO_NOT_TRACK", tm.blocked())
            self.assertFalse(tm.capture("app_opened"))
        self.assertFalse(tm.OUTBOX.exists())

    def test_usage_is_on_by_default_the_others_are_opt_in(self):
        self.assertTrue(tm.capture("app_opened"))
        self.assertFalse(tm.capture("compat_report", {}, level="compat"))
        self.assertFalse(tm.capture("$exception", {}, level="diagnostics"))
        self.assertEqual([e["event"] for e in self.queued()], ["app_opened"])

    def test_events_are_anonymous(self):
        tm.capture("app_opened")
        e = self.queued()[0]
        self.assertEqual(e["distinct_id"], tm.settings()["id"])
        self.assertIs(e["properties"]["$process_person_profile"], False)
        self.assertIs(e["properties"]["$geoip_disable"], True)
        self.assertEqual(e["properties"]["app_version"], "9.9.9")

    def test_turning_a_level_off_drops_its_unsent_events(self):
        tm.update_settings({"diagnostics": True})
        tm.capture("app_opened")
        tm.diagnostic("somewhere", RuntimeError("boom"))
        self.assertEqual(len(self.queued()), 2)
        tm.update_settings({"diagnostics": False})
        self.assertEqual([e["event"] for e in self.queued()], ["app_opened"])
        tm.update_settings({"usage": False})
        self.assertEqual(self.queued(), [])
        self.assertFalse(tm.capture("app_opened"))

    def test_the_same_error_is_sent_once_in_a_while(self):
        tm.update_settings({"diagnostics": True})
        for _ in range(3):
            tm.diagnostic("POST /api/android install", RuntimeError("boom"))
        self.assertEqual(len(self.queued()), 1)

    def test_connection_failures_are_sent_once_per_session(self):
        # The status poll meets an asleep or absent headset every few seconds (638 timeouts from
        # six people in two weeks): one event per kind per session, whatever the address or route,
        # for errors marked where ssh ran as ssh failing to reach the headset.
        tm.update_settings({"diagnostics": True})
        with mock.patch.object(tm.time, "time", return_value=1000.0):
            for ip in ("192.168.1.20", "192.168.1.21", "10.0.0.5"):
                for where in ("POST /api/comfort status", "job steam"):
                    tm.diagnostic(where, link_error(f"ssh: connect to host {ip} port 22: Connection timed out"))
                    tm.diagnostic(where, link_error(f"ssh: connect to host {ip} port 22: Host is down"))
                    tm.diagnostic(where, link_error("Timed out talking to frame"))
            tm.diagnostic("POST /api/comfort status",
                          link_error("ssh: Could not resolve hostname frame: No such host is known."))
        with mock.patch.object(tm.time, "time", return_value=1000.0 + 10 * tm.REPEAT_WINDOW):
            tm.diagnostic("POST /api/comfort status", link_error("client_loop: send disconnect: Connection reset"))
            tm.diagnostic("POST /api/comfort status", link_error("ssh: Could not resolve hostname frame"))
        sent = [e["properties"] for e in self.queued()]
        self.assertEqual([p["error_category"] for p in sent], ["frame_unreachable", "frame_not_set_up"])
        self.assertTrue(all(p["$exception_message"].startswith("ssh: ") for p in sent))

    def test_only_marked_errors_are_held_for_the_session(self):
        # The same words without the mark (from a download, a file name, anything not ssh) keep
        # the usual 10-minute window.
        tm.update_settings({"diagnostics": True})
        tm.diagnostic("POST /api/comfort status", link_error("ssh: connect to host 10.0.0.5 port 22: Connection timed out"))
        tm.diagnostic("job web", RuntimeError("ssh: connect to host 10.0.0.5 port 22: Connection timed out"))
        tm.diagnostic("job web", RuntimeError("ssh: connect to host 10.0.0.5 port 22: Connection timed out"))
        tm.diagnostic("job web", RuntimeError("download failed: [Errno 54] Connection reset by peer"))
        tm.diagnostic("job web", RuntimeError("urlopen error [Errno 60] Operation timed out"))
        self.assertEqual([e["properties"]["error_category"] for e in self.queued()],
                         ["frame_unreachable", "frame_unreachable", "download_failed", "frame_unreachable"])

    def test_real_errors_are_still_sent_beside_connection_failures(self):
        tm.update_settings({"diagnostics": True})
        tm.diagnostic("POST /api/comfort status", link_error("ssh: connect to host 10.0.0.5 port 22: Connection timed out"))
        tm.diagnostic("POST /api/comfort status", KeyError("battery"))
        tm.diagnostic("POST /api/android install", RuntimeError("boom"))
        tm.diagnostic("POST /api/comfort status", RuntimeError("ssh exited 255"))  # the command's own exit code?
        self.assertEqual([e["properties"]["error_category"] for e in self.queued()],
                         ["frame_unreachable", "internal_error", "other", "other"])

    def test_page_events_are_checked(self):
        self.assertTrue(tm.page_event({"event": "tab_viewed", "properties": {"tab": "android", "extra": "x"}})["queued"])
        self.assertEqual(self.queued()[0]["properties"].get("extra"), None)
        with self.assertRaises(ValueError):
            tm.page_event({"event": "anything_else"})
        with self.assertRaises(ValueError):
            tm.page_event({"event": "tab_viewed", "properties": {"tab": "/Users/me/secret"}})


class Lifecycle(Base):
    def test_install_update_and_one_open_a_day(self):
        tm.app_started()
        tm.app_started()
        self.assertEqual([e["event"] for e in self.queued()], ["app_installed", "app_opened"])
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_VERSION": "10.0.0"}):
            tm.app_started()
        e = self.queued()[-1]
        self.assertEqual((e["event"], e["properties"]["from_version"]), ("app_updated", "9.9.9"))

    def test_opens_installs_and_updates_carry_the_country(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_TIMEZONE": "Europe/Berlin"}):
            tm.app_started()
            with mock.patch.dict(os.environ, {"FRAME_CONTROL_VERSION": "10.0.0"}):
                tm.app_started()
        self.assertEqual([(e["event"], e["properties"].get("country")) for e in self.queued()],
                         [("app_installed", "DE"), ("app_opened", "DE"), ("app_updated", "DE")])
        self.assertNotIn("Europe/Berlin", tm.OUTBOX.read_text())

    def test_only_zones_in_the_tz_table_give_a_country(self):
        for tz, want in (("America/Argentina/Buenos_Aires", "AR"), ("Asia/Calcutta", "IN"), ("US/Pacific", "US"),
                         ("Europe/Vaduz", "LI"), ("Africa/Asmera", "ER"), ("Africa/Asmara", "ER"), ("Pacific/Truk", "FM"),
                         ("Pacific/Ponape", "FM"), ("America/Montreal", "CA"), ("America/Nassau", "BS"),
                         ("Europe/Amsterdam", "NL"), ("Etc/GMT+10", None), ("CET", None),
                         ("EET", None), ("WET", None), ("PST8PDT", None), ("HST", None), ("Pacific/Johnston", "UM"),
                         ("Japan", "JP"), ("UTC", None), ("alice", None),
                         ("home/alex/private", None), ("America/alex", None), ("../etc/passwd", None)):
            with mock.patch.dict(os.environ, {"FRAME_CONTROL_TIMEZONE": tz}):
                self.assertEqual(tm.country(), want, tz)

    def test_country_from_tz_or_localtime_in_a_checkout(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_TIMEZONE": "", "TZ": ":Asia/Tokyo"}):
            self.assertEqual(tm.country(), "JP")
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_TIMEZONE": "", "TZ": ""}):
            for target, want in (("/usr/share/zoneinfo/Europe/Paris", "FR"),
                                 ("/var/db/timezone/zoneinfo/Australia/Sydney", "AU"),
                                 ("/home/alex/zoneinfo/alex/private", None), ("/etc/mytime", None)):
                with mock.patch.object(tm.os.path, "realpath", return_value=target):
                    self.assertEqual(tm.country(), want, target)

    def test_a_new_user_who_opens_settings_first_still_gets_the_notice(self):
        tm.update_settings({"compat": True})
        self.assertFalse(tm.state()["noticeShown"])
        tm.update_settings({"noticeShown": True})
        self.assertTrue(tm.state()["noticeShown"])

    def test_frame_build_once(self):
        tm.frame_seen("20260922.1", "3.8")
        tm.frame_seen("20260922.1", "3.8")
        self.assertEqual(len(self.queued()), 1)

    def test_frame_once_per_headset_with_saved_count(self):
        tm.frame_seen("20260922.1", "3.8", "0123456789abcdef", 2)
        tm.frame_seen("20260922.1", "3.8", "0123456789abcdef", 2)
        tm.frame_seen("20260922.1", "3.8", "fedcba9876543210", 2)
        props = [e["properties"] for e in self.queued()]
        self.assertEqual([(p["headset_id"], p["headsets_saved"]) for p in props],
                         [("0123456789abcdef", 2), ("fedcba9876543210", 2)])

    def test_frame_many_headsets_are_not_sent_twice(self):
        ids = ["%016x" % n for n in range(25)]
        for _ in range(2):
            for h in ids:
                tm.frame_seen("20260922.1", "3.8", h, 25)
        self.assertEqual(len(self.queued()), 25)

    def test_frame_headset_id_must_be_the_hash(self):
        tm.frame_seen("20260922.1", "3.8", "/etc/machine-id contents", None)
        p = self.queued()[0]["properties"]
        self.assertNotIn("headset_id", p)
        self.assertNotIn("headsets_saved", p)


class HeadsetId(unittest.TestCase):
    def test_hash_of_machine_id_and_none_without_one(self):
        import hashlib
        import frame_status
        with mock.patch.object(frame_status, "read", return_value="abc123"):
            self.assertEqual(frame_status.headset_id(),
                             hashlib.sha256(b"frame-control-headset:abc123").hexdigest()[:16])
        with mock.patch.object(frame_status, "read", return_value=None):
            self.assertIsNone(frame_status.headset_id())


class Sending(Base):
    def serve(self, status=200):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"status": 1}')

            def log_message(self, *a):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        os.environ["FRAME_CONTROL_POSTHOG_HOST"] = f"http://127.0.0.1:{httpd.server_port}"
        return got

    def test_nothing_is_sent_before_the_notice_was_shown(self):
        got = self.serve()
        tm.capture("app_opened")
        self.assertEqual(tm.flush(), 0)
        self.assertEqual(got, [])
        tm.update_settings({"noticeShown": True})
        self.assertEqual(tm.flush(), 1)
        path, body = got[0]
        self.assertEqual((path, body["api_key"], body["batch"][0]["event"]), ("/batch/", "phc_test", "app_opened"))
        self.assertEqual(self.queued(), [])
        self.assertEqual([e["event"] for e in tm.state()["sent"]], ["app_opened"])

    def test_an_older_notice_still_sends(self):
        got = self.serve()
        tm._save({**tm._defaults(), "notice_shown": True, "notice_version": 1})
        tm.capture("app_opened")
        self.assertEqual(tm.flush(), 1)
        self.assertEqual(len(got), 1)

    def test_an_older_notice_is_shown_again_and_holds_back_the_country(self):
        got = self.serve()
        tm.SETTINGS.write_text(json.dumps({"id": "x", "notice_shown": True, "installed_sent": True,
                                           "last_version": "9.9.9"}))  # as 0.4.5 saved it
        self.assertFalse(tm.state()["noticeShown"])
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_TIMEZONE": "Europe/Berlin"}):
            tm.app_started()
            self.assertEqual(tm.flush(), 1)  # the usual statistics still go
            tm.update_settings({"noticeShown": True})
            self.assertTrue(tm.state()["noticeShown"])
            tm._save({**tm.settings(), "last_open_day": None})  # the next day
            tm.app_started()
            self.assertEqual(tm.flush(), 1)
        self.assertEqual([b["batch"][0]["properties"].get("country") for _, b in got], [None, "DE"])

    def test_turning_usage_on_still_waits_for_the_new_notice(self):
        got = self.serve()
        tm.SETTINGS.write_text(json.dumps({"id": "x", "usage": False, "notice_shown": True, "installed_sent": True,
                                           "last_version": "9.9.9"}))  # 0.4.5, usage off
        tm.update_settings({"usage": True})
        self.assertFalse(tm.state()["noticeShown"])  # so the page shows it
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_TIMEZONE": "Europe/Berlin"}):
            tm.app_started()
        self.assertEqual(tm.flush(), 1)
        self.assertNotIn("country", got[0][1]["batch"][0]["properties"])

    def test_a_failed_send_keeps_the_events(self):
        self.serve(status=500)
        tm.update_settings({"noticeShown": True})
        tm.capture("app_opened")
        self.assertEqual(tm.flush(), 0)
        self.assertEqual(len(self.queued()), 1)


class Scrub(unittest.TestCase):
    def test_personal_details_are_removed(self):
        home = str(Path.home())
        text = (f"open {home}/Downloads/My Game.apk failed; ssh alex@192.168.1.20 (frame.local) "
                "key ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM steam 76561198000000000 mac 3c:22:fb:12:34:56 "
                "url https://example.com/private/path?token=abc phc_abcdefghijklmnopqrstu C:\\Users\\Bob\\x "
                "/home/carol/y")
        out = tm.scrub(text)
        for leaked in (home, "192.168.1.20", "frame.local", "AAAAC3Nza", "76561198000000000", "3c:22:fb",
                       "private/path", "phc_abcdefghijklmnopqrstu", "Bob", "carol", "alex@"):
            self.assertNotIn(leaked, out)
        self.assertIn("https://example.com/…", out)
        self.assertIn("~/Downloads", out)

    def test_categories(self):
        self.assertEqual(tm.categorize("adb: failed to install: INSTALL_FAILED_NO_MATCHING_ABIS: x"),
                         ("android_installer", "INSTALL_FAILED_NO_MATCHING_ABIS"))
        self.assertEqual(tm.categorize("X has no arm64-v8a build (armeabi-v7a)")[0], "apk_wrong_abi")
        self.assertEqual(tm.categorize("ssh: connect to host 10.0.0.2 port 22: Connection refused")[0],
                         "frame_unreachable")
        self.assertEqual(tm.categorize("something new")[0], "other")

    def test_connection_failures_seen_from_released_versions(self):
        # Wording from 0.4.0's error reports (addresses replaced), on Windows, macOS and Linux.
        unreachable = [
            "ssh: connect to host 192.168.1.20 port 22: Connection timed out",
            "ssh: connect to host 192.168.1.20 port 22: Operation timed out",
            "ssh: connect to host 192.168.1.20 port 22: No route to host",
            "ssh: connect to host 192.168.1.20 port 22: Host is down",
            "ssh: connect to host 192.168.1.20 port 22: Unknown error",
            "mux_client_request_session: read from master failed: Broken pipe\n"
            "ssh: connect to host 192.168.1.20 port 22: Host is down",
            "client_loop: send disconnect: Connection reset",
            "banner exchange: Connection to UNKNOWN port -1: Connection refused",
            "Timed out talking to frame",
        ]
        for message in unreachable:
            self.assertEqual(tm.categorize(message)[0], "frame_unreachable", message)
        for message in ("ssh: Could not resolve hostname frame: No such host is known.",
                        "ssh: Could not resolve hostname fe80::1%wireless_32773: No such host is known."):
            self.assertEqual(tm.categorize(message)[0], "frame_not_set_up", message)
        self.assertEqual(tm.categorize("ssh exited 1")[0], "other")
        self.assertEqual(tm.categorize("ssh exited 255")[0], "other")  # may be the command's own exit code
        self.assertEqual(tm.categorize("download failed: [Errno 54] Connection reset by peer")[0], "download_failed")
        self.assertEqual(tm.categorize("frame@10.0.0.2: Permission denied (publickey).")[0], "frame_auth")

    def test_install_failure_categories(self):
        import frame_android
        self.assertEqual(tm.categorize(frame_android.LayerMissing(frame_android.LAYER_MISSING))[0], "layer_missing")
        # The message 0.4.0 sent, so old and new builds land in the same bucket.
        self.assertEqual(tm.categorize("the OpenXR compatibility layer isn't built; run "
                                       "frame/openxr-compat/build.sh")[0], "layer_missing")
        self.assertEqual(tm.categorize("could not prepare the APK for the Frame: ZIP64 APKs are unsupported")[0],
                         "apk_repack_failed")
        self.assertEqual(tm.categorize(FileNotFoundError(2, "No such file or directory", "ssh"))[0], "tool_missing")
        self.assertEqual(tm.categorize("[WinError 2] The system cannot find the file specified")[0], "tool_missing")

    def test_apk_install_failures_that_were_other(self):
        """What frame_android.install can raise, worded as it is, that 0.4.x sent as 'other'
        (or as frame_unreachable, when it was the Steam client or a slow copy)."""
        tail = 'Traceback (most recent call last):\n  File "<stdin>", line 19, in target_ws\n'
        cases = {
            "steam_not_running": [
                "SharedJSContext not found: is the Steam client running?",
                tail + "urllib.error.URLError: <urlopen error [Errno 111] Connection refused>",
                tail + "ConnectionRefusedError: [Errno 111] Connection refused",
            ],
            "steam_not_responding": [
                "websocket handshake failed",
                tail + "TimeoutError: timed out",
                tail + "socket.timeout: timed out",
                tail + '  File "<stdin>", line 45, in _read\nEOFError',
            ],
            "steam_js_error": ['JS error: {"exceptionId": 1, "text": "Uncaught", "exception": '
                               '{"description": "Error: Steam artwork API unavailable; installation is incomplete"}}'],
            "artwork_failed": [
                "Steam artwork renderer did not produce every slot",
                tail + "ValueError: incomplete artwork render",
                tail + "ValueError: hero render is 0 bytes of png",
                "could not prepare artwork: unknown artwork slot",
                tail + "ValueError: all five Steam artwork slots are required",
            ],
            "copy_timeout": ["copying Big Game (timed out).apk to the Frame timed out",
                             "copying launch.sh to the Frame timed out"],
            "copy_failed": [
                "copying game.apk to the Frame failed: rsync: connection unexpectedly closed (0 bytes received "
                "so far) [sender]\nrsync error: error in rsync protocol data stream (code 12) at io.c(231)",
                "copying game.apk to the Frame failed: ",
            ],
            "apk_unreadable": [
                "x.apk: not a readable APK: [Errno 2] No such file or directory: 'x.apk'",
                "x.apk: could not read the APK manifest: Error -3 while decompressing data: invalid block type",
                "x.apk: the APK manifest has no package name",
                "x.apk: lib/arm64-v8a/libx.so in the APK uses a compression Android does not",
                "x.apk: AndroidManifest.xml in the APK is 40 MB, more than a real one (16 MB)",
            ],
        }
        for category, messages in cases.items():
            for message in messages:
                self.assertEqual(tm.categorize(message)[0], category, message)
        # Still where they were: ssh's own failures, and a full disk during a copy.
        self.assertEqual(tm.categorize("timed out talking to frame")[0], "frame_unreachable")
        self.assertEqual(tm.categorize("copying a.apk to the Frame failed: ssh: connect to host 10.0.0.2 "
                                       "port 22: Connection refused")[0], "frame_unreachable")
        self.assertEqual(tm.categorize("copying a.apk to the Frame failed: rsync: write failed on "
                                       "\"app.apk.part\": No space left on device (28)")[0], "frame_disk_full")
        self.assertEqual(tm.categorize("download failed: timed out")[0], "download_failed")

    def test_python_errors_are_internal_whatever_they_say(self):
        import json as _json
        for error in (KeyError("paths"), TypeError("'NoneType' object is not subscriptable"),
                      AttributeError("x"), _json.JSONDecodeError("Expecting value", "", 0),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                      KeyError("timed out")):
            self.assertEqual(tm.categorize(error)[0], "internal_error", repr(error))
        self.assertEqual(tm.categorize("KeyError: 'paths'")[0], "other")  # only the exception itself
        self.assertEqual(tm.categorize(OSError(28, "No space left on device"))[0], "frame_disk_full")

    def test_privacy_doc_lists_every_category(self):
        doc = (ROOT / "docs" / "privacy.md").read_text()
        for name in {n for n, _ in tm.CATEGORIES} | {"internal_error", "other"}:
            self.assertIn(f"`{name}`", doc)


class Compat(Base):
    def test_reports_are_shared_only_after_opting_in_without_file_names(self):
        r = {"id": "r1", "package": "org.example", "version": "1.0", "rating": "works", "via": "user",
             "notes": f"from {Path.home()}/x", "source": "MyPrivateBuild.apk", "date": "2026-09-28T10:00:00"}
        self.assertFalse(tm.compat_report(r))
        tm.update_settings({"compat": True})
        self.assertTrue(tm.compat_report(r))
        p = self.queued()[-1]["properties"]
        self.assertEqual((p["package"], p["rating"], p["id"]), ("org.example", "works", "r1"))
        self.assertNotIn("source", p)
        self.assertNotIn(str(Path.home()), p["notes"])
        r2 = dict(r, id="r2", source="https://f-droid.org/repo/org.example_1.apk")
        tm.compat_report(r2)
        self.assertEqual(self.queued()[-1]["properties"]["source"], "https://f-droid.org/…")

    def test_opting_in_shares_earlier_local_reports(self):
        with mock.patch.object(db, "shared", return_value=False), \
                mock.patch.object(db, "_outbox", return_value=[{"id": "old1", "package": "org.a", "rating": "works",
                                                                "date": "2026-09-01T00:00:00"}]):
            tm.update_settings({"compat": True})
            tm.update_settings({"compat": True})  # already sent: not again
        self.assertEqual([e["properties"]["id"] for e in self.queued() if e["event"] == "compat_report"], ["old1"])


class LinkFailureProvenance(Base):
    """Only ssh, where it runs, decides that it couldn't reach the headset; the error report
    then holds that back for the session. Nothing in a message can claim it."""

    def setUp(self):
        super().setUp()
        import frame_android
        import frame_webinstall
        import server
        self.server, self.android, self.web = server, frame_android, frame_webinstall
        for target, name, kw in ((server, "ensure_master", {}), (server, "LINK", {"new": None}),
                                 (server, "repair_ssh_config", {"return_value": False})):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        tm.update_settings({"diagnostics": True})

    def ssh_fails(self, module, returncode, stderr, stdout=""):
        """Run module.ssh with ssh exiting `returncode`; -> the exception, as the route handler gets it."""
        done = subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)
        with mock.patch.object(frame_host, "run_ssh", return_value=done):
            try:
                module.ssh("true")
            except Exception as e:  # noqa: BLE001
                return e
        self.fail("ssh did not raise")

    def poll_fails(self):
        e = self.ssh_fails(self.server, 255, "ssh: connect to host 10.0.0.5 port 22: Connection timed out\r\n")
        tm.diagnostic("POST /api/comfort status", e)

    def test_a_status_poll_that_cannot_reach_the_frame_is_sent_once(self):
        for _ in range(5):
            self.poll_fails()
        for stderr in ("Warning: Permanently added '10.0.0.5' (ED25519) to the list of known hosts.\r\n"
                       "kex_exchange_identification: read: Connection reset by peer\r\n",
                       "banner exchange: Connection to UNKNOWN port -1: Connection refused\r\n",
                       "\x1b[0mssh: connect to host 10.0.0.5 port 22: Unknown error\n"):
            tm.diagnostic("POST /api/comfort status", self.ssh_fails(self.server, 255, stderr))
        with mock.patch.object(frame_host, "run_ssh", side_effect=subprocess.TimeoutExpired("ssh", 30)):
            with self.assertRaises(self.server.Failure) as caught:
                self.server.ssh("true")
        tm.diagnostic("POST /api/comfort status", caught.exception)
        e = self.ssh_fails(self.android, 255, "ssh: connect to host 10.0.0.5 port 22: Host is down\n")
        tm.diagnostic("job steam", e)
        self.assertEqual([p["properties"]["error_category"] for p in self.queued()], ["frame_unreachable"])

    def test_host_key_and_login_refusals_are_sent_once(self):
        # 0.4.2: each of these came in as a $exception from the status poll, again every 10 minutes.
        for _ in range(3):
            for stderr in ("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\r\n"
                           "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\r\n"
                           "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\r\nHost key verification failed.\r\n",
                           "No ED25519 host key is known for frame-control-f7b4e9ac and you have requested strict "
                           "checking.\r\nHost key verification failed.\r\n",
                           "jane doe@frame-control-f7b4e9ac: Permission denied (publickey,password).\r\n",
                           "steamos@192.168.1.9: Permission denied (publickey,password).\n"):
                e = self.ssh_fails(self.server, 255, stderr)
                self.assertTrue(getattr(e, "frame_link_failed", False), stderr)
                tm.diagnostic("POST /api/comfort status", e)
        self.assertEqual([p["properties"]["error_category"] for p in self.queued()], ["frame_auth"])
        # A command on the Frame that was refused something isn't ssh refusing the headset.
        for code, stderr in ((1, "steamos@frame: Permission denied (publickey).\n"),
                             (255, "cat: /root/x: Permission denied\n"),
                             (255, "scp: /home/steamos/x: Permission denied\n")):
            self.assertFalse(getattr(self.ssh_fails(self.server, code, stderr), "frame_link_failed", False), stderr)

    def test_a_command_that_fails_on_the_frame_is_not_marked(self):
        self.poll_fails()
        for module in (self.server, self.android):
            for code, stderr in ((255, ""), (255, "x: ssh: connect to host frame port 22: Connection timed out\n"),
                                 (1, "ssh: connect to host 10.0.0.5 port 22: Connection timed out\n")):
                e = self.ssh_fails(module, code, stderr)
                self.assertFalse(getattr(e, "frame_link_failed", False), (module.__name__, code, stderr))
            # Review round 4: the command's own output (stdout) isn't ssh speaking.
            e = self.ssh_fails(module, 255, "", stdout="ssh: connect to host frame port 22: Connection timed out\n")
            self.assertFalse(getattr(e, "frame_link_failed", False), module.__name__)

    def request(self, path, body):
        """POST to the real server, as the page does."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        c.request("POST", path, json.dumps(body), {"X-Frame-UI": self.server.UI_KEY, "Content-Type": "application/json"})
        r = c.getresponse()
        result = r.status, json.loads(r.read())
        c.close()
        return result

    def serve(self):
        self.httpd = self.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def test_routes_that_rewrap_a_link_failure_keep_its_mark(self):
        # Review round 4: /api/android and /api/titles re-raise frame_android's FrameError as a
        # Failure; after the first connection failure, theirs are held back too, also past the
        # 10-minute window.
        self.serve()
        down = subprocess.CompletedProcess(["ssh"], 255, "", "ssh: connect to host 10.0.0.5 port 22: Connection timed out\r\n")
        meta = {"label": "Test App", "game_id": 5, "shortcut": None}
        with mock.patch.object(frame_host, "run_ssh", return_value=down), \
                mock.patch.object(self.android, "_meta_or_fail", return_value=meta), \
                mock.patch.object(self.server.frame_titles, "ensure_utils"):
            self.assertEqual(self.request("/api/comfort", {"action": "status"})[0], 503)  # the page shows its offline message
            for t in (1000.0, 1000.0 + 2 * tm.REPEAT_WINDOW):
                with mock.patch.object(tm.time, "time", return_value=t):
                    self.assertEqual(self.request("/api/android", {"action": "launch", "package": "org.test"})[0], 503)
                    self.assertEqual(self.request("/api/titles", {"action": "launch", "id": "mygame"})[0], 503)
            # Not a link failure: still reported, through the same re-wrap.
            with mock.patch.object(self.android, "_meta_or_fail", side_effect=self.android.FrameError("boom")):
                self.assertEqual(self.request("/api/android", {"action": "launch", "package": "org.test"})[0], 502)
        sent = [(e["properties"]["where"], e["properties"]["error_category"]) for e in self.queued()
                if e["event"] == "$exception"]
        # The comfort poll's own failure isn't one (see the next test).
        self.assertEqual(sent, [("POST /api/android launch", "frame_unreachable"), ("POST /api/android launch", "other")])

    def test_the_comfort_poll_meeting_an_unreachable_frame_is_not_sent(self):
        # 0.4.2: most reports were this poll (every 5 s) finding the headset asleep, away or
        # refusing ssh. Like GET /api/status, it isn't reported; a session started or cancelled
        # by hand still is, once, and a real failure of the poll is too.
        self.serve()
        for stderr in ("ssh: connect to host 10.0.0.5 port 22: Connection timed out\r\n",
                       "banner exchange: Connection to UNKNOWN port -1: Connection refused\r\n",
                       "ssh: Could not resolve hostname frame: nodename nor servname provided\r\n",
                       "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\r\nHost key verification failed.\r\n",
                       "No ED25519 host key is known for frame-control-f7b4e9ac and you have requested strict "
                       "checking.\r\nHost key verification failed.\r\n",
                       "steamos@192.168.1.9: Permission denied (publickey,password).\r\n"):
            down = subprocess.CompletedProcess(["ssh"], 255, "", stderr)
            with mock.patch.object(frame_host, "run_ssh", return_value=down):
                self.assertGreaterEqual(self.request("/api/comfort", {"action": "status"})[0], 500, stderr)
        self.assertEqual(self.queued(), [])
        down = subprocess.CompletedProcess(["ssh"], 255, "", "ssh: connect to host 10.0.0.5 port 22: Host is down\r\n")
        with mock.patch.object(frame_host, "run_ssh", return_value=down):
            self.request("/api/comfort", {"action": "cancel"})
        broken = subprocess.CompletedProcess(["ssh"], 1, "", "Traceback (most recent call last):\nOSError: boom\n")
        with mock.patch.object(frame_host, "run_ssh", return_value=broken):
            self.assertEqual(self.request("/api/comfort", {"action": "status"})[0], 502)
        sent = [(e["properties"]["where"], e["properties"]["error_category"]) for e in self.queued()
                if e["event"] == "$exception"]
        self.assertEqual(sent, [("POST /api/comfort cancel", "frame_unreachable"), ("POST /api/comfort status", "other")])

    def test_download_failures_after_a_poll_failure_are_still_sent(self):
        # Review round 3: these file names, in the real checksum message through the web-link worker,
        # were held back with the poll's connection failures.
        self.poll_fails()
        names = ("Connection closed by frame port 22.zip", "Connection reset by frame port 22.apk",
                 "kex_exchange_identification: read.zip", "banner exchange: payload.zip",
                 "ssh: connect to host frame port 22: x.zip", "Timed out talking to frame")
        for name in names:
            job = {"phase": "download", "done": 0, "total": None, "detail": "", "message": None, "error": None,
                   "cancel": False}
            plan = {"name": None, "exe": None, "url": "https://example.com/x.zip", "kind": "zip"}
            error = self.web.WebInstallError(f"{name} doesn't match the manifest's sha256; not installing it")
            with mock.patch.object(self.web, "download", side_effect=error):
                self.server._webinstall_run(plan, job)
            self.assertEqual(job["phase"], "error")
        exceptions = [e["properties"] for e in self.queued() if e["event"] == "$exception"]
        self.assertEqual([p["error_category"] for p in exceptions], ["frame_unreachable"] + ["download_failed"] * len(names))
        finished = [e["properties"] for e in self.queued() if e["event"] == "install_finished"]
        self.assertEqual({p["error_category"] for p in finished}, {"download_failed"})


class InstallFinished(unittest.TestCase):
    def test_failure_category_only_no_text(self):
        """install_finished carries a fixed category for a failure, never the message or a file name."""
        import frame_android
        with mock.patch.object(tm, "capture") as capture, mock.patch.object(tm, "diagnostic"):
            tm.install_finished("apk", False, 0.0, frame_android.LayerMissing(
                "C:\\Users\\Bob\\My Game.apk: " + frame_android.LAYER_MISSING), catalog=False)
            props = capture.call_args[0][1]
            self.assertEqual(props["error_category"], "layer_missing")
            self.assertNotIn("Bob", repr(props))
            self.assertEqual(set(props), {"kind", "ok", "seconds", "error_category", "catalog"})
            tm.install_finished("apk", False)
            self.assertEqual(capture.call_args[0][1]["error_category"], "other")


class ApkInstallJobs(unittest.TestCase):
    """The whole job path: what the page is told, and what telemetry sends, once."""

    def setUp(self):
        import frame_android
        import frame_webinstall
        import server
        self.server, self.android, self.web = server, frame_android, frame_webinstall
        for target, name, kw in ((server, "ensure_master", {}), (server.frame_catalog, "app", {}),
                                 (server.frame_catalog, "add_report", {})):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def run_job(self, body):
        job = self.server.android(body)["job"]
        for _ in range(500):
            with self.server._jobs_lock:
                state = dict(self.server._jobs[job])
            if state["done"]:
                return state
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_missing_layer_warning_reaches_every_completion_message(self):
        meta = {"label": "VR", "package": "org.test.vr", "vr_issues": [self.android.LAYER_MISSING_NOTE]}
        with mock.patch.object(self.server.frame_catalog, "install", return_value=meta):
            state = self.run_job({"action": "install", "package": "org.test.vr"})
        self.assertIsNone(state["error"])
        self.assertIn(self.android.LAYER_MISSING_NOTE, state["message"])
        with mock.patch.object(self.server.frame_apk_versions, "install", return_value=meta):
            state = self.run_job({"action": "install", "package": "org.test.vr", "url": "https://example.com/v.apk"})
        self.assertIn(self.android.LAYER_MISSING_NOTE, state["message"])
        with mock.patch.object(self.android, "install", return_value=meta):
            self.assertIn(self.android.LAYER_MISSING_NOTE, self.web.dispatch("/tmp/v.apk")["message"])
        # A normal install says nothing about the layer.
        with mock.patch.object(self.server.frame_catalog, "install", return_value=dict(meta, vr_issues=[])):
            state = self.run_job({"action": "install", "package": "org.test.vr"})
        self.assertNotIn("OpenXR", state["message"])

    def test_unexpected_failure_is_one_event_and_one_diagnostic(self):
        info = {"package": "org.test.flat", "label": "Flat", "abis": [], "min_sdk": None, "vr": False,
                "vr_activity": False, "launchable": True, "repairable": False}
        sent = []
        tm._seen_errors.clear()
        with mock.patch.object(tm, "enabled", return_value=True), \
                mock.patch.object(tm, "capture", side_effect=lambda e, p=None, level="usage": sent.append((e, p))), \
                mock.patch.object(self.android, "apk_info", side_effect=lambda path: dict(info)), \
                mock.patch.object(self.android, "_install",
                                  side_effect=FileNotFoundError(2, "No such file or directory", "scp")), \
                mock.patch.object(self.server.frame_catalog, "install",
                                  side_effect=lambda pkg: self.android.install("/tmp/x.apk")):
            state = self.run_job({"action": "install", "package": "org.test.flat"})
        self.assertIn("FileNotFoundError", state["error"])
        events = [e for e, _ in sent]
        self.assertEqual(events.count("install_finished"), 1)
        self.assertEqual(events.count("$exception"), 1, events)
        finished = next(p for e, p in sent if e == "install_finished")
        self.assertEqual((finished["ok"], finished["error_category"]), (False, "tool_missing"))
        # The same failure through a web link: one event and one diagnostic there too.
        sent.clear()
        tm._seen_errors.clear()
        job = {"phase": "download", "done": 0, "total": None, "detail": "", "message": None, "error": None,
               "cancel": False}
        plan = {"name": None, "exe": None, "url": "https://example.com/x.apk", "kind": "apk"}
        with mock.patch.object(tm, "enabled", return_value=True), \
                mock.patch.object(tm, "capture", side_effect=lambda e, p=None, level="usage": sent.append((e, p))), \
                mock.patch.object(self.android, "apk_info", side_effect=lambda path: dict(info)), \
                mock.patch.object(self.android, "_install",
                                  side_effect=FileNotFoundError(2, "No such file or directory", "scp")), \
                mock.patch.object(self.server.frame_webinstall, "download",
                                  side_effect=lambda plan, tmp, **kw: os.path.join(tmp, "x.apk")):
            self.server._webinstall_run(plan, job)
        self.assertEqual(job["phase"], "error")
        self.assertIn("FileNotFoundError", job["error"])
        events = [e for e, _ in sent]
        self.assertEqual(events.count("install_finished"), 1, events)
        self.assertEqual(events.count("$exception"), 1, events)
        finished = next(p for e, p in sent if e == "install_finished")
        self.assertEqual((finished["kind"], finished["error_category"]), ("apk", "tool_missing"))
        # A FrameError still gets its install diagnostic (the job reports it as well, as before).
        sent.clear()
        tm._seen_errors.clear()
        with mock.patch.object(tm, "enabled", return_value=True), \
                mock.patch.object(tm, "capture", side_effect=lambda e, p=None, level="usage": sent.append((e, p))), \
                mock.patch.object(self.android, "apk_info", side_effect=lambda path: dict(info)), \
                mock.patch.object(self.android, "_install", side_effect=self.android.FrameError("timed out talking to frame")), \
                mock.patch.object(self.server.frame_catalog, "install",
                                  side_effect=lambda pkg: self.android.install("/tmp/x.apk")):
            self.run_job({"action": "install", "package": "org.test.flat"})
        self.assertEqual([e for e, _ in sent].count("install_finished"), 1)


class ApkInstallReports(unittest.TestCase):
    """server.apk_installed: an APK that won't install is reported; connection trouble isn't."""

    def setUp(self):
        import server
        self.server = server
        for target, name in ((server.frame_catalog, "add_report"), (server.frame_telemetry, "install_finished")):
            p = mock.patch.object(target, name)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def test_wrong_abi_is_an_install_failed_report(self):
        info = {"package": "org.x", "version": "2.0", "label": "X"}
        self.server.apk_installed(info, None, self.server.frame_android.FrameError(
            "X has no arm64-v8a build (armeabi-v7a); Lepton is 64-bit ARM only"), 3.0)
        args, kw = self.add_report.call_args
        self.assertEqual((args[0], args[1], kw["result"], kw["via"]), ("org.x", "2.0", "install_failed", "install"))
        self.assertIs(self.install_finished.call_args[0][1], False)

    def test_install_without_layer_is_flagged(self):
        self.server.apk_installed({"package": "com.private.vr", "xr_layer_missing": True}, {}, None, 4.0)
        self.assertIs(self.install_finished.call_args[1]["xr_layer_missing"], True)
        self.server.apk_installed({"package": "com.private.vr"}, {}, None, 4.0)
        self.assertIsNone(self.install_finished.call_args[1]["xr_layer_missing"])

    def test_connection_trouble_is_not_reported(self):
        self.server.apk_installed({"package": "org.x", "version": "2.0"}, None,
                                  self.server.frame_android.FrameError("timed out talking to frame"), 3.0)
        self.add_report.assert_not_called()

    def test_private_package_names_stay_here(self):
        with mock.patch.dict(self.server.frame_catalog._cache, {"by_pkg": {"org.public": {}}}):
            self.server.apk_installed({"package": "com.private.thing", "version": "1"}, {"package": "com.private.thing"},
                                      None, 2.0)
            self.assertIsNone(self.install_finished.call_args[1]["package"])
            self.server.apk_installed({"package": "org.public", "version": "1"}, {"package": "org.public"}, None, 2.0)
            self.assertEqual(self.install_finished.call_args[1]["package"], "org.public")


class CommunitySync(unittest.TestCase):
    def ev(self, i, who="a", day="2026-09-28", **kw):
        return ({"id": f"id{i}", "package": "org.x", "rating": "works", "date": f"{day}T00:00:00",
                 "via": "probe", **kw}, who, f"{day} 10:00:00")

    def test_rows_are_validated_marked_and_capped_per_reporter(self):
        events = [self.ev(i) for i in range(5)] + [self.ev(9, who="b", rating="nonsense"), self.ev(10, who="b")]
        rows, skipped = db.community_rows(events, {}, cap=3)
        self.assertEqual([r["id"] for r in rows], ["id0", "id1", "id2", "id10"])
        self.assertTrue(all(r["via"] == "community-probe" for r in rows))
        self.assertEqual(len(skipped), 3)

    def test_the_cap_and_duplicates_hold_across_syncs(self):
        state = {}
        rows, _ = db.community_rows([self.ev(i) for i in range(3)], state, cap=3)
        self.assertEqual(len(rows), 3)
        rows, skipped = db.community_rows([self.ev(i) for i in range(6)], state, cap=3)  # overlapping re-read
        self.assertEqual(rows, [])
        self.assertEqual([why for _, why in skipped], ["over the daily limit for one reporter"] * 3)

    def test_a_malformed_event_is_skipped_not_fatal(self):
        rows, skipped = db.community_rows([self.ev(1, via=["probe"]), ("not json", "a", "2026-09-28"), self.ev(2)], {})
        self.assertEqual([r["id"] for r in rows], ["id2"])
        self.assertEqual(len(skipped), 2)


class Regressions(Base):
    """Findings from the cross-provider review."""

    def test_urls_lose_credentials_paths_and_private_hosts(self):
        for text, leaked in (("https://alice:secret@example.com/private.apk?token=credential", ("alice", "secret", "private", "credential")),
                             ("https://alice:secret@192.168.1.4/private.apk", ("alice", "192.168", "private")),
                             ("fe80::1234 and 2001:db8::5", ("fe80", "2001:db8")),
                             ("sk-proj-abcdefghijklmnopqrstuv", ("abcdefghijk",)),
                             ("http://frame.local:8080/x", ("frame.local", "8080"))):
            out = tm.scrub(text)
            for s in leaked:
                self.assertNotIn(s, out, (text, out))

    def test_compat_labels_versions_and_sources_are_scrubbed(self):
        tm.update_settings({"compat": True})
        tm.compat_report({"id": "r9", "package": "org.x", "rating": "works", "date": "2026-09-28T00:00:00",
                          "label": "alice@example.com build", "version": "1.0-alice@example.com",
                          "source": "https://alice:secret@192.168.1.4/private.apk"})
        p = self.queued()[-1]["properties"]
        self.assertNotIn("alice", json.dumps(p))
        self.assertNotIn("source", p)

    def test_an_unsent_report_is_shared_again_after_opting_out_and_in(self):
        with mock.patch.object(db, "shared", return_value=False), \
                mock.patch.object(db, "_outbox", return_value=[{"id": "q1", "package": "org.a", "rating": "works",
                                                                "date": "2026-09-01T00:00:00"}]):
            tm.update_settings({"compat": True})
            tm.update_settings({"compat": False})
            self.assertEqual(self.queued(), [])
            tm.update_settings({"compat": True})
        self.assertEqual([e["properties"]["id"] for e in self.queued() if e["event"] == "compat_report"], ["q1"])

    def test_opting_out_waits_for_a_send_in_progress(self):
        tm.update_settings({"noticeShown": True})
        tm.capture("app_opened")
        order = []
        started = threading.Event()

        def slow_open(req, timeout):
            started.set()
            time.sleep(0.3)
            order.append("sent")
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False, read=lambda: b"{}")

        with mock.patch.object(tm.urllib.request, "urlopen", side_effect=slow_open):
            th = threading.Thread(target=tm.flush)
            th.start()
            started.wait(2)
            tm.update_settings({"usage": False})
            order.append("opted out")
            th.join()
        self.assertEqual(order, ["sent", "opted out"])

    def test_project_id_comes_from_the_config(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_PROJECT": "12345"}):
            self.assertEqual(tm.config()["project"], "12345")


class ReportProblem(Base):
    """Report a problem: diagnostics are scrubbed and bounded; the report goes privately to PostHog."""

    def serve(self, status=200):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"status":"Ok"}')

            def log_message(self, *a):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        p = mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_HOST": f"http://127.0.0.1:{httpd.server_port}"})
        p.start()
        self.addCleanup(p.stop)
        return got

    def test_diagnostics_are_scrubbed_and_include_the_log(self):
        log = tm.STATE / "server.log"
        log.write_text("GET /api/status 200\nTraceback: ssh alice@192.168.1.9 failed in %s/x\n" % Path.home())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": str(log)}):
            text = fr.diagnostics(["16:00  Install failed: https://bob:pw@example.com/a.apk"], include_logs=True, limit=5000)
        self.assertIn("Frame Control 9.9.9", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("GET /api/status", text)
        for leaked in ("alice", "192.168.1.9", str(Path.home()), "bob", "pw@"):
            self.assertNotIn(leaked, text)

    def test_a_report_is_bounded_in_utf16_units(self):
        body = {"title": "Live view stops", "message": "It stops 😀 " * 800, "diagnostics": "log 😀 line\n" * 2000}
        title, text, diag = fr.compose(body)
        self.assertLessEqual(fr.u16(text), fr.TEXT_MAX)
        self.assertLessEqual(fr.u16(diag), fr.DIAG_MAX)
        self.assertTrue(text.startswith("It stops"))
        with self.assertRaises(ValueError):
            fr.compose({"title": "hi", "message": "It stops after a minute."})

    def test_logs_only_when_asked_and_environment_is_kept_first(self):
        log = tm.STATE / "server.log"
        log.write_text("".join(f"old line {i}\n" for i in range(200)) + "newest line\n")
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": str(log)}):
            plain = fr.diagnostics(["Copy Jane Doe tax return.pdf to ~/Downloads"])
            full = fr.diagnostics(["Install failed"], include_logs=True, limit=400)
        self.assertNotIn("Jane Doe", plain)
        self.assertNotIn("line", plain)
        self.assertTrue(full.startswith("Frame Control 9.9.9"))
        self.assertIn("Install failed", full)
        self.assertIn("newest line", full)
        self.assertLessEqual(fr.u16(full), 400)

    def test_the_previewed_diagnostics_are_what_is_sent(self):
        got = self.serve()
        fr.send({"title": "Live view stops", "message": "It stops after a minute.",
                 "diagnostics": "Frame Control 9.9.9\nssh janes-mac.tail12345.ts.net failed"})
        diag = got[0][1]["batch"][0]["properties"]["diagnostics"]
        self.assertIn("Frame Control 9.9.9", diag)
        self.assertNotIn("janes-mac", diag)

    def test_send_is_a_private_posthog_event_whatever_the_settings(self):
        got = self.serve()
        tm.update_settings({"usage": False})  # analytics off: a deliberate report still goes
        with mock.patch.object(fr.frame_contact, "from_report", return_value=("contact-id", 1)):  # test_contact
            res = fr.send({"kind": "idea", "title": "Live view stops", "message": "It stops after a minute.",
                           "contact": "me@example.com", "contactFollowup": True})
        path, body = got[0]
        event = body["batch"][0]
        self.assertEqual((path, body["api_key"], event["event"]), ("/batch/", "phc_test", "problem_report"))
        props = event["properties"]
        self.assertEqual((props["kind"], props["title"], props["message"], props["contact"], props["report_id"]),
                         ("idea", "Live view stops", "It stops after a minute.", "me@example.com", res["id"]))
        self.assertEqual((props["contact_followup"], props["contact_id"], props["contact_rev"]), (True, "contact-id", 1))
        self.assertEqual((props["$process_person_profile"], props["$geoip_disable"]), (False, True))
        self.assertNotEqual(event["distinct_id"], tm.settings()["id"])  # not linked to the analytics
        self.assertIn(res["id"], res["message"])
        self.assertEqual([e["event"] for e in tm._read_lines(tm.SENT)], ["problem_report"])

    def test_a_sent_report_is_not_an_error_if_the_local_log_fails(self):
        self.serve()
        with mock.patch.object(tm, "record_sent", side_effect=OSError("disk full")):
            res = fr.send({"title": "Live view stops", "message": "It stops after a minute."})
        self.assertTrue(res["id"])

    def test_events_queued_by_older_versions_get_the_placeholder_address(self):
        got = self.serve()
        tm.update_settings({"noticeShown": True})
        tm._write_lines(tm.OUTBOX, [{"event": "app_opened", "distinct_id": "x", "uuid": "u1",
                                     "properties": {"level": "usage"}}])
        self.assertEqual(tm.flush(), 1)
        self.assertEqual(got[0][1]["batch"][0]["properties"]["$ip"], "0.0.0.0")
        self.assertEqual(tm._read_lines(tm.SENT)[0]["properties"]["$ip"], "0.0.0.0")

    def test_the_inbox_skips_malformed_reports(self):
        good = ["2026-09-28T09:50:00Z", "AB12CD34", "bug", "Live view stops", "It stops.", None,
                "0.4.0", "macOS", "", "", None, None, None]
        rows = [["2026-09-28T10:00:00Z", "X", "bug", "Hand-made", None, None, None, None, None, None, None, None, None],
                ["short"], good]
        with mock.patch.object(db, "_posthog_query", return_value={"results": rows}), \
             mock.patch.object(sys, "argv", ["frame_report.py", "inbox"]), \
             mock.patch("builtins.print") as out:
            fr.main()
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("AB12CD34", printed)
        self.assertIn("Hand-made", printed)

    def test_a_refused_report_is_an_error(self):
        self.serve(status=401)
        with self.assertRaisesRegex(fr.ReportError, "HTTP 401"):
            fr.send({"title": "Live view stops", "message": "It stops after a minute."})
        self.assertEqual(tm._read_lines(tm.SENT), [])

    def test_no_key_means_no_report(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_KEY": ""}), \
             mock.patch.object(tm, "HERE", tm.STATE):
            with self.assertRaisesRegex(fr.ReportError, "no PostHog project key"):
                fr.send({"title": "Live view stops", "message": "It stops after a minute."})


if __name__ == "__main__":
    unittest.main()
