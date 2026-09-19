# Serena V8 — Installation & Troubleshooting

Serena V8 is a replacement fork of Serena and is distributed with the package name `serena-agent`. There must be exactly one package-manager owner for the `serena/` package. Do not combine an upstream `serena-agent` install with a legacy `serena-v8` distribution or a manual `site-packages` overlay.

## Supported installation model

Use `uv tool` for installation, upgrades, and rollback:

```bash
# Fresh install or upgrade from Serena 1.x
uv tool install --force git+https://github.com/elysiacores/Serena-V8-Turbo.git

# Verify
serena --version
serena-v8-doctor
uv tool list
```

Expected V8 release identity:

```text
Serena 8.0.0a1
```

For a local checkout:

```bash
SERENA_V8_SOURCE=. ./scripts/install-v8.sh
```

Do not use `pip install`, `uv pip install`, or copy `src/serena` into another tool environment. Those commands target different environment models and can create an installation that works temporarily while package metadata still belongs to another distribution.

## Why old installs broke

Older V8 instructions could put both `serena-agent` and `serena-v8` metadata in one Python environment while both distributions owned the same `serena/` files. Uninstalling either distribution could then remove files required by the other. Manual overlays had the inverse problem: runtime files were V8 while package-manager metadata still reported Serena 1.x.

The current release fixes this by using `serena-agent` as the single distribution identity and keeping V8-specific code under `serena_v8/` inside the same wheel.

## Diagnose an installation

Run:

```bash
serena-v8-doctor
which serena
serena --version
uv tool list
```

A healthy V8 install reports the same version (`8.0.0a1`) for the distribution and runtime and does not report a legacy `serena-v8` distribution.

If `serena-v8-doctor` says the executable is outside the active environment, inspect your PATH:

```bash
type -a serena
uv tool dir --bin
```

Then update your shell once if the uv tool bin directory is missing:

```bash
uv tool update-shell
```

Open a new shell and rerun the doctor.

## Migrating an old manual overlay

A manual overlay cannot be repaired reliably by copying more files over it. Reinstall the tool environment from package metadata:

```bash
# Remove a legacy V8 tool if it exists under the old distribution name.
uv tool uninstall serena-v8 2>/dev/null || true

# Replace whichever serena-agent tool environment currently exists.
uv tool install --force git+https://github.com/elysiacores/Serena-V8-Turbo.git

serena-v8-doctor
```

Your user configuration under `~/.serena/` and project `.serena/` directories is not stored inside the tool environment and is not removed by this operation.

## `serena --version` still shows 1.x

The shell is finding a different executable. Check every candidate:

```bash
type -a serena
uv tool list
```

Reinstall V8 and refresh PATH:

```bash
uv tool install --force git+https://github.com/elysiacores/Serena-V8-Turbo.git
uv tool update-shell
```

Do not fix this by copying files into whichever `site-packages` directory happens to be returned by `python`; that Python may not be the interpreter used by the uv tool.

## MCP starts but semantic tools fail

First verify the same executable outside the MCP client:

```bash
SERENA_BIN="$(command -v serena)"
"$SERENA_BIN" start-mcp-server --transport stdio --project /absolute/path/to/project --tool-timeout 100 --log-level INFO
```

If this works directly but not from a service/tunnel, the issue is usually the service environment or executable path. Configure the service with the absolute result of `command -v serena`; do not paste `/home/user/...` paths from another machine.

## Startup versus first heavy semantic query

By default V8 creates Serena's native language-server manager before MCP readiness but does not force an extra semantic warm-up probe. This keeps startup lower for sessions that mostly use filesystem or lightweight symbol tools.

For reference-heavy sessions, opt into semantic prewarm:

```bash
export SERENA_V8_SEMANTIC_PREWARM=1
```

This deliberately trades a slower MCP startup for a faster first deep LSP query such as `find_referencing_symbols`. It does not change warm steady-state behavior.

## TypeScript/Svelte language server does not start

Services often have a smaller PATH than an interactive shell. Verify Node from the same service account:

```bash
command -v node
command -v npm
node --version
```

Add the actual Node directory and the uv tool bin directory to the service PATH. Avoid assuming a Hermes, nvm, fnm, or system Node location exists on another machine.

## External edits look stale

V8 synchronizes external file changes before semantic queries. POSIX uses the fast mtime/ctime/size metadata path by default; `ctime_ns` still changes when content is rewritten even if a tool restores the old mtime. Windows uses content hashing because ctime semantics differ. On coarse or unusual filesystems you can opt into a short digest window with `SERENA_V8_FRESHNESS_HASH_WINDOW_MS` (milliseconds), or enable strict hashing below.

For filesystems or tooling that preserve both mtime and ctime, enable strict hashing:

```bash
export SERENA_V8_STRICT_FRESHNESS_HASH=1
```

Strict mode is correctness-first and costs more I/O on large repositories.

## Telemetry and latency

Per-workspace telemetry is written below:

```text
~/.serena-v8/stats/
```

List snapshots:

```bash
python -c "from pathlib import Path; print(*Path.home().glob('.serena-v8/stats/*.json'), sep='\n')"
```

The live dispatcher records total latency plus scheduler queue and execution stages. Use these values to distinguish scheduler contention from the actual semantic operation before changing concurrency limits.

## Benchmark correctly

The MCP latency benchmark requires an explicit executable and expected version so PATH mistakes cannot silently benchmark another Serena:

```bash
python benchmarks/mcp_latency_benchmark.py \
  --project /absolute/path/to/project \
  --tool list_dir \
  --arguments '{"relative_path":".","recursive":false}' \
  --rounds 10 \
  --executable "$(command -v serena)" \
  --expected-version 8.0.0a1 \
  --expect-contains files
```

The report includes warm P50/P95/P99, startup/first-call latency, and RSS for the Serena process plus recursive child/LSP processes. Run `benchmarks/performance_suite.py` for the default multi-tool self-suite, and use `benchmarks/performance_gate.py` with a saved baseline to turn P50/P95/P99, startup, and process-tree RSS regressions into a failing CI/local check.

## Roll back to upstream Serena

Because V8 now uses the same distribution identity, rollback is a normal tool replacement rather than an uninstall dance:

```bash
uv tool install --force serena-agent
serena --version
```

To return to V8 later:

```bash
uv tool install --force git+https://github.com/elysiacores/Serena-V8-Turbo.git
serena-v8-doctor
```

## Installation smoke test for contributors

From the repository root:

```bash
./scripts/smoke-install.sh
```

The smoke test builds the wheel and verifies fresh install, migration from upstream Serena 1.7, rollback to upstream, forward migration back to V8, uninstall/reinstall, CLI identity, the installation doctor, a real MCP functional probe, and a semantic MCP probe in isolated uv tool directories.
