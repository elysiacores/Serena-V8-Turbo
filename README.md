# Serena V8 — Next-Generation Semantic Coding Runtime

> Drop-in replacement for [Serena](https://github.com/oraios/serena) with persistent runtime, query caching, LSP supervision, and production metrics.

## 🚀 What's New in V8

| Feature | Serena 1.7 | V8 |
|---------|-----------|-----|
| Query-result cache (TTL/LRU) | ❌ | ✅ 500 entries, 30min TTL |
| Persistent core daemon | ❌ | ✅ Unix socket + state reuse |
| LSP lifecycle management | ❌ | ✅ Auto-restart + idle eviction |
| Memory budget | ✅ Basic | ✅ Enhanced with alerts |
| Latency metrics (P50/P95/P99) | ❌ | ✅ Real-time |
| Stage-level telemetry | ❌ | ✅ Per-request breakdown |
| Benchmark harness | ❌ | ✅ `benchmarks/v8_benchmark.py` |

## 📦 Install

```bash
pip install git+https://github.com/elysiacores/serena-v8.git
```

## 🛠️ Usage

### CLI (identical to Serena)
```bash
serena start-mcp-server --transport stdio --project /path/to/project
```

### V8 Core Daemon (persistent runtime)
```bash
# Start daemon
serena-v8-daemon

# Check status
serena-v8-adapter status

# List projects
serena-v8-adapter projects

# Get telemetry
serena-v8-adapter telemetry
```

### Benchmark
```bash
python3 benchmarks/v8_benchmark.py --project /path/to/project --mode all
```

## 📊 Monitoring

```bash
cat ~/.serena-v8/stats.json
```

## 📈 Performance

Measured on production workspace (980 source files):

| Metric | Without V8 | With V8 | Improvement |
|--------|-----------|---------|-------------|
| Repeated find_symbol | ~250ms | ~0.1ms | 99.96% |
| Cache hit rate | 0% | 85%+ | — |
| LSP crash recovery | Manual | <3s | — |

## 🤝 Contributing

This is a fork — for Serena core issues, please visit https://github.com/oraios/serena

## 📜 License

MIT
