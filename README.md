# Serena V8 — Next-Generation Semantic Coding Runtime

> Drop-in replacement for [Serena](https://github.com/oraios/serena) — install once, use immediately. No need to install Serena first.

---

## What V8 Improves Over Serena

| Capability | Serena Original | V8 |
|------------|----------------|-----|
| Symbol search (find_symbol) | ~2.5s every time | ~0.005s after first call |
| Result caching | ❌ None | ✅ TTL 30min, 500 entries |
| LSP crash recovery | ❌ Manual restart | ✅ Auto-restart < 3s |
| LSP memory management | ❌ Unbounded | ✅ Idle eviction + alerts |
| Performance tracking | ❌ None | ✅ P50/P95/P99 per stage |
| Persistent symbol index | ❌ None | ✅ SQLite, survives restarts |
| Composite tools | ❌ None | ✅ 1 call replaces 3-4 calls |

---

## Installation

### Method 1: pip (recommended)
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
```

### Method 2: uv (faster)
```bash
uv pip install git+https://github.com/elysiacores/serena-v8.git
```

### Method 3: Development (editable)
```bash
git clone https://github.com/elysiacores/serena-v8.git
cd serena-v8
pip install -e .
```

> **Important:** ❌ **Do NOT install Serena first** — V8 includes everything (solidlsp, interprompt, etc.)  
> ❌ **No extra configuration needed** — Use the exact same commands as Serena

---

## Usage

### Same Commands as Serena (drop-in replacement)
```bash
# Start MCP server
serena start-mcp-server --project /path/to/project

# Use with tunnel-client (in config)
command: "serena start-mcp-server --project /path/to/project"

# Check version
serena --version
# → 8.0.0-dev.1

# Check V8 status
serena-v8 status

# View stats
cat ~/.serena-v8/stats.json
```

### Example Stats Output
```json
{
  "timestamp": 1789725324.638,
  "projects": {
    "/home/user/project": {
      "cache_entries": 42,
      "requests": 128,
      "p50_ms": 230.0,
      "p95_ms": 445.38,
      "errors": 0
    }
  }
}
```

---

## V8 Architecture

```
ChatGPT / Hermes / Claude
        │
        ▼
┌─────────────────┐
│ MCP Tunnel      │  ← Already exists, no changes needed
│ (tunnel-client) │
└────────┬────────┘
         │ stdio
         ▼
┌─────────────────┐
│ V8 Core Daemon  │  ← Added by V8 (persistent, reuses LSP)
│ - Project state │
│ - Symbol index  │
│ - Cache (L1→L2) │
│ - LSP lifecycle │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌───────┐ ┌───────────┐
│ LSP   │ │ SQLite    │
│ Pool  │ │ Index     │
└───────┘ └───────────┘
```

---

## V8 Configuration Files

| File | Purpose |
|------|---------|
| `~/.serena-v8/stats.json` | Runtime stats (written every 10s) |
| `~/.serena-v8/symbol_index.db` | Persistent symbol index (SQLite) |
| `~/.serena-v8/cache.db` | Search result cache (SQLite) |
| `~/.serena-v8/daemon.sock` | V8 Core Daemon (unix socket) |

---

## Migrating from Serena to V8

### If you already have Serena installed
```bash
# 1. Uninstall Serena (optional)
pip uninstall serena-agent

# 2. Install V8
pip install git+https://github.com/elysiacores/serena-v8.git

# 3. Verify
serena --version
# → 8.0.0-dev.1 ← Success!

# 4. Your tunnel configs work unchanged — no edits needed
```

### Fresh install
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
# Ready to use — no configuration needed
```

---

## Testing

```bash
# Test V8 runtime loads
python3 -c "
import serena
print(f'Version: {serena.__version__}')
print(f'V8 Identity: {serena.get_v8_identity()}')
"

# Test query cache
python3 -c "
from serena.symbol import _v8_symbol_cache
_v8_symbol_cache.put('test', ['symbol1'])
print(f'Cache hit: {_v8_symbol_cache.get(\"test\")}')
print(f'Stats: {_v8_symbol_cache.stats()}')
"

# Test LSP Manager
python3 -c "
from serena_v8.lsp_manager import LSPLifecycleManager
mgr = LSPLifecycleManager()
mgr.register('/proj', 'go', ['gopls', 'serve'])
print(mgr.stats())
"

# Test Multi-Tier Cache
python3 -c "
from serena_v8.cache import MultiTierCache
cache = MultiTierCache()
cache.put('/proj', 'find_symbol', {'name': 'App'}, {'result': 'ok'})
result, tier = cache.get('/proj', 'find_symbol', {'name': 'App'})
print(f'Tier: {tier}, Result: {result}')
"
```

---

## Benchmarking

```bash
# Run benchmarks
python3 benchmarks/v8_benchmark.py --project /path/to/project --mode all

# View results
cat benchmarks/results/*.json
```

---

## Troubleshooting

### Issue: `serena --version` still shows 1.7.0
Fix: Reinstall V8:
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
```

### Issue: `ModuleNotFoundError: serena_v8`
Fix: V8 runtime not loaded. Try:
```bash
pip install -e ~/Projects/serena-v8-fork
```

### Issue: tunnel config not switching to V8
Fix: Update tunnel-client config to use `serena` (from V8):
```yaml
mcp:
  commands:
    - command: "serena start-mcp-server --project /path"
```

### Issue: Stats file is empty
Fix: Stats are written every 10s. Wait and check again:
```bash
cat ~/.serena-v8/stats.json
```

---

## Development

```bash
# Clone
git clone https://github.com/elysiacores/serena-v8.git
cd serena-v8

# Install in dev mode
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run benchmarks
python3 benchmarks/v8_benchmark.py --project /path/to/project
```

---

## Project Structure

```
serena-v8/
├── src/
│   ├── serena/              # Core Serena + V8 patches
│   │   ├── __init__.py      # V8 version identity
│   │   ├── symbol.py        # + V8 query cache hooks
│   │   ├── v8_runtime.py    # V8 telemetry, cache, memory
│   │   └── tools/           # Tool classes
│   ├── serena_v8/           # V8-specific components
│   │   ├── core_daemon.py   # Persistent core daemon
│   │   ├── scheduler.py     # Smart request scheduler
│   │   ├── index.py         # Persistent symbol index
│   │   ├── lsp_manager.py   # LSP lifecycle manager
│   │   └── cache.py         # Multi-tier cache
│   ├── solidlsp/            # LSP protocol handler
│   └── interprompt/         # Prompt templates
├── benchmarks/
│   └── v8_benchmark.py      # Benchmark harness
├── pyproject.toml
└── README.md
```

---

## Performance Targets

| Metric | Target |
|--------|--------|
| `get_current_config` | < 10ms |
| `list_dir` | < 20ms |
| `read_file` | < 20ms |
| Indexed `find_symbol` | < 50ms |
| `find_symbol + body` | < 100ms |
| `search_for_pattern` | < 150ms |
| Semantic/LSP queries | < 300ms warm |
| `find_referencing_symbols` | < 800ms P95 |
| Stress (1000 calls) | 0 timeouts, 0 crashes |

---

## License

MIT (compatible with MIT portions of Serena)

---

## Links

- Original Serena: https://github.com/oraios/serena
- V8 Repository: https://github.com/elysiacores/serena-v8
- Issue Tracker: https://github.com/elysiacores/serena-v8/issues
