#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EXPECTED_VERSION="${SERENA_V8_VERSION:-}"
ROUNDS="${SERENA_V8_GATE_ROUNDS:-3}"
TIMEOUT="${SERENA_V8_GATE_TIMEOUT:-120}"
MAX_WARM_P95_MS="${SERENA_V8_GATE_MAX_WARM_P95_MS:-250}"
MAX_STARTUP_MS="${SERENA_V8_GATE_MAX_STARTUP_MS:-10000}"
MAX_RSS_MB="${SERENA_V8_GATE_MAX_RSS_MB:-512}"
MAX_FRESHNESS_P95_MS="${SERENA_V8_GATE_MAX_FRESHNESS_P95_MS:-25}"
FRESHNESS_FILES="${SERENA_V8_GATE_FRESHNESS_FILES:-2000}"
RESULTS_ROOT="${SERENA_V8_RESULTS_DIR:-$ROOT/benchmarks/results}"
WORKLOADS_FILE="${SERENA_V8_GATE_WORKLOADS:-}"

if [[ -x "$ROOT/.venv/bin/serena" ]]; then
  SERENA_BIN="$ROOT/.venv/bin/serena"
else
  SERENA_BIN="$(command -v serena || true)"
fi
if [[ -z "$SERENA_BIN" ]]; then
  echo "ERROR: no Serena executable found; run 'uv sync --extra dev' first." >&2
  exit 2
fi
SERENA_BIN="$(realpath "$SERENA_BIN")"
if [[ -z "$EXPECTED_VERSION" ]]; then
  EXPECTED_VERSION="$(uv run --extra dev python -c 'from serena_v8._version import VERSION; print(VERSION)')"
fi

echo "== static quality =="
uv run --extra dev ruff check src benchmarks tests
uv run --extra dev python -m compileall -q src benchmarks tests

echo "== regression suite =="
uv run --extra dev python -m pytest -q

if [[ "${SERENA_V8_SKIP_INSTALL_SMOKE:-0}" != "1" ]]; then
  echo "== clean-wheel install/MCP smoke =="
  bash "$ROOT/scripts/smoke-install.sh"
fi

echo "== MCP performance suite =="
mkdir -p "$RESULTS_ROOT"
SUITE_LOG="$(mktemp)"
trap 'rm -f "$SUITE_LOG"' EXIT
WORKLOAD_ARGS=()
if [[ -n "$WORKLOADS_FILE" ]]; then
  WORKLOAD_ARGS=(--workloads "$WORKLOADS_FILE")
fi
uv run --extra dev python "$ROOT/benchmarks/performance_suite.py" \
  --project "$ROOT" \
  --executable "$SERENA_BIN" \
  --expected-version "$EXPECTED_VERSION" \
  --rounds "$ROUNDS" \
  --timeout "$TIMEOUT" \
  --output-dir "$RESULTS_ROOT" \
  "${WORKLOAD_ARGS[@]}" | tee "$SUITE_LOG"

SUITE_DIR="$(sed -n 's/^saved=//p' "$SUITE_LOG" | tail -1)"
if [[ -z "$SUITE_DIR" || ! -f "$SUITE_DIR/summary.json" ]]; then
  echo "ERROR: performance suite did not produce summary.json" >&2
  exit 3
fi

echo "== freshness performance/correctness =="
FRESHNESS_JSON="$SUITE_DIR/freshness.json"
uv run --extra dev python "$ROOT/benchmarks/freshness_poll_benchmark.py" \
  --files "$FRESHNESS_FILES" \
  --rounds 20 > "$FRESHNESS_JSON"

uv run --extra dev python - "$SUITE_DIR/summary.json" "$FRESHNESS_JSON" \
  "$MAX_WARM_P95_MS" "$MAX_STARTUP_MS" "$MAX_RSS_MB" "$MAX_FRESHNESS_P95_MS" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text())
freshness = json.loads(Path(sys.argv[2]).read_text())
max_p95, max_startup, max_rss, max_freshness = map(float, sys.argv[3:7])

failures = []
workloads = summary.get("workloads", {})
if not workloads:
    failures.append("performance suite returned no workloads")
for name, report in workloads.items():
    if report.get("validation", {}).get("status") != "passed":
        failures.append(f"{name}: correctness validation did not pass")

observed = summary.get("summary", {})
checks = (
    ("max_warm_p95_ms", observed.get("max_warm_p95_ms"), max_p95),
    ("max_startup_ms", observed.get("max_startup_ms"), max_startup),
    ("max_rss_mb", observed.get("max_rss_mb"), max_rss),
)
for name, value, limit in checks:
    if value is None:
        failures.append(f"missing metric: {name}")
    elif float(value) > limit:
        failures.append(f"{name}={float(value):.3f} exceeds {limit:.3f}")

if freshness.get("validation", {}).get("status") != "passed":
    failures.append("freshness correctness validation did not pass")
freshness_p95 = freshness.get("stable_poll_ms", {}).get("p95")
if freshness_p95 is None:
    failures.append("missing freshness P95")
elif float(freshness_p95) > max_freshness:
    failures.append(f"freshness_p95_ms={float(freshness_p95):.3f} exceeds {max_freshness:.3f}")

if failures:
    print("SERENA V8 RELEASE GATE: FAIL")
    for failure in failures:
        print(f"- {failure}")
    raise SystemExit(1)

print("SERENA V8 RELEASE GATE: PASS")
print(
    "max warm P95={:.3f} ms | startup={:.3f} ms | RSS={:.3f} MB | freshness P95={:.3f} ms".format(
        float(observed["max_warm_p95_ms"]),
        float(observed["max_startup_ms"]),
        float(observed["max_rss_mb"]),
        float(freshness_p95),
    )
)
print(f"results={Path(sys.argv[1]).parent}")
PY
