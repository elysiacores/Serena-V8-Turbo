"""Regression coverage for the optional rewrite transaction boundary."""
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import tempfile
from pathlib import Path

from serena_v8.sidecars import SidecarResult, SidecarKind, SidecarStatus


from serena_v8 import lsp_sync
from serena.tools.file_tools import CreateTextFileTool


class NativeRewriteSyncTests(unittest.TestCase):
    def test_import_does_not_replace_native_edit_methods(self):
        original = CreateTextFileTool.apply
        importlib.reload(lsp_sync)
        self.assertIs(CreateTextFileTool.apply, original)

    def test_sync_uses_the_supplied_active_project_manager(self):
        manager = Mock()
        project = SimpleNamespace(get_language_server_manager_or_raise=lambda: manager)
        self.assertTrue(lsp_sync.LSPDocumentSync().notify_changed("sample.py", project))
        manager.sync_file_system_changes.assert_called_once_with()


from serena.tools.sidecar_tools import AstGrepRewriteTool
from serena.tools.tools_base import ToolMarkerCanEdit


class RewriteTransactionSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / "sample.py"
        self.original = b"value = foo()\r\n"
        self.target.write_bytes(self.original)
        self.manager = Mock()
        self.project = SimpleNamespace(project_root=str(self.root), get_language_server_manager_or_raise=lambda: self.manager)
        self.diagnostics = Mock()
        self.diagnostics.apply.return_value = '{"diagnostics": []}'
        self.agent = SimpleNamespace(get_active_project_or_raise=lambda: self.project, get_tool_by_name=lambda name: self.diagnostics)
        self.tool = AstGrepRewriteTool(self.agent)

    def run_rewrite(self, operation):
        runner = SimpleNamespace(ast_grep_rewrite=operation)
        with patch("serena.tools.sidecar_tools.runner_for_workspace", return_value=runner):
            return json.loads(self.tool.apply("foo()", "bar()", "python", "sample.py", approved=True))

    def test_failed_subprocess_partial_write_is_restored_and_synced(self):
        for failure in (SidecarStatus.ERROR, SidecarStatus.TIMEOUT, RuntimeError("runner exploded")):
            with self.subTest(failure=failure):
                self.manager.reset_mock()
                def operation(*args):
                    self.target.write_bytes(b"partial write")
                    if isinstance(failure, Exception):
                        raise failure
                    return SidecarResult(SidecarKind.AST_GREP, failure, str(self.root), error="runner failed")
                payload = self.run_rewrite(operation)
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertTrue(payload["rolled_back"])
                self.assertTrue(payload["cache_invalidated"])
                self.manager.sync_file_system_changes.assert_called_once_with()
                self.assertTrue(payload["rollback_lsp_sync"])

    def test_rewrite_is_an_edit_even_when_preview_is_default(self):
        self.assertTrue(issubclass(AstGrepRewriteTool, ToolMarkerCanEdit))
