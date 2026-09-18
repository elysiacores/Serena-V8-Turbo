#!/usr/bin/env python3
"""
Serena V8 — Benchmark Harness

Tests:
A. Direct core function
B. Local MCP stdio
C. MCP Tunnel (if available)
D. ChatGPT → MCP Tunnel → Serena V8

Measures every stage of the request path.
"""

import json
import os
import sys
import time
import subprocess
import statistics
from pathlib import Path
from typing import Callable, Any
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════
# Benchmark Infrastructure
# ═══════════════════════════════════════════════════════════════

@dataclass
class BenchmarkResult:
    name: str
    runs: int
    times: list[float] = field(default_factory=list)
    errors: int = 0
    timeouts: int = 0
    
    @property
    def min_ms(self):
        return round(min(self.times) * 1000, 2) if self.times else 0
    
    @property
    def max_ms(self):
        return round(max(self.times) * 1000, 2) if self.times else 0
    
    @property
    def avg_ms(self):
        return round(statistics.mean(self.times) * 1000, 2) if self.times else 0
    
    @property
    def p50_ms(self):
        if not self.times: return 0
        s = sorted(self.times)
        return round(s[len(s)//2] * 1000, 2)
    
    @property
    def p95_ms(self):
        if not self.times: return 0
        s = sorted(self.times)
        return round(s[int(len(s)*0.95)] * 1000, 2)
    
    @property
    def p99_ms(self):
        if not self.times: return 0
        s = sorted(self.times)
        return round(s[int(len(s)*0.99)] * 1000, 2)
    
    def to_dict(self):
        return {
            "name": self.name,
            "runs": self.runs,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "avg_ms": self.avg_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


class V8Benchmark:
    """Benchmark harness for Serena V8."""
    
    def __init__(self, project: str, transport: str = "stdio"):
        self.project = str(Path(project).resolve())
        self.transport = transport
        self.results: list[BenchmarkResult] = []
    
    def benchmark(self, name: str, fn: Callable, runs: int = 10, warmup: int = 2) -> BenchmarkResult:
        """Run a benchmark."""
        result = BenchmarkResult(name=name, runs=runs)
        
        # Warmup
        for _ in range(warmup):
            try:
                fn()
            except Exception:
                pass
        
        # Benchmark
        for i in range(runs):
            t0 = time.perf_counter()
            try:
                fn()
                elapsed = time.perf_counter() - t0
                result.times.append(elapsed)
            except Exception as e:
                result.errors += 1
                print(f"  Error in run {i}: {e}")
        
        self.results.append(result)
        return result
    
    def print_results(self):
        """Print all benchmark results."""
        print("\n" + "="*70)
        print(" SERENA V8 BENCHMARK RESULTS")
        print(f" Project: {self.project}")
        print(f" Transport: {self.transport}")
        print("="*70)
        
        for r in self.results:
            print(f"\n {r.name}")
            print(f"  Runs: {r.runs} | Errors: {r.errors} | Timeouts: {r.timeouts}")
            print(f"  Min: {r.min_ms:8.2f}ms | Avg: {r.avg_ms:8.2f}ms | Max: {r.max_ms:8.2f}ms")
            print(f"  P50: {r.p50_ms:8.2f}ms | P95: {r.p95_ms:8.2f}ms | P99: {r.p99_ms:8.2f}ms")
        
        print("\n" + "="*70)
    
    def save_results(self, path: str):
        """Save results to JSON."""
        data = {
            "project": self.project,
            "transport": self.transport,
            "timestamp": time.time(),
            "results": [r.to_dict() for r in self.results],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\nResults saved to {path}")


# ═══════════════════════════════════════════════════════════════
# Benchmark: Direct Core Function
# ═══════════════════════════════════════════════════════════════

def benchmark_direct(project: str):
    """Benchmark direct Python function calls."""
    print("\n### BENCHMARK A: Direct Core Function")
    
    bench = V8Benchmark(project, "direct")
    
    # Import V8
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from serena.project import Project
    from serena.config.serena_config import SerenaConfig
    from serena.symbol import LanguageServerSymbolRetriever, _v8_symbol_cache
    
    config = SerenaConfig.from_config_file(str(Path.home() / ".serena/serena_config.yml"), generate_if_missing=True)
    project_obj = Project.load(project, config)
    retriever = LanguageServerSymbolRetriever(project_obj)
    
    # find_symbol
    def find_symbol():
        return retriever.find("App")
    
    r = bench.benchmark("find_symbol", find_symbol, runs=10, warmup=2)
    print(f"  find_symbol: P50={r.p50_ms}ms, P95={r.p95_ms}ms")
    
    # find_symbol + body
    def find_symbol_body():
        return retriever.find("App")
    
    r = bench.benchmark("find_symbol+body", find_symbol_body, runs=10, warmup=2)
    print(f"  find_symbol+body: P50={r.p50_ms}ms, P95={r.p95_ms}ms")
    
    # find_referencing_symbols
    def find_refs():
        return retriever.find_referencing_symbols("App", relative_file_path="frontend/src/app/page.tsx")
    
    r = bench.benchmark("find_referencing_symbols", find_refs, runs=5, warmup=1)
    print(f"  find_referencing_symbols: P50={r.p50_ms}ms, P95={r.p95_ms}ms")
    
    bench.print_results()
    return bench


# ═══════════════════════════════════════════════════════════════
# Benchmark: Local MCP stdio
# ═══════════════════════════════════════════════════════════════

def benchmark_stdio(project: str):
    """Benchmark via MCP stdio transport."""
    print("\n### BENCHMARK B: Local MCP stdio")
    
    bench = V8Benchmark(project, "stdio")
    
    def make_request(tool: str, args: dict) -> dict:
        """Make a single MCP request via stdio."""
        proc = subprocess.Popen(
            ["serena", "start-mcp-server", "--transport", "stdio",
             "--project", project, "--tool-timeout", "100",
             "--log-level", "WARNING", "--context", "desktop-app"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        def send(method, params):
            msg = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}) + "\n"
            proc.stdin.write(msg.encode())
            proc.stdin.flush()
        
        def recv():
            while True:
                line = proc.stdout.readline().decode().strip()
                if line:
                    try: return json.loads(line)
                    except: continue
        
        # Initialize
        send("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bench","version":"1.0"}})
        recv()
        
        # Call tool
        t0 = time.perf_counter()
        send("tools/call", {"name": tool, "arguments": args})
        result = recv()
        elapsed = time.perf_counter() - t0
        
        proc.terminate()
        proc.wait(timeout=5)
        
        return result, elapsed
    
    # find_symbol
    def find_symbol():
        _, elapsed = make_request("find_symbol", {"name_path_pattern": "App"})
        return elapsed
    
    r = bench.benchmark("find_symbol (stdio)", find_symbol, runs=5, warmup=1)
    print(f"  find_symbol: P50={r.p50_ms}ms, P95={r.p95_ms}ms")
    
    # read_file
    def read_file():
        _, elapsed = make_request("read_file", {"relative_path": "frontend/src/app/page.tsx"})
        return elapsed
    
    r = bench.benchmark("read_file (stdio)", read_file, runs=5, warmup=1)
    print(f"  read_file: P50={r.p50_ms}ms, P95={r.p95_ms}ms")
    
    bench.print_results()
    return bench


# ═══════════════════════════════════════════════════════════════
# Benchmark: Cold vs Warm
# ═══════════════════════════════════════════════════════════════

def benchmark_cold_warm(project: str):
    """Benchmark cold vs warm performance."""
    print("\n### BENCHMARK C: Cold vs Warm")
    
    bench = V8Benchmark(project, "stdio")
    
    # Cold start (new process each time)
    def cold_find_symbol():
        proc = subprocess.Popen(
            ["serena", "start-mcp-server", "--transport", "stdio",
             "--project", project, "--tool-timeout", "100",
             "--log-level", "WARNING", "--context", "desktop-app"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        def send(method, params):
            msg = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}) + "\n"
            proc.stdin.write(msg.encode())
            proc.stdin.flush()
        
        def recv():
            while True:
                line = proc.stdout.readline().decode().strip()
                if line:
                    try: return json.loads(line)
                    except: continue
        
        send("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bench","version":"1.0"}})
        recv()
        
        t0 = time.perf_counter()
        send("tools/call", {"name":"find_symbol","arguments":{"name_path_pattern":"App"}})
        recv()
        elapsed = time.perf_counter() - t0
        
        proc.terminate()
        proc.wait(timeout=5)
        return elapsed
    
    r = bench.benchmark("find_symbol (cold)", cold_find_symbol, runs=5, warmup=0)
    print(f"  Cold: P50={r.p50_ms}ms, P95={r.p95_ms}ms")
    
    bench.print_results()
    return bench


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Serena V8 Benchmark")
    parser.add_argument("--project", required=True, help="Project path")
    parser.add_argument("--mode", choices=["direct", "stdio", "cold_warm", "all"], default="all")
    parser.add_argument("--output", help="Output JSON file")
    args = parser.parse_args()
    
    print("="*70)
    print(" SERENA V8 BENCHMARK HARNESS")
    print(f" Project: {args.project}")
    print(f" Mode: {args.mode}")
    print("="*70)
    
    results = []
    
    if args.mode in ("direct", "all"):
        results.append(benchmark_direct(args.project))
    
    if args.mode in ("stdio", "all"):
        results.append(benchmark_stdio(args.project))
    
    if args.mode in ("cold_warm", "all"):
        results.append(benchmark_cold_warm(args.project))
    
    # Save results
    if args.output:
        all_data = {
            "project": args.project,
            "timestamp": time.time(),
            "benchmarks": [r.to_dict() for b in results for r in b.results],
        }
        with open(args.output, "w") as f:
            json.dump(all_data, f, indent=2)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
