"""
Serena V8 — Next-Generation Semantic Coding Runtime
"""

__version__ = "8.0.0-dev.1"
__build__ = "2026-09-18"
__commit__ = "v8-phase1"


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
    except:
        pass
    return version

# V8 Runtime
from serena.v8_runtime import (
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
        import serena_v8.lsp_sync
        _lsp_sync_loaded = True

import logging
log = logging.getLogger(__name__)

def _init_log_configuration():
    """Initialize logging for Serena V8."""
    from sensai.util import logging as sensai_logging
    sensai_logging.basicConfig(level=sensai_logging.INFO)

_init_log_configuration()

log.info(f"Serena V8 {__version__} initialized")
