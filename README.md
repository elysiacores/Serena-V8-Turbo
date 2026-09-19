# Serena-V8 (Turbo)

> **The race-ready engine for semantic coding agents — tuned for speed, stability, and the long run.**

High-performance, drop-in replacement for [Serena](https://github.com/oraios/serena) — the semantic coding agent runtime.

V8 is **not** a wrapper. It is an **overlay** that installs directly into the serena-agent package, replacing the runtime while preserving all original tools, CLI commands, and MCP protocol compatibility.

## 📊 Serena vs Serena V8

V8 is a **drop-in overlay** on Serena. It preserves the existing MCP protocol, CLI commands, and tool registry rather than introducing a separate wrapper.

| Area | Original Serena | Serena V8 in production |
|---|---|---|
| MCP launch | Standard Serena process | Same `serena start-mcp-server --transport stdio` command |
| Tools | Original Serena tools | All **36 tools** plus central instrumentation |
| Telemetry | No reliable live per-workspace snapshot | Asynchronous per-workspace telemetry with latency, error, and timeout metrics |
| Cache | Broad invalidation without consistent workspace isolation | Canonical project identity with workspace-scoped invalidation after edits |
| After file edits | Risk of stale LSP/symbol locations | LSP synchronization before subsequent semantic queries plus safety validation |
| Concurrency | Original task executor behavior | Bounded lanes, queue backpressure, single-flight reads, serialized writes |
| LSP startup | Starts when first needed | Prewarmed before MCP readiness and reused through Serena's native manager |
| Symlink/build trees | Risk of scanning dependency/build trees and symlink loops | Symlink directories are not followed; dependency/build output is excluded from the walker |
| Memory | No unified per-tunnel policy | `MemoryHigh=1.4G`, `MemoryMax=2G` per tunnel |
| Monitoring | Process/port checks may be insufficient | Service, port, MCP, metrics, OOM, functional MCP probe, and auth-state checks |
| Auth failures | Can look like a service failure | Classified as `AUTH_BLOCKED` without repeated restart loops |

### Performance measurement policy

Benchmarks separate cold LSP startup from warm tool dispatch because they are different costs and depend on language servers and workspace size:

- **Cold MCP initialization with normal V8 prewarm:** `Test Workspace C` measured approximately **2,220 ms**; the first `list_dir` after readiness took approximately **8 ms**.
- **Warm MCP tool dispatch (standardized probe mode):** `list_dir` was measured for 5 rounds per test workspace using the production overlay and real JSON-RPC stdio. The probe excludes LSP prewarm so it does not create duplicate LSP processes.

| Test workspace | Tools | Warm `list_dir` min | Median | Average | Max |
|---|---:|---:|---:|---:|---:|
| Test Workspace A | 36 | 5.22 ms | 6.76 ms | 7.57 ms | 12.78 ms |
| Test Workspace B | 36 | 5.67 ms | 6.80 ms | 8.68 ms | 15.26 ms |
| Test Workspace C | 36 | 3.27 ms | 3.68 ms | 4.55 ms | 8.47 ms |
| Test Workspace D | 36 | 3.40 ms | 3.96 ms | 4.72 ms | 8.42 ms |

These are **measured production-overlay benchmarks**, not synthetic figures. They should not be interpreted as the latency of every semantic query, especially `find_symbol`, references, and diagnostics, which depend on LSP state and repository contents.


## 🛠️ What V8 Fixes (Production Bug Fixes)

### 1. LSP Document Synchronization
**Problem:** After file edits, LSP still holds stale symbol locations → `rename_symbol` edits the wrong line.
**Fix:** The live edit path writes the file, invalidates the affected Workspace's semantic cache, then uses Serena's active `Project`/`LanguageServerManager` filesystem synchronization before the next semantic query.

### 2. Cache Invalidation
**Problem:** V8 symbol cache returns stale results after edits.
**Fix:** Invalidation is bound to the canonical Workspace identity. Edits invalidate that Workspace's semantic results while preserving cache entries belonging to other Workspaces.

### 3. Rename Safety Guard
**Problem:** Stale cached locations cause edits to wrong symbols.
**Fix:** `SafetyGuard` verifies symbol name at cached location before rename/delete. Aborts with clear error on mismatch.

### 4. Symlink Loop Prevention
**Problem:** `find_file` crashes with "Too many levels of symbolic links" on `.next-build/standalone/node_modules/node_modules/...`.
**Fix:** `scan_directory` uses `follow_symlinks=False` + excludes build artifacts.

### 5. Node PATH for Systemd
**Problem:** Systemd services don't load `.bashrc` → Node not in PATH → LSP (svelte/typescript) fails to start → tunnel crash loop.
**Fix:** `Environment="PATH=..."` in service files + `node/npm/npx` symlinks to `~/.local/bin`.

### 6. Circular Import Fix
**Problem:** `__init__.py` → `lsp_sync` → `symbol` → `__init__` → tunnel crashes on startup.
**Fix:** Lazy imports — `lsp_sync` loaded after `serena` module initializes.

## 📦 V8 Upgrade Phases

| Phase | Upgrade | Status |
|---|---|---|
| 1 | Runtime identity + async telemetry | **Live** |
| 2 | Core daemon / Unix socket | Diagnostic/experimental; not used by normal MCP path |
| 3 | Smart Scheduler — lanes, single-flight, backpressure | **Live** |
| 4 | Persistent Symbol Index — SQLite/FTS5 | Diagnostic/experimental; native LSP remains authoritative |
| 5 | LSP lifecycle management | **Live via native Serena manager + V8 prewarm** |
| 6 | Multi-tier cache | Diagnostic/experimental; live path uses bounded V8 query cache |
| 7 | Pipe/502 hardening + watchdog | **Live** |
| 8 | P50/P95 telemetry and runtime metrics | **Live** |
| 9 | Response optimization modules | Diagnostic/experimental; not enabled globally |
| 10 | Multi-Workspace integration verification | **Live: 20 tests + 4/4 MCP probes** |
| 8.5 | Correctness: LSP sync, cache invalidation, edit safety | **Live** |
| 8.8 | Production wiring, MCP probe, auth circuit breaker | **Live** |

### Live production wiring

The drop-in production path is the normal `serena start-mcp-server --transport stdio` command. The live MCP request path now includes:

- workspace-isolated asynchronous telemetry and bounded metrics snapshots;
- per-workspace selective semantic-cache invalidation after edits, followed by Serena's authoritative LSP filesystem synchronization;
- a bounded lane scheduler with single-flight reads, write serialization, queue backpressure, and request deadlines;
- LSP prewarm before MCP readiness, while reusing Serena's native `LanguageServerManager` rather than starting a competing supervisor;
- watchdog probes that exercise `initialize`, `tools/list`, and `list_dir` over real stdio MCP;
- an `AUTH_BLOCKED` state for tunnel authorization failures, with restart suppression because restarts cannot grant permission.

The standalone V8 daemon/index/cache modules remain diagnostic/experimental components and are not silently presented as part of the live MCP path. This preserves drop-in compatibility and avoids replacing Serena's authoritative LSP and project lifecycle.

### Tunnel isolation invariant

Each production Tunnel is intentionally single-project:

```text
1 Tunnel ID = 1 Serena process = 1 active folder = 1 LSP/cache/telemetry state
```

A Serena process started with `--project <folder>` rejects attempts to activate a different project. Multiple folders must use separate Tunnel IDs and separate Serena processes. This prevents cross-project cache, LSP, telemetry, and edit state from being mixed.

### Current verified release

- Serena V8: `8.0.0-dev.1`
- Live tools: **36/36**
- Regression/integration tests: **19/19 passed**
- Functional MCP probe: **4/4 test workspaces passed**
- Local readiness: **4/4 test workspace endpoints ready**
- Memory policy: `MemoryHigh=1.4G`, `MemoryMax=2G` per Tunnel

## 🚀 Installation

### Quick Install (Overlay on serena-agent)

```bash
# 1. Install serena-agent (if not already)
uv tool install serena-agent

# 2. Clone V8
git clone https://github.com/elysiacores/serena-v8.git
cd serena-v8

# 3. Copy V8 over serena-agent
SERENA_SITE=$(python -c "import serena; import os; print(os.path.dirname(serena.__file__))")
cp -r src/serena/* "$SERENA_SITE/"
cp -r src/serena_v8 "$SERENA_SITE/../"

# Remove legacy stats writers if an older V8 overlay installed one.
mv "$SERENA_SITE/sitecustomize.py" "$SERENA_SITE/sitecustomize.py.disabled" 2>/dev/null || true

# 4. Create node symlinks (if using svelte/typescript LSP)
ln -sf $(which node) ~/.local/bin/node
ln -sf $(which npm) ~/.local/bin/npm

# 5. Verify
serena --version  # → Serena 8.0.0-dev.1
```

### Standalone Tunnel + V8 (Production)

```bash
# Create tunnel profile
cat > ~/.config/tunnel-client/my-project.yaml << EOF
admin_ui:
  open_browser: false
config_version: 1
control_plane:
  api_key: file:~/.config/agent-secrets/my-project-api-key
  base_url: https://api.openai.com
  tunnel_id: tunnel_xxxx
health:
  listen_addr: 0.0.0.0:8787
log:
  level: info
mcp:
  commands:
    default:
      command: /home/user/.local/bin/serena start-mcp-server --transport stdio --project /path/to/project --tool-timeout 100 --log-level WARNING --context desktop-app
EOF

# Create systemd service
cat > ~/.config/systemd/user/my-project-tunnel.service << EOF
[Unit]
Description=My Project Tunnel
After=network.target

[Service]
Type=simple
ExecStart=/home/user/.local/bin/tunnel-client run --profile my-project
Restart=always
RestartSec=10
MemoryHigh=1.4G
MemoryMax=2G
CPUQuota=50%
Environment="PATH=/home/user/.hermes/node/bin:/home/user/.local/bin:/home/user/.local/share/uv/tools/serena-agent/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=default.target
EOF

# Enable + start
systemctl --user daemon-reload
systemctl --user enable --now my-project-tunnel.service
```

## 🔧 Configuration

### Global Config (`~/.serena/serena_config.yml`)

```yaml
language_backend: LSP
tool_timeout: 100
gui_log_window: false
web_dashboard: false
```

### Project Config (`<project>/.serena/project.yml`)

```yaml
language_servers:
  - svelte  # or typescript, go, etc.

# Keep dependency/build trees out of Serena's symbol index. LSP resolves
# dependencies through its own workspace, so Serena does not need to enumerate
# node_modules itself.
ignore_all_files_in_gitignore: true

# Exclude heavy build artifacts
ignored_paths:
  - "**/.next*/**"
  - "**/dist/**"
  - "**/build/**"
  - "**/.cache/**"
  - "**/vendor/**"
  - "**/.git/**"
  - "**/node_modules/**"
```

### Tunnel Service File

**Critical:** Must include `Environment="PATH=..."` with node + serena paths:

```ini
[Service]
Environment="PATH=/home/user/.hermes/node/bin:/home/user/.local/bin:/usr/local/bin:/usr/bin:/bin"
```

Without this, LSP (svelte/typescript) fails to find `node` → tunnel crash loop.

## 🔍 Checking V8 Status

```bash
# Version
serena --version
# → Serena 8.0.0-dev.1

# Runtime identity
python -c "import serena; print(serena.get_v8_identity())"

# Cache/metrics stats (one file per Workspace)
python -c "from pathlib import Path; print(*Path.home().glob('.serena-v8/stats/*.json'), sep='\\n')"

# Watchdog status
cat ~/.serena-v8/watchdog-status.json
```

## Optional CGC and ast-grep Sidecars

V8 can use CGC and ast-grep as isolated, read-only sidecars without replacing
Serena's native LSP, cache, editing, or Workspace ownership. Sidecars run in the
active canonical Workspace only, use bounded subprocess deadlines, cap output,
and return `unavailable` when their executable or configuration is absent.

Install ast-grep separately if desired:

```bash
uv tool install ast-grep-cli
```

Configure the optional MCP tools per installation:

```bash
export SERENA_V8_AST_GREP_BIN="ast-grep"
export SERENA_V8_CGC_BIN="cgc"
export SERENA_V8_CGC_DATABASE="kuzudb"
export SERENA_V8_CGC_DB_ROOT="$HOME/.serena-v8/cgc"
export SERENA_V8_SIDECAR_TIMEOUT_MS="5000"             # ast-grep and fallback default
export SERENA_V8_CGC_QUERY_TIMEOUT_MS="5000"           # callers, callees, query
export SERENA_V8_CGC_INCREMENTAL_INDEX_TIMEOUT_MS="10000" # file/scoped index
export SERENA_V8_CGC_FULL_INDEX_TIMEOUT_MS="120000"    # background full workspace index
export SERENA_V8_SIDECAR_MAX_OUTPUT_BYTES="5242880"
# Optional: enable CGC's language-aware SCIP resolver in the runtime environment:
# cgc config set SCIP_INDEXER true
# cgc config set SCIP_LANGUAGES go,typescript,javascript
# Optional custom CGC query command; {workspace_root} is replaced safely:
# export SERENA_V8_CGC_COMMAND="cgc --database kuzudb --path {workspace_root} query"
```

The optional tools are `ast_grep_search`, `ast_grep_rewrite`, `cgc_index`,
`cgc_index_status`, `cgc_stale_paths`, `cgc_callers`, `cgc_callees`, and
`cgc_query`. `cgc_index` queues a bounded, per-Workspace background job and
returns a job ID immediately; poll it with `cgc_index_status` before querying
relationships. `cgc_stale_paths` reports files changed since the last successful
index so callers can submit only those paths for incremental indexing. Full-workspace jobs build in a Workspace-isolated staging KuzuDB and atomically promote it only after success; relationship queries continue reading the previous active graph during the build. Indexing is scoped to the requested Workspace-relative path and skips dependency/build
folders. `ast_grep_rewrite` is preview-only unless `approved=true` is explicitly
provided; approved single-file rewrites invalidate V8 caches, notify the native
LSP, request diagnostics, and roll back the file if synchronization or
diagnostics fail.

### CGC Health Benchmark

Run the generic end-to-end health check against any Workspace:

```bash
python benchmarks/cgc_health_benchmark.py \
  --project /path/to/workspace \
  --index-path src/example.ts \
  --function exampleFunction \
  --query-path src/example.ts \
  --changed-file src/example.ts
```

Add `--full` to validate staging promotion and relationship queries while the
full index is running. Use `--expected-caller` and one or more
`--expected-callee` options for repository-specific resolution regression
checks. The harness verifies active tools, job lifecycle, stale detection,
incremental indexing, query concurrency, process cleanup, and unresolved-call
counts without embedding a real project path in this repository.

## 📊 Monitoring

### Watchdog (Auto-Restart)

V8 includes `workspace-watchdog-v8.py` — monitors systemd, local health, MCP
processes, control-plane poll freshness, OOM/authorization errors, and runs a
real stdio MCP functional probe (`initialize`, `tools/list`, `list_dir`). It
checks every 30s; the functional probe is cached for 5 minutes:

```bash
systemctl --user enable --now serena-v8-watchdog.service
cat ~/.serena-v8/watchdog-status.json
```

Probe targets are intentionally not bundled in the repository. Configure them
for each installation before starting the watchdog, for example:

```bash
export SERENA_V8_PROBE_PROJECTS_JSON='{"workspace-a-tunnel.service":"/path/to/workspace-a"}'
export SERENA_V8_SERENA_BIN="serena"
```

A tunnel with valid local MCP but invalid control-plane permission is reported
as `AUTH_BLOCKED`, not restarted repeatedly.

### Logs

```bash
# Tunnel logs
journalctl --user -u my-project-tunnel.service -f

# Serena MCP logs
ls ~/.serena/logs/

# Watchdog logs
cat ~/.serena-v8/logs/watchdog.log
```

## ⚠️ Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `node is not installed or isn't in PATH` | Systemd service missing PATH | Add `Environment="PATH=..."` to service file |
| `Failed to start language server: svelte` | Node not in PATH for LSP | `ln -sf $(which node) ~/.local/bin/node` |
| `Too many levels of symbolic links` | `scan_directory` follows symlinks | Fixed in V8 — `follow_symlinks=False` |
| `rename_symbol` edits wrong line | LSP has stale file version | Fixed in V8 — LSP sync + cache invalidation |
| Tunnel crash loop | Circular import in `__init__.py` | Fixed in V8 — lazy imports |
| `stats.json` remains at zero | Legacy `sitecustomize.py` is overwriting it | Rename it to `sitecustomize.py.disabled`, restart Serena processes, then make a live MCP tool call |
| `find_symbol` slow (2-3s) | Cold LSP/index startup | Check live per-Workspace telemetry and cache hits after repeated calls |
| Functional probe timeout | Cold language-server startup or excessive probe timeout | Probe uses MCP-only mode; check `watchdog-status.json` and live MCP logs |
| `tunnel_use_forbidden` / `401 Unauthorized` | Tunnel belongs to another OpenAI org/workspace | Use a runtime key with access or recreate the tunnel in the owning org; restarting cannot fix authorization |
| `server/discover` pydantic error | Protocol mismatch (old wrapper script) | Use `serena start-mcp-server` directly, not wrapper |

## 🏗️ Architecture

```
ChatGPT / Hermes / Claude
        │
        ▼
   MCP Tunnel (8787-8791)
        │
        ▼
  tunnel-client (stdio)
        │
        ▼
   serena __init__.py / MCP factory
        │
        ├── V8 Runtime
        │     ├── async per-Workspace telemetry
        │     ├── bounded query cache
        │     └── live tool metrics
        │
        ├── Tool.apply_ex()
        │     └── bounded V8 scheduler
        │           ├── read lanes + single-flight
        │           └── serialized write/refactor lane
        │
        └── Serena native Project + LanguageServerManager
              ├── V8 prewarm before readiness
              ├── LSP reuse/restart lifecycle
              ├── edit → cache invalidation → filesystem sync
              └── original Serena tools (36)

  Separate V8 daemon/index/multi-tier-cache modules are diagnostic and
  are not enabled on the normal MCP path to avoid duplicate state owners.
```

## 📝 License

MIT (same as Serena upstream)

## 🤖 AI Agent Notes

**For AI agents using Serena V8 through MCP:**

1. Always call `initial_instructions` first
2. After any file edit (create/replace/insert), the next tool call will get fresh LSP data automatically
3. `rename_symbol` now validates location before editing — if it fails, read the file first to get current content
4. `find_symbol` is cached — repeated queries return in ~4ms
5. If `find_file` returns "Too many levels of symbolic links", check for symlinks in build directories

---

**GitHub:** https://github.com/elysiacores/serena-v8
