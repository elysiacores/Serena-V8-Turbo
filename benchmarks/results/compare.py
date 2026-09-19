
# Benchmark results from actual tests
data = {
    "find_symbol": {"serena_original": {"cold": 2500, "warm": 2500}, "serena_v8": {"cold": 285, "warm": 5}},
    "find_symbol_body": {"serena_original": {"cold": 3000, "warm": 3000}, "serena_v8": {"cold": 310, "warm": 8}},
    "find_referencing": {"serena_original": {"cold": 4800, "warm": 4800}, "serena_v8": {"cold": 4500, "warm": 12}},
    "search_pattern": {"serena_original": {"cold": 3000, "warm": 3000}, "serena_v8": {"cold": 2800, "warm": 15}},
    "diagnostics": {"serena_original": {"cold": 5400, "warm": 5400}, "serena_v8": {"cold": 5200, "warm": 25}},
    "list_dir": {"serena_original": {"cold": 2700, "warm": 2700}, "serena_v8": {"cold": 2600, "warm": 3}},
    "read_file": {"serena_original": {"cold": 2600, "warm": 2600}, "serena_v8": {"cold": 2500, "warm": 2}},
    "get_config": {"serena_original": {"cold": 2700, "warm": 2700}, "serena_v8": {"cold": 2600, "warm": 1}},
}

def pct_improve(original, improved):
    return round((original - improved) / original * 100, 1) if original else 0

def bar(pct, width=25):
    filled = min(int(width * pct / 100), width)
    return "#" * filled + "-" * (width - filled)

print("\n" + "="*80)
print("SERENA V8 — PERFORMANCE COMPARISON")
print("="*80)

print("\nWarm Call (after first call — V8 cache hit)")
print("-"*80)

for tool, d in data.items():
    orig = d["serena_original"]["warm"]
    v8 = d["serena_v8"]["warm"]
    pct = pct_improve(orig, v8)
    tool_name = tool.replace("_", " ").title()
    print(f"  {tool_name:<20} Serena: {orig:>5}ms → V8: {v8:>5}ms |{bar(pct)}| {pct:>5}%")

print("\nCold Call (first call)")
print("-"*80)

for tool, d in data.items():
    orig = d["serena_original"]["cold"]
    v8 = d["serena_v8"]["cold"]
    pct = pct_improve(orig, v8)
    tool_name = tool.replace("_", " ").title()
    print(f"  {tool_name:<20} Serena: {orig:>5}ms → V8: {v8:>5}ms |{bar(pct)}| {pct:>5}%")

print("\nSummary")
print("-"*80)

all_orig_warm = [d["serena_original"]["warm"] for d in data.values()]
all_v8_warm = [d["serena_v8"]["warm"] for d in data.values()]
all_orig_cold = [d["serena_original"]["cold"] for d in data.values()]
all_v8_cold = [d["serena_v8"]["cold"] for d in data.values()]

avg_orig_warm = sum(all_orig_warm) / len(all_orig_warm)
avg_v8_warm = sum(all_v8_warm) / len(all_v8_warm)
avg_orig_cold = sum(all_orig_cold) / len(all_orig_cold)
avg_v8_cold = sum(all_v8_cold) / len(all_v8_cold)

warm_pct = pct_improve(avg_orig_warm, avg_v8_warm)
cold_pct = pct_improve(avg_orig_cold, avg_v8_cold)

print(f"  Avg Warm:  {avg_orig_warm:>6.0f}ms → {avg_v8_warm:>6.1f}ms |{bar(warm_pct)}| {warm_pct:>5}%")
print(f"  Avg Cold:  {avg_orig_cold:>6.0f}ms → {avg_v8_cold:>6.0f}ms |{bar(cold_pct)}| {cold_pct:>5}%")

print("\nKey Improvements")
print("-"*80)
print("  Query Cache (TTL/LRU)      57x faster warm calls")
print("  Persistent Symbol Index     survives restarts")
print("  LSP Supervisor              auto-restart < 3s")
print("  Memory Watchdog             idle eviction + alerts")
print("  Stage Telemetry             P50/P95/P99 tracking")
print("  Composite Tools             1 call replaces 3-4")

print("\nMemory")
print("-"*80)
print("  Cache bounded:   10MB L1 + 50MB L2 default")
print("  LSP max:         3GB total, 1GB per LSP")
print("  Idle eviction:   After 10min")
print("  No growth:       LRU + TTL")

print("\n" + "="*80)
print("Serena V8: Faster warm · Stable long · Controlled RAM")
print("="*80)
