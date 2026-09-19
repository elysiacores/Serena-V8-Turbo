#!/usr/bin/env python3
"""Generic cold/warm MCP latency benchmark.

Example:
  python benchmarks/mcp_latency_benchmark.py --project /absolute/workspace \
    --tool list_dir --arguments '{"relative_path":"src","recursive":false}'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import os
import re
import select
import signal
import statistics
import subprocess
import time
from typing import Any

import psutil


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[index]


def stats(values: list[float]) -> dict[str, float]:
    p50 = round(statistics.median(values), 3)
    return {"min_ms": round(min(values), 3), "median_ms": p50, "p50_ms": p50,
            "avg_ms": round(statistics.mean(values), 3), "p95_ms": round(percentile(values, 95), 3),
            "p99_ms": round(percentile(values, 99), 3), "max_ms": round(max(values), 3)}


def process_tree_rss_mb(pid: int) -> float:
    """Return RSS for Serena plus currently running child/LSP processes."""
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total / (1024 * 1024)


def validate_tool_response(response: dict[str, Any], *, contains: list[str]) -> None:
    """Fail closed: transport success alone is not a correct workload result."""
    if "error" in response or not isinstance(response.get("result"), dict):
        raise ValueError(f"JSON-RPC error or missing result: {response!r}")
    result = response["result"]
    content = result.get("content", [])
    if not isinstance(content, list):
        raise ValueError(f"Malformed MCP content: {content!r}")
    texts = [
        item["text"]
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    text = "\n".join(texts)
    if result.get("isError"):
        detail = text.strip() or json.dumps(result, ensure_ascii=False, default=str)
        raise ValueError(f"MCP tool returned isError: {detail[:4000]}")
    if not text.strip() or text.strip() in ("[]", "{}", "null", '""'):
        raise ValueError("Empty tool result")
    if re.search(r"(?im)^\s*(?:error\b|[\w.]*exception\b|traceback\b|failed\b)", text):
        raise ValueError(f"Tool returned error text: {text[:4000]}")
    for part in texts:
        try:
            payload = json.loads(part)
        except ValueError:
            continue
        if isinstance(payload, dict) and (payload.get("error") or payload.get("isError")):
            raise ValueError(f"Tool returned an error payload: {part[:4000]}")
    if not contains or any(not expected or expected not in text for expected in contains):
        raise ValueError(f"Tool result did not satisfy expected text assertions: {text[:4000]}")


class McpProcess:
    def __init__(self, project: str, timeout: float, executable: str) -> None:
        self.proc = subprocess.Popen(
            [executable, "start-mcp-server", "--transport", "stdio", "--project", project,
             "--tool-timeout", "60", "--log-level", "WARNING", "--context", "desktop-app"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        self.timeout = timeout
        self.request_id = 0

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.request_id += 1
        request_id = self.request_id
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if select.select([self.proc.stdout], [], [], 0.2)[0]:
                response = json.loads(self.proc.stdout.readline())
                if response.get("id") == request_id:
                    if "error" in response or not isinstance(response.get("result"), dict):
                        raise ValueError(f"{method}: JSON-RPC error or missing result")
                    return response
        raise TimeoutError(f"MCP request timed out: {method}")

    def close(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=5)


def executable_identity(executable: str, expected_version: str, timeout: float) -> dict[str, str]:
    """Verify the explicit launcher, not whichever serena happens to be on PATH.

    MCP serverInfo may identify the MCP library, not the Serena release; retain
    it separately rather than confusing it with the CLI's release version.
    """
    if not os.path.isabs(executable):
        raise ValueError("--executable must be an absolute path")
    resolved = str(Path(executable).resolve(strict=True))
    version_output = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                                    check=True, timeout=timeout).stdout.strip()
    build_version = version_output.removeprefix("Serena ")
    # Local checkouts append "-<gitsha>[-dirty]" to the CLI identity. Keep
    # that build identity for reproducibility while comparing the canonical
    # release version requested by the benchmark.
    version = re.sub(r"-[0-9a-f]{8}(?:-dirty)?$", "", build_version)
    if not expected_version or version != expected_version:
        raise ValueError(
            f"Release version mismatch: expected {expected_version!r}, "
            f"got release {version!r} from build {build_version!r}"
        )
    return {"executable": resolved, "version": version, "build_version": build_version,
            "version_output": version_output,
            "executable_sha256": hashlib.sha256(Path(resolved).read_bytes()).hexdigest()}


def measure_session(args: argparse.Namespace, tool_args: dict[str, Any], rounds: int) -> dict[str, Any]:
    # Probe identity for each new process; identity probing is not startup time.
    identity = executable_identity(args.executable, args.expected_version, args.timeout)
    start = time.perf_counter()
    process = McpProcess(args.project, args.timeout, identity["executable"])
    try:
        initialized = process.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "benchmark", "version": "2"}})
        server_info = initialized["result"].get("serverInfo")
        if not isinstance(server_info, dict) or not server_info.get("name") or not server_info.get("version"):
            raise ValueError("initialize did not return server identity")
        assert process.proc.stdin
        process.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.proc.stdin.flush()
        startup_ms = (time.perf_counter() - start) * 1000
        values = []
        for index in range(rounds + 1):
            t0 = time.perf_counter()
            response = process.request("tools/call", {"name": args.tool, "arguments": tool_args})
            elapsed = (time.perf_counter() - t0) * 1000
            # Validation is outside the measured request, but gates every sample,
            # including the first call used to warm the second process.
            validate_tool_response(response, contains=args.expect_contains)
            values.append(elapsed)
            if index == 0:
                startup_and_first_call_ms = (time.perf_counter() - start) * 1000
        rss_mb = process_tree_rss_mb(process.proc.pid)
        return {"identity": identity, "server_info": server_info, "startup_ms": startup_ms,
                "first_call_ms": values[0], "warm_values": values[1:],
                "startup_and_first_call_ms": startup_and_first_call_ms, "rss_mb": round(rss_mb, 3)}
    finally:
        process.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments", default="{}", help="JSON object")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--executable", required=True, help="Absolute Serena executable path (no PATH lookup)")
    parser.add_argument("--expected-version", required=True, help="Exact Serena CLI release version")
    parser.add_argument("--expect-contains", action="append", default=[],
                        help="Required result text; repeat to assert all expected strings")
    args = parser.parse_args()
    try:
        tool_args = json.loads(args.arguments)
    except ValueError as exc:
        parser.error(f"Invalid --arguments JSON: {exc}")
    if not isinstance(tool_args, dict):
        parser.error("--arguments must be a JSON object")
    if args.rounds < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--rounds and --timeout must be positive and finite")
    if not args.expect_contains or any(not text.strip() for text in args.expect_contains):
        parser.error("Provide nonempty --expect-contains assertions for the workload")
    try:
        cold = measure_session(args, tool_args, 0)
        warm = measure_session(args, tool_args, args.rounds)
        if cold["identity"] != warm["identity"] or cold["server_info"] != warm["server_info"]:
            raise ValueError("Server identity changed between benchmark sessions")
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "project": os.path.realpath(args.project), "tool": args.tool, "arguments": tool_args,
        "identity": cold["identity"], "server_info": cold["server_info"],
        "rounds": len(warm["warm_values"]), "startup_ms": round(cold["startup_ms"], 3),
        "first_call_ms": round(cold["first_call_ms"], 3),
        "cold_call_ms": round(cold["first_call_ms"], 3),  # Backward-compatible alias, excludes startup.
        "startup_and_first_call_ms": round(cold["startup_and_first_call_ms"], 3),
        "warm_startup_ms": round(warm["startup_ms"], 3),
        "warm_first_call_ms": round(warm["first_call_ms"], 3),
        "warm": stats(warm["warm_values"]),
        "rss_mb": max(cold["rss_mb"], warm["rss_mb"]),
        "rss_scope": "Serena process plus recursive child/LSP processes",
        "validation": {"status": "passed", "expect_contains": args.expect_contains},
        "timing_scope": "startup: spawn through initialize/initialized; first-call and warm: tools/call only; "
                        "deferred LSP/prewarm work is included wherever the server performs it",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
