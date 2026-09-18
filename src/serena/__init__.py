"""
Serena V8 — Next-Generation Semantic Coding Runtime
"""

__version__ = "8.0.0-dev.1"
__build__ = "2026-09-18"
__commit__ = "v8-phase1"

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

# V8 Cache (in-source)
from serena.symbol import _v8_symbol_cache

import logging
log = logging.getLogger(__name__)

def _init_log_configuration():
    """Initialize logging for Serena V8."""
    from sensai.util import logging as sensai_logging
    sensai_logging.basicConfig(level=sensai_logging.INFO)

_init_log_configuration()

log.info(f"Serena V8 {__version__} initialized")
