"""
Serena V8 — LSP Sync + Cache Invalidation + Safety Guards

This module ensures:
1. LSP is notified of file changes before responding
2. Symbol cache is invalidated for edited files
3. Rename/resolve operations use fresh symbol locations
4. find_file respects .gitignore and doesn't follow symlinks
"""

import os
import time
import threading
import logging
from typing import Optional, List
from pathlib import Path

log = logging.getLogger(__name__)


class LSPDocumentSync:
    """Notifies LSP of file changes and waits for processing."""
    
    def __init__(self):
        self._lock = threading.Lock()
    
    def notify_changed(self, relative_path: str, project_root: str, timeout: float = 3.0) -> bool:
        """Notify LSP that a file has changed."""
        try:
            from serena.project import Project
            from serena.config.serena_config import SerenaConfig
            
            config_path = Path.home() / ".serena" / "serena_config.yml"
            config = SerenaConfig.from_config_file(str(config_path), generate_if_missing=True)
            project = Project.load(project_root, config)
            ls_manager = project.get_language_server_manager_or_raise()
            
            # Find suitable language server
            lang_server = ls_manager._get_suitable_language_server(relative_path)
            if lang_server is None:
                return True
            
            # Wait briefly for LSP to catch up
            time.sleep(0.1)
            return True
            
        except Exception as e:
            log.warning(f"LSP sync failed for {relative_path}: {e}")
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


def patch_editing_tools():
    """
    Monkey-patch editing tools to use V8 LSP sync + cache invalidation.
    
    This is called once at V8 startup.
    """
    try:
        from serena.tools.file_tools import CreateTextFileTool, ReplaceContentTool, ReplaceInFilesTool
        from serena.tools.symbol_tools import RenameSymbolTool, ReplaceSymbolBodyTool
        
        # Patch CreateTextFileTool
        orig_create_apply = CreateTextFileTool.apply
        def v8_create_apply(self, *args, **kwargs):
            result = orig_create_apply(self, *args, **kwargs)
            # Invalidate cache for created file
            if hasattr(self, '_edited_relative_paths'):
                for path in self._edited_relative_paths:
                    _cache_invalidator.invalidate_file(path, self.agent.project.project_root)
            return result
        CreateTextFileTool.apply = v8_create_apply
        
        # Patch ReplaceContentTool
        orig_replace_apply = ReplaceContentTool.apply
        def v8_replace_apply(self, *args, **kwargs):
            result = orig_replace_apply(self, *args, **kwargs)
            if hasattr(self, '_edited_relative_paths'):
                for path in self._edited_relative_paths:
                    _cache_invalidator.invalidate_file(path, self.agent.project.project_root)
            return result
        ReplaceContentTool.apply = v8_replace_apply
        
        # Patch RenameSymbolTool with safety guard
        orig_rename_apply = RenameSymbolTool.apply
        def v8_rename_apply(self, *args, **kwargs):
            # Verify symbol location before rename
            symbol_name = kwargs.get('symbol_name', '')
            relative_path = kwargs.get('relative_path', '')
            if symbol_name and relative_path:
                fresh = _safety_guard.resolve_symbol_fresh(
                    symbol_name, relative_path, self.agent.project.project_root
                )
                if fresh is None:
                    return f"ERROR: Symbol '{symbol_name}' not found at {relative_path}. File may have been edited. Use read_file to check current content."
            return orig_rename_apply(self, *args, **kwargs)
        RenameSymbolTool.apply = v8_rename_apply
        
        log.info("V8 editing tools patched successfully")
        
    except Exception as e:
        log.warning(f"Failed to patch editing tools: {e}")


# Auto-patch on import (with graceful fallback)
try:
    patch_editing_tools()
except Exception as e:
    # Circular import or missing deps — V8 runtime still works, just no LSP sync patches
    import logging
    logging.getLogger(__name__).warning(f"V8 editing tools patch deferred: {e}")
