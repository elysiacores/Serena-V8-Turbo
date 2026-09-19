#!/usr/bin/env python3
"""Run a correctness-gated MCP performance suite and save per-workload reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LATENCY_BENCH = ROOT / "benchmarks" / "mcp_latency_benchmark.py"

DEFAULT_WORKLOADS: list[dict[str, Any]] = [
    {
        "name": "list_dir_root",
        "tool": "list_dir",
        "arguments": {"relative_path": ".", "recursive": False},
        "expect_contains": ["README.md"],
    },
    {
        "name": "find_symbol_scheduler",
        "tool": "find_symbol",
        "arguments": {
            "name_path_pattern": "get_scheduler",
            "relative_path": "src/serena_v8/scheduler.py",
            "include_body": False,
            "max_matches": 1,
        },
        "expect_contains": ["get_scheduler"],
    },
    {
        "name": "references_scheduler",
        "tool": "find_referencing_symbols",
        "arguments": {
            "name_path": "get_scheduler",
            "relative_path": "src/serena_v8/scheduler.py",
        },
        "expect_contains": ["src/serena_v8/runtime/dispatcher.py"],
    },
    {
        "name": "search_dispatcher",
        "tool": "search_for_pattern",
        "arguments": {
            "substring_pattern": "get_dispatcher",
            "relative_path": "src",
            "restrict_search_to_code_files": True,
        },
        "expect_contains": ["src/serena/tools/tools_base.py"],
    },
    {
        "name": "scheduler_overview",
        "tool": "get_symbols_overview",
        "arguments": {
            "relative_path": "src/serena_v8/scheduler.py",
            "depth": 1,
        },
        "expect_contains": ["SmartScheduler"],
    },
]


def load_workloads(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return DEFAULT_WORKLOADS
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list) or not payload:
        raise ValueError("workload file must contain a non-empty JSON list")
    return payload


def run_workload(
    workload: dict[str, Any],
    *,
    project: str,
    executable: str,
    expected_version: str,
    rounds: int,
    timeout: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(LATENCY_BENCH),
        "--project",
        project,
        "--tool",
        str(workload["tool"]),
        "--arguments",
        json.dumps(workload.get("arguments", {}), separators=(",", ":")),
        "--rounds",
        str(rounds),
        "--timeout",
        str(timeout),
        "--executable",
        executable,
        "--expected-version",
        expected_version,
    ]
    for expected in workload.get("expect_contains", []):
        command.extend(["--expect-contains", str(expected)])
    completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout * (rounds + 5))
    if completed.returncode != 0:
        raise RuntimeError(
            f"{workload.get('name', workload['tool'])} failed:\n{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(ROOT))
    parser.add_argument("--executable", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--workloads", help="JSON workload list; defaults to Serena self-benchmarks")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output-dir", default=str(ROOT / "benchmarks" / "results"))
    args = parser.parse_args()

    workloads = load_workloads(args.workloads)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir).resolve() / f"suite-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    reports: dict[str, dict[str, Any]] = {}
    for workload in workloads:
        name = str(workload.get("name") or workload["tool"])
        report = run_workload(
            workload,
            project=str(Path(args.project).resolve()),
            executable=str(Path(args.executable).resolve()),
            expected_version=args.expected_version,
            rounds=args.rounds,
            timeout=args.timeout,
        )
        reports[name] = report
        (output_dir / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            f"{name}: p50={report['warm']['p50_ms']:.3f}ms "
            f"p95={report['warm']['p95_ms']:.3f}ms "
            f"startup={report['startup_ms']:.3f}ms rss={report['rss_mb']:.3f}MB"
        )

    summary = {
        "project": str(Path(args.project).resolve()),
        "expected_version": args.expected_version,
        "rounds": args.rounds,
        "workloads": reports,
        "summary": {
            "max_warm_p95_ms": max(report["warm"]["p95_ms"] for report in reports.values()),
            "max_startup_ms": max(report["startup_ms"] for report in reports.values()),
            "max_rss_mb": max(report["rss_mb"] for report in reports.values()),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"saved={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
