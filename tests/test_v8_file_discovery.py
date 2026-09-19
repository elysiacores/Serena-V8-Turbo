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

    def test_directory_exclusions_do_not_hide_regular_files(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("generated", "vendor", "node_modules", "target", "logs"):
                (Path(root) / name).write_text("source")
            result = scan_directory(root, recursive=True, relative_to=root)
            self.assertEqual(set(result.files), {"generated", "vendor", "node_modules", "target", "logs"})

    def test_default_exclusions_survive_nested_recursion(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "src" / "node_modules").mkdir(parents=True)
            (base / "src" / "node_modules" / "hidden.ts").write_text("hidden")
            result = scan_directory(root, recursive=True, relative_to=root)
            self.assertEqual(result.files, [])

    def test_explicit_ignore_callback_can_include_generated_source(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            generated = base / "generated"
            generated.mkdir()
            source = generated / "client.ts"
            source.write_text("export const ok = true")
            result = scan_directory(
                str(base),
                recursive=True,
                relative_to=str(base),
                is_ignored_dir=lambda _path: False,
                is_ignored_file=lambda _path: False,
            )
            self.assertIn("generated/client.ts", result.files)

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
