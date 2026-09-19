"""Serena V8 performance runtime.

Keep package import lightweight: production hot-path modules import scheduler/runtime frequently,
so heavyweight legacy components are loaded lazily only when explicitly requested.
"""
from __future__ import annotations

from serena_v8._version import VERSION as __version__

__all__ = [
    "__version__",
    "QueryCache",
    "LSPSupervisor",
    "MemoryBudget",
    "MetricsCollector",
    "SerenaV8",
]

_LEGACY_EXPORTS = {
    "QueryCache",
    "LSPSupervisor",
    "MemoryBudget",
    "MetricsCollector",
    "SerenaV8",
}


def __getattr__(name: str):
    if name in _LEGACY_EXPORTS:
        from serena_v8 import core

        return getattr(core, name)
    raise AttributeError(name)
