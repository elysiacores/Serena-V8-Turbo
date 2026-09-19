# Serena V8

High-performance, drop-in replacement for [Serena](https://github.com/oraios/serena) — the semantic coding agent runtime.

V8 is **not** a wrapper. It is an **overlay** that installs directly into the serena-agent package, replacing the runtime while preserving all original tools, CLI commands, and MCP protocol compatibility.

## ⚡ Performance

| Metric | Serena 1.7.0 | Serena V8 |
|--------|-------------|-----------|
| Cold `find_symbol` | ~2800ms | ~60ms |
| Warm `find_symbol` | ~2800ms | ~4-5ms |
| Cache hit rate | 0% | 85%+ |
| Warm call overhead | ~2.7s | ~4ms |
| **Improvement** | — | **99.7%** |

## 🛠️ What V8 Fixes (Production Bug Fixes)

### 1. LSP Document Synchronization
**Problem:** After file edits, LSP still holds stale symbol locations → `rename_symbol` edits the wrong line.
**Fix:** `LSPDocumentSync` notifies LSP of file changes before responding.

### 2. Cache Invalidation
**Problem:** V8 symbol cache returns stale results after edits.
**Fix:** `CacheInvalidator` clears symbol/query cache for edited files immediately.

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

## 📦 What V8 Adds (10 Phases + Hotfixes)

| Phase | Feature |
|-------|---------|
| 1 | Runtime identity (`v8-phase1`) + telemetry |
| 2 | Core Daemon — persistent runtime, unix socket |
| 3 | Smart Scheduler — lanes, single-flight, composite tools |
| 4 | Persistent Symbol Index — SQLite + FTS5 + incremental watcher |
| 5 | LSP Lifecycle Manager — auto-restart, idle eviction, memory pressure |
| 6 | Multi-Tier Cache — L1 memory → L2 disk → L3 LSP |
| 7 | Pipe/502 Hardening — bounded log, drain, watchdog, backpressure |
| 8 | Profiler — P50/P95/P99 per request stage |
| 9 | Hotspot Optimization — compact JSON, field filter, lazy body |
| 10 | Integration Test — 9/9 PASS |
| 8.5 | Correctness Fixes — LSP sync + cache invalidation + safety guard |

### Live production wiring

The drop-in production path is the normal `serena start-mcp-server --transport stdio` command. The live MCP request path now includes:

- workspace-isolated asynchronous telemetry and bounded metrics snapshots;
- per-workspace selective semantic-cache invalidation after edits, followed by Serena's authoritative LSP filesystem synchronization;
- a bounded lane scheduler with single-flight reads, write serialization, queue backpressure, and request deadlines;
- LSP prewarm before MCP readiness, while reusing Serena's native `LanguageServerManager` rather than starting a competing supervisor;
- watchdog probes that exercise `initialize`, `tools/list`, and `list_dir` over real stdio MCP;
- an `AUTH_BLOCKED` state for tunnel authorization failures, with restart suppression because restarts cannot grant permission.

The standalone V8 daemon/index/cache modules remain diagnostic/experimental components and are not silently presented as part of the live MCP path. This preserves drop-in compatibility and avoids replacing Serena's authoritative LSP and project lifecycle.

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
MemoryMax=512M
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

## 📊 Monitoring

### Watchdog (Auto-Restart)

V8 includes `workspace-watchdog-v8.py` — monitors systemd, local health, MCP
processes, control-plane poll freshness, and OOM/authorization errors every 30s:

```bash
systemctl --user enable --now serena-v8-watchdog.service
```

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
| `find_symbol` slow (2-3s) | Cold LSP/index startup | Check live `metrics.tools` and cache hits after repeated calls |
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
   serena __init__.py
        │
        ├── V8 Runtime (identity, telemetry, cache)
        ├── V8 LSP Sync (document notify + cache invalidate)
        └── Serena 1.7.0 base (patched with V8)
                │
                ├── LSP Manager (auto-restart, idle eviction)
                ├── Symbol Cache (TTL + LRU)
                ├── Smart Scheduler (lanes, single-flight)
                └── 36 tools (find_symbol, rename, edit, diagnostics...)
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
