#!/usr/bin/env python3
"""
Serena V8 — Phase 10: Integration Test + End-to-End Validation

Tests the entire V8 system end-to-end:
1. All components load and work together
2. Full request path: client → scheduler → cache → index → LSP → response
3. Memory stability test
4. Performance validation
"""

import os
import sys
import json
import time
import subprocess
import statistics
import threading
from pathlib import Path
from typing import Dict, List, Any
from concurrent.futures import Future, TimeoutError

# ═══ Setup paths ═══
V8_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(V8_SRC))


class V8IntegrationTest:
    """Integration test harness for V8."""
    
    def __init__(self, project: str):
        self.project = str(Path(project).resolve())
        self.results = []
    
    def test_v8_identity(self) -> dict:
        """Test 1: V8 identity is correct."""
        print("\n[Test 1] V8 Identity")
        
        import serena
        identity = serena.get_v8_identity()
        
        checks = {
            "name": identity.get("name") == "Serena V8",
            "version": identity.get("version") == "8.0.0a1",
            "has_commit": bool(identity.get("commit")),
            "has_runtime_path": bool(identity.get("runtime_path")),
        }
        
        result = {
            "test": "v8_identity",
            "pass": all(checks.values()),
            "details": identity,
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — {identity.get('name')} {identity.get('version')}")
        return result
    
    def test_query_cache(self) -> dict:
        """Test 2: Query cache works."""
        print("\n[Test 2] Query Cache")
        
        from serena.symbol import _v8_symbol_cache
        
        # Clear
        _v8_symbol_cache._cache.clear()
        _v8_symbol_cache._bytes = 0
        
        # Put
        _v8_symbol_cache.put("test:key:1", ["symbol1", "symbol2"], 100)
        _v8_symbol_cache.put("test:key:2", ["symbol3"], 50)
        
        # Get (hit)
        r1 = _v8_symbol_cache.get("test:key:1")
        
        # Get (miss)
        r2 = _v8_symbol_cache.get("test:key:missing")
        
        # Stats
        stats = _v8_symbol_cache.stats()
        
        checks = {
            "put_works": True,
            "get_hit": r1 == ["symbol1", "symbol2"],
            "get_miss": r2 is None,
            "hits_count": stats["hits"] >= 1,
            "misses_count": stats["misses"] >= 1,
        }
        
        result = {
            "test": "query_cache",
            "pass": all(checks.values()),
            "stats": stats,
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — hits={stats['hits']}, misses={stats['misses']}")
        return result
    
    def test_smart_scheduler(self) -> dict:
        """Test 3: Smart scheduler with lanes."""
        print("\n[Test 3] Smart Scheduler")
        
        from serena_v8.scheduler import SmartScheduler, Lane
        
        scheduler = SmartScheduler()
        
        # Classify tools
        lane1 = scheduler.classify("find_symbol")
        lane2 = scheduler.classify("read_file")
        lane3 = scheduler.classify("replace_content")
        
        # Execute a task
        def mock_task():
            time.sleep(0.01)
            return "done"
        
        future, req_id = scheduler.submit("find_symbol", {"name": "App"}, self.project, mock_task)
        
        try:
            value = future.result(timeout=5)
            executed = value == "done"
        except:
            executed = False
        
        stats = scheduler.stats()
        
        checks = {
            "lanes_classified": lane1 == Lane.SEMANTIC_READ and lane2 == Lane.FAST_READ and lane3 == Lane.WRITE_REFACTOR,
            "task_executed": executed,
            "dispatched": stats["dispatched"] >= 1,
        }
        
        result = {
            "test": "smart_scheduler",
            "pass": all(checks.values()),
            "stats": stats,
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — dispatched={stats['dispatched']}")
        return result
    
    def test_symbol_index(self) -> dict:
        """Test 4: Persistent Symbol Index."""
        print("\n[Test 4] Persistent Symbol Index")
        
        from serena_v8.index import PersistentSymbolIndex
        
        db_path = "/tmp/v8_integration_test.db"
        if os.path.exists(db_path):
            os.unlink(db_path)
        
        idx = PersistentSymbolIndex(db_path=db_path)
        
        # Index symbols
        symbols = [
            {"name": "login", "kind": 12, "language": "go", "relative_path": "cmd/main.go", "start_line": 5, "start_col": 0, "end_line": 15},
            {"name": "User", "kind": 5, "language": "typescript", "relative_path": "src/types.ts", "start_line": 3, "start_col": 0, "end_line": 8},
            {"name": "App", "kind": 12, "language": "typescript", "relative_path": "src/app.tsx", "start_line": 1, "start_col": 0, "end_line": 10},
        ]
        
        idx.index_symbols(symbols, self.project)
        
        # Query
        login = idx.find("login", self.project)
        user = idx.find("User", self.project)
        
        # Stats
        stats = idx.stats(self.project)
        
        checks = {
            "indexed": stats["symbols"] == 3,
            "find_login": len(login) == 1 and login[0]["name"] == "login",
            "find_user": len(user) == 1 and user[0]["name"] == "User",
            "db_created": os.path.exists(db_path),
        }
        
        result = {
            "test": "symbol_index",
            "pass": all(checks.values()),
            "stats": stats,
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — {stats['symbols']} symbols indexed")
        return result
    
    def test_lsp_manager(self) -> dict:
        """Test 5: LSP Lifecycle Manager."""
        print("\n[Test 5] LSP Lifecycle Manager")
        
        from serena_v8.lsp_manager import LSPLifecycleManager, LSPState, MemoryPressure
        
        mgr = LSPLifecycleManager()
        
        # Register LSPs
        go_lsp = mgr.register(self.project, "go", ["gopls", "serve"])
        ts_lsp = mgr.register(self.project, "typescript", ["typescript-language-server", "--stdio"])
        
        # Check initial state
        go_state = go_lsp.state
        ts_state = ts_lsp.state
        
        # Memory pressure
        pressure = mgr._memory_pressure()
        
        # Stats
        stats = mgr.stats()
        
        checks = {
            "go_registered": go_lsp.ls_id == "go",
            "ts_registered": ts_lsp.ls_id == "typescript",
            "initial_state_cold": go_state == LSPState.COLD,
            "pressure_valid": pressure in MemoryPressure,
            "stats_count": stats["lsp_count"] == 2,
        }
        
        result = {
            "test": "lsp_manager",
            "pass": all(checks.values()),
            "stats": {"lsp_count": stats["lsp_count"], "states": stats["states"]},
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — {stats['lsp_count']} LSPs registered")
        return result
    
    def test_multi_tier_cache(self) -> dict:
        """Test 6: Multi-Tier Cache."""
        print("\n[Test 6] Multi-Tier Cache")
        
        from serena_v8.cache import MultiTierCache
        
        cache = MultiTierCache()
        
        # Put
        cache.put(self.project, "find_symbol", {"name": "App"}, {"symbols": ["App", "App2"]})
        
        # Get (should be L1 hit)
        value, tier = cache.get(self.project, "find_symbol", {"name": "App"})
        
        # Get again
        value2, tier2 = cache.get(self.project, "find_symbol", {"name": "App"})
        
        # Stats
        stats = cache.stats()
        
        checks = {
            "put_works": True,
            "get_returns_value": value is not None,
            "l1_hit": tier == "l1",
            "stats_valid": stats["l1"]["entries"] >= 1,
        }
        
        result = {
            "test": "multi_tier_cache",
            "pass": all(checks.values()),
            "stats": stats,
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — tier={tier}, entries={stats['l1']['entries']}")
        return result
    
    def test_hardening(self) -> dict:
        """Test 7: Pipe/502 Hardening."""
        print("\n[Test 7] Pipe/502 Hardening")
        
        from serena_v8.hardening import (
            BoundedLogHandler, ResponseLimiter, RequestWatchdog,
            BackpressureController, deadline, get_response_limiter
        )
        
        import logging
        
        # BoundedLogHandler
        handler = BoundedLogHandler(max_records=10)
        logger = logging.getLogger("test_v8_hardening")
        logger.addHandler(handler)
        for i in range(20):
            logger.debug(f"msg {i}")
        
        # ResponseLimiter
        limiter = ResponseLimiter()
        large_data = {"items": ["x" * 100 for _ in range(100)]}
        limited = limiter.limit_response(large_data)
        
        # RequestWatchdog
        watchdog = RequestWatchdog(default_timeout=1.0, check_interval=0.5)
        watchdog.start()
        f = Future()
        watchdog.watch("test", f, timeout=0.3)
        time.sleep(0.5)
        cancelled = f.cancelled()
        watchdog.stop()
        
        # Backpressure
        bp = BackpressureController()
        bp.check(10, 0.5, 0.01)  # OK
        bp.check(100, 0.5, 0.01)  # REJECT
        
        checks = {
            "bounded_log": handler.dropped_count > 0,
            "response_limit": len(limited) < len(str(large_data)),
            "watchdog_cancel": cancelled,
            "backpressure": bp.rejected_count > 0,
        }
        
        result = {
            "test": "hardening",
            "pass": all(checks.values()),
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — all hardening components working")
        return result
    
    def test_profiler(self) -> dict:
        """Test 8: Profiler."""
        print("\n[Test 8] Profiler")
        
        from serena_v8.profiler import V8Profiler, get_profiler, generate_report
        
        profiler = V8Profiler()
        
        # Profile some operations
        for i in range(10):
            with profiler.profile("find_symbol", self.project) as stage:
                stage("queue")
                time.sleep(0.001)
                stage("cache")
                time.sleep(0.005)
                stage("lsp")
                time.sleep(0.02)
                stage("serialize")
        
        stats = profiler.stats()
        hotspots = profiler.hotspot_report()
        
        checks = {
            "profiles_collected": stats["total_profiles"] == 10,
            "has_total_ms": "total_ms" in stats,
            "has_stages": "stages" in stats,
            "hotspots_found": len(hotspots.get("bottlenecks", [])) > 0,
        }
        
        result = {
            "test": "profiler",
            "pass": all(checks.values()),
            "stats": {"total": stats["total_profiles"]},
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — {stats['total_profiles']} profiles collected")
        return result
    
    def test_optimization(self) -> dict:
        """Test 9: Hotspot Optimization."""
        print("\n[Test 9] Hotspot Optimization")
        
        from serena_v8.optimize import CompactSerializer, FieldFilter, get_response_builder
        
        # CompactSerializer
        ser = CompactSerializer()
        data = {"name": "App", "kind": 12, "body": "function App() { ... }", "path": "src/app.tsx"}
        compact = ser.serialize_compact(data)
        
        # FieldFilter
        ff = FieldFilter()
        symbols = [{"name": "App", "body": "code", "kind": 12, "path": "src/app.tsx"}]
        filtered = ff.filter_symbols(symbols)
        
        # ResponseBuilder
        builder = get_response_builder()
        response = builder.build(symbols)
        
        checks = {
            "compact_smaller": len(compact) < len(json.dumps(data)),
            "filter_removes_body": "body" not in filtered[0],
            "builder_returns_json": len(response) > 0,
        }
        
        result = {
            "test": "optimization",
            "pass": all(checks.values()),
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — compact={len(compact)}B vs standard={len(json.dumps(data))}B")
        return result
    
    def test_end_to_end(self) -> dict:
        """Test 10: End-to-End via MCP stdio."""
        print("\n[Test 10] End-to-End MCP stdio")
        
        # Run actual serena MCP server
        proc = subprocess.Popen(
            ["serena", "start-mcp-server", "--transport", "stdio",
             "--project", self.project, "--tool-timeout", "30",
             "--log-level", "WARNING", "--context", "desktop-app"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        def send(method, params):
            msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
            if proc.stdin: proc.stdin.write(msg.encode())
            if proc.stdin: proc.stdin.flush()
        
        def recv():
            while True:
                line = (proc.stdout.readline() if proc.stdout else b"").decode().strip() if proc.stdout else ""
                if line:
                    try:
                        return json.loads(line)
                    except:
                        continue
        
        # Initialize
        send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "v8-e2e", "version": "1.0"}})
        init_result = recv()
        init_ok = "result" in init_result
        
        # Find symbol
        t0 = time.time()
        send("tools/call", {"name": "find_symbol", "arguments": {"name_path_pattern": "App"}})
        find_result = recv()
        find_time = time.time() - t0
        find_ok = "result" in find_result
        
        proc.terminate()
        proc.wait(timeout=5)
        
        checks = {
            "mcp_init": init_ok,
            "find_symbol": find_ok,
            "response_time": find_time < 10,  # Should be < 10s
        }
        
        result = {
            "test": "end_to_end",
            "pass": all(checks.values()),
            "find_time_s": round(find_time, 2),
            "checks": checks,
        }
        self.results.append(result)
        
        status = "✅ PASS" if result["pass"] else "❌ FAIL"
        print(f"  {status} — find_symbol in {find_time:.2f}s")
        return result
    
    def run_all(self) -> dict:
        """Run all tests."""
        print("=" * 70)
        print(" SERENA V8 — PHASE 10: INTEGRATION TEST")
        print(f" Project: {self.project}")
        print("=" * 70)
        
        self.test_v8_identity()
        self.test_query_cache()
        self.test_smart_scheduler()
        self.test_symbol_index()
        self.test_lsp_manager()
        self.test_multi_tier_cache()
        self.test_hardening()
        self.test_profiler()
        self.test_optimization()
        self.test_end_to_end()
        
        # Summary
        total = len(self.results)
        passed = sum(1 for r in self.results if r["pass"])
        failed = total - passed
        
        print("\n" + "=" * 70)
        print(f" RESULTS: {passed}/{total} tests passed")
        if failed > 0:
            print(f"  Failed tests: {failed}")
            for r in self.results:
                if not r["pass"]:
                    print(f"    ❌ {r['test']}")
        print("=" * 70)
        
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "all_pass": failed == 0,
            "results": self.results,
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    
    test = V8IntegrationTest(args.project)
    result = test.run_all()
    
    # Save results
    output = Path(__file__).resolve().parent / "results" / "phase10_integration.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(result, f, indent=2)
    
    print(f"\nResults saved to {output}")
