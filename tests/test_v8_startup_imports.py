from __future__ import annotations

import subprocess
import sys
import unittest

from serena.util.text_utils import render_html


class V8StartupImportTests(unittest.TestCase):
    def test_mcp_import_keeps_optional_http_and_gui_stacks_lazy(self) -> None:
        code = r'''
import sys
import serena.mcp
for name in (
    "requests",
    "bs4",
    "serena.jetbrains.jetbrains_plugin_client",
    "serena.project_server",
):
    if name in sys.modules:
        raise SystemExit(f"unexpected eager import: {name}")
print("OK")
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(completed.stdout.strip(), "OK")

    def test_html_renderer_still_loads_parser_on_demand(self) -> None:
        self.assertEqual(render_html("<p>Hello&nbsp;<b>V8</b></p>"), "Hello V8")


if __name__ == "__main__":
    unittest.main()
