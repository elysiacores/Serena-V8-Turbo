"""Audit regressions for the standalone benchmark (stdlib-only runner)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "audit_latency_benchmark", Path(__file__).parents[1] / "benchmarks/mcp_latency_benchmark.py"
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkCorrectnessAuditTests(unittest.TestCase):
    def test_audit_gate_rejects_failure_envelopes_and_empty_or_wrong_answers(self):
        self.assertTrue(callable(getattr(benchmark, "validate_tool_response", None)),
                        "Benchmark needs an executable correctness gate")
        bad = [
            {"error": {"code": -1, "message": "failed"}},
            {"result": {"isError": True, "content": [{"type": "text", "text": "expected.py"}]}},
            {"result": {"content": [{"type": "text", "text": "Error: expected.py unavailable"}]}},
            {"result": {"content": [{"type": "text", "text": '{"error":"expected.py missing"}'}]}},
            {"result": {"content": []}},
            {"result": {"content": [{"type": "text", "text": "[]"}]}},
            {"result": {"content": [{"type": "text", "text": "wrong.py"}]}},
        ]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(ValueError):
                benchmark.validate_tool_response(response, contains=["expected.py"])
        good = {"result": {"content": [{"type": "text", "text": '{"files":["expected.py"]}'}]}}
        benchmark.validate_tool_response(good, contains=["expected.py"])
    def run_fixture(self, *, mode="good", extra=()):
        # A real stdio child, never a substitute for production latency evidence.
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "serena-fixture"
            executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys, time
if "--version" in sys.argv:
    print("Serena fixture-release-deadbeef-dirty")
    raise SystemExit(0)
initialized = False
calls = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "notifications/initialized":
        initialized = True
        continue
    if method == "initialize":
        time.sleep(0.06)
        result = {"serverInfo": {"name": "fixture", "version": "mcp-library-version"}}
    else:
        calls += 1
        mode = os.environ.get("BENCH_AUDIT_MODE", "good")
        result = {"content": [{"type": "text", "text": '{"files":["expected.py"]}'}]}
        if not initialized:
            result = {"isError": True}
        elif mode == "empty" or (mode == "late-empty" and calls > 1):
            result = {"content": [{"type": "text", "text": "[]"}]}
        elif mode == "tool-error":
            result["isError"] = True
        elif mode == "error-text":
            result["content"][0]["text"] = "Error executing tool: expected.py"
        elif mode == "wrong":
            result["content"][0]["text"] = "other.py"
    response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    if os.environ.get("BENCH_AUDIT_MODE") == "rpc-error":
        response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -1, "message": "failed"}}
    print(json.dumps(response), flush=True)
''')
            executable.chmod(0o700)
            command = [sys.executable, str(Path(benchmark.__file__)), "--project", tmp,
                       "--tool", "find_file", "--rounds", "2", "--timeout", "2",
                       "--executable", str(executable), "--expected-version", "fixture-release",
                       "--expect-contains", "expected.py", *extra]
            return subprocess.run(command, text=True, capture_output=True, timeout=10,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "BENCH_AUDIT_MODE": mode})

    def test_audit_cli_verifies_identity_and_separates_startup_from_first_call(self):
        completed = self.run_fixture()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["identity"]["version"], "fixture-release")
        self.assertEqual(report["identity"]["build_version"], "fixture-release-deadbeef-dirty")
        self.assertTrue(os.path.isabs(report["identity"]["executable"]))
        self.assertEqual(len(report["identity"]["executable_sha256"]), 64)
        self.assertEqual(report["rounds"], 2)
        self.assertGreaterEqual(report["startup_ms"], 50)
        self.assertGreaterEqual(report["warm_startup_ms"], 50)
        self.assertEqual(report["cold_call_ms"], report["first_call_ms"])
        self.assertGreaterEqual(report["startup_and_first_call_ms"], report["startup_ms"])
        self.assertEqual(report["validation"]["status"], "passed")

    def test_audit_cli_never_reports_latency_for_failed_results(self):
        for mode in ("rpc-error", "tool-error", "error-text", "wrong", "empty", "late-empty"):
            with self.subTest(mode=mode):
                completed = self.run_fixture(mode=mode)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")
                self.assertIn("Benchmark failed", completed.stderr)

    def test_audit_cli_rejects_wrong_release_before_measuring(self):
        completed = self.run_fixture(extra=("--expected-version", "wrong-release"))
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertIn("version mismatch", completed.stderr)


if __name__ == "__main__":
    unittest.main()
