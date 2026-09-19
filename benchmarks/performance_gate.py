"""Fail a build when a Serena V8 benchmark violates absolute or baseline limits."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def metric(data: dict, dotted: str) -> float:
    value = data
    for part in dotted.split("."):
        value = value[part]
    return float(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--max-regression-percent", type=float, default=15.0)
    parser.add_argument("--max-warm-p95-ms", type=float)
    parser.add_argument("--max-startup-ms", type=float)
    parser.add_argument("--max-rss-mb", type=float)
    args = parser.parse_args()

    current = load(args.current)
    failures: list[str] = []
    if current.get("validation", {}).get("status") != "passed":
        failures.append("benchmark correctness validation did not pass")

    absolute = {
        "warm.p95_ms": args.max_warm_p95_ms,
        "startup_ms": args.max_startup_ms,
        "rss_mb": args.max_rss_mb,
    }
    for name, limit in absolute.items():
        if limit is None:
            continue
        try:
            value = metric(current, name)
        except (KeyError, TypeError, ValueError):
            failures.append(f"missing metric: {name}")
            continue
        if value > limit:
            failures.append(f"{name}={value:.3f} exceeds limit {limit:.3f}")

    if args.baseline:
        baseline = load(args.baseline)
        for name in ("warm.p50_ms", "warm.p95_ms", "warm.p99_ms", "startup_ms", "first_call_ms", "rss_mb"):
            try:
                before = metric(baseline, name)
                after = metric(current, name)
            except (KeyError, TypeError, ValueError):
                continue
            if before <= 0:
                continue
            regression = (after - before) / before * 100
            if regression > args.max_regression_percent:
                failures.append(
                    f"{name} regressed {regression:.1f}% ({before:.3f} -> {after:.3f}); "
                    f"limit {args.max_regression_percent:.1f}%"
                )

    if failures:
        print("PERFORMANCE GATE: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PERFORMANCE GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
