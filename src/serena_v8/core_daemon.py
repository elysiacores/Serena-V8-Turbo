"""
Serena V8 Core Daemon — Persistent Runtime

Key benefits:
- Single process owns project state, LSP, index, cache
- Thin MCP adapter connects via unix socket
- Avoids cold-start overhead per request
- Reuses LSP processes across MCP sessions

Protocol: JSON messages over Unix stream socket
"""

import os
import sys
import json
import time
import signal
import socket
import threading
import subprocess
from pathlib import Path
from typing import Any, Optional, Dict, List
from collections import OrderedDict, defaultdict
import logging

# V8 runtime
from serena.v8_runtime import (
    v8_status, get_telemetry, get_query_cache, get_memory_info, _v8_start_time
)

log = logging.getLogger(__name__)


class V8ProjectSession:
    """Persistent session for a single project."""
    
    def __init__(self, project_root: str):
        self.project_root = str(Path(project_root).resolve())
        self.created_at = time.time()
        self.last_access = time.time()
        self.request_count = 0
        self.error_count = 0
        self.lsp_processes: List[subprocess.Popen] = []
        self.symbol_index: Dict[str, Any] = {}
        self._lock = threading.Lock()
    
    def touch(self):
        self.last_access = time.time()
        self.request_count += 1
    
    @property
    def uptime(self):
        return round(time.time() - self.created_at, 1)
    
    def stats(self):
        return {
            "project": self.project_root,
            "uptime": self.uptime,
            "requests": self.request_count,
            "errors": self.error_count,
            "lsp_count": len(self.lsp_processes),
        }


class V8CoreDaemon:
    """
    Serena V8 persistent core daemon.
    
    Responsibilities:
    - Load and cache project state
    - Manage LSP lifecycle
    - Serve tool requests via thin MCP adapter
    - Track memory, metrics, cache
    """
    
    def __init__(self, socket_path: Optional[str] = None, log_level: int = logging.INFO):
        self.socket_path = socket_path or str(Path.home() / ".serena-v8" / "daemon.sock")
        self.log_level = log_level
        self.projects: Dict[str, V8ProjectSession] = {}
        self._running = False
        self._server = None
        self._lock = threading.Lock()
        self._start_time = time.time()
        
        # Daemon state
        self._total_requests = 0
        self._total_errors = 0
        self._total_timeouts = 0
    
    def start(self):
        """Start the V8 core daemon."""
        self._running = True
        
        # Ensure socket directory exists
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Remove stale socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)
        
        # Start socket server
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(32)
        self._server.settimeout(1.0)
        
        log.info(f"Serena V8 Core Daemon listening on {self.socket_path}")
        print(f"Serena V8 Core Daemon listening on {self.socket_path}")
        
        # Accept loop
        try:
            while self._running:
                try:
                    client, addr = self._server.accept()
                    threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
                except socket.timeout:
                    continue
                except OSError:
                    if self._running:
                        raise
        finally:
            self._cleanup()
    
    def _handle_client(self, client: socket.socket):
        """Handle a client connection."""
        try:
            buf = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
                
                # Process complete messages
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
            return self._list_projects()
        elif action == "project_status":
            return self._project_status(project)
        elif action == "project_stats":
            return self._project_stats(project)
        elif action == "telemetry":
            return get_telemetry().stats()
        elif action == "tools_list":
            return self._list_tools(project)
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
            "total_errors": self._total_errors,
            "memory": get_memory_info(),
            "telemetry": get_telemetry().stats(),
        }
    
    def _list_projects(self):
        """List active projects."""
        return list(self.projects.keys())
    
    def _project_status(self, project: str):
        """Get project session status."""
        if project not in self.projects:
            return None
        return self.projects[project].stats()
    
    def _project_stats(self, project: str) -> dict:
        """Get project stats."""
        if project not in self.projects:
            return {"error": "project not loaded"}
        
        session = self.projects[project]
        return {
            **session.stats(),
            "cache": get_query_cache().stats(),
        }
    
    def _list_tools(self, project: str):
        """List available tools for a project."""
        # Return standard tool list
        return [
            "find_symbol",
            "find_referencing_symbols",
            "get_symbols_overview",
            "read_file",
            "create_text_file",
            "replace_content",
            "search_for_pattern",
            "list_dir",
            "get_current_config",
        ]
    
    def _shutdown(self, *args):
        """Graceful shutdown."""
        self._running = False
        if self._server:
            self._server.close()
    
    def _cleanup(self):
        """Cleanup resources."""
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


class V8MCPAdapter:
    """
    Thin MCP adapter that connects to V8 Core Daemon.
    
    Instead of starting a new serena process per request,
    this adapter forwards requests to the persistent daemon.
    """
    
    def __init__(self, socket_path: Optional[str] = None):
        self.socket_path = socket_path or str(Path.home() / ".serena-v8" / "daemon.sock")
    
    def _send(self, request: dict) -> dict:
        """Send request to daemon."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        sock.sendall(json.dumps(request).encode() + b"\n")
        
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        sock.close()
        
        line = buf.split(b"\n")[0]
        return json.loads(line)
    
    def status(self) -> dict:
        """Get daemon status."""
        return self._send({"action": "status"})
    
    def projects(self) -> List[str]:
        """List projects."""
        return self._send({"action": "projects"})
    
    def project_status(self, project: str) -> Optional[dict]:
        """Get project status."""
        return self._send({"action": "project_status", "project": project})
    
    def telemetry(self) -> dict:
        """Get telemetry."""
        return self._send({"action": "telemetry"})
    
    def shutdown(self):
        """Shutdown daemon."""
        self._send({"action": "shutdown"})


# ═══════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════

def daemon_main():
    """Start V8 Core Daemon."""
    import argparse
    parser = argparse.ArgumentParser(prog="serena-v8-daemon")
    parser.add_argument("--socket", help="Unix socket path")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    
    logging.basicConfig(level=getattr(logging, args.log_level))
    
    daemon = V8CoreDaemon(socket_path=args.socket)
    daemon.start()


def adapter_main():
    """V8 MCP Adapter CLI."""
    import argparse
    parser = argparse.ArgumentParser(prog="serena-v8")
    sub = parser.add_subparsers(dest="command")
    
    sub.add_parser("status", help="Daemon status")
    sub.add_parser("projects", help="List projects")
    
    p_status = sub.add_parser("project", help="Project status")
    p_status.add_argument("project_path")
    
    sub.add_parser("telemetry", help="Get telemetry")
    sub.add_parser("shutdown", help="Shutdown daemon")
    
    args = parser.parse_args()
    
    adapter = V8MCPAdapter()
    
    if args.command == "status":
        print(json.dumps(adapter.status(), indent=2))
    elif args.command == "projects":
        print(json.dumps(adapter.projects(), indent=2))
    elif args.command == "project":
        print(json.dumps(adapter.project_status(args.project_path), indent=2))
    elif args.command == "telemetry":
        print(json.dumps(adapter.telemetry(), indent=2))
    elif args.command == "shutdown":
        adapter.shutdown()
    else:
        parser.print_help()


if __name__ == "__main__":
    daemon_main()
