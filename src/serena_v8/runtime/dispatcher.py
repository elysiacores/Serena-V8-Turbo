"""Central hot path for MCP tool execution.

The dispatcher owns admission/scheduling and completion telemetry so Serena's
compatibility layer does not need to know scheduler or telemetry internals.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from serena_v8.scheduler import SmartScheduler, get_scheduler


class V8Dispatcher:
    """Execute one Serena operation through the V8 scheduler and record stages."""

    def __init__(
        self,
        scheduler: SmartScheduler | None = None,
        recorder: Callable[..., None] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._recorder = recorder

    def _record(self, tool_name: str, total_ms: float, **kwargs: Any) -> None:
        recorder = self._recorder
        if recorder is None:
            from serena.v8_runtime import record_tool_call

            recorder = record_tool_call
        recorder(tool_name, total_ms, **kwargs)

    def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        project: str,
        operation: Callable[[], str],
        timeout: float,
        serialized_operation: Callable[[], str] | None = None,
    ) -> str:
        started = time.perf_counter()
        scheduler = self._scheduler or get_scheduler()
        future = None
        request_id = ""
        error = False
        timed_out = False

        try:
            lane = scheduler.classify(tool_name)
            selected_operation = serialized_operation if lane.value == "write_refactor" and serialized_operation is not None else operation
            future, request_id = scheduler.submit(tool_name, args, project, selected_operation, timeout=timeout)
            request = getattr(future, "_serena_v8_request", None)
            # Python cannot safely cancel a running mutating thread. A queued
            # write may expire before it starts, but once started we wait for
            # its definitive result instead of returning while it mutates.
            if request is not None and request.lane.value == "write_refactor":
                return future.result()
            return future.result(timeout=timeout + 1)
        except TimeoutError:
            error = True
            timed_out = True
            raise
        except Exception:
            error = True
            raise
        finally:
            total_ms = (time.perf_counter() - started) * 1000
            request = getattr(future, "_serena_v8_request", None) if future is not None else None
            queue_ms = request.wait_time_ms if request is not None else 0.0
            execution_ms = request.execution_time_ms if request is not None else max(0.0, total_ms - queue_ms)
            lane = request.lane.value if request is not None else "unknown"
            self._record(
                tool_name,
                total_ms,
                error=error,
                timeout=timed_out,
                project=project,
                queue_ms=queue_ms,
                execution_ms=execution_ms,
                lane=lane,
                request_id=request_id,
                deduplicated=request_id.startswith("dedup:"),
            )


_dispatcher: V8Dispatcher | None = None


def get_dispatcher() -> V8Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = V8Dispatcher()
    return _dispatcher
