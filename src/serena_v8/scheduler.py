"""
Serena V8 — Smart Request Scheduler

Features:
- Separate lanes: FAST_READ, SEMANTIC_READ, WRITE_REFACTOR
- Bounded concurrency per lane
- Request cancellation + deadlines
- Fairness + backpressure
- Single-flight deduplication
"""

import time
import threading
import json
from enum import Enum
from typing import Callable, Optional, Dict, Tuple
from dataclasses import dataclass, field
from concurrent.futures import Future
import logging
import uuid

log = logging.getLogger(__name__)


class Lane(Enum):
    """Scheduler lanes with different concurrency characteristics."""
    FAST_READ = "fast_read"          # list_dir, read_file, cached queries
    SEMANTIC_READ = "semantic_read"  # find_symbol, references, diagnostics
    WRITE_REFACTOR = "write_refactor"  # replace, rename, delete


@dataclass
class ScheduledRequest:
    """A request waiting in the scheduler."""
    id: str
    lane: Lane
    tool: str
    args: dict
    project: str
    future: Future
    fn: Callable
    created_at: float = field(default_factory=time.perf_counter)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancelled: bool = False
    timer: Optional[threading.Timer] = None
    timeout_seconds: float = 0.0

    @property
    def wait_time_ms(self) -> float:
        endpoint = self.started_at or time.perf_counter()
        return round(max(0.0, endpoint - self.created_at) * 1000, 3)

    @property
    def execution_time_ms(self) -> float:
        if not self.started_at:
            return 0.0
        endpoint = self.finished_at or time.perf_counter()
        return round(max(0.0, endpoint - self.started_at) * 1000, 3)
    
    def cancel(self):
        self.cancelled = True
        self.future.cancel()


class LaneConfig:
    """Configuration for a scheduler lane."""
    
    def __init__(self, max_concurrent: int, max_queue: int, timeout: float):
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self.timeout = timeout


# Default lane configurations
LANE_CONFIGS = {
    Lane.FAST_READ: LaneConfig(max_concurrent=16, max_queue=100, timeout=30),
    Lane.SEMANTIC_READ: LaneConfig(max_concurrent=4, max_queue=50, timeout=120),
    Lane.WRITE_REFACTOR: LaneConfig(max_concurrent=1, max_queue=20, timeout=300),
}

# Tool-to-lane mapping. Only explicitly audited reads are concurrent.
# Unknown tools remain serialized/exclusive by default in classify().
TOOL_LANES = {
    # Cheap filesystem/config reads.
    "list_dir": Lane.FAST_READ,
    "read_file": Lane.FAST_READ,
    "find_file": Lane.FAST_READ,
    "search_for_pattern": Lane.FAST_READ,
    "get_current_config": Lane.FAST_READ,
    "list_memories": Lane.FAST_READ,
    "read_memory": Lane.FAST_READ,
    "list_queryable_projects": Lane.FAST_READ,
    "ast_grep_search": Lane.FAST_READ,
    "cgc_index_status": Lane.FAST_READ,
    "cgc_stale_paths": Lane.FAST_READ,

    # LSP / semantic / graph reads.
    "get_symbols_overview": Lane.SEMANTIC_READ,
    "find_symbol": Lane.SEMANTIC_READ,
    "find_declaration": Lane.SEMANTIC_READ,
    "find_referencing_symbols": Lane.SEMANTIC_READ,
    "find_implementations": Lane.SEMANTIC_READ,
    "get_diagnostics_for_file": Lane.SEMANTIC_READ,
    "get_diagnostics_for_symbol": Lane.SEMANTIC_READ,
    "cgc_callers": Lane.SEMANTIC_READ,
    "cgc_callees": Lane.SEMANTIC_READ,
    "cgc_query": Lane.SEMANTIC_READ,

    # Known mutations. The conservative fallback catches all other tools too.
    "create_text_file": Lane.WRITE_REFACTOR,
    "replace_content": Lane.WRITE_REFACTOR,
    "replace_in_files": Lane.WRITE_REFACTOR,
    "replace_symbol_body": Lane.WRITE_REFACTOR,
    "insert_after_symbol": Lane.WRITE_REFACTOR,
    "insert_before_symbol": Lane.WRITE_REFACTOR,
    "delete_lines": Lane.WRITE_REFACTOR,
    "replace_lines": Lane.WRITE_REFACTOR,
    "rename_symbol": Lane.WRITE_REFACTOR,
    "rename_memory": Lane.WRITE_REFACTOR,
    "write_memory": Lane.WRITE_REFACTOR,
    "delete_memory": Lane.WRITE_REFACTOR,
    "edit_memory": Lane.WRITE_REFACTOR,
    "ast_grep_rewrite": Lane.WRITE_REFACTOR,
    "cgc_index": Lane.WRITE_REFACTOR,
}


class SmartScheduler:
    """
    Smart request scheduler with separate lanes and bounded concurrency.
    
    Features:
    - Lane-based concurrency control
    - Request cancellation and deadlines
    - Single-flight deduplication
    - Queue depth limits with backpressure
    """
    
    def __init__(self, lane_configs: Optional[Dict] = None):
        self._configs = lane_configs or LANE_CONFIGS
        self._lanes: Dict[Lane, list] = {lane: [] for lane in Lane}
        self._active: Dict[Lane, int] = {lane: 0 for lane in Lane}
        # Dispatch and completion can recursively dispatch the next request.
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        
        # Single-flight deduplication
        self._in_flight: Dict[str, Future] = {}
        
        # Metrics
        self._total_dispatched = 0
        self._total_cancelled = 0
        self._total_timeout = 0
        self._total_rejected = 0
    
    def classify(self, tool: str) -> Lane:
        """Only explicitly audited reads may run concurrently or deduplicate.

        Unknown tools include shell commands, project switches and plugins;
        treating them as reads silently drops mutations and bypasses exclusion.
        """
        return TOOL_LANES.get(tool, Lane.WRITE_REFACTOR)
    
    def _single_flight_key(self, tool: str, args: dict, project: str) -> str:
        """Generate key for single-flight deduplication."""
        # Normalize args for comparison
        normalized = json.dumps(args, sort_keys=True, default=str)
        return f"{project}:{tool}:{normalized}"
    
    def submit(self, tool: str, args: dict, project: str,
               fn: Callable, timeout: float = 0) -> Tuple[Future, str]:
        """
        Submit a request to the scheduler.
        
        Returns (Future, request_id).
        If a matching request is already in-flight, returns that future instead.
        """
        lane = self.classify(tool)
        config = self._configs[lane]
        timeout = timeout or config.timeout
        
        # Single-flight check
        with self._lock:
            # Single-flight is safe for idempotent reads only. Never merge
            # mutations: identical write requests may be intentional retries.
            key = self._single_flight_key(tool, args, project)
            if lane != Lane.WRITE_REFACTOR and key in self._in_flight:
                existing = self._in_flight[key]
                if not existing.done():
                    log.info(f"Single-flight: deduplicating {tool}")
                    return existing, f"dedup:{key}"
            
            # Queue depth check
            if len(self._lanes[lane]) >= config.max_queue:
                self._total_rejected += 1
                raise QueueFullError(f"Lane {lane.value} queue full ({config.max_queue})")
            
            # Create request
            request_id = str(uuid.uuid4())[:12]
            future = Future()
            request = ScheduledRequest(
                id=request_id,
                lane=lane,
                tool=tool,
                args=args,
                project=project,
                future=future,
                fn=fn,
                timeout_seconds=timeout,
            )
            # Keep timing metadata on the Future so the central dispatcher can
            # report queue/execution stages without a second request registry.
            setattr(future, "_serena_v8_request", request)

            # Register for single-flight
            self._in_flight[key] = future
            
            # Add to queue
            self._lanes[lane].append(request)
            timer = threading.Timer(timeout, self._timeout_request, args=(request,))
            request.timer = timer
            timer.daemon = True
            timer.start()
            
            # Dispatch if capacity available
            self._try_dispatch(lane)
            
            return future, request_id

    def _timeout_request(self, request: ScheduledRequest):
        """Expire queued work/read waiters; never pretend a running write stopped."""
        with self._lock:
            if request.future.done():
                return
            if request.lane == Lane.WRITE_REFACTOR and request.future.running():
                return
            request.cancelled = True
            self._total_timeout += 1
            request.future.set_exception(TimeoutError(
                f"{request.tool} exceeded {request.timeout_seconds}s deadline"
            ))
    
    def _try_dispatch(self, lane: Lane):
        """Try to dispatch queued requests in a lane."""
        with self._lock:
            config = self._configs[lane]
            queue = self._lanes[lane]
            
            while queue and self._active[lane] < config.max_concurrent:
                # Writes exclude every lane, not just other writes. Once a
                # writer queues, let existing reads drain before admitting more.
                if lane == Lane.WRITE_REFACTOR:
                    if any(self._active.values()):
                        break
                elif self._active[Lane.WRITE_REFACTOR] or self._lanes[Lane.WRITE_REFACTOR]:
                    break
                request = queue.pop(0)
                
                if request.cancelled or not request.future.set_running_or_notify_cancel():
                    continue
                
                self._active[lane] += 1
                self._total_dispatched += 1
                request.started_at = time.perf_counter()
                
                # Execute in background
                threading.Thread(
                    target=self._execute,
                    args=(request,),
                    daemon=True,
                ).start()
    
    def _execute(self, request: ScheduledRequest):
        """Execute a scheduled request."""
        result = None
        error = None
        try:
            if request.cancelled:
                return
            result = request.fn()
        except Exception as exc:
            error = exc
        finally:
            request.finished_at = time.perf_counter()
            with self._lock:
                if request.timer is not None:
                    request.timer.cancel()
                    request.timer = None
                self._active[request.lane] -= 1
                key = self._single_flight_key(request.tool, request.args, request.project)
                if key in self._in_flight and self._in_flight[key] is request.future:
                    del self._in_flight[key]
                for lane in (Lane.WRITE_REFACTOR, Lane.FAST_READ, Lane.SEMANTIC_READ):
                    self._try_dispatch(lane)
        # Publish completion after the lane accounting is consistent.
        if not request.cancelled and not request.future.done():
            if error is not None:
                request.future.set_exception(error)
            else:
                request.future.set_result(result)
    
    def cancel(self, request_id: str) -> bool:
        """Cancel a request by ID."""
        with self._lock:
            for lane, queue in self._lanes.items():
                for request in queue:
                    if request.id == request_id:
                        request.cancel()
                        self._total_cancelled += 1
                        return True
        return False
    
    @property
    def queue_depth(self) -> Dict[str, int]:
        with self._lock:
            return {lane.value: len(queue) for lane, queue in self._lanes.items()}
    
    @property
    def active_count(self) -> Dict[str, int]:
        with self._lock:
            return {lane.value: count for lane, count in self._active.items()}
    
    def stats(self) -> dict:
        return {
            "dispatched": self._total_dispatched,
            "cancelled": self._total_cancelled,
            "timeout": self._total_timeout,
            "rejected": self._total_rejected,
            "queue_depth": self.queue_depth,
            "active": self.active_count,
            "in_flight": len(self._in_flight),
        }


class QueueFullError(Exception):
    """Raised when scheduler queue is full."""
    pass


# ═══════════════════════════════════════════════════════════════
# Composite Tools (Phase 3)
# ═══════════════════════════════════════════════════════════════

COMPOSITE_TOOLS = {
    "inspect_symbol": {
        "description": "Get comprehensive info about a symbol in one request",
        "operations": ["body", "references", "implementations", "diagnostics"],
    },
    "inspect_file": {
        "description": "Get overview of a file (symbols + diagnostics)",
        "operations": ["symbols", "diagnostics"],
    },
    "inspect_flow": {
        "description": "Trace a code flow from symbol to references",
        "operations": ["symbol", "references", "implementations"],
    },
    "impact_analysis": {
        "description": "Analyze impact of changing a symbol",
        "operations": ["references", "implementations", "diagnostics"],
    },
    "prepare_edit_context": {
        "description": "Prepare context for editing (symbol + body + references)",
        "operations": ["symbol", "body", "references"],
    },
}


def register_composite_tools():
    """Register composite tools with Serena."""
    # This would be implemented as actual Tool subclasses
    # For now, just log that they're available
    for name, info in COMPOSITE_TOOLS.items():
        log.info(f"Composite tool available: {name}")


# ═══════════════════════════════════════════════════════════════
# Global scheduler instance
# ═══════════════════════════════════════════════════════════════

_scheduler: Optional[SmartScheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> SmartScheduler:
    """Get or create the global scheduler."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = SmartScheduler()
        return _scheduler