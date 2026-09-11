"""Integration checks against real local child processes, not mocked counters."""
import subprocess
import sys
import unittest

from process_metrics import measure_process


class ProcessMetricsTests(unittest.TestCase):
    def run_child(self, source):
        return measure_process([sys.executable, "-c", source], stdout=subprocess.DEVNULL)

    def test_peak_survives_free_and_is_not_inherited_by_next_child(self):
        high = self.run_child("x = bytearray(64 * 1024 * 1024); del x")
        low = self.run_child("pass")
        self.assertEqual(high["returncode"], 0)
        self.assertEqual(low["returncode"], 0)
        self.assertGreater(high["peak_resident_bytes"] - low["peak_resident_bytes"], 32 * 1024 * 1024)
        if "peak_commit_bytes" in high:
            self.assertGreater(high["peak_commit_bytes"] - low["peak_commit_bytes"], 32 * 1024 * 1024)
        self.assertGreater(high["load_ms"], 0)
        self.assertGreaterEqual(high["cpu_ms"], 0)

    def test_failure_exit_is_preserved(self):
        result = self.run_child("raise SystemExit(7)")
        self.assertEqual(result["returncode"], 7)
        self.assertGreater(result["peak_resident_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
