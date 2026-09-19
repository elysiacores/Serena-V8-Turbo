"""
Serena V8 — Phase 4: Persistent Symbol Index + Core Daemon

Key changes:
1. Persistent SQLite symbol index (survives across serena restarts)
2. V8 Core Daemon runs persistently, tunnel-client connects via unix socket
3. Symbol index serves find_symbol without LSP for simple queries
4. Incremental file watcher updates index on changes
"""

import os
import socket
import json
import time
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Dict, List, Set
import logging
import hashlib

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# SQLite-based Persistent Symbol Index
# ═══════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_path TEXT,
    kind INTEGER,
    language TEXT,
    relative_path TEXT NOT NULL,
    start_line INTEGER,
    start_col INTEGER,
    end_line INTEGER,
    end_col INTEGER,
    signature TEXT,
    signature_hash TEXT,
    file_hash TEXT,
    project TEXT NOT NULL,
    indexed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_project ON symbols(project);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(relative_path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(name, name_path, content='symbols', content_rowid='id');

CREATE TABLE IF NOT EXISTS file_hashes (
    relative_path TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    mtime REAL,
    hash TEXT,
    indexed_at REAL
);
"""


class PersistentSymbolIndex:
    """
    SQLite-based persistent symbol index.
    
    Benefits:
    - Survives across serena restarts
    - Fast lookups without LSP for simple queries
    - Incremental updates on file changes
    """
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path.home() / ".serena-v8" / "symbol_index.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()
    
    def _init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
    
    def index_symbol(self, symbol: dict, project: str):
        """Index a single symbol."""
        with self._lock:
            self._conn.execute("""
                INSERT INTO symbols 
                (name, name_path, kind, language, relative_path, 
                 start_line, start_col, end_line, end_col, 
                 signature, signature_hash, file_hash, project, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.get("name", ""),
                symbol.get("name_path", ""),
                symbol.get("kind", 0),
                symbol.get("language", ""),
                symbol.get("relative_path", ""),
                symbol.get("start_line", 0),
                symbol.get("start_col", 0),
                symbol.get("end_line", 0),
                symbol.get("end_col", 0),
                symbol.get("signature", ""),
                symbol.get("signature_hash", ""),
                symbol.get("file_hash", ""),
                project,
                time.time(),
            ))
    
    def index_symbols(self, symbols: List[dict], project: str):
        """Bulk index symbols."""
        with self._lock:
            self._conn.executemany("""
                INSERT INTO symbols 
                (name, name_path, kind, language, relative_path, 
                 start_line, start_col, end_line, end_col, 
                 signature, signature_hash, file_hash, project, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    s.get("name", ""),
                    s.get("name_path", ""),
                    s.get("kind", 0),
                    s.get("language", ""),
                    s.get("relative_path", ""),
                    s.get("start_line", 0),
                    s.get("start_col", 0),
                    s.get("end_line", 0),
                    s.get("end_col", 0),
                    s.get("signature", ""),
                    s.get("signature_hash", ""),
                    s.get("file_hash", ""),
                    project,
                    time.time(),
                )
                for s in symbols
            ])
            self._conn.commit()
    
    def find(self, name: str, project: str, limit: int = 100) -> List[dict]:
        """Find symbols by name (fast path, no LSP needed)."""
        with self._lock:
            cursor = self._conn.execute("""
                SELECT name, name_path, kind, language, relative_path, 
                       start_line, start_col, end_line, end_col, signature
                FROM symbols 
                WHERE name = ? AND project = ?
                LIMIT ?
            """, (name, project, limit))
            
            return [
                {
                    "name": row[0],
                    "name_path": row[1],
                    "kind": row[2],
                    "language": row[3],
                    "relative_path": row[4],
                    "start_line": row[5],
                    "start_col": row[6],
                    "end_line": row[7],
                    "end_col": row[8],
                    "signature": row[9],
                }
                for row in cursor.fetchall()
            ]
    
    def search(self, pattern: str, project: str, limit: int = 100) -> List[dict]:
        """Search symbols using FTS5."""
        with self._lock:
            cursor = self._conn.execute("""
                SELECT s.name, s.name_path, s.kind, s.language, s.relative_path, 
                       s.start_line, s.start_col, s.end_line, s.end_col, s.signature
                FROM symbol_fts f
                JOIN symbols s ON f.rowid = s.id
                WHERE symbol_fts MATCH ? AND s.project = ?
                LIMIT ?
            """, (pattern, project, limit))
            
            return [
                {
                    "name": row[0],
                    "name_path": row[1],
                    "kind": row[2],
                    "language": row[3],
                    "relative_path": row[4],
                    "start_line": row[5],
                    "start_col": row[6],
                    "end_line": row[7],
                    "end_col": row[8],
                    "signature": row[9],
                }
                for row in cursor.fetchall()
            ]
    
    def invalidate_file(self, relative_path: str, project: str):
        """Invalidate all symbols from a changed file."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM symbols WHERE relative_path = ? AND project = ?",
                (relative_path, project)
            )
            self._conn.execute(
                "DELETE FROM file_hashes WHERE relative_path = ? AND project = ?",
                (relative_path, project)
            )
            self._conn.commit()
    
    def invalidate_project(self, project: str):
        """Invalidate all symbols for a project."""
        with self._lock:
            self._conn.execute("DELETE FROM symbols WHERE project = ?", (project,))
            self._conn.execute("DELETE FROM file_hashes WHERE project = ?", (project,))
            self._conn.commit()
    
    def get_file_hash(self, relative_path: str, project: str) -> Optional[str]:
        """Get cached file hash."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT hash FROM file_hashes WHERE relative_path = ? AND project = ?",
                (relative_path, project)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    
    def set_file_hash(self, relative_path: str, project: str, file_hash: str, mtime: float):
        """Cache file hash."""
        with self._lock:
            self._conn.execute("""
                INSERT OR REPLACE INTO file_hashes (relative_path, project, mtime, hash, indexed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (relative_path, project, mtime, file_hash, time.time()))
            self._conn.commit()
    
    def stats(self, project: str) -> dict:
        """Get index statistics."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE project = ?", (project,)
            )
            symbols = cursor.fetchone()[0]
            
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM file_hashes WHERE project = ?", (project,)
            )
            files = cursor.fetchone()[0]
            
            return {
                "symbols": symbols,
                "files": files,
                "db_size_mb": round(os.path.getsize(self.db_path) / 1024 / 1024, 2),
            }


# ═══════════════════════════════════════════════════════════════
# Incremental File Watcher
# ═══════════════════════════════════════════════════════════════

class IncrementalWatcher:
    """
    Watch repository for changes and update index incrementally.
    """
    
    def __init__(self, project_root: str, index: PersistentSymbolIndex):
        self.project_root = Path(project_root)
        self.index = index
        self._running = False
        self._thread = None
        self._debounce = 1.0  # seconds
        self._pending: Set[str] = set()
        self._lock = threading.Lock()
    
    def start(self):
        """Start watching for file changes."""
        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop watching."""
        self._running = False
    
    def _watch_loop(self):
        """Main watch loop."""
        while self._running:
            time.sleep(self._debounce)
            
            with self._lock:
                files = list(self._pending)
                self._pending.clear()
            
            for f in files:
                self._process_file(f)
    
    def _process_file(self, relative_path: str):
        """Process a changed file."""
        full_path = self.project_root / relative_path
        if not full_path.exists():
            return
        
        # Check if file actually changed
        mtime = full_path.stat().st_mtime
        content = full_path.read_bytes()
        file_hash = hashlib.md5(content).hexdigest()
        
        cached_hash = self.index.get_file_hash(relative_path, str(self.project_root))
        if cached_hash == file_hash:
            return  # No change
        
        # Invalidate old symbols
        self.index.invalidate_file(relative_path, str(self.project_root))
        
        # Update hash
        self.index.set_file_hash(relative_path, str(self.project_root), file_hash, mtime)
        
        # Re-index would happen here (requires LSP or tree-sitter)
        # For now, just invalidate and let next query re-index
    
    def mark_changed(self, relative_path: str):
        """Mark a file as changed."""
        with self._lock:
            self._pending.add(relative_path)


# ═══════════════════════════════════════════════════════════════
# V8 Core Daemon (Persistent Runtime)
# ═══════════════════════════════════════════════════════════════

class V8CoreDaemon:
    """
    Persistent Serena V8 runtime daemon.
    
    Responsibilities:
    - Own project state, LSP, index, cache
    - Serve tool requests via thin MCP adapter
    - Track memory, metrics, cache
    """
    
    def __init__(self, socket_path: Optional[str] = None):
        self.socket_path = socket_path or str(Path.home() / ".serena-v8" / "daemon.sock")
        self.projects: Dict[str, Any] = {}
        self.indexes: Dict[str, PersistentSymbolIndex] = {}
        self._running = False
        self._server = None
        self._start_time = time.time()
        self._total_requests = 0
    
    def get_or_create_index(self, project: str) -> PersistentSymbolIndex:
        """Get or create symbol index for a project."""
        if project not in self.indexes:
            self.indexes[project] = PersistentSymbolIndex()
        return self.indexes[project]
    
    def start(self):
        """Start the V8 core daemon."""
        self._running = True
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(32)
        self._server.settimeout(1.0)
        
        log.info(f"Serena V8 Core Daemon listening on {self.socket_path}")
        
        try:
            while self._running:
                try:
                    client, addr = self._server.accept()
                    threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
                except socket.timeout:
                    continue
        finally:
            self._cleanup()
    
    def _handle_client(self, client):
        """Handle a client connection."""
        try:
            buf = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
                
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        response = self._process_request(line)
                        client.sendall(json.dumps(response).encode() + b"\n")
        except Exception as e:
            log.error(f"Client error: {e}")
        finally:
            client.close()
    
    def _process_request(self, data: bytes) -> dict:
        """Process a single request."""
        try:
            request = json.loads(data)
        except json.JSONDecodeError:
            return {"error": "invalid json"}
        
        action = request.get("action", "")
        project = request.get("project", "")
        
        if action == "status":
            return self._status()
        elif action == "projects":
            return {"projects": list(self.projects.keys())}
        elif action == "index_stats":
            return self._index_stats(project)
        elif action == "shutdown":
            self._shutdown()
            return {"ok": True}
        else:
            return {"error": f"unknown action: {action}"}
    
    def _status(self) -> dict:
        """Daemon status."""
        return {
            "status": "running",
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "projects": len(self.projects),
            "total_requests": self._total_requests,
        }
    
    def _index_stats(self, project: str) -> dict:
        """Get index stats for a project."""
        if project not in self.indexes:
            return {"error": "project not indexed"}
        return self.indexes[project].stats(project)
    
    def _shutdown(self):
        """Graceful shutdown."""
        self._running = False
        if self._server:
            self._server.close()
    
    def _cleanup(self):
        """Cleanup resources."""
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


# ═══════════════════════════════════════════════════════════════
# Global instances
# ═══════════════════════════════════════════════════════════════

_global_index: Optional[PersistentSymbolIndex] = None
_global_lock = threading.Lock()


def get_global_index() -> PersistentSymbolIndex:
    """Get or create global symbol index."""
    global _global_index
    with _global_lock:
        if _global_index is None:
            _global_index = PersistentSymbolIndex()
        return _global_index


def daemon_main():
    """Start V8 Core Daemon."""
    import argparse
    parser = argparse.ArgumentParser(prog="serena-v8-daemon")
    parser.add_argument("--socket", help="Unix socket path")
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    
    daemon = V8CoreDaemon(socket_path=args.socket)
    daemon.start()


if __name__ == "__main__":
    daemon_main()
