"""Compatibility entry point for the correctness-gated V8 performance suite.

This legacy command now delegates to the canonical benchmark harness so tool
schemas, correctness gates, executable identity, and result reporting cannot
drift independently.
"""
from __future__ import annotations

from performance_suite import main as performance_suite_main


if __name__ == "__main__":
    raise SystemExit(performance_suite_main(default_rounds=10))
