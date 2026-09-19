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

    def test_find_file_signature_supports_bounded_output(self):
        signature = FindFileTool.apply
        self.assertIn("max_results", signature.__annotations__ or {})
        self.assertIn("max_answer_chars", signature.__annotations__ or {})


if __name__ == "__main__":
    unittest.main()
