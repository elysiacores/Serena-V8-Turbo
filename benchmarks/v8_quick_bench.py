#!/usr/bin/env python3
"""V8 Quick Benchmark — 3 rounds per test"""

import subprocess, json, time, statistics, sys
from pathlib import Path


def warm_bench(project, tool, args, rounds=3):
    proc = subprocess.Popen(
        ["serena", "start-mcp-server", "--transport", "stdio",
         "--project", project, "--tool-timeout", "30",
         "--log-level", "WARNING", "--context", "desktop-app"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    
    def send(method, params):
        msg = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}) + "\n"
        if proc.stdin: proc.stdin.write(msg.encode()); proc.stdin.flush()
    
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
    
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        send("tools/call", {"name": tool, "arguments": args})
        recv()
        times.append(time.perf_counter() - t0)
    
    proc.terminate()
    proc.wait(timeout=5)
    
    s = sorted(times)
    return {
        "min_ms": round(min(s) * 1000, 2),
        "avg_ms": round(statistics.mean(s) * 1000, 2),
        "p95_ms": round(s[int(len(s)*0.95)] * 1000, 2),
    }


def main():
    project = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "example-workspace")
    
    tests = [
        ("search_for_pattern", {"pattern": "export", "relative_path": "frontend/src"}),
        ("find_symbol", {"name_path_pattern": "App", "relative_path": "frontend/src/app/page.tsx"}),
        ("get_symbols_overview", {"relative_path": "frontend/src/app"}),
        ("list_dir", {"relative_path": "frontend/src/app"}),
        ("get_diagnostics", {"relative_path": "frontend/src/app/page.tsx"}),
    ]
    
    print("V8 QUICK WARM BENCHMARK")
    print("=" * 60)
    
    results = {}
    for tool, args in tests:
        try:
            stats = warm_bench(project, tool, args, rounds=3)
            key = f"{tool}:{args.get('relative_path', '')}"
            results[key] = stats
            print(f"\n{tool}")
            print(f"  Min: {stats['min_ms']:>8.2f}ms")
            print(f"  Avg: {stats['avg_ms']:>8.2f}ms")
            print(f"  P95: {stats['p95_ms']:>8.2f}ms")
        except Exception as e:
            print(f"\n{tool}: FAILED - {e}")
    
    print("\n" + "=" * 60)
    
    # Save
    out = Path.home() / "SuperProjects" / "serena-v8-fork" / "benchmarks" / "results" / f"quick_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump({"timestamp": time.time(), "results": results}, f, indent=2)
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
