import json
import tempfile
import unittest
from pathlib import Path

from serena.util.file_system import scan_directory
from serena.tools.file_tools import FindFileTool


class FileDiscoverySafetyTests(unittest.TestCase):
    def test_recursive_scan_excludes_dependency_and_generated_directories(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "src").mkdir()
            (base / "src" / "keep.ts").write_text("ok")
            for name in ("node_modules", "vendor", "coverage", "logs", "target", "generated", "tmp"):
                (base / name).mkdir()
                (base / name / "hidden.ts").write_text("must not be returned")
            result = scan_directory(str(base), recursive=True, relative_to=str(base))
            self.assertEqual(result.files, ["src/keep.ts"])

    def test_list_dir_defaults_to_safe_bounded_discovery(self):
        import inspect
        from serena.tools.file_tools import ListDirTool

        signature = inspect.signature(ListDirTool.apply)
        self.assertTrue(signature.parameters["skip_ignored_files"].default)
        self.assertEqual(signature.parameters["max_results"].default, 1000)

        signature = FindFileTool.apply
        self.assertIn("max_results", signature.__annotations__ or {})
        self.assertIn("max_answer_chars", signature.__annotations__ or {})


if __name__ == "__main__":
    unittest.main()
