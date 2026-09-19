import re
import unittest
from unittest.mock import patch

import serena.project  # initialize the normal Serena/SolidLSP import stack first
from serena.util.file_proxy import FileCollection, FileProxy
from serena.util.text_utils import search_files, search_text


class _MemoryFile(FileProxy):
    def __init__(self, path: str, content: str):
        self.path = path
        self.content = content

    def get_contents(self) -> str:
        return self.content

    def get_relative_path(self) -> str:
        return self.path

    def is_glob_supported(self):
        return True


class SearchHotPathTests(unittest.TestCase):
    def test_no_match_does_not_split_lines(self):
        with patch("serena.util.text_utils.TextUtils.split_lines") as split_lines:
            self.assertEqual(search_text("missing", content="alpha\nbeta\n"), [])
            split_lines.assert_not_called()

    def test_search_files_compiles_regex_once_per_request(self):
        files = FileCollection([
            _MemoryFile("a.py", "needle = 1\n"),
            _MemoryFile("b.py", "needle = 2\n"),
            _MemoryFile("c.py", "other = 3\n"),
        ])
        real_compile = re.compile
        calls = []

        def compile_once(pattern, flags=0):
            calls.append((pattern, flags))
            return real_compile(pattern, flags)

        with patch("serena.util.text_utils.re.compile", side_effect=compile_once):
            matches = search_files(files, r"needle\s*=\s*\d+", multiline=False)

        self.assertEqual(len(matches), 2)
        self.assertEqual(len(calls), 1)

    def test_precompiled_pattern_preserves_multiline_context(self):
        compiled = re.compile(r"alpha.*beta", re.MULTILINE | re.DOTALL)
        matches = search_text(compiled, content="before\nalpha\nmid\nbeta\nafter\n", context_lines_before=1, context_lines_after=1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].lines[0].line_number, 0)
        self.assertEqual(matches[0].matched_lines[0].line_number, 1)
        self.assertEqual(matches[0].matched_lines[-1].line_number, 3)
        self.assertEqual(matches[0].lines[-1].line_number, 4)

    def test_line_index_preserves_crlf_boundary_semantics(self):
        content = "first\r\nneedle\r\nlast"
        matches = search_text(r"needle\r\n", content=content, multiline=True)
        self.assertEqual(len(matches), 1)
        self.assertEqual([line.line_number for line in matches[0].matched_lines], [1])

    def test_many_matches_scale_linearly_enough_for_large_files(self):
        content = "".join(f"needle {i}\n" for i in range(10_000))
        matches = search_text("needle", content=content, multiline=False)
        self.assertEqual(len(matches), 10_000)
        self.assertEqual(matches[0].matched_lines[0].line_number, 0)
        self.assertEqual(matches[-1].matched_lines[0].line_number, 9_999)


if __name__ == "__main__":
    unittest.main()
