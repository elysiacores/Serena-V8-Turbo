#!/usr/bin/env python3
"""Benchmark steady-state freshness polling and recent-file race safety without an LSP."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
from types import SimpleNamespace

from serena.ls_manager import LanguageServerFileChangeNotifier


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument(
        "--hash-window-ms",
        type=int,
        default=2000,
        help="Recent-file digest safety window; defaults to the production value.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=-1,
        help="Age generated files before steady-state timing; -1 means hash-window + 50 ms.",
    )
    args = parser.parse_args()
    if args.files < 1 or args.rounds < 1 or args.hash_window_ms < 0:
        parser.error("--files/--rounds must be positive and --hash-window-ms non-negative")

    settle_ms = args.settle_ms if args.settle_ms >= 0 else args.hash_window_ms + 50
    old_window = os.environ.get("SERENA_V8_FRESHNESS_HASH_WINDOW_MS")
    os.environ["SERENA_V8_FRESHNESS_HASH_WINDOW_MS"] = str(args.hash_window_ms)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names: list[str] = []
            for index in range(args.files):
                name = f"module_{index:05d}.py"
                (root / name).write_text(f"value = {index:05d}\n")
                names.append(name)

            # Measure the normal steady-state path, not the intentionally more
            # expensive short digest window immediately after a checkout/edit.
            if settle_ms:
                time.sleep(settle_ms / 1000)

            project = SimpleNamespace(project_root=directory, gather_source_files=lambda: list(names))
            manager = SimpleNamespace(iter_language_servers=lambda: iter(()))
            notifier = LanguageServerFileChangeNotifier(project, manager)

            stable_ms: list[float] = []
            for _ in range(args.rounds):
                started = time.perf_counter()
                changed = notifier.poll_and_notify()
                stable_ms.append((time.perf_counter() - started) * 1000)
                if changed != 0:
                    raise RuntimeError(f"stable poll unexpectedly reported {changed} changes")

            # Stable-file preserved-mtime edit: POSIX ctime normally catches it.
            target = root / names[len(names) // 2]
            before = target.stat()
            original = target.read_text()
            replacement = original.replace("value", "other", 1)
            if len(replacement) != len(original):
                raise AssertionError("benchmark edit must preserve file size")
            target.write_text(replacement)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

            started = time.perf_counter()
            stable_changed = notifier.poll_and_notify()
            stable_change_ms = (time.perf_counter() - started) * 1000
            if stable_changed != 1:
                raise RuntimeError(f"stable preserved-mtime edit was not detected: changed={stable_changed}")

            # Recent-file race: establish a digest-bearing baseline, then make an
            # immediate same-size edit while preserving mtime. This exercises the
            # safety window that protects coarse timestamp filesystems.
            recent_name = "recent_race.py"
            recent = root / recent_name
            recent.write_text("value = 10000\n")
            names.append(recent_name)
            if notifier.poll_and_notify() != 1:
                raise RuntimeError("recent race fixture creation was not detected")
            recent_stamp = recent.stat()
            recent.write_text("value = 20000\n")
            os.utime(recent, ns=(recent_stamp.st_atime_ns, recent_stamp.st_mtime_ns))

            started = time.perf_counter()
            recent_changed = notifier.poll_and_notify()
            recent_change_ms = (time.perf_counter() - started) * 1000
            if recent_changed != 1:
                raise RuntimeError(f"recent preserved-mtime edit was not detected: changed={recent_changed}")

            report = {
                "files": args.files,
                "rounds": args.rounds,
                "hash_window_ms": args.hash_window_ms,
                "settle_ms": settle_ms,
                "stable_poll_ms": {
                    "p50": round(statistics.median(stable_ms), 3),
                    "p95": round(percentile(stable_ms, 95), 3),
                    "p99": round(percentile(stable_ms, 99), 3),
                    "max": round(max(stable_ms), 3),
                },
                "stable_preserved_mtime_change_ms": round(stable_change_ms, 3),
                "recent_preserved_mtime_change_ms": round(recent_change_ms, 3),
                "validation": {
                    "status": "passed",
                    "stable_detected_changes": stable_changed,
                    "recent_detected_changes": recent_changed,
                },
            }
            print(json.dumps(report, indent=2))
            return 0
    finally:
        if old_window is None:
            os.environ.pop("SERENA_V8_FRESHNESS_HASH_WINDOW_MS", None)
        else:
            os.environ["SERENA_V8_FRESHNESS_HASH_WINDOW_MS"] = old_window


if __name__ == "__main__":
    raise SystemExit(main())
