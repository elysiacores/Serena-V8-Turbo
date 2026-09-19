import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class V8RuntimeIntegrationTests(unittest.TestCase):
    def test_analytics_does_not_import_anthropic_on_module_import(self):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import serena.analytics; print('anthropic' in sys.modules)",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(probe.stdout.strip(), "False")

    def test_record_tool_call_updates_stats_file(self):
        from serena.v8_runtime import flush_stats, get_telemetry, record_tool_call

        telemetry = get_telemetry()
        before = telemetry.stats().get("total_requests", telemetry.stats().get("total", 0))
        with tempfile.TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "stats.json"
            record_tool_call("find_symbol", 12.5, stats_path=stats_path)
            flush_stats()
            payload = json.loads(stats_path.read_text())

        after = telemetry.stats().get("total_requests", telemetry.stats().get("total", 0))
        self.assertEqual(after, before + 1)
        self.assertGreaterEqual(payload["metrics"]["total"], 1)
        self.assertIn("symbol_cache", payload["cache"])

    def test_workspace_stats_paths_are_isolated(self):
        from serena.v8_runtime import workspace_stats_path

        first = workspace_stats_path("/tmp/example-workspace-a")
        second = workspace_stats_path("/tmp/example-workspace-b")
        self.assertNotEqual(first, second)
        self.assertIn("stats", str(first))
        self.assertTrue(first.name.endswith(".json"))

    def test_async_stats_flush_writes_workspace_snapshot(self):
        from serena.v8_runtime import flush_stats, record_tool_call

        with tempfile.TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "workspace.json"
            record_tool_call("list_dir", 3.0, stats_path=stats_path)
            flush_stats()
            payload = json.loads(stats_path.read_text())

        self.assertGreaterEqual(payload["metrics"]["total"], 1)

    def test_symbol_cache_can_be_cleared_after_an_edit(self):
        from serena.symbol import _V8QueryCache

        cache = _V8QueryCache()
        cache.put("find:/repo:MySymbol:src/a.ts", ["old"])
        self.assertEqual(cache.get("find:/repo:MySymbol:src/a.ts"), ["old"])
        cache.clear()
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertIsNone(cache.get("find:/repo:MySymbol:src/a.ts"))

    def test_project_identity_is_absolute_path_not_object_id(self):
        from serena.symbol import make_v8_cache_key

        key = make_v8_cache_key("find", "/tmp/example/../project", "MySymbol", "src/a.ts")
        self.assertTrue(key.startswith("find:/tmp/project:"), key)
        self.assertNotIn("0x", key)

    def test_edit_saves_then_invalidates_cache_and_syncs_lsp(self):
        from serena.code_editor import CodeEditor
        from serena.symbol import _v8_symbol_cache, make_v8_cache_key

        class Config:
            encoding = "utf-8"

        class LineEnding:
            newline_str = "\n"

        class Project:
            def __init__(self, root):
                self.project_root = root
                self.project_config = Config()
                self.line_ending = LineEnding()
                self.sync_count = 0

            def ls_sync_file_system_changes(self):
                self.sync_count += 1
                return 1

        class Edited(CodeEditor.EditedFile):
            def __init__(self, relative_path, contents):
                super().__init__(relative_path)
                self.contents = contents

            def get_contents(self):
                return self.contents

            def set_contents(self, contents):
                self.contents = contents

            def delete_text_between_positions(self, start_pos, end_pos):
                raise NotImplementedError

            def insert_text_at_position(self, pos, text):
                raise NotImplementedError

        class Editor(CodeEditor):
            @contextmanager
            def _open_file_context(self, relative_path):
                path = Path(self.project_root) / relative_path
                yield Edited(relative_path, path.read_text())

            def _find_unique_symbol(self, name_path, relative_file_path):
                raise NotImplementedError

            def rename_symbol(self, name_path, relative_path, new_name):
                raise NotImplementedError

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "sample.ts"
            file_path.write_text("old")
            project = Project(tmp)
            editor = Editor(project)
            cache_key = make_v8_cache_key("find", tmp, "sample.ts")
            _v8_symbol_cache.put(cache_key, ["stale"])
            with editor.edited_file_context("sample.ts") as edited:
                edited.set_contents("new")

            self.assertEqual(file_path.read_text(), "new")
            self.assertEqual(project.sync_count, 1)
            self.assertIsNone(_v8_symbol_cache.get(cache_key))

    def test_create_text_file_invalidates_cache_and_syncs_lsp(self):
        from serena.symbol import _v8_symbol_cache, make_v8_cache_key
        from serena.tools.file_tools import CreateTextFileTool

        class Config:
            encoding = "utf-8"

        class LineEnding:
            newline_str = "\n"

        class Project:
            def __init__(self, root):
                self.project_root = root
                self.project_config = Config()
                self.line_ending = LineEnding()
                self.sync_count = 0

            def ls_sync_file_system_changes(self):
                self.sync_count += 1

            def validate_relative_path(self, _path):
                return None

        class Agent:
            def __init__(self, project):
                self.project = project

            def get_active_project_or_raise(self):
                return self.project

            def is_using_language_server(self):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            project = Project(tmp)
            tool = CreateTextFileTool(Agent(project))
            cache_key = make_v8_cache_key("find", tmp, "any")
            _v8_symbol_cache.put(cache_key, ["stale"])

            result = tool.apply("new.ts", "export const value = 1")

            self.assertIn("File created", result)
            self.assertEqual(project.sync_count, 1)
            self.assertIsNone(_v8_symbol_cache.get(cache_key))


if __name__ == "__main__":
    unittest.main()
