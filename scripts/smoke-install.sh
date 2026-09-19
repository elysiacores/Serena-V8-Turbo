#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# A real clean-install smoke must not inherit the developer's Serena config,
# caches, trust settings, or registered projects from HOME.
export HOME="$TMP/home"
mkdir -p "$HOME"
export UV_TOOL_DIR="$TMP/tools"
export UV_TOOL_BIN_DIR="$TMP/bin"
mkdir -p "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"

WHEEL_DIR="$TMP/wheel"
mkdir -p "$WHEEL_DIR"
uv build --wheel --out-dir "$WHEEL_DIR" "$ROOT" >/dev/null
WHEEL="$(find "$WHEEL_DIR" -name 'serena_agent-*.whl' -print -quit)"
test -n "$WHEEL"

# Fresh install.
uv tool install "$WHEEL" >/dev/null
TOOL_PY="$UV_TOOL_DIR/serena-agent/bin/python"
EXPECTED_VERSION="$("$TOOL_PY" -c 'import importlib.metadata as m; print(m.version("serena-agent"))')"
[[ "$("$UV_TOOL_BIN_DIR/serena" --version)" == "Serena $EXPECTED_VERSION" ]]
PATH="$UV_TOOL_BIN_DIR:$PATH" "$UV_TOOL_BIN_DIR/serena-v8-doctor"

# Functional MCP probe from the clean wheel: startup, initialize and a real
# tools/call must all work before installation is considered healthy.
"$TOOL_PY" "$ROOT/benchmarks/mcp_latency_benchmark.py" \
  --project "$ROOT" \
  --tool list_dir \
  --arguments '{"relative_path":".","recursive":false}' \
  --rounds 2 \
  --timeout 120 \
  --executable "$UV_TOOL_BIN_DIR/serena" \
  --expected-version "$EXPECTED_VERSION" \
  --expect-contains pyproject.toml >"$TMP/mcp-smoke.json"
python3 - "$TMP/mcp-smoke.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report["validation"]["status"] == "passed"
assert report["rounds"] == 2
print("MCP functional smoke: PASS")
PY

# Broad CI guardrails catch catastrophic latency/memory regressions without
# pretending shared CI runners are stable enough for microbenchmark thresholds.
"$TOOL_PY" "$ROOT/benchmarks/performance_gate.py" \
  --current "$TMP/mcp-smoke.json" \
  --max-warm-p95-ms 250 \
  --max-startup-ms 15000 \
  --max-rss-mb 700

"$TOOL_PY" "$ROOT/benchmarks/mcp_latency_benchmark.py" \
  --project "$ROOT" \
  --tool find_symbol \
  --arguments '{"name_path_pattern":"SmartScheduler","relative_path":"src/serena_v8/scheduler.py"}' \
  --rounds 1 \
  --timeout 120 \
  --executable "$UV_TOOL_BIN_DIR/serena" \
  --expected-version "$EXPECTED_VERSION" \
  --expect-contains SmartScheduler >"$TMP/semantic-smoke.json"
python3 - "$TMP/semantic-smoke.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report["validation"]["status"] == "passed"
print("Semantic MCP smoke: PASS")
PY

# Migration: replace upstream 1.7 in a clean tool root with this V8 wheel.
rm -rf "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"
mkdir -p "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"
uv tool install 'serena-agent==1.7.0' >/dev/null
[[ "$("$UV_TOOL_BIN_DIR/serena" --version)" == 'Serena 1.7.0' ]]
uv tool install --force "$WHEEL" >/dev/null
[[ "$("$UV_TOOL_BIN_DIR/serena" --version)" == "Serena $EXPECTED_VERSION" ]]
PATH="$UV_TOOL_BIN_DIR:$PATH" "$UV_TOOL_BIN_DIR/serena-v8-doctor"

# Rollback to upstream and forward again must be a normal replacement.
uv tool install --force serena-agent >/dev/null
ROLLBACK_VERSION="$("$UV_TOOL_BIN_DIR/serena" --version)"
[[ "$ROLLBACK_VERSION" == Serena\ * ]]
[[ "$ROLLBACK_VERSION" != "Serena $EXPECTED_VERSION" ]]
uv tool install --force "$WHEEL" >/dev/null
[[ "$("$UV_TOOL_BIN_DIR/serena" --version)" == "Serena $EXPECTED_VERSION" ]]

# Uninstall/reinstall must remain package-manager clean.
uv tool uninstall serena-agent >/dev/null
[[ ! -e "$UV_TOOL_BIN_DIR/serena" ]]
uv tool install "$WHEEL" >/dev/null
[[ "$("$UV_TOOL_BIN_DIR/serena" --version)" == "Serena $EXPECTED_VERSION" ]]

echo 'install smoke: PASS'
