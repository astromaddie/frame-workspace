"""Authenticated companion diagnostics must only forward bounded read requests."""
import sandbox  # noqa: F401
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
import server


class StreamStatsRequests(unittest.TestCase):
    def test_current_session_query_keeps_timing_fields_without_stream_credentials(self):
        result = {"streams": [{"id": 7, "frames": [{"s": 10, "cap": 100, "dec": 120}]}]}
        with mock.patch.object(server, "LOCAL", False), mock.patch.object(server.macview, "call", return_value=result) as call:
            self.assertEqual(server.macview_action({"action": "stream_stats", "id": 7, "since": 9, "settle": 0}), result)
            call.assert_called_once_with("/stats", id=7, since=9, settle=0)

    def test_invalid_or_extra_fields_never_reach_the_agent(self):
        invalid = [
            {"action": "stream_stats"},
            {"action": "stream_stats", "id": True},
            {"action": "stream_stats", "id": "7"},
            {"action": "stream_stats", "id": 0},
            {"action": "stream_stats", "id": 2**31},
            {"action": "stream_stats", "id": 7, "since": -1},
            {"action": "stream_stats", "id": 7, "settle": 5_000_001},
            {"action": "stream_stats", "id": 7, "src": "display:1"},
            {"action": "stream_stats", "id": 7, "k": "not-an-agent-token"},
        ]
        with mock.patch.object(server, "LOCAL", False), mock.patch.object(server.macview, "call") as call:
            for body in invalid:
                with self.subTest(body=body), self.assertRaises(server.Failure) as error:
                    server.macview_action(body)
                self.assertEqual(error.exception.status, 400)
            call.assert_not_called()
