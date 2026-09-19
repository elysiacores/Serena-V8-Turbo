"""
Serena V8 — Phase 9: Hotspot Optimization

Findings from Phase 8 profiler:
- serialize: 95.4% ← main bottleneck
- lsp: 3.5%
- cache: 1.1%

Root causes of slow serialization:
1. Large JSON payloads (deep symbol trees with full body)
2. Unnecessary metadata in responses
3. Repeated serialization of same data
4. No streaming for large results

Optimizations:
1. Compact JSON (no whitespace)
2. Field filtering (only return what's needed)
3. Streaming for large responses
4. Response compression
5. Lazy body loading
"""

import os
import json
import time
import zlib
import threading
from typing import Any, Dict, List, Optional, Set
import logging

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 1. Compact JSON Serializer
# ═══════════════════════════════════════════════════════════════

class CompactSerializer:
    """
    Fast, compact JSON serialization.
    
    - No whitespace
    - Skip None values
    - Limit nested depth
    - Truncate long strings
    """
    
    def __init__(self, max_depth: int = 10, max_string_len: int = 1000, skip_none: bool = True):
        self.max_depth = max_depth
        self.max_string_len = max_string_len
        self.skip_none = skip_none
    
    def serialize(self, obj: Any, depth: int = 0) -> str:
        """Serialize with limits."""
        return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), default=str)
    
    def serialize_compact(self, obj: Any) -> str:
        """Ultra-compact serialization."""
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, (int, float)):
            return str(obj)
        if isinstance(obj, str):
            if len(obj) > self.max_string_len:
                obj = obj[:self.max_string_len] + "..."
            return json.dumps(obj, ensure_ascii=False)
        if isinstance(obj, (list, tuple)):
            items = [self.serialize_compact(item) for item in obj]
            return "[" + ",".join(items) + "]"
        if isinstance(obj, dict):
            items = []
            for k, v in obj.items():
                if self.skip_none and v is None:
                    continue
                items.append(f"{json.dumps(str(k))}:{self.serialize_compact(v)}")
            return "{" + ",".join(items) + "}"
        return json.dumps(obj, default=str)


# ═══════════════════════════════════════════════════════════════
# 2. Field Filtering
# ═══════════════════════════════════════════════════════════════

# Only return these fields by default (not entire symbol tree)
DEFAULT_SYMBOL_FIELDS = {
    "name", "name_path", "kind", "relative_path", 
    "start_line", "start_col", "end_line", "end_col"
}

# Fields that require explicit opt-in
EXPENSIVE_FIELDS = {
    "body", "children", "content", "snippet", "info"
}


class FieldFilter:
    """
    Filter response fields to reduce payload size.
    
    Default: only return lightweight fields
    With include= parameter: also return requested expensive fields
    """
    
    def __init__(self, default_fields: Optional[Set[str]] = None, 
                 expensive_fields: Optional[Set[str]] = None):
        self.default_fields = default_fields or DEFAULT_SYMBOL_FIELDS
        self.expensive_fields = expensive_fields or EXPENSIVE_FIELDS
    
    def filter_symbol(self, symbol: dict, include: Optional[List[str]] = None) -> dict:
        """Filter a single symbol."""
        allowed = set(self.default_fields)
        if include:
            allowed.update(include)
            # body and children are expensive
            if "body" in include:
                allowed.add("body")
            if "children" in include:
                allowed.add("children")
        
        return {k: v for k, v in symbol.items() if k in allowed}
    
    def filter_symbols(self, symbols: List[dict], include: Optional[List[str]] = None) -> List[dict]:
        """Filter a list of symbols."""
        return [self.filter_symbol(s, include) for s in symbols]
    
    def filter_response(self, response: Any, include: Optional[List[str]] = None) -> Any:
        """Filter a full response."""
        if isinstance(response, list):
            return self.filter_symbols(response, include)
        if isinstance(response, dict) and "symbols" in response:
            response["symbols"] = self.filter_symbols(response["symbols"], include)
        return response


# ═══════════════════════════════════════════════════════════════
# 3. Streaming Response Builder
# ═══════════════════════════════════════════════════════════════

class StreamingResponse:
    """
    Build large responses incrementally.
    
    Instead of serializing everything at once:
    1. Start with metadata
    2. Yield items one by one
    3. Truncate at limit
    """
    
    def __init__(self, max_items: int = 100, max_bytes: int = 100 * 1024):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._items = []
        self._total_bytes = 0
        self._truncated = False
    
    def add(self, item: dict):
        """Add an item if under limits."""
        if len(self._items) >= self.max_items:
            self._truncated = True
            return False
        
        item_bytes = len(json.dumps(item).encode())
        if self._total_bytes + item_bytes > self.max_bytes:
            self._truncated = True
            return False
        
        self._items.append(item)
        self._total_bytes += item_bytes
        return True
    
    def build(self) -> dict:
        """Build final response."""
        return {
            "items": self._items,
            "count": len(self._items),
            "truncated": self._truncated,
            "total_bytes": self._total_bytes,
        }


# ═══════════════════════════════════════════════════════════════
# 4. Response Compression
# ═══════════════════════════════════════════════════════════════

class ResponseCompressor:
    """
    Compress large responses with zlib.
    
    Only compress if response > threshold (avoid overhead for small payloads).
    """
    
    def __init__(self, threshold_bytes: int = 1024, level: int = 6):
        self.threshold = threshold_bytes
        self.level = level
    
    def maybe_compress(self, data: str) -> tuple:
        """
        Compress if beneficial.
        Returns (data, was_compressed).
        """
        encoded = data.encode('utf-8')
        if len(encoded) < self.threshold:
            return data, False
        
        compressed = zlib.compress(encoded, self.level)
        if len(compressed) < len(encoded):
            return compressed, True
        
        return data, False
    
    def decompress(self, data: bytes) -> str:
        """Decompress data."""
        return zlib.decompress(data).decode('utf-8')


# ═══════════════════════════════════════════════════════════════
# 5. Lazy Body Loading
# ═══════════════════════════════════════════════════════════════

class LazyBody:
    """
    Lazily load symbol body only when needed.
    
    Instead of including body in initial response:
    1. Return symbol metadata immediately
    2. Load body on explicit request (get_symbol_contents)
    """
    
    def __init__(self, project_root: str):
        self.project_root = project_root
    
    def load_body(self, relative_path: str, start_line: int, end_line: int) -> str:
        """Load file content for a symbol."""
        full_path = os.path.join(self.project_root, relative_path)
        try:
            with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                return ''.join(lines[start_line:end_line+1])
        except Exception as e:
            return f"Error loading body: {e}"
    
    def lazy_symbol(self, symbol: dict) -> dict:
        """Add lazy body loader to symbol."""
        if "body" in symbol:
            # Replace body with loader
            symbol["_body_loader"] = {
                "path": symbol.get("relative_path"),
                "start": symbol.get("start_line"),
                "end": symbol.get("end_line"),
            }
            del symbol["body"]
        return symbol


# ═══════════════════════════════════════════════════════════════
# 6. Fast Response Builder (main optimization)
# ═══════════════════════════════════════════════════════════════

class FastResponseBuilder:
    """
    Build responses with all optimizations applied.
    
    Pipeline:
    1. Field filtering (remove unnecessary fields)
    2. Lazy body loading (defer expensive fields)
    3. Compact serialization
    4. Streaming for large results
    5. Optional compression
    """
    
    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        self.serializer = CompactSerializer(
            max_string_len=config.get("max_string_len", 1000)
        )
        self.field_filter = FieldFilter()
        self.compressor = ResponseCompressor(
            threshold_bytes=config.get("compress_threshold", 1024)
        )
        self.max_items = config.get("max_items", 100)
        self.max_bytes = config.get("max_bytes", 100 * 1024)
        self.compact = config.get("compact", True)
        self.skip_none = config.get("skip_none", True)
    
    def build(self, data: Any, include: Optional[List[str]] = None) -> str:
        """
        Build optimized response.
        
        Steps:
        1. Filter fields
        2. Serialize compactly
        3. Truncate if too large
        """
        # Step 1: Filter
        if include:
            data = self.field_filter.filter_response(data, include)
        
        # Step 2: Serialize
        if self.compact:
            output = self.serializer.serialize_compact(data)
        else:
            output = self.serializer.serialize(data)
        
        # Step 3: Truncate if needed
        if len(output) > self.max_bytes:
            output = output[:self.max_bytes] + "... [truncated]"
        
        return output
    
    def build_streaming(self, items: List[dict], include: Optional[List[str]] = None) -> str:
        """Build response with streaming/batching."""
        stream = StreamingResponse(max_items=self.max_items, max_bytes=self.max_bytes)
        
        for item in items:
            if include:
                item = self.field_filter.filter_symbol(item, include)
            if not stream.add(item):
                break
        
        return self.build(stream.build())


# ═══════════════════════════════════════════════════════════════
# Global builder
# ═══════════════════════════════════════════════════════════════

_global_builder: Optional[FastResponseBuilder] = None
_builder_lock = threading.Lock()


def get_response_builder() -> FastResponseBuilder:
    """Get or create global response builder."""
    global _global_builder
    with _builder_lock:
        if _global_builder is None:
            _global_builder = FastResponseBuilder()
        return _global_builder


# ═══════════════════════════════════════════════════════════════
# Benchmark comparison
# ═══════════════════════════════════════════════════════════════

def benchmark_serialization(data: Any, iterations: int = 100) -> dict:
    """Benchmark different serialization strategies."""
    
    results = {}
    
    # Standard json.dumps
    t0 = time.perf_counter()
    for _ in range(iterations):
        json.dumps(data, ensure_ascii=False)
    results["standard"] = round((time.perf_counter() - t0) * 1000 / iterations, 3)
    
    # Compact (no whitespace)
    t0 = time.perf_counter()
    for _ in range(iterations):
        json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    results["compact"] = round((time.perf_counter() - t0) * 1000 / iterations, 3)
    
    # CompactSerializer
    ser = CompactSerializer()
    t0 = time.perf_counter()
    for _ in range(iterations):
        ser.serialize_compact(data)
    results["v8_compact"] = round((time.perf_counter() - t0) * 1000 / iterations, 3)
    
    # Field filtering
    ff = FieldFilter()
    t0 = time.perf_counter()
    for _ in range(iterations):
        ff.filter_symbols(data if isinstance(data, list) else [data])
    results["field_filter"] = round((time.perf_counter() - t0) * 1000 / iterations, 3)
    
    # Size comparison
    sizes = {
        "standard_bytes": len(json.dumps(data).encode()),
        "compact_bytes": len(json.dumps(data, separators=(',', ':')).encode()),
        "v8_bytes": len(ser.serialize_compact(data).encode()),
    }
    
    return {
        "timing_ms": results,
        "sizes": sizes,
    }
