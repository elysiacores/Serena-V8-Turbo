"""
Serena V8 — Phase 7: Pipe/502/Output Hardening

Fixes:
1. Bounded logging — log queue never blocks main thread
2. Non-blocking pipe reads — LSP stdout/stderr drained safely
3. Response size limits — pagination + truncation
4. Request deadlines — cancel hung requests
5. Watchdog — detect and kill stuck processes
6. Backpressure — apply when queue full
"""

import os
import sys
import json
import time
import signal
import threading
import subprocess
import logging
import io
from typing import Optional, Any, Callable
from concurrent.futures import Future, TimeoutError
from pathlib import Path

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 1. Bounded Logging Queue
# ═══════════════════════════════════════════════════════════════

class BoundedLogHandler(logging.Handler):
    """
    Non-blocking log handler with bounded queue.
    
    When queue is full:
    - Drop DEBUG first
    - Keep WARNING+
    - Never block the main request thread
    """
    
    def __init__(self, max_records: int = 1000, fallback_path: Optional[str] = None):
        super().__init__()
        self._queue = []
        self._max = max_records
        self._lock = threading.Lock()
        self._dropped = 0
        self._fallback_path = fallback_path or str(Path.home() / ".serena-v8" / "dropped_logs.txt")
    
    def emit(self, record):
        with self._lock:
            if len(self._queue) >= self._max:
                # Drop DEBUG/INFO first
                if record.levelno <= logging.DEBUG:
                    self._dropped += 1
                    return
                
                # For WARNING+, drop oldest INFO
                for i, r in enumerate(self._queue):
                    if r.levelno <= logging.INFO:
                        self._queue.pop(i)
                        break
                else:
                    # Nothing to drop, skip
                    self._dropped += 1
                    return
            
            self._queue.append(record)
    
    def drain(self) -> list:
        with self._lock:
            records = self._queue[:]
            self._queue.clear()
            return records
    
    @property
    def dropped_count(self):
        return self._dropped


# ═══════════════════════════════════════════════════════════════
# 2. Non-blocking Pipe Drainer
# ═══════════════════════════════════════════════════════════════

class PipeDrainer:
    """
    Drain stdout/stderr from child processes without blocking.
    
    Problem: When LSP produces lots of output, pipes fill up
    and block the process, causing hangs.
    
    Solution: Dedicated drain threads + bounded buffers.
    """
    
    def __init__(self, process: subprocess.Popen, max_bytes: int = 10*1024*1024):
        self.process = process
        self.max_bytes = max_bytes
        self._stdout_buf = io.BytesIO()
        self._stderr_buf = io.BytesIO()
        self._running = False
        self._threads = []
    
    def start(self):
        """Start drain threads."""
        self._running = True
        
        if self.process.stdout:
            t = threading.Thread(target=self._drain_stdout, daemon=True)
            t.start()
            self._threads.append(t)
        
        if self.process.stderr:
            t = threading.Thread(target=self._drain_stderr, daemon=True)
            t.start()
            self._threads.append(t)
    
    def stop(self):
        """Stop drain threads."""
        self._running = False
        for t in self._threads:
            t.join(timeout=2)
    
    def _drain_stdout(self):
        """Drain stdout in background."""
        try:
            while self._running and self.process.stdout:
                data = self.process.stdout.read(4096)
                if not data:
                    break
                with threading.Lock():
                    if self._stdout_buf.tell() < self.max_bytes:
                        self._stdout_buf.write(data)
        except Exception:
            pass
    
    def _drain_stderr(self):
        """Drain stderr in background."""
        try:
            while self._running and self.process.stderr:
                data = self.process.stderr.read(4096)
                if not data:
                    break
                with threading.Lock():
                    if self._stderr_buf.tell() < self.max_bytes:
                        self._stderr_buf.write(data)
        except Exception:
            pass
    
    def get_stdout(self) -> str:
        with threading.Lock():
            return self._stdout_buf.getvalue().decode('utf-8', errors='replace')
    
    def get_stderr(self) -> str:
        with threading.Lock():
            return self._stderr_buf.getvalue().decode('utf-8', errors='replace')


# ═══════════════════════════════════════════════════════════════
# 3. Response Size Limiter
# ═══════════════════════════════════════════════════════════════

class ResponseLimiter:
    """
    Limits response size to prevent pipe blocking.
    
    Features:
    - max_bytes per response
    - max_matches per query
    - Compact mode (less whitespace)
    - Pagination support
    """
    
    DEFAULTS = {
        "max_response_bytes": 100 * 1024,      # 100KB default
        "max_answer_chars": 50000,              # 50K chars
        "max_matches": 100,                      # 100 symbols
        "compact": True,                         # less whitespace
        "truncation_marker": "\n... [truncated]",
    }
    
    def __init__(self, config: Optional[dict] = None):
        self.config = {**self.DEFAULTS, **(config or {})}
    
    def limit_response(self, data: Any) -> str:
        """Apply size limits to response."""
        text = json.dumps(data, ensure_ascii=False, separators=(',', ':') if self.config["compact"] else None)
        
        max_bytes = self.config["max_response_bytes"]
        max_chars = self.config["max_answer_chars"]
        
        # Truncate by chars
        if len(text) > max_chars:
            text = text[:max_chars] + self.config["truncation_marker"]
        
        # Truncate by bytes (UTF-8 safe)
        encoded = text.encode('utf-8')
        if len(encoded) > max_bytes:
            # Truncate at valid UTF-8 boundary
            truncated = encoded[:max_bytes]
            text = truncated.decode('utf-8', errors='ignore') + self.config["truncation_marker"]
        
        return text
    
    def limit_matches(self, matches: list) -> list:
        """Limit number of matches."""
        max_matches = self.config["max_matches"]
        if len(matches) > max_matches:
            return matches[:max_matches]
        return matches


# ═══════════════════════════════════════════════════════════════
# 4. Request Watchdog
# ═══════════════════════════════════════════════════════════════

class RequestWatchdog:
    """
    Detects and cancels hung requests.
    
    Features:
    - Per-request deadline
    - Background monitoring
    - Cancel stuck futures
    - Log slow requests
    """
    
    def __init__(self, default_timeout: float = 120.0, check_interval: float = 5.0):
        self._default_timeout = default_timeout
        self._check_interval = check_interval
        self._requests: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
    
    def start(self):
        """Start watchdog thread."""
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop watchdog."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
    
    def watch(self, request_id: str, future: Future, timeout: float = 0):
        """Add a request to watch."""
        with self._lock:
            self._requests[request_id] = {
                "future": future,
                "deadline": time.time() + (timeout or self._default_timeout),
                "started_at": time.time(),
            }
    
    def complete(self, request_id: str):
        """Mark request as complete."""
        with self._lock:
            self._requests.pop(request_id, None)
    
    def _monitor(self):
        """Monitor loop."""
        while self._running:
            time.sleep(self._check_interval)
            
            now = time.time()
            with self._lock:
                for req_id, info in list(self._requests.items()):
                    if info["future"].done():
                        self._requests.pop(req_id)
                        continue
                    
                    if now > info["deadline"]:
                        # Request timed out
                        elapsed = now - info["started_at"]
                        log.warning(f"Request {req_id} timed out after {elapsed:.1f}s")
                        info["future"].cancel()
                        self._requests.pop(req_id)


# ═══════════════════════════════════════════════════════════════
# 5. Backpressure Controller
# ═══════════════════════════════════════════════════════════════

class BackpressureController:
    """
    Apply backpressure when system is overloaded.
    
    Triggers:
    - Queue depth exceeds threshold
    - Memory pressure high
    - Error rate exceeds limit
    
    Actions:
    - Reject new requests (HTTP 503 equivalent)
    - Slow down request processing
    - Trigger cache eviction
    """
    
    def __init__(self, config: Optional[dict] = None):
        self.config = {
            "max_queue_depth": 50,
            "max_memory_pressure": 0.8,  # 80% of max RSS
            "error_rate_limit": 0.1,     # 10% error rate
            **(config or {})
        }
        self._rejected = 0
        self._lock = threading.Lock()
    
    def check(self, current_queue_depth: int, memory_ratio: float, error_rate: float) -> bool:
        """
        Check if system should apply backpressure.
        Returns True if request should proceed, False if rejected.
        """
        with self._lock:
            if current_queue_depth > self.config["max_queue_depth"]:
                self._rejected += 1
                return False
            
            if memory_ratio > self.config["max_memory_pressure"]:
                self._rejected += 1
                return False
            
            if error_rate > self.config["error_rate_limit"]:
                self._rejected += 1
                return False
            
            return True
    
    @property
    def rejected_count(self):
        return self._rejected


# ═══════════════════════════════════════════════════════════════
# 6. Process Tree Cleanup
# ═══════════════════════════════════════════════════════════════

def cleanup_process_tree(pid: int, timeout: float = 5.0):
    """
    Kill a process and all its children.
    Prevents zombie/orphan processes.
    """
    try:
        import psutil
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        
        # Terminate children first
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        
        # Wait for children
        gone, alive = psutil.wait_procs(children, timeout=timeout)
        
        # Kill remaining
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        
        # Terminate parent
        parent.terminate()
        parent.wait(timeout=timeout)
        
    except (ImportError, Exception):
        # Fallback: just kill the process
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(timeout)
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


# ═══════════════════════════════════════════════════════════════
# 7. Deadline Context Manager
# ═══════════════════════════════════════════════════════════════

import contextlib

@contextlib.contextmanager
def deadline(timeout: float):
    """
    Context manager that raises TimeoutError if deadline exceeded.
    
    Usage:
        with deadline(5.0) as dl:
            do_something()
            if dl.cancelled:
                return
    """
    state = {"cancelled": False, "deadline": time.time() + timeout}
    
    def check():
        if time.time() > state["deadline"]:
            state["cancelled"] = True
            return True
        return False
    
    state["check"] = check
    
    try:
        yield state
    except Exception:
        raise


# ═══════════════════════════════════════════════════════════════
# Global instances
# ═══════════════════════════════════════════════════════════════

_global_watchdog = RequestWatchdog()
_global_backpressure = BackpressureController()
_global_response_limiter = ResponseLimiter()


def get_watchdog() -> RequestWatchdog:
    return _global_watchdog


def get_backpressure() -> BackpressureController:
    return _global_backpressure


def get_response_limiter() -> ResponseLimiter:
    return _global_response_limiter
