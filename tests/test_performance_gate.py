import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "benchmarks" / "performance_gate.py"


class PerformanceGateTests(unittest.TestCase):
    def write_report(self, directory: Path, name: str, *, p50: float, p95: float, p99: float, startup: float, first: float, rss: float) -> Path:
        path = directory / name
        path.write_text(json.dumps({
            "validation": {"status": "passed"},
            "warm": {"p50_ms": p50, "p95_ms": p95, "p99_ms": p99},
            "startup_ms": startup,
            "first_call_ms": first,
            "rss_mb": rss,
        }))
        return path

    def test_gate_passes_within_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            baseline = self.write_report(directory, "baseline.json", p50=10, p95=20, p99=30, startup=100, first=40, rss=100)
            current = self.write_report(directory, "current.json", p50=10.5, p95=21, p99=31, startup=105, first=41, rss=102)
            completed = subprocess.run([
                sys.executable, str(GATE),
                "--current", str(current),
                "--baseline", str(baseline),
                "--max-regression-percent", "10",
                "--max-warm-p95-ms", "25",
                "--max-startup-ms", "120",
                "--max-rss-mb", "110",
            ], text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("PASS", completed.stdout)

    def test_gate_fails_p50_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            baseline = self.write_report(directory, "baseline.json", p50=10, p95=20, p99=30, startup=100, first=40, rss=100)
            current = self.write_report(directory, "current.json", p50=13, p95=20, p99=30, startup=100, first=40, rss=100)
            completed = subprocess.run([
                sys.executable, str(GATE),
                "--current", str(current),
                "--baseline", str(baseline),
                "--max-regression-percent", "15",
            ], text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("warm.p50_ms regressed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
