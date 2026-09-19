#!/usr/bin/env python3
"""
V8 Quick Warm Benchmark — measures warm/cold without process spawn overhead
"""

import subprocess
import json
import time
import statistics
import sys
from pathlib import Path


def warm_benchmark(project, tool, args, rounds=10):
    """Warm benchmark - single process, multiple calls."""
    proc = subprocess.Popen(
        ["serena", "start-mcp-server", "--transport", "stdio",
         "--project", project, "--tool-timeout", "60",
         "--log-level", "WARNING", "--context", "desktop-app"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    
    def send(method, params):
        msg = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}) + "\n"
        if proc.stdin: proc.stdin.write(msg.encode())
        if proc.stdin: proc.stdin.flush()
    
    def recv():
        while True:
            line = (proc.stdout.readline() if proc.stdout else b"").decode().strip()
            if line:
                try: return json.loads(line)
                except: continue
    
    send("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bench","version":"1.0"}})
    recv()
    
    # Warmup
    send("tools/call", {"name": tool, "arguments": args})
    recv()
    
    # Warm rounds
    times = []
    for i in range(rounds):
        t0 = time.perf_counter()
        send("tools/call", {"name": tool, "arguments": args})
        recv()
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
    
    proc.terminate()
    proc.wait(timeout=5)
    
    s = sorted(times)
    n = len(s)
    return {
        "min_ms": round(min(s) * 1000, 2),
        "max_ms": round(max(s) * 1000, 2),
        "avg_ms": round(statistics.mean(s) * 1000, 2),
        "median_ms": round(statistics.median(s) * 1000, 2),
        "p95_ms": round(s[int(n * 0.95)] * 1000, 2),
        "std_ms": round(statistics.stdev(s) * 1000, 2) if n > 1 else 0,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    
    print("=" * 70)
    print(f"V8 WARM BENCHMARK — {args.workspace}")
    print("=" * 70)
    
    tests = [
        ("search_for_pattern", {"pattern": "export", "relative_path": "frontend/src"}),
        ("find_symbol", {"name_path_pattern": "App", "relative_path": "frontend/src/app/page.tsx"}),
        ("find_referencing_symbols", {"symbol_name": "App", "relative_path": "frontend/src/app/page.tsx"}),
        ("get_symbols_overview", {"relative_path": "frontend/src/app"}),
        ("list_dir", {"relative_path": "frontend/src/app"}),
        ("get_diagnostics", {"relative_path": "frontend/src/app/page.tsx"}),
    ]
    
    results = {}
    
    for tool, targs in tests:
        path = targs.get("relative_path", "")
        key = f"{tool}:{path}"
        
        try:
            stats = warm_benchmark(args.project, tool, targs, rounds=10)
            results[key] = stats
            
            print(f"\n✅ {tool}")
            print(f"   Path: {path}")
            print(f"   Min: {stats['min_ms']:>8.2f}ms")
            print(f"   Avg: {stats['avg_ms']:>8.2f}ms")
            print(f"   P95: {stats['p95_ms']:>8.2f}ms")
            print(f"   Max: {stats['max_ms']:>8.2f}ms")
            print(f"   Std: {stats['std_ms']:>8.2f}ms")
        except Exception as e:
            print(f"\n❌ {tool}: {e}")
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    all_avgs = [r["avg_ms"] for r in results.values() if "avg_ms" in r]
    all_p95 = [r["p95_ms"] for r in results.values() if "p95_ms" in r]
    
    if all_avgs:
        print(f"\nOverall Avg: {statistics.mean(all_avgs):.1f}ms")
        print(f"Overall P95: {sorted(all_p95)[int(len(all_p95)*0.95)]:.1f}ms")
    
    # Save
    out = Path(__file__).resolve().parent / "results" / f"warm_{args.workspace}_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "workspace": args.workspace,
            "project": args.project,
            "timestamp": time.time(),
            "results": results,
        }, f, indent=2)
    print(f"\nSaved to: {out}")


if __name__ == "__main__":
    main()
