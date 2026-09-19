"""
Serena V8 — Next-Generation Semantic Coding Runtime
"""

import importlib
import logging

from serena_v8._version import BUILD, COMMIT, VERSION

__version__ = VERSION
__build__ = BUILD
__commit__ = COMMIT


def serena_version() -> str:
    """Return V8 version string."""
    from serena.util.git import get_git_status
    version = __version__
    try:
        git_status = get_git_status()
        if git_status is not None:
            version += f"-{git_status.commit[:8]}"
            if not git_status.is_clean:
                version += "-dirty"
    except Exception:
        pass
    return version

# V8 Runtime. This stays below ``serena_version`` to avoid an import cycle.
from serena.v8_runtime import (  # noqa: E402
    get_v8_identity,
    v8_status,
    v8_status_json,
    v8_measure,
    get_telemetry,
    get_query_cache,
    get_memory_info,
    V8QueryCache,
    V8Telemetry,
)

__all__ = [
    "V8QueryCache",
    "V8Telemetry",
    "get_memory_info",
    "get_query_cache",
    "get_telemetry",
    "get_v8_identity",
    "serena_version",
    "v8_measure",
    "v8_status",
    "v8_status_json",
]

# V8 Cache + LSP Sync (lazy import to avoid circular import)
_v8_symbol_cache = None
_lsp_sync_loaded = False

def _get_v8_symbol_cache():
    global _v8_symbol_cache
    if _v8_symbol_cache is None:
        from serena.symbol import _v8_symbol_cache as _src
        _v8_symbol_cache = _src
    return _v8_symbol_cache

def _load_lsp_sync():
    global _lsp_sync_loaded
    if not _lsp_sync_loaded:
        importlib.import_module("serena_v8.lsp_sync")
        _lsp_sync_loaded = True


log = logging.getLogger(__name__)

def _init_log_configuration():
    """Initialize logging for Serena V8."""
    from sensai.util import logging as sensai_logging
    sensai_logging.basicConfig(level=sensai_logging.INFO)

_init_log_configuration()

log.info(f"Serena V8 {__version__} initialized")
