"""
Serena V8 — Smart Request Scheduler

Features:
- Separate lanes: FAST_READ, SEMANTIC_READ, WRITE_REFACTOR
- Bounded concurrency per lane
- Request cancellation + deadlines
- Fairness + backpressure
- Single-flight deduplication
"""

import atexit
import time
import threading
import json
import heapq
from collections import deque
from enum import Enum
from typing import Callable, Optional, Dict, Tuple
from dataclasses import dataclass, field
from concurrent.futures import Future, ThreadPoolExecutor
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
    timeout_seconds: float = 0.0
    deadline_at: float = 0.0

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
        self._lanes: Dict[Lane, deque[ScheduledRequest]] = {lane: deque() for lane in Lane}
        self._active: Dict[Lane, int] = {lane: 0 for lane in Lane}
        self._lock = threading.RLock()
        self._deadline_condition = threading.Condition(self._lock)
        self._deadlines: list[tuple[float, int, ScheduledRequest]] = []
        self._deadline_seq = 0
        self._shutdown = False

        # Single-flight deduplication
        self._in_flight: Dict[str, Future] = {}

        # A bounded shared pool removes per-request thread creation while lane
        # limits still enforce semantic/read/write concurrency.
        max_workers = max(1, sum(config.max_concurrent for config in self._configs.values()))
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="serena-v8-scheduler")
        self._deadline_thread = threading.Thread(
            target=self._deadline_loop,
            name="serena-v8-deadlines",
            daemon=True,
        )
        self._deadline_thread.start()

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

    def _remove_in_flight(self, request: ScheduledRequest) -> None:
        key = self._single_flight_key(request.tool, request.args, request.project)
        if self._in_flight.get(key) is request.future:
            self._in_flight.pop(key, None)

    def _deadline_loop(self) -> None:
        """Manage all request deadlines with one daemon thread."""
        while True:
            with self._deadline_condition:
                while not self._shutdown:
                    while self._deadlines and self._deadlines[0][2].future.done():
                        heapq.heappop(self._deadlines)
                    if not self._deadlines:
                        self._deadline_condition.wait()
                        continue
                    deadline_at, _, request = self._deadlines[0]
                    delay = deadline_at - time.perf_counter()
                    if delay > 0:
                        self._deadline_condition.wait(timeout=delay)
                        continue
                    heapq.heappop(self._deadlines)
                    self._timeout_request_locked(request)
                    break
                if self._shutdown:
                    return
    
    def submit(self, tool: str, args: dict, project: str,
               fn: Callable, timeout: float = 0) -> Tuple[Future, str]:
        """Submit a request and return its future plus request id."""
        lane = self.classify(tool)
        config = self._configs[lane]
        timeout = timeout or config.timeout

        with self._lock:
            key = self._single_flight_key(tool, args, project)
            if lane != Lane.WRITE_REFACTOR and key in self._in_flight:
                existing = self._in_flight[key]
                if not existing.done():
                    log.info(f"Single-flight: deduplicating {tool}")
                    return existing, f"dedup:{key}"

            if len(self._lanes[lane]) >= config.max_queue:
                self._total_rejected += 1
                raise QueueFullError(f"Lane {lane.value} queue full ({config.max_queue})")

            request_id = str(uuid.uuid4())[:12]
            future = Future()
            created_at = time.perf_counter()
            request = ScheduledRequest(
                id=request_id,
                lane=lane,
                tool=tool,
                args=args,
                project=project,
                future=future,
                fn=fn,
                created_at=created_at,
                timeout_seconds=timeout,
                deadline_at=created_at + timeout,
            )
            setattr(future, "_serena_v8_request", request)
            self._in_flight[key] = future
            self._lanes[lane].append(request)

            self._deadline_seq += 1
            heapq.heappush(self._deadlines, (request.deadline_at, self._deadline_seq, request))
            self._deadline_condition.notify()
            self._try_dispatch(lane)
            return future, request_id

    def _timeout_request(self, request: ScheduledRequest):
        """Expire queued/read work; never pretend a running mutation stopped."""
        with self._lock:
            self._timeout_request_locked(request)

    def _timeout_request_locked(self, request: ScheduledRequest) -> None:
        if request.future.done():
            return
        if request.lane == Lane.WRITE_REFACTOR and request.future.running():
            return

        request.cancelled = True
        self._total_timeout += 1
        self._remove_in_flight(request)
        if not request.future.running():
            try:
                self._lanes[request.lane].remove(request)
            except ValueError:
                pass
        request.future.set_exception(
            TimeoutError(f"{request.tool} exceeded {request.timeout_seconds}s deadline")
        )
        self._try_dispatch(request.lane)
    
    def _try_dispatch(self, lane: Lane):
        """Try to dispatch queued requests in a lane."""
        with self._lock:
            config = self._configs[lane]
            queue = self._lanes[lane]

            while queue and self._active[lane] < config.max_concurrent:
                if lane == Lane.WRITE_REFACTOR:
                    if any(self._active.values()):
                        break
                elif self._active[Lane.WRITE_REFACTOR] or self._lanes[Lane.WRITE_REFACTOR]:
                    break

                request = queue.popleft()
                if request.cancelled or request.future.done() or not request.future.set_running_or_notify_cancel():
                    self._remove_in_flight(request)
                    continue

                self._active[lane] += 1
                self._total_dispatched += 1
                request.started_at = time.perf_counter()
                self._executor.submit(self._execute, request)
    
    def _execute(self, request: ScheduledRequest):
        """Execute one scheduled request inside the shared worker pool."""
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
                self._active[request.lane] -= 1
                self._remove_in_flight(request)
                self._deadline_condition.notify()
                for lane in (Lane.WRITE_REFACTOR, Lane.FAST_READ, Lane.SEMANTIC_READ):
                    self._try_dispatch(lane)

        if not request.cancelled and not request.future.done():
            if error is not None:
                request.future.set_exception(error)
            else:
                request.future.set_result(result)
            with self._deadline_condition:
                self._deadline_condition.notify()
    
    def cancel(self, request_id: str) -> bool:
        """Cancel a queued request by ID."""
        with self._lock:
            for lane, queue in self._lanes.items():
                for request in tuple(queue):
                    if request.id != request_id:
                        continue
                    if request.future.running():
                        return False
                    request.cancel()
                    try:
                        queue.remove(request)
                    except ValueError:
                        pass
                    self._remove_in_flight(request)
                    self._total_cancelled += 1
                    self._deadline_condition.notify()
                    self._try_dispatch(lane)
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
        with self._lock:
            pending_deadlines = sum(1 for _, _, request in self._deadlines if not request.future.done())
            return {
                "dispatched": self._total_dispatched,
                "cancelled": self._total_cancelled,
                "timeout": self._total_timeout,
                "rejected": self._total_rejected,
                "queue_depth": {lane.value: len(queue) for lane, queue in self._lanes.items()},
                "active": {lane.value: count for lane, count in self._active.items()},
                "in_flight": len(self._in_flight),
                "pending_deadlines": pending_deadlines,
            }

    def shutdown(self, wait: bool = False) -> None:
        """Stop scheduler housekeeping and release worker-pool resources."""
        with self._deadline_condition:
            if self._shutdown:
                return
            self._shutdown = True
            self._deadline_condition.notify_all()
        if self._deadline_thread is not threading.current_thread():
            self._deadline_thread.join(timeout=1.0)
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


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


def _shutdown_scheduler() -> None:
    scheduler = _scheduler
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass


atexit.register(_shutdown_scheduler)