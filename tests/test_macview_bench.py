"""Guard benchmark source selection against capturing the wrong desktop."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "macview_bench", Path(__file__).resolve().parents[1] / "scripts/macview-bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class DisplaySelectionTests(unittest.TestCase):
    def setUp(self):
        self.physical = {"main": True, "name": "Built-in", "src": "display:1"}
        self.workspace = {"main": False, "name": "Frame Workspace", "src": "display:9"}

    def test_workspace_is_selected_independently_of_main_and_enumeration_order(self):
        self.assertIs(bench.select_display([self.physical, self.workspace], "workspace"), self.workspace)
        self.assertIs(bench.select_display([self.workspace, self.physical], "main"), self.physical)

    def test_missing_workspace_does_not_fall_back_to_physical_display(self):
        with self.assertRaises(SystemExit):
            bench.select_display([self.physical], "workspace")

    def test_ambiguous_workspace_requires_resolution(self):
        with self.assertRaises(SystemExit):
            bench.select_display([self.workspace, dict(self.workspace, src="display:10")], "workspace")


class BenchmarkOwnershipTests(unittest.TestCase):
    def test_overlapping_run_is_rejected_before_any_device_action(self):
        with bench.tempfile.TemporaryDirectory() as directory, \
                patch.object(bench.tempfile, "gettempdir", return_value=directory), \
                patch.object(bench, "_run_suite", return_value=[]) as run:
            path = Path(directory) / f"frame-macview-bench-{bench.os.getuid()}.lock"
            with path.open("a") as lease:
                bench.fcntl.flock(lease, bench.fcntl.LOCK_EX | bench.fcntl.LOCK_NB)
                with self.assertRaises(SystemExit):
                    bench.run_suite(None, None)
                run.assert_not_called()
            self.assertEqual(bench.run_suite(None, None), [])
            run.assert_called_once()
