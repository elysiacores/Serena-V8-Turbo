#!/usr/bin/env bash
set -euo pipefail

SOURCE="${SERENA_V8_SOURCE:-git+https://github.com/elysiacores/Serena-V8-Turbo.git}"
EXPECTED_VERSION="${SERENA_V8_VERSION:-}"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required. Install uv first, then rerun this script." >&2
  exit 2
fi

# Old V8 releases used a second distribution name while exporting the same
# executables/package. Remove that legacy tool first to avoid executable and
# file-ownership collisions. This is a no-op when it is not installed.
uv tool uninstall serena-v8 >/dev/null 2>&1 || true

# Some legacy instructions pip-installed the old serena-v8 distribution inside
# the serena-agent tool environment. Remove that metadata/files first; the
# replacement install below immediately rebuilds the complete environment.
TOOL_ROOT="$(uv tool dir)/serena-agent"
if [[ -x "$TOOL_ROOT/bin/python" ]]; then
  uv pip uninstall --python "$TOOL_ROOT/bin/python" serena-v8 >/dev/null 2>&1 || true
fi

# V8 intentionally uses the upstream distribution identity (serena-agent) as a
# replacement fork. --force upgrades/downgrades the existing tool atomically
# instead of creating two owners for the same serena/ package.
uv tool install --force "$SOURCE"

if ! command -v serena >/dev/null 2>&1; then
  echo "ERROR: uv installed Serena but its tool bin directory is not on PATH." >&2
  echo "Run: uv tool update-shell" >&2
  exit 3
fi

actual="$(serena --version | sed -E 's/^Serena[[:space:]]+//')"
if [[ -n "$EXPECTED_VERSION" && "$actual" != "$EXPECTED_VERSION" ]]; then
  echo "ERROR: expected Serena $EXPECTED_VERSION, got $actual" >&2
  echo "serena executable: $(command -v serena)" >&2
  exit 4
fi

# The doctor derives the canonical expected version from the installed V8
# package, so normal upgrades do not require editing this installer.
serena-v8-doctor
printf 'Serena V8 %s installed successfully via %s\n' "$actual" "$SOURCE"
