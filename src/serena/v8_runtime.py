"""
Serena V8 — Next-Generation Semantic Coding Runtime
"""

__version__ = "8.0.0-dev.1"
__build__ = "2026-09-18"
__commit__ = "v8-phase1"
__protocol_version__ = "1.0"

import os
import time
import threading
import json
import hashlib
import platform
from pathlib import Path
from collections import OrderedDict, defaultdict
from typing import Any, Optional, Dict
import statistics

# ═══════════════════════════════════════════════════════════════
# V8 RUNTIME IDENTITY
# ═══════════════════════════════════════════════════════════════

def get_v8_identity() -> dict:
    """Return V8 runtime identity — never ambiguous about what's running."""
    return {
        "name": "Serena V8",
        "version": __version__,
        "build": __build__,
        "commit": __commit__,
        "protocol": __protocol_version__,
        "runtime_path": os.path.dirname(os.path.abspath(__file__)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pid": os.getpid(),
        "start_time": _v8_start_time,
        "uptime_seconds": round(time.time() - _v8_start_time, 1),
    }

_v8_start_time = time.time()

# ═══════════════════════════════════════════════════════════════
# V8 TELEMETRY SYSTEM
# ═══════════════════════════════════════════════════════════════

class V8Telemetry:
    """Stage-level telemetry for every tool request."""
    
    def __init__(self, max_history: int = 10000):
        self._requests = []
        self._lock = threading.Lock()
        self._max_history = max_history
        self._start_time = time.time()
        
    def record(self, record: dict):
        """Record a tool execution with full stage breakdown."""
        with self._lock:
            record["_ts"] = time.time()
            self._requests.append(record)
            # Bound memory
            if len(self._requests) > self._max_history:
                self._requests = self._requests[-self._max_history:]
    
    def stats(self) -> dict:
        """Compute aggregate statistics."""
        with self._lock:
            if not self._requests:
                return {"total": 0}
            
            totals = [r.get("total_ms", 0) for r in self._requests]
            queues = [r.get("queue_ms", 0) for r in self._requests]
            caches = [r.get("cache_ms", 0) for r in self._requests]
            lsps = [r.get("lsp_ms", 0) for r in self._requests]
            serials = [r.get("serialize_ms", 0) for r in self._requests]
            transports = [r.get("transport_ms", 0) for r in self._requests]
            
            def pct(data, p):
                if not data: return 0
                s = sorted(data)
                return round(s[int(len(s) * p)], 2)
            
            return {
                "total_requests": len(self._requests),
                "uptime_seconds": round(time.time() - self._start_time, 1),
                "total_ms": {
                    "p50": pct(totals, 0.5),
                    "p95": pct(totals, 0.95),
                    "p99": pct(totals, 0.99),
                    "avg": round(statistics.mean(totals), 2) if totals else 0,
                    "max": round(max(totals), 2) if totals else 0,
                },
                "queue_ms": {"p50": pct(queues, 0.5), "p95": pct(queues, 0.95)},
                "cache_ms": {"p50": pct(caches, 0.5), "p95": pct(caches, 0.95)},
                "lsp_ms": {"p50": pct(lsps, 0.5), "p95": pct(lsps, 0.95)},
                "serialize_ms": {"p50": pct(serials, 0.5)},
                "transport_ms": {"p50": pct(transports, 0.5), "p95": pct(transports, 0.95)},
                "cache_hits": sum(1 for r in self._requests if r.get("cache_hit")),
                "cache_misses": sum(1 for r in self._requests if r.get("cache_miss")),
                "errors": sum(1 for r in self._requests if r.get("error")),
                "timeouts": sum(1 for r in self._requests if r.get("timeout")),
            }
    
    def recent(self, n: int = 10) -> list:
        with self._lock:
            return self._requests[-n:]
    
    def tool_breakdown(self) -> dict:
        """Per-tool statistics."""
        with self._lock:
            tools = defaultdict(list)
            for r in self._requests:
                tools[r.get("tool", "unknown")].append(r)
            
            result = {}
            for tool, records in tools.items():
                totals = [r.get("total_ms", 0) for r in records]
                result[tool] = {
                    "count": len(records),
                    "p50_ms": round(sorted(totals)[len(totals)//2], 2) if totals else 0,
                    "p95_ms": round(sorted(totals)[int(len(totals)*0.95)], 2) if totals else 0,
                    "errors": sum(1 for r in records if r.get("error")),
                }
            return result

# Global telemetry instance
_v8_telemetry = V8Telemetry()

def get_telemetry() -> V8Telemetry:
    return _v8_telemetry

# ═══════════════════════════════════════════════════════════════
# V8 REQUEST TIMER (context manager for stage measurement)
# ═══════════════════════════════════════════════════════════════

import contextlib
import time

@contextlib.contextmanager
def v8_measure(tool_name: str, project: str = "", **extra):
    """Context manager to measure all stages of a tool request."""
    stages = {}
    record = {
        "tool": tool_name,
        "project": project,
        "cache_hit": False,
        "cache_miss": False,
        "error": False,
        "timeout": False,
    }
    record.update(extra)
    
    _t0 = time.perf_counter()
    stage_start = _t0
    
    def stage(name):
        nonlocal stage_start
        now = time.perf_counter()
        stages[name] = round((now - stage_start) * 1000, 3)
        stage_start = now
    
    try:
        yield stage
    except Exception as e:
        record["error"] = True
        record["error_msg"] = str(e)[:200]
        raise
    finally:
        record["total_ms"] = round((time.perf_counter() - _t0) * 1000, 3)
        record["queue_ms"] = stages.get("queue", 0)
        record["cache_ms"] = stages.get("cache", 0)
        record["index_ms"] = stages.get("index", 0)
        record["lsp_ms"] = stages.get("lsp", 0)
        record["serialize_ms"] = stages.get("serialize", 0)
        record["transport_ms"] = stages.get("transport", 0)
        record["fs_ms"] = stages.get("fs", 0)
        _v8_telemetry.record(record)

# ═══════════════════════════════════════════════════════════════
# V8 MEMORY TRACKER
# ═══════════════════════════════════════════════════════════════

def get_memory_info() -> dict:
    """Get current memory usage."""
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        return {
            "rss_mb": round(mem.rss / 1024 / 1024, 1),
            "vms_mb": round(mem.vms / 1024 / 1024, 1),
            "num_threads": proc.num_threads(),
            "cpu_percent": proc.cpu_percent(),
            "open_files": len(proc.open_files()),
            "connections": len(proc.net_connections()),
        }
    except ImportError:
        return {"error": "psutil not available"}

# ═══════════════════════════════════════════════════════════════
# V8 STATUS COMMAND
# ═══════════════════════════════════════════════════════════════

def v8_status() -> dict:
    """Full V8 runtime status."""
    return {
        "identity": get_v8_identity(),
        "telemetry": _v8_telemetry.stats(),
        "tools": _v8_telemetry.tool_breakdown(),
        "memory": get_memory_info(),
    }

def v8_status_json() -> str:
    return json.dumps(v8_status(), indent=2, default=str)

# ═══════════════════════════════════════════════════════════════
# V8 QUERY CACHE (L1 in-memory LRU)
# ═══════════════════════════════════════════════════════════════

class V8QueryCache:
    """Bounded LRU cache with TTL and memory limits."""
    
    def __init__(self, max_entries: int = 1000, max_bytes: int = 50*1024*1024, ttl_seconds: int = 1800):
        self._cache = OrderedDict()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._ttl = ttl_seconds
        self._current_bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
    
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                self.misses += 1
                return None
            entry = self._cache[key]
            if time.time() - entry["t"] > self._ttl:
                del self._cache[key]
                self._current_bytes -= entry.get("size", 0)
                self.evictions += 1
                self.misses += 1
                return None
            self._cache.move_to_end(key)
            self.hits += 1
            return entry["v"]
    
    def put(self, key: str, value: Any, size_bytes: int = 0):
        with self._lock:
            if key in self._cache:
                self._current_bytes -= self._cache[key].get("size", 0)
                del self._cache[key]
            while (len(self._cache) >= self._max_entries or 
                   self._current_bytes + size_bytes > self._max_bytes) and self._cache:
                _, oldest = self._cache.popitem(last=False)
                self._current_bytes -= oldest.get("size", 0)
                self.evictions += 1
            self._cache[key] = {"v": value, "t": time.time(), "size": size_bytes}
            self._current_bytes += size_bytes
    
    def invalidate_file(self, file_path: str):
        with self._lock:
            to_remove = [k for k in self._cache if file_path in k]
            for k in to_remove:
                self._current_bytes -= self._cache[k].get("size", 0)
                del self._cache[k]
    
    def stats(self):
        total = self.hits + self.misses
        return {
            "entries": len(self._cache),
            "bytes": self._current_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(self.hits / total, 4) if total else 0,
        }

# Global query cache
_v8_query_cache = V8QueryCache()

def get_query_cache() -> V8QueryCache:
    return _v8_query_cache


def write_v8_stats(stats_path: Path | str | None = None) -> Path:
    """Write one authoritative runtime snapshot atomically.

    The symbol cache is imported lazily to avoid the serena package import
    cycle that previously prevented the MCP server from starting.
    """
    from serena.symbol import _v8_symbol_cache

    path = Path(stats_path) if stats_path is not None else Path.home() / ".serena-v8" / "stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    telemetry_stats = _v8_telemetry.stats()
    payload = {
        "timestamp": time.time(),
        "identity": get_v8_identity(),
        "cache": {
            "symbol_cache": _v8_symbol_cache.stats(),
            "query_cache": _v8_query_cache.stats(),
        },
        "metrics": {
            "total": telemetry_stats.get("total_requests", telemetry_stats.get("total", 0)),
            "errors": telemetry_stats.get("errors", 0),
            "timeouts": telemetry_stats.get("timeouts", 0),
            "p50_ms": telemetry_stats.get("total_ms", {}).get("p50", 0),
            "p95_ms": telemetry_stats.get("total_ms", {}).get("p95", 0),
            "tools": _v8_telemetry.tool_breakdown(),
        },
        "memory": get_memory_info(),
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def record_tool_call(
    tool_name: str,
    elapsed_ms: float,
    *,
    error: bool = False,
    timeout: bool = False,
    project: str = "",
    stats_path: Path | str | None = None,
) -> None:
    """Record a completed MCP tool call and refresh the V8 status file."""
    _v8_telemetry.record(
        {
            "tool": tool_name,
            "project": project,
            "total_ms": round(elapsed_ms, 3),
            "error": error,
            "timeout": timeout,
        }
    )
    write_v8_stats(stats_path)
