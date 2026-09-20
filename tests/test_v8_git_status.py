from __future__ import annotations

import unittest
from unittest.mock import patch

from serena.util.git import get_git_status


class V8GitStatusTests(unittest.TestCase):
    def test_porcelain_v2_status_is_parsed_from_single_git_call(self) -> None:
        output = "\n".join(
            [
                "# branch.oid 0123456789abcdef",
                "# branch.head main",
                "1 .M N... 100644 100644 100644 abc abc src/a.py",
                "1 M. N... 100644 100644 100644 abc def src/b.py",
                "? scratch.txt",
            ]
        )
        with patch("serena.util.git.subprocess_check_output", return_value=output) as run:
            status = get_git_status()

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.commit, "0123456789abcdef")
        self.assertTrue(status.has_unstaged_changes)
        self.assertTrue(status.has_staged_uncommitted_changes)
        self.assertTrue(status.has_untracked_files)
        run.assert_called_once()

    def test_clean_status_has_no_dirty_flags(self) -> None:
        with patch(
            "serena.util.git.subprocess_check_output",
            return_value="# branch.oid fedcba9876543210\n# branch.head main",
        ):
            status = get_git_status()

        self.assertIsNotNone(status)
        assert status is not None
        self.assertFalse(status.has_unstaged_changes)
        self.assertFalse(status.has_staged_uncommitted_changes)
        self.assertFalse(status.has_untracked_files)


if __name__ == "__main__":
    unittest.main()
