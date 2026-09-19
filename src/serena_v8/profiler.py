"""
Serena V8 — Phase 8: Profiling & Hotspot Detection

Measures where time actually goes in the request path:
- transport_ms: MCP/tunnel round-trip
- queue_ms: time waiting in scheduler
- dispatch_ms: tool dispatch overhead
- cache_ms: cache lookup
- index_ms: symbol index query
- lsp_ms: LSP round-trip
- serialize_ms: JSON serialization
- fs_ms: filesystem operations

Then reports P50/P95/P99 per stage, per tool, per project.
"""

import time
import threading
import statistics
import cProfile
import pstats
import io
from typing import Dict, List, Optional, Any
from contextlib import contextmanager
from pathlib import Path
from collections import defaultdict
import logging

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Stage Profiler
# ═══════════════════════════════════════════════════════════════

class StageProfile:
    """Profile for a single tool execution."""
    
    def __init__(self, tool: str, project: str):
        self.tool = tool
        self.project = project
        self.stages: Dict[str, float] = {}
        self.start = time.perf_counter()
        self.end = 0.0
        self.total = 0.0
        self.cache_hit = False
        self.cache_miss = False
        self.error = False
        self.error_msg = ""
        self.output_bytes = 0
        self.timestamp = time.time()
    
    def record_stage(self, name: str, duration_ms: float):
        self.stages[name] = round(duration_ms, 3)
    
    def finalize(self, output_bytes: int = 0, error: bool = False, error_msg: str = ""):
        self.end = time.perf_counter()
        self.total = round((self.end - self.start) * 1000, 3)
        self.output_bytes = output_bytes
        self.error = error
        self.error_msg = error_msg or ""
    
    def to_dict(self):
        return {
            "tool": self.tool,
            "project": self.project,
            "total_ms": self.total,
            "stages": self.stages,
            "cache_hit": self.cache_hit,
            "cache_miss": self.cache_miss,
            "error": self.error,
            "output_bytes": self.output_bytes,
            "timestamp": self.timestamp,
        }


class V8Profiler:
    """
    Global profiler that collects stage-level timing data.
    
    Usage:
        profiler = V8Profiler()
        
        with profiler.profile("find_symbol", "/project") as p:
            p.stage("queue")
            # ... queue logic
            p.stage("cache")
            # ... cache lookup
            p.stage("lsp")
            # ... LSP call
            p.stage("serialize")
        
        stats = profiler.stats()
    """
    
    def __init__(self, max_profiles: int = 10000):
        self._profiles: List[StageProfile] = []
        self._max = max_profiles
        self._lock = threading.Lock()
    
    @contextmanager
    def profile(self, tool: str, project: str):
        """Context manager to profile a tool execution."""
        p = StageProfile(tool, project)
        stage_start = time.perf_counter()
        
        def stage(name: float):
            nonlocal stage_start
            now = time.perf_counter()
            p.record_stage(name, (now - stage_start) * 1000)
            stage_start = now
        
        try:
            yield stage
        except Exception as e:
            p.error = True
            p.error_msg = str(e)[:500]
            raise
        finally:
            p.finalize()
            with self._lock:
                self._profiles.append(p)
                if len(self._profiles) > self._max:
                    self._profiles = self._profiles[-self._max:]
    
    def stats(self, tool: str = None, project: str = None) -> Dict[str, Any]:
        """Get aggregated profiler stats."""
        with self._lock:
            profiles = self._profiles
            if tool:
                profiles = [p for p in profiles if p.tool == tool]
            if project:
                profiles = [p for p in profiles if p.project == project]
            
            if not profiles:
                return {"total": 0}
            
            totals = [p.total for p in profiles]
            
            # Aggregate stages
            all_stages = defaultdict(list)
            for p in profiles:
                for name, dur in p.stages.items():
                    all_stages[name].append(dur)
            
            def pct(data, p):
                s = sorted(data)
                return round(s[int(len(s) * p)], 3) if s else 0
            
            stage_stats = {}
            for name, times in all_stages.items():
                stage_stats[name] = {
                    "p50_ms": pct(times, 0.5),
                    "p95_ms": pct(times, 0.95),
                    "p99_ms": pct(times, 0.99),
                    "avg_ms": round(statistics.mean(times), 3),
                    "total_ms": round(sum(times), 3),
                }
            
            return {
                "total_profiles": len(profiles),
                "total_ms": {
                    "p50": pct(totals, 0.5),
                    "p95": pct(totals, 0.95),
                    "p99": pct(totals, 0.99),
                    "avg": round(statistics.mean(totals), 3),
                    "min": round(min(totals), 3),
                    "max": round(max(totals), 3),
                },
                "stages": stage_stats,
                "cache_hits": sum(1 for p in profiles if p.cache_hit),
                "cache_misses": sum(1 for p in profiles if p.cache_miss),
                "errors": sum(1 for p in profiles if p.error),
            }
    
    def slowest(self, n: int = 10) -> List[Dict]:
        """Get slowest tool executions."""
        with self._lock:
            sorted_profiles = sorted(self._profiles, key=lambda p: p.total, reverse=True)
            return [p.to_dict() for p in sorted_profiles[:n]]
    
    def tool_breakdown(self) -> Dict[str, Dict]:
        """Break down by tool."""
        with self._lock:
            tools = defaultdict(list)
            for p in self._profiles:
                tools[p.tool].append(p)
            
            result = {}
            for tool, profiles in tools.items():
                totals = [p.total for p in profiles]
                result[tool] = {
                    "count": len(profiles),
                    "p50_ms": round(sorted(totals)[len(totals)//2], 2) if totals else 0,
                    "p95_ms": round(sorted(totals)[int(len(totals)*0.95)], 2) if totals else 0,
                    "avg_ms": round(statistics.mean(totals), 2) if totals else 0,
                    "errors": sum(1 for p in profiles if p.error),
                }
            return result
    
    def hotspot_report(self) -> Dict[str, Any]:
        """
        Identify where most time is spent.
        Returns sorted list of bottlenecks.
        """
        with self._lock:
            if not self._profiles:
                return {"message": "no profiles yet"}
            
            # Aggregate all stage times
            stage_totals = defaultdict(float)
            stage_counts = defaultdict(int)
            
            for p in self._profiles:
                for name, dur in p.stages.items():
                    stage_totals[name] += dur
                    stage_counts[name] += 1
            
            total_time = sum(stage_totals.values())
            
            bottlenecks = []
            for stage, total in sorted(stage_totals.items(), key=lambda x: -x[1]):
                bottlenecks.append({
                    "stage": stage,
                    "total_ms": round(total, 2),
                    "percentage": round(total / total_time * 100, 1) if total_time else 0,
                    "count": stage_counts[stage],
                })
            
            return {
                "total_profiled_ms": round(total_time, 2),
                "total_requests": len(self._profiles),
                "bottlenecks": bottlenecks,
            }
    
    def export_flamegraph(self, path: str):
        """Export data suitable for flame graph visualization."""
        with self._lock:
            lines = []
            for p in self._profiles:
                stages = ";".join(p.stages.keys())
                lines.append(f"{p.tool}:{stages} {int(p.total * 1000)}")
            
            with open(path, "w") as f:
                f.write("\n".join(lines))
    
    def clear(self):
        """Clear all profiles."""
        with self._lock:
            self._profiles.clear()


# ═══════════════════════════════════════════════════════════════
# Global profiler
# ═══════════════════════════════════════════════════════════════

_global_profiler = V8Profiler()


def get_profiler() -> V8Profiler:
    return _global_profiler


# ═══════════════════════════════════════════════════════════════
# CPU Profiler (cProfile wrapper)
# ═══════════════════════════════════════════════════════════════

class CPUProfiler:
    """
    cProfile wrapper for deep profiling of specific operations.
    """
    
    def __init__(self):
        self._profiler = None
    
    def start(self):
        self._profiler = cProfile.Profile()
        self._profiler.enable()
    
    def stop(self) -> str:
        if not self._profiler:
            return ""
        self._profiler.disable()
        
        stream = io.StringIO()
        stats = pstats.Stats(self._profiler, stream=stream)
        stats.sort_stats("cumulative")
        stats.print_stats(30)  # Top 30 functions
        
        return stream.getvalue()
    
    def dump(self, path: str):
        if self._profiler:
            self._profiler.dump_stats(path)


@contextmanager
def cpu_profile():
    """Context manager for CPU profiling."""
    prof = CPUProfiler()
    prof.start()
    try:
        yield prof
    finally:
        output = prof.stop()
        log.info(f"CPU Profile:\n{output}")


# ═══════════════════════════════════════════════════════════════
# Report Generator
# ═══════════════════════════════════════════════════════════════

def generate_report() -> str:
    """Generate a comprehensive profiling report."""
    profiler = get_profiler()
    
    stats = profiler.stats()
    hotspots = profiler.hotspot_report()
    slowest = profiler.slowest(5)
    tools = profiler.tool_breakdown()
    
    lines = []
    lines.append("=" * 80)
    lines.append("SERENA V8 — PROFILING REPORT")
    lines.append("=" * 80)
    
    lines.append(f"\nTotal Requests: {stats['total_profiles']}")
    
    if 'total_ms' in stats:
        total = stats['total_ms']
        lines.append("Total Time:")
        lines.append(f"  P50: {total['p50']:>8.2f}ms")
        lines.append(f"  P95: {total['p95']:>8.2f}ms")
        lines.append(f"  P99: {total['p99']:>8.2f}ms")
        lines.append(f"  Avg: {total['avg']:>8.2f}ms")
        lines.append(f"  Max: {total['max']:>8.2f}ms")
    
    if 'stages' in stats:
        lines.append("\nStage Breakdown:")
        for stage, s in sorted(stats['stages'].items(), key=lambda x: -x[1]['total_ms']):
            lines.append(f"  {stage:<20} P50: {s['p50_ms']:>8.2f}ms  P95: {s['p95_ms']:>8.2f}ms  Total: {s['total_ms']:>10.2f}ms")
    
    if 'bottlenecks' in hotspots:
        lines.append("\nHotspots (where time is spent):")
        for b in hotspots['bottlenecks']:
            bar = "#" * int(b['percentage'] / 2)
            lines.append(f"  {b['stage']:<20} {b['percentage']:>5.1f}% {bar}")
    
    if tools:
        lines.append("\nPer-Tool Breakdown:")
        for tool, t in sorted(tools.items(), key=lambda x: -x[1]['avg_ms']):
            lines.append(f"  {tool:<20} Count: {t['count']:>4}  P50: {t['p50_ms']:>8.2f}ms  P95: {t['p95_ms']:>8.2f}ms  Avg: {t['avg_ms']:>8.2f}ms  Errors: {t['errors']}")
    
    if slowest:
        lines.append("\nSlowest Requests:")
        for s in slowest[:5]:
            lines.append(f"  {s['tool']:<20} {s['total_ms']:>10.2f}ms  {s.get('project', '')[:50]}")
    
    lines.append("\n" + "=" * 80)
    
    return "\n".join(lines)


def save_report(path: Optional[str] = None):
    """Save profiling report to file."""
    path = path or str(Path.home() / ".serena-v8" / "profile_report.txt")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    
    report = generate_report()
    with open(path, "w") as f:
        f.write(report)
    
    print(report)
    print(f"\nReport saved to: {path}")
