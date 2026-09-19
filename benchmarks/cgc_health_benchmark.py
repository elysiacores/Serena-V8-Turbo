#!/usr/bin/env python3
"""End-to-end CGC health and benchmark harness.

Usage:
  python benchmarks/cgc_health_benchmark.py --project /path/to/workspace \
      --index-path src/example.ts --function example

The harness is generic: project paths, symbols, and expected relationships are
provided at runtime and are never embedded in the repository.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class McpClient:
    def __init__(self, project: Path, command: str = "serena") -> None:
        self.project = project.resolve(strict=True)
        self.process = subprocess.Popen(
            [command, "start-mcp-server", "--transport", "stdio", "--project", str(self.project),
             "--tool-timeout", "20", "--log-level", "WARNING", "--context", "desktop-app"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env={**os.environ, "SERENA_V8_SKIP_PREWARM": "1"},
        )
        self._next_id = 0

    def request(self, method: str, params: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("MCP pipes are unavailable")
        self._next_id += 1
        request_id = self._next_id
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], 0.5)
            if not ready:
                continue
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("MCP process exited before response")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message
        raise TimeoutError(f"MCP request timed out: {method}")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        content = response.get("result", {}).get("content", [])
        text = next((item.get("text") for item in content if item.get("type") == "text"), "{}")
        return json.loads(text)

    def close(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)


def wait_for_job(client: McpClient, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    states: list[str] = []
    while time.monotonic() < deadline:
        status = client.call_tool("cgc_index_status", {"job_id": job_id})
        states.append(status.get("state", "unknown"))
        if status.get("state") in {"completed", "failed"}:
            status["observed_states"] = states
            return status
        time.sleep(0.5)
    raise TimeoutError(f"CGC job did not finish: {job_id}")


def timed_call(client: McpClient, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    result = client.call_tool(name, arguments)
    return result, (time.monotonic() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--index-path", default=".")
    parser.add_argument("--function", required=True)
    parser.add_argument("--query-path", default=None)
    parser.add_argument("--changed-file", default=None, help="Temporarily modify and restore this file to test stale detection")
    parser.add_argument("--full", action="store_true", help="Run full index and query while it is running")
    parser.add_argument("--expected-caller", default=None)
    parser.add_argument("--expected-callee", action="append", default=[])
    parser.add_argument("--serena", default="serena")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    client = McpClient(Path(args.project), args.serena)
    report: dict[str, Any] = {"project": str(Path(args.project).resolve()), "checks": {}}
    original: bytes | None = None
    changed: Path | None = None
    try:
        client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "cgc-health", "version": "1"}})
        tools = client.request("tools/list", {}).get("result", {}).get("tools", [])
        names = {tool.get("name") for tool in tools}
        required = {"cgc_index", "cgc_index_status", "cgc_callers", "cgc_callees", "cgc_stale_paths"}
        report["checks"]["tools_active"] = sorted(required & names)
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(f"missing CGC tools: {missing}")

        queued, queue_ms = timed_call(client, "cgc_index", {"path": args.index_path})
        report["index_queue_ms"] = round(queue_ms, 3)
        report["checks"]["job_id_returned"] = bool(queued.get("job_id"))
        job_id = queued["job_id"]

        if args.full:
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                status = client.call_tool("cgc_index_status", {"job_id": job_id})
                if status.get("state") == "running":
                    query, query_ms = timed_call(client, "cgc_callers", {"function": args.function, "path": args.query_path})
                    report["query_while_indexing_ms"] = round(query_ms, 3)
                    report["checks"]["query_while_indexing_ok"] = query.get("status") == "ok"
                    break
                if status.get("state") in {"completed", "failed"}:
                    break
                time.sleep(0.5)

        final = wait_for_job(client, job_id, args.timeout)
        report["index_status"] = final
        report["checks"]["index_completed"] = final.get("state") == "completed"
        index_result = final.get("result", {})
        index_output = index_result.get("stdout", "") + index_result.get("stderr", "")
        unresolved_match = re.search(r"Skipped\s+(\d+)\s+unresolved", index_output, re.IGNORECASE)
        report["unresolved_call_relationships"] = int(unresolved_match.group(1)) if unresolved_match else 0
        report["checks"]["unresolved_call_relationships_zero"] = report["unresolved_call_relationships"] == 0

        callers, callers_ms = timed_call(client, "cgc_callers", {"function": args.function, "path": args.query_path})
        callees, callees_ms = timed_call(client, "cgc_callees", {"function": args.function, "path": args.query_path})
        report["callers_ms"] = round(callers_ms, 3)
        report["callees_ms"] = round(callees_ms, 3)
        report["checks"]["callers_ok"] = callers.get("status") == "ok"
        report["checks"]["callees_ok"] = callees.get("status") == "ok"
        callers_output = callers.get("stdout", "") + callers.get("stderr", "")
        callees_output = callees.get("stdout", "") + callees.get("stderr", "")
        if args.expected_caller and args.full:
            report["checks"]["expected_caller_found"] = args.expected_caller in callers_output
        if args.expected_callee and args.full:
            report["checks"]["expected_callees_found"] = all(
                name in callees_output for name in args.expected_callee
            )

        if args.changed_file:
            candidate = (Path(args.project).resolve() / args.changed_file).resolve()
            candidate.relative_to(Path(args.project).resolve())
            changed = candidate
            original_bytes = candidate.read_bytes()
            original = original_bytes
            candidate.write_bytes(original_bytes + b"\n")
            stale = client.call_tool("cgc_stale_paths", {})
            stale_paths = stale.get("stale_paths", [])
            report["stale_paths"] = stale_paths
            report["checks"]["changed_file_detected"] = args.changed_file in stale_paths
            incremental, incremental_ms = timed_call(client, "cgc_index", {"path": args.changed_file})
            report["incremental_queue_ms"] = round(incremental_ms, 3)
            incremental_final = wait_for_job(client, incremental["job_id"], args.timeout)
            report["incremental_status"] = incremental_final
            report["checks"]["incremental_completed"] = incremental_final.get("state") == "completed"
        report["ok"] = all(value is True for key, value in report["checks"].items() if key not in {"tools_active"})
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
    finally:
        if changed is not None and original is not None:
            changed.write_bytes(original)
        client.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
