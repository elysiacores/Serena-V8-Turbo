"""
Serena V8 — Phase 5: LSP Lifecycle Manager

LSP processes are the biggest memory consumers in Serena.
This manager:

1. Tracks per-LSP: RSS, CPU, idle time, request count, restart count, crash count
2. Auto-restarts crashed LSP with exponential backoff
3. Evicts idle LSP when memory pressure is high
4. Provides LSP status/metrics to observability layer
5. Enables hibernation (stop + restart on demand)
"""

import time
import subprocess
import threading
import logging
from enum import Enum
from typing import Dict, List, Optional
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class LSPState(Enum):
    COLD = "cold"           # Not started
    STARTING = "starting"   # Spawning process
    WARM = "warm"           # Active and healthy
    IDLE = "idle"           # No recent requests
    HIBERNATED = "hibernated"  # Stopped to save memory
    STOPPED = "stopped"     # Terminated
    CRASHED = "crashed"     # Died unexpectedly


@dataclass
class LSPProcess:
    """Tracks state and metrics for a single LSP process."""
    
    # Identity
    ls_id: str                          # e.g. "typescript", "go", "svelte"
    project: str                        # project root path
    command: List[str]                  # command to start LSP
    
    # Process handle
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    
    # State
    state: LSPState = LSPState.COLD
    state_changed_at: float = field(default_factory=time.time)
    
    # Metrics
    start_time: float = 0.0
    restart_count: int = 0
    crash_count: int = 0
    request_count: int = 0
    last_request_time: float = 0.0
    last_heartbeat: float = 0.0
    total_cpu_time: float = 0.0
    
    # Memory
    rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    
    # Backoff
    last_crash_time: float = 0.0
    consecutive_crashes: int = 0
    next_restart_delay: float = 1.0
    
    @property
    def idle_time(self) -> float:
        if self.last_request_time == 0:
            return 0
        return time.time() - self.last_request_time
    
    @property
    def uptime(self) -> float:
        if self.start_time == 0:
            return 0
        return time.time() - self.start_time
    
    def touch(self):
        self.last_request_time = time.time()
        self.last_heartbeat = self.last_request_time
    
    def to_dict(self) -> dict:
        return {
            "ls_id": self.ls_id,
            "project": self.project,
            "pid": self.pid,
            "state": self.state.value,
            "uptime": round(self.uptime, 1),
            "idle_time": round(self.idle_time, 1),
            "rss_mb": round(self.rss_mb, 1),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "requests": self.request_count,
            "restarts": self.restart_count,
            "crashes": self.crash_count,
            "consecutive_crashes": self.consecutive_crashes,
        }


class MemoryPressure(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LSPLifecycleManager:
    """
    Manages LSP process lifecycle with memory awareness.
    
    Key features:
    - Auto-restart crashed LSP with exponential backoff
    - Idle eviction after configurable TTL
    - Memory pressure-based hibernation
    - Per-process metrics and health tracking
    - Bounded total RSS across all LSP processes
    """
    
    def __init__(self, config: Optional[Dict] = None):
        # Default config
        self.config = {
            "max_total_rss_mb": 3072,
            "max_lsp_rss_mb": 1024,
            "idle_eviction_seconds": 600,       # 10 minutes
            "hibernation_check_interval": 60,   # Check every minute
            "max_restarts_per_hour": 5,
            "restart_backoff_base": 1.0,
            "restart_backoff_max": 60.0,
            "health_check_interval": 10,
        }
        if config:
            self.config.update(config)
        
        # Process registry
        self._processes: Dict[str, LSPProcess] = {}  # key: f"{project}:{ls_id}"
        self._lock = threading.Lock()
        
        # Background tasks
        self._running = False
        self._monitor_thread = None
        self._health_thread = None
        
        # Metrics
        self._total_restarts = 0
        self._total_crashes = 0
        self._total_evictions = 0
        self._total_hibernations = 0
    
    def start(self):
        """Start background monitoring threads."""
        self._running = True
        
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()
        
        log.info("LSP Lifecycle Manager started")
    
    def stop(self):
        """Stop all LSP processes and background tasks."""
        self._running = False
        
        with self._lock:
            for key, lsp in list(self._processes.items()):
                self._stop_lsp(lsp)
        
        log.info("LSP Lifecycle Manager stopped")
    
    # ════════════════════════════════════════════════════════
    # LSP Registration & Lifecycle
    # ════════════════════════════════════════════════════════
    
    def register(self, project: str, ls_id: str, command: List[str]) -> LSPProcess:
        """Register a new LSP type for a project."""
        key = f"{project}:{ls_id}"
        
        with self._lock:
            if key in self._processes:
                return self._processes[key]
            
            lsp = LSPProcess(
                ls_id=ls_id,
                project=project,
                command=command,
            )
            self._processes[key] = lsp
            log.info(f"Registered LSP: {key}")
            return lsp
    
    def get_or_create(self, project: str, ls_id: str, command: List[str]) -> LSPProcess:
        """Get existing LSP or create if not registered."""
        key = f"{project}:{ls_id}"
        
        with self._lock:
            if key in self._processes:
                lsp = self._processes[key]
                if lsp.state in (LSPState.COLD, LSPState.STOPPED, LSPState.CRASHED):
                    self._start_lsp(lsp)
                return lsp
            
            lsp = LSPProcess(ls_id=ls_id, project=project, command=command)
            self._processes[key] = lsp
            self._start_lsp(lsp)
            return lsp
    
    def mark_request(self, project: str, ls_id: str):
        """Mark that an LSP served a request."""
        key = f"{project}:{ls_id}"
        with self._lock:
            if key in self._processes:
                self._processes[key].touch()
    
    def get_lsp(self, project: str, ls_id: str) -> Optional[LSPProcess]:
        """Get LSP process info."""
        key = f"{project}:{ls_id}"
        return self._processes.get(key)
    
    def get_all_lsp(self) -> List[LSPProcess]:
        """Get all registered LSP processes."""
        return list(self._processes.values())
    
    # ════════════════════════════════════════════════════════
    # LSP Process Control
    # ════════════════════════════════════════════════════════
    
    def _start_lsp(self, lsp: LSPProcess):
        """Start an LSP process."""
        # Check memory pressure before starting
        pressure = self._memory_pressure()
        if pressure == MemoryPressure.CRITICAL:
            log.warning(f"Memory pressure CRITICAL, delaying LSP start: {lsp.ls_id}")
            time.sleep(5)
        
        lsp.state = LSPState.STARTING
        lsp.state_changed_at = time.time()
        
        try:
            proc = subprocess.Popen(
                lsp.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            
            lsp.process = proc
            lsp.pid = proc.pid
            lsp.state = LSPState.WARM
            lsp.start_time = time.time()
            lsp.last_heartbeat = time.time()
            lsp.state_changed_at = time.time()
            
            log.info(f"Started LSP {lsp.ls_id} (PID {lsp.pid})")
            
        except Exception as e:
            log.error(f"Failed to start LSP {lsp.ls_id}: {e}")
            lsp.state = LSPState.CRASHED
            lsp.crash_count += 1
            lsp.consecutive_crashes += 1
    
    def _stop_lsp(self, lsp: LSPProcess):
        """Gracefully stop an LSP process."""
        if lsp.process is None:
            return
        
        lsp.state = LSPState.STOPPED
        lsp.state_changed_at = time.time()
        
        try:
            lsp.process.terminate()
            lsp.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            lsp.process.kill()
            lsp.process.wait(timeout=2)
        except Exception as e:
            log.error(f"Error stopping LSP {lsp.ls_id}: {e}")
        
        lsp.process = None
        lsp.pid = None
        log.info(f"Stopped LSP {lsp.ls_id}")
    
    def _restart_lsp(self, lsp: LSPProcess):
        """Restart an LSP process with backoff."""
        self._stop_lsp(lsp)
        
        # Exponential backoff
        delay = min(
            self.config["restart_backoff_base"] * (2 ** lsp.consecutive_crashes),
            self.config["restart_backoff_max"]
        )
        
        log.info(f"Restarting LSP {lsp.ls_id} in {delay:.1f}s (crash #{lsp.consecutive_crashes})")
        time.sleep(delay)
        
        lsp.restart_count += 1
        lsp.next_restart_delay = delay * 2
        self._total_restarts += 1
        
        self._start_lsp(lsp)
    
    # ════════════════════════════════════════════════════════
    # Health Monitoring
    # ════════════════════════════════════════════════════════
    
    def _health_loop(self):
        """Periodically check LSP health."""
        while self._running:
            time.sleep(self.config["health_check_interval"])
            
            with self._lock:
                for key, lsp in list(self._processes.items()):
                    self._check_lsp_health(lsp)
    
    def _check_lsp_health(self, lsp: LSPProcess):
        """Check and update LSP health."""
        if lsp.state in (LSPState.STOPPED, LSPState.HIBERNATED, LSPState.COLD):
            return
        
        if lsp.process is None:
            return
        
        # Check if process is alive
        ret = lsp.process.poll()
        if ret is not None:
            # Process died
            log.warning(f"LSP {lsp.ls_id} (PID {lsp.pid}) died with code {ret}")
            lsp.state = LSPState.CRASHED
            lsp.crash_count += 1
            lsp.consecutive_crashes += 1
            lsp.last_crash_time = time.time()
            self._total_crashes += 1
            
            # Auto-restart
            if lsp.consecutive_crashes < 10:
                threading.Thread(target=self._restart_lsp, args=(lsp,), daemon=True).start()
            else:
                log.error(f"LSP {lsp.ls_id} exceeded max consecutive crashes, giving up")
                lsp.state = LSPState.STOPPED
            
            return
        
        # Update RSS
        try:
            import psutil
            proc = psutil.Process(lsp.pid)
            mem = proc.memory_info()
            lsp.rss_mb = mem.rss / 1024 / 1024
            lsp.peak_rss_mb = max(lsp.peak_rss_mb, lsp.rss_mb)
            
            # Check for memory overuse
            if lsp.rss_mb > self.config["max_lsp_rss_mb"]:
                log.warning(f"LSP {lsp.ls_id} using {lsp.rss_mb:.0f}MB > {self.config['max_lsp_rss_mb']}MB, restarting")
                self._restart_lsp(lsp)
                
        except (ImportError, Exception):
            pass
    
    # ════════════════════════════════════════════════════════
    # Memory Management
    # ════════════════════════════════════════════════════════
    
    def _monitor_loop(self):
        """Periodically monitor and evict idle LSP."""
        while self._running:
            time.sleep(self.config["hibernation_check_interval"])
            
            pressure = self._memory_pressure()
            
            if pressure in (MemoryPressure.HIGH, MemoryPressure.CRITICAL):
                self._evict_idle_lsp()
    
    def _memory_pressure(self) -> MemoryPressure:
        """Calculate current memory pressure from tracked LSP RSS values."""
        total_rss = sum(
            lsp.rss_mb for lsp in self._processes.values()
            if lsp.state == LSPState.WARM
        )

        max_rss = self.config["max_total_rss_mb"]
        ratio = total_rss / max_rss

        if ratio > 0.95:
            return MemoryPressure.CRITICAL
        if ratio > 0.8:
            return MemoryPressure.HIGH
        if ratio > 0.6:
            return MemoryPressure.MEDIUM
        if ratio > 0.3:
            return MemoryPressure.LOW
        return MemoryPressure.NONE
    
    def _evict_idle_lsp(self):
        """Evict idle LSP processes to reclaim memory."""
        with self._lock:
            # Find idle LSP (idle > threshold)
            candidates = [
                lsp for lsp in self._processes.values()
                if lsp.state == LSPState.WARM and lsp.idle_time > self.config["idle_eviction_seconds"]
            ]
            
            if not candidates:
                return
            
            # Sort by idle time (most idle first)
            candidates.sort(key=lambda x: x.idle_time, reverse=True)
            
            # Evict half of candidates
            for lsp in candidates[:max(1, len(candidates) // 2)]:
                log.info(f"Hibernating idle LSP: {lsp.ls_id} (idle {lsp.idle_time:.0f}s)")
                self._stop_lsp(lsp)
                lsp.state = LSPState.HIBERNATED
                self._total_hibernations += 1
    
    def hibernate_lsp(self, project: str, ls_id: str):
        """Explicitly hibernate an LSP."""
        key = f"{project}:{ls_id}"
        with self._lock:
            if key in self._processes:
                lsp = self._processes[key]
                self._stop_lsp(lsp)
                lsp.state = LSPState.HIBERNATED
    
    def wake_lsp(self, project: str, ls_id: str) -> bool:
        """Wake a hibernated LSP."""
        key = f"{project}:{ls_id}"
        with self._lock:
            if key not in self._processes:
                return False
            
            lsp = self._processes[key]
            if lsp.state == LSPState.HIBERNATED:
                self._start_lsp(lsp)
                return True
            return False
    
    # ════════════════════════════════════════════════════════
    # Metrics & Status
    # ════════════════════════════════════════════════════════
    
    def stats(self) -> dict:
        """Get comprehensive LSP manager stats."""
        with self._lock:
            lsp_stats = [lsp.to_dict() for lsp in self._processes.values()]
            
            total_rss = sum(s["rss_mb"] for s in lsp_stats)
            total_requests = sum(s["requests"] for s in lsp_stats)
            total_restarts = sum(s["restarts"] for s in lsp_stats)
            total_crashes = sum(s["crashes"] for s in lsp_stats)
            
            states = {}
            for s in lsp_stats:
                state = s["state"]
                states[state] = states.get(state, 0) + 1
            
            return {
                "lsp_count": len(lsp_stats),
                "total_rss_mb": round(total_rss, 1),
                "total_requests": total_requests,
                "total_restarts": total_restarts + self._total_restarts,
                "total_crashes": total_crashes + self._total_crashes,
                "total_evictions": self._total_evictions,
                "total_hibernations": self._total_hibernations,
                "states": states,
                "memory_pressure": self._memory_pressure().value,
                "processes": lsp_stats,
            }
