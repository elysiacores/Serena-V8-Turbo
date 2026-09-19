#!/usr/bin/env python3
"""Serena V8 tunnel watchdog with end-to-end health checks."""

from __future__ import annotations

import json
import logging
import re
import signal
import socket
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

CHECK_INTERVAL = 30
RESTART_COOLDOWN = 60
MAX_RESTARTS_PER_HOUR = 5
MAX_POLL_AGE_SECONDS = 90
LOG_DIR = Path.home() / ".serena-v8" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TUNNEL_PORTS = {
    "inspi365-tunnel.service": 8787,
    "tp-tunnel.service": 8788,
    "tummun-tunnel.service": 8789,
    "makinni-tunnel.service": 8790,
    "tpos-tunnel.service": 8791,
}

log = logging.getLogger("v8-watchdog")


def configure_logging() -> None:
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for handler in (logging.FileHandler(LOG_DIR / "watchdog.log"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        log.addHandler(handler)


def evaluate_tunnel_health(
    *,
    active: bool,
    port_open: bool,
    recent_log: str,
    poll_age_seconds: float | None,
    mcp_running: bool,
) -> dict:
    """Classify health across systemd, local HTTP, MCP, and control plane."""
    lowered = recent_log.lower()
    if "tunnel_use_forbidden" in lowered or "401 unauthorized" in lowered:
        return {"ok": False, "reason": "control_plane_unauthorized"}
    if "stdio mcp command stdin write failed" in lowered or "file already closed" in lowered:
        return {"ok": False, "reason": "mcp_stdio_closed"}
    if "oom-kill" in lowered or "killed by the oom" in lowered:
        return {"ok": False, "reason": "mcp_oom_killed"}
    if not active:
        return {"ok": False, "reason": "service_inactive"}
    if not port_open:
        return {"ok": False, "reason": "local_port_closed"}
    if not mcp_running:
        return {"ok": False, "reason": "mcp_process_missing"}
    if poll_age_seconds is None or poll_age_seconds > MAX_POLL_AGE_SECONDS:
        return {"ok": False, "reason": "control_plane_poll_stale"}
    return {"ok": True, "reason": "ok"}


class Watchdog:
    def __init__(self) -> None:
        configure_logging()
        self._running = True
        self._restart_times: dict[str, list[datetime]] = {}
        self._last_restart: dict[str, datetime] = {}
        self._status_file = Path.home() / ".serena-v8" / "watchdog-status.json"
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:
        log.info("Received signal %s, shutting down", signum)
        self._running = False

    @staticmethod
    def _run(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def is_service_active(self, service: str) -> bool:
        try:
            return self._run(["systemctl", "--user", "is-active", service]).stdout.strip() == "active"
        except Exception:
            return False

    @staticmethod
    def is_port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            return False

    def is_mcp_running(self, service: str) -> bool:
        try:
            status = self._run(["systemctl", "--user", "status", service, "--no-pager", "--full"]).stdout
            return "serena start-mcp-server" in status
        except Exception:
            return False

    def recent_logs(self, service: str, since: str = "3 minutes ago") -> str:
        try:
            return self._run(
                ["journalctl", "--user", "-u", service, "--since", since, "--no-pager", "-o", "cat"],
                timeout=12,
            ).stdout
        except Exception:
            return ""

    def poll_age_seconds(self, port: int) -> float | None:
        try:
            metrics = self._run(["curl", "-fsS", "--max-time", "3", f"http://127.0.0.1:{port}/metrics"]).stdout
            match = re.search(r"commands_poll_last_successful_timestamp_seconds(?:\{[^}]*\})?\s+([0-9.eE+-]+)", metrics)
            if not match:
                return None
            timestamp = float(match.group(1))
            # The client emits zero until the first command poll succeeds;
            # that is an unknown age, not a timestamp from Unix epoch.
            if timestamp == 0:
                return 0
            return max(0.0, time.time() - timestamp)
        except Exception:
            return None

    def restart_service(self, service: str) -> bool:
        now = datetime.now()
        if service in self._last_restart and (now - self._last_restart[service]).total_seconds() < RESTART_COOLDOWN:
            return False
        cutoff = now - timedelta(hours=1)
        history = [stamp for stamp in self._restart_times.get(service, []) if stamp > cutoff]
        self._restart_times[service] = history
        if len(history) >= MAX_RESTARTS_PER_HOUR:
            log.error("%s exceeded restart limit", service)
            return False
        result = self._run(["systemctl", "--user", "restart", service], timeout=20)
        if result.returncode != 0:
            log.error("Failed to restart %s: %s", service, result.stderr.strip())
            return False
        self._last_restart[service] = now
        history.append(now)
        log.warning("Restarted %s", service)
        return True

    def run_checks(self) -> dict:
        status = {"timestamp": datetime.now().isoformat(), "tunnels": {}, "issues": []}
        for service, port in TUNNEL_PORTS.items():
            active = self.is_service_active(service)
            port_open = self.is_port_open(port)
            mcp_running = self.is_mcp_running(service)
            logs = self.recent_logs(service)
            poll_age = self.poll_age_seconds(port)
            health = evaluate_tunnel_health(
                active=active,
                port_open=port_open,
                recent_log=logs,
                poll_age_seconds=poll_age,
                mcp_running=mcp_running,
            )
            entry = {
                "service": service,
                "port": port,
                "active": active,
                "port_open": port_open,
                "mcp_running": mcp_running,
                "poll_age_seconds": round(poll_age, 1) if poll_age is not None else None,
                **health,
            }
            status["tunnels"][service] = entry
            if not health["ok"]:
                status["issues"].append(f"{service}: {health['reason']}")
                failure_log = LOG_DIR / f"{service}-failure-{int(time.time())}.log"
                failure_log.write_text(logs[-10000:], encoding="utf-8")
                # Authentication cannot be repaired by restarting; avoid a restart loop.
                if health["reason"] != "control_plane_unauthorized":
                    self.restart_service(service)
        temp = self._status_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        temp.replace(self._status_file)
        return status

    def run(self) -> None:
        log.info("V8 watchdog started")
        while self._running:
            try:
                status = self.run_checks()
                healthy = sum(1 for item in status["tunnels"].values() if item["ok"])
                log.info("Tunnel health: %s/%s; issues=%s", healthy, len(TUNNEL_PORTS), status["issues"])
            except Exception:
                log.exception("Watchdog check failed")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    Watchdog().run()
