"""
Serena V8 — Phase 6: Bounded Multi-Layer Cache

L1: In-memory LRU (fast)
L2: SQLite persistent disk cache
L3: LSP authoritative source

Prevents monotonic memory growth.
"""

import os
import sys
import json
import time
import sqlite3
import threading
from typing import Any, Optional, Dict, Tuple
from collections import OrderedDict
from pathlib import Path
import logging
import hashlib

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# L1: In-Memory LRU Cache
# ═══════════════════════════════════════════════════════════════

class L1MemoryCache:
    """Small, fast in-memory LRU cache."""
    
    def __init__(self, max_entries: int = 200, max_bytes: int = 10 * 1024 * 1024, ttl: int = 300):
        self._cache = OrderedDict()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._ttl = ttl
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
            
            self._cache[key] = {
                "v": value,
                "t": time.time(),
                "size": size_bytes,
            }
            self._current_bytes += size_bytes
    
    def invalidate(self, key: str):
        with self._lock:
            if key in self._cache:
                self._current_bytes -= self._cache[key].get("size", 0)
                del self._cache[key]
    
    def invalidate_prefix(self, prefix: str):
        with self._lock:
            to_remove = [k for k in self._cache if k.startswith(prefix)]
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


# ═══════════════════════════════════════════════════════════════
# L2: SQLite Persistent Disk Cache
# ═══════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    value BLOB,
    created_at REAL NOT NULL,
    ttl REAL NOT NULL,
    size INTEGER NOT NULL,
    project TEXT,
    tool TEXT
);

CREATE INDEX IF NOT EXISTS idx_cache_project ON cache_entries(project);
CREATE INDEX IF NOT EXISTS idx_cache_tool ON cache_entries(tool);
"""


class L2DiskCache:
    """Persistent disk cache using SQLite."""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path.home() / ".serena-v8" / "cache.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()
    
    def _init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
    
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT value, created_at, ttl FROM cache_entries WHERE key = ?",
                (key,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            value_blob, created_at, ttl = row
            
            # Check TTL
            if time.time() - created_at > ttl:
                self._conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                self._conn.commit()
                return None
            
            try:
                return json.loads(value_blob)
            except:
                return None
    
    def put(self, key: str, value: Any, ttl: int = 3600, project: str = "", tool: str = ""):
        with self._lock:
            value_blob = json.dumps(value).encode()
            self._conn.execute("""
                INSERT OR REPLACE INTO cache_entries (key, value, created_at, ttl, size, project, tool)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (key, value_blob, time.time(), ttl, len(value_blob), project, tool))
            self._conn.commit()
    
    def invalidate(self, key: str):
        with self._lock:
            self._conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
            self._conn.commit()
    
    def invalidate_prefix(self, prefix: str):
        with self._lock:
            self._conn.execute("DELETE FROM cache_entries WHERE key LIKE ?", (prefix + "%",))
            self._conn.commit()
    
    def invalidate_project(self, project: str):
        with self._lock:
            self._conn.execute("DELETE FROM cache_entries WHERE project = ?", (project,))
            self._conn.commit()
    
    def cleanup_expired(self):
        with self._lock:
            self._conn.execute(
                "DELETE FROM cache_entries WHERE created_at + ttl < ?",
                (time.time(),)
            )
            self._conn.commit()
    
    def stats(self):
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cache_entries")
            count, total_bytes = cursor.fetchone()
            return {
                "entries": count or 0,
                "bytes": total_bytes or 0,
                "size_mb": round((total_bytes or 0) / 1024 / 1024, 2),
            }


# ═══════════════════════════════════════════════════════════════
# L3: LSP Authoritative Source (no cache, direct LSP)
# ═══════════════════════════════════════════════════════════════

class L3LSPSource:
    """Direct LSP query — always returns fresh data."""
    
    def __init__(self, lsp_manager=None):
        self.lsp_manager = lsp_manager
    
    def query(self, tool: str, project: str, args: dict):
        """Execute tool directly via LSP — no caching."""
        # This would call the actual Serena tool implementation
        # For now, return None to indicate cache miss
        return None


# ═══════════════════════════════════════════════════════════════
# Multi-Tier Cache Manager
# ═══════════════════════════════════════════════════════════════

class MultiTierCache:
    """
    Multi-tier cache: L1 (memory) → L2 (disk) → L3 (LSP).
    
    Features:
    - Automatic tiered lookup
    - Memory pressure eviction
    - TTL-based expiration
    - Per-project invalidation
    - Cache key generation with file/project hash
    """
    
    def __init__(self, config: Optional[Dict] = None):
        # Default config
        self.config = {
            "l1_enabled": True,
            "l1_max_entries": 200,
            "l1_max_bytes": 10 * 1024 * 1024,
            "l1_ttl": 300,  # 5 min
            
            "l2_enabled": True,
            "l2_default_ttl": 3600,  # 1 hour
            
            "max_key_length": 1024,
        }
        if config:
            self.config.update(config)
        
        # Tiers
        self.l1 = L1MemoryCache(
            max_entries=self.config["l1_max_entries"],
            max_bytes=self.config["l1_max_bytes"],
            ttl=self.config["l1_ttl"],
        ) if self.config["l1_enabled"] else None
        
        self.l2 = L2DiskCache() if self.config["l2_enabled"] else None
        
        # Metrics
        self._l1_hits = 0
        self._l2_hits = 0
        self._l3_hits = 0
    
    def _make_key(self, project: str, tool: str, args: dict) -> str:
        """Generate cache key from project + tool + args."""
        normalized = json.dumps(args, sort_keys=True, default=str)
        raw = f"{project}:{tool}:{normalized}"
        # Truncate if too long
        if len(raw) > self.config["max_key_length"]:
            h = hashlib.md5(raw.encode()).hexdigest()
            return f"{project}:{tool}:{h}"
        return raw
    
    def get(self, project: str, tool: str, args: dict) -> Tuple[Optional[Any], str]:
        """
        Get from cache.
        Returns (value, tier) where tier is "l1", "l2", "l3", or "miss".
        """
        key = self._make_key(project, tool, args)
        
        # L1
        if self.l1:
            value = self.l1.get(key)
            if value is not None:
                self._l1_hits += 1
                return value, "l1"
        
        # L2
        if self.l2:
            value = self.l2.get(key)
            if value is not None:
                self._l2_hits += 1
                # Promote to L1
                if self.l1:
                    self.l1.put(key, value)
                return value, "l2"
        
        return None, "miss"
    
    def put(self, project: str, tool: str, args: dict, value: Any, ttl: int = 0):
        """Put into cache."""
        key = self._make_key(project, tool, args)
        ttl = ttl or self.config["l2_default_ttl"]
        size = len(str(value).encode())
        
        # L1
        if self.l1:
            self.l1.put(key, value, size_bytes=size)
        
        # L2
        if self.l2:
            self.l2.put(key, value, ttl=ttl, project=project, tool=tool)
    
    def invalidate(self, project: str, tool: str = "", key: str = ""):
        """Invalidate cache entry."""
        if key:
            if self.l1:
                self.l1.invalidate(key)
            if self.l2:
                self.l2.invalidate(key)
        elif tool:
            prefix = f"{project}:{tool}:"
            if self.l1:
                self.l1.invalidate_prefix(prefix)
            if self.l2:
                self.l2.invalidate_prefix(prefix)
        else:
            if self.l1:
                self.l1.invalidate_prefix(f"{project}:")
            if self.l2:
                self.l2.invalidate_project(project)
    
    def stats(self):
        l1_stats = self.l1.stats() if self.l1 else {"enabled": False}
        l2_stats = self.l2.stats() if self.l2 else {"enabled": False}
        
        total_hits = self._l1_hits + self._l2_hits + self._l3_hits
        
        return {
            "l1": l1_stats,
            "l2": l2_stats,
            "hits": {
                "l1": self._l1_hits,
                "l2": self._l2_hits,
                "l3": self._l3_hits,
                "total": total_hits,
            },
        }


# ═══════════════════════════════════════════════════════════════
# Global instance
# ═══════════════════════════════════════════════════════════════

_global_cache: Optional[MultiTierCache] = None
_global_cache_lock = threading.Lock()


def get_global_cache() -> MultiTierCache:
    """Get or create global cache."""
    global _global_cache
    with _global_cache_lock:
        if _global_cache is None:
            _global_cache = MultiTierCache()
        return _global_cache
