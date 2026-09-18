# Serena V8 — Performance & Stability Fork

> Drop-in replacement for [Serena](https://github.com/oraios/serena) with query caching, LSP supervision, memory limits, and production metrics.

## 🚀 What's New in V8

| Feature | Serena 1.7 | V8 |
|---------|-----------|-----|
| Query-result cache (find_symbol, find_references) | ❌ | ✅ TTL/LRU, 500 entries, 30min TTL |
| LSP process supervision | ❌ | ✅ Crash/hang detection + auto-restart |
| Memory budget (RSS/threads/FD limits) | ✅ Basic | ✅ Enhanced with alerts |
| Latency metrics (P50/P95/P99) | ❌ | ✅ Real-time |
| Stats dashboard | ❌ | ✅ `~/.serena-v8/stats.json` |

## 📦 Install

### From PyPI (recommended)
```bash
pip install serena-v8
```

### From GitHub
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
```

### For Development
```bash
git clone https://github.com/elysiacores/serena-v8.git
cd serena-v8
pip install -e ".[dev]"
```

## 🛠️ Usage

### CLI (identical to Serena)
```bash
# Start MCP server
serena-v8 start-mcp-server --transport stdio --project /path/to/project

# All original commands work
serena-v8 --help
```

### With ChatGPT Tunnel
```bash
# In your tunnel config
command: "serena-v8 start-mcp-server --transport stdio --project /path/to/project"
```

## 📊 Monitoring

### View Stats
```bash
cat ~/.serena-v8/stats.json
```

### Example Output
```json
{
  "timestamp": 1789725324.638,
  "projects": {
    "/home/user/project": {
      "cache_entries": 42,
      "requests": 128,
      "errors": 0,
      "timeouts": 0,
      "p50_ms": 230.0,
      "p95_ms": 445.38,
      "p99_ms": 475.4
    }
  }
}
```

### Health Check
```bash
python3 -c "
import json
d = json.load(open('/home/user/.serena-v8/stats.json'))
for proj, s in d['projects'].items():
    print(f'{proj}: {s[\"cache_entries\"]} cached, P50={s[\"p50_ms\"]}ms')
"
```

## ⚙️ Configuration

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `V8_CACHE_TTL` | 1800 | Cache TTL in seconds |
| `V8_CACHE_MAX` | 500 | Max cache entries |
| `V8_MAX_RSS_MB` | 2048 | Max RSS per workspace |
| `V8_STATS_INTERVAL` | 10 | Stats write interval (seconds) |

### Per-Project Config (`.serena-v8.yml`)
```yaml
cache_ttl: 3600
max_entries: 1000
max_rss_mb: 4096
```

## 🔧 Migration from Serena

```bash
# Uninstall Serena
pip uninstall serena-agent

# Install V8
pip install serena-v8

# That's it — all your configs and tunnels work unchanged
```

## 📈 Performance Impact

Measured on production workspace (980 source files, 5 workspaces):

| Metric | Without V8 | With V8 | Improvement |
|--------|-----------|---------|-------------|
| Repeated find_symbol | ~250ms | ~0.1ms | 99.96% |
| Cache hit rate | 0% | 85%+ | — |
| LSP crash recovery | Manual | <3s | — |
| Memory visibility | None | Full | — |

## 🤝 Contributing

This is a fork — for Serena core issues, please visit https://github.com/oraios/serena

For V8-specific improvements, open issues/PRs here.

## 📜 License

MIT (compatible with Serena's MIT portions)
