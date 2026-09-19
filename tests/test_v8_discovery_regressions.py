"""Discovery regressions using real project policy and temporary filesystem trees."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pathspec import PathSpec

from serena.project import Project
from serena.tools.file_tools import FindFileTool, ListDirTool


def make_tool(tool_cls, root, ignored=(), answer_chars=100000):
    # No LSP/startup thread is needed to exercise the real native path policy.
    project = Project.__new__(Project)
    project.project_root = str(root)
    project._Project__ignore_spec = PathSpec.from_lines("gitwildmatch", ignored)
    project._ignore_spec_available = threading.Event()
    project._ignore_spec_available.set()
    agent = SimpleNamespace(
        get_active_project_or_raise=lambda: project,
        serena_config=SimpleNamespace(default_max_tool_answer_chars=answer_chars),
    )
    return tool_cls(agent)


def discover(tool, **kwargs):
    if isinstance(tool, FindFileTool):
        return json.loads(tool.apply("*.ts", ".", **kwargs))
    return json.loads(tool.apply(".", True, **kwargs))


class DiscoveryRegressionTests(unittest.TestCase):
    def test_max_results_stops_traversal_instead_of_scanning_all_matches(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(50):
                (Path(root) / f"source_{i}.ts").write_text("source")
            for tool_cls in (FindFileTool, ListDirTool):
                with self.subTest(tool=tool_cls.__name__):
                    tool = make_tool(tool_cls, root)
                    with patch.object(tool.project, "is_ignored_path", wraps=tool.project.is_ignored_path) as check:
                        result = discover(tool, max_results=2)
                    self.assertLessEqual(check.call_count, 4)  # root + two results + lookahead
                    self.assertEqual(result["returned_count"], 2)
                    self.assertEqual(result["total_count"], 3)
                    self.assertFalse(result["total_count_is_exact"])
                    self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
