#!/usr/bin/env python3
"""Generic cold/warm MCP latency benchmark.

Example:
  python benchmarks/mcp_latency_benchmark.py --project /absolute/workspace \
    --tool list_dir --arguments '{"relative_path":"src","recursive":false}'
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import statistics
import subprocess
import time
from typing import Any


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[index]


def stats(values: list[float]) -> dict[str, float]:
    return {"min_ms": round(min(values), 3), "median_ms": round(statistics.median(values), 3),
            "avg_ms": round(statistics.mean(values), 3), "p95_ms": round(percentile(values, 95), 3),
            "p99_ms": round(percentile(values, 99), 3), "max_ms": round(max(values), 3)}


class McpProcess:
    def __init__(self, project: str, timeout: float) -> None:
        self.proc = subprocess.Popen(
            ["serena", "start-mcp-server", "--transport", "stdio", "--project", project,
             "--tool-timeout", "60", "--log-level", "WARNING", "--context", "desktop-app"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                    return response
        raise TimeoutError(f"MCP request timed out: {method}")

    def close(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments", default="{}", help="JSON object")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    tool_args = json.loads(args.arguments)
    if not isinstance(tool_args, dict):
        parser.error("--arguments must be a JSON object")

    cold_start = time.perf_counter()
    cold = McpProcess(args.project, args.timeout)
    try:
        cold.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "benchmark", "version": "1"}})
        t0 = time.perf_counter()
        cold.request("tools/call", {"name": args.tool, "arguments": tool_args})
        cold_ms = (time.perf_counter() - t0) * 1000
    finally:
        cold.close()

    warm = McpProcess(args.project, args.timeout)
    try:
        warm.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "benchmark", "version": "1"}})
        warm.request("tools/call", {"name": args.tool, "arguments": tool_args})
        values = []
        for _ in range(max(1, args.rounds)):
            t0 = time.perf_counter()
            warm.request("tools/call", {"name": args.tool, "arguments": tool_args})
            values.append((time.perf_counter() - t0) * 1000)
    finally:
        warm.close()

    print(json.dumps({"project": os.path.realpath(args.project), "tool": args.tool,
                      "rounds": len(values), "cold_call_ms": round(cold_ms, 3),
                      "warm": stats(values)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
