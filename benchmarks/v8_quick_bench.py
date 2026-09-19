"""Compatibility entry point for the correctness-gated V8 performance suite.

Uses the same validated workloads and explicit executable identity checks as
``performance_suite.py``; only the default round count differs.
"""
from __future__ import annotations

from performance_suite import main as performance_suite_main


if __name__ == "__main__":
    raise SystemExit(performance_suite_main(default_rounds=3))
