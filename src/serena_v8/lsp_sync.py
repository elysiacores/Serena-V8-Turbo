"""
Serena V8 — LSP Sync + Cache Invalidation + Safety Guards

This module ensures:
1. LSP is notified of file changes before responding
2. Symbol cache is invalidated for edited files
3. Rename/resolve operations use fresh symbol locations
4. find_file respects .gitignore and doesn't follow symlinks
"""

import os
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from serena.project import Project
from pathlib import Path

log = logging.getLogger(__name__)


class LSPDocumentSync:
    """Synchronize the existing active project's native language servers."""

    def notify_changed(self, relative_path: str, project: "Project") -> bool:
        """Notify one known edit, falling back to legacy full polling adapters."""
        try:
            notify_known = getattr(project, "ls_notify_file_changed", None)
            if callable(notify_known):
                return bool(notify_known(relative_path))
            project.get_language_server_manager_or_raise().sync_file_system_changes()
            return True
        except Exception:
            log.warning("LSP sync failed for %s", relative_path, exc_info=True)
            return False


class CacheInvalidator:
    """Invalidates V8 caches when files change."""
    
    @staticmethod
    def invalidate_file(relative_path: str, project_root: str):
        """Invalidate all caches for a file."""
        try:
            from serena.symbol import _v8_symbol_cache
            _v8_symbol_cache.invalidate_project_file(relative_path, project_root)
        except Exception as exc:
            log.debug("V8 symbol cache invalidation skipped: %s", exc)

        # Invalidate V8 query cache for this workspace only.
        try:
            from serena.v8_runtime import _v8_query_cache
            _v8_query_cache.invalidate_project_file(relative_path, project_root)
        except Exception as exc:
            log.debug("V8 query cache invalidation skipped: %s", exc)


class SafetyGuard:
    """Prevents editing wrong symbols when location info is stale."""
    
    @staticmethod
    def verify_symbol_at_location(relative_path: str, expected_name: str, 
                                   line: int, col: int, project_root: str) -> bool:
        """Verify that the symbol at (line, col) matches expected_name."""
        full_path = os.path.join(project_root, relative_path)
        try:
            with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            
            if line >= len(lines):
                return False
            
            line_content = lines[line]
            if expected_name in line_content[col:col+len(expected_name)+10]:
                return True
            if expected_name in line_content:
                return True
            return False
        except Exception as e:
            log.warning(f"Safety check failed for {relative_path}:{line}:{col}: {e}")
            return True
    
    @staticmethod
    def resolve_symbol_fresh(symbol_name: str, relative_path: str, project_root: str) -> Optional[dict]:
        """Resolve symbol from fresh file content (not cache)."""
        try:
            full_path = os.path.join(project_root, relative_path)
            if not os.path.exists(full_path):
                return None
            
            with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            
            idx = content.find(symbol_name)
            if idx == -1:
                return None
            
            lines_before = content[:idx].count('\n')
            line_start = content.rfind('\n', 0, idx) + 1
            col = idx - line_start
            
            return {
                "name": symbol_name,
                "relative_path": relative_path,
                "line": lines_before,
                "col": col,
            }
        except Exception as e:
            log.warning(f"Symbol resolution failed: {e}")
            return None


class FindFileFilter:
    """Respects .gitignore and excludes build artifacts."""
    
    EXCLUDE_PATTERNS = [
        '.git', '__pycache__', 'node_modules', '.next', '.next-build',
        'dist', 'build', '.cache', '.turbo', '.venv', 'venv'
    ]
    
    @staticmethod
    def is_excluded(path: str) -> bool:
        """Check if path should be excluded."""
        parts = Path(path).parts
        for part in parts:
            if part in FindFileFilter.EXCLUDE_PATTERNS:
                return True
        return False
    
    @staticmethod
    def safe_scandir(path: str):
        """os.scandir that doesn't follow symlinks."""
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            rel = os.path.relpath(entry.path, path)
                            if FindFileFilter.is_excluded(rel):
                                continue
                        yield entry
                    except OSError:
                        continue
        except OSError:
            return


# Global instances
_lsp_sync = LSPDocumentSync()
_cache_invalidator = CacheInvalidator()
_safety_guard = SafetyGuard()


def get_lsp_sync() -> LSPDocumentSync:
    return _lsp_sync


def get_cache_invalidator() -> CacheInvalidator:
    return _cache_invalidator


def get_safety_guard() -> SafetyGuard:
    return _safety_guard
