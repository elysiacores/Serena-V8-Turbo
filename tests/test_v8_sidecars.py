import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from serena_v8.sidecars import (
    SidecarConfig,
    SidecarKind,
    SidecarRunner,
    SidecarStatus,
    WorkspaceCgcIndexer,
)


class SidecarRunnerTests(unittest.TestCase):
    def test_workspace_identity_is_canonical_and_isolated(self):
        with tempfile.TemporaryDirectory() as parent:
            root_a = Path(parent) / "workspace-a"
            root_b = Path(parent) / "workspace-b"
            root_a.mkdir()
            root_b.mkdir()
            config = SidecarConfig.from_environment(str(root_a), environ={})
            self.assertEqual(config.workspace_root, str(root_a.resolve()))
            self.assertNotEqual(
                config.workspace_root,
                SidecarConfig.from_environment(str(root_b), environ={}).workspace_root,
            )

    def test_ast_grep_command_is_scoped_to_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={
                "SERENA_V8_AST_GREP_BIN": "ast-grep-test",
            })
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "match\n", ""))
            result = runner.ast_grep_search("$X", "python", ".")
            self.assertEqual(result.status, SidecarStatus.OK)
            self.assertEqual(result.kind, SidecarKind.AST_GREP)
            self.assertEqual(result.stdout, "match\n")
            command = runner.last_command
            self.assertEqual(command[0], "ast-grep-test")
            self.assertEqual(command[1:4], ("run", "--pattern", "$X"))
            self.assertEqual(runner.last_cwd, str(Path(root).resolve()))

    def test_ast_grep_rewrite_preview_and_apply(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.ts"
            path.write_text("const value = foo();\n")
            runner = SidecarRunner(SidecarConfig.from_environment(root, environ={}))
            preview = runner.ast_grep_rewrite("foo()", "bar()", "typescript", ".", False)
            self.assertEqual(preview.status, SidecarStatus.OK)
            self.assertEqual(path.read_text(), "const value = foo();\n")
            applied = runner.ast_grep_rewrite("foo()", "bar()", "typescript", ".", True)
            self.assertEqual(applied.status, SidecarStatus.OK)
            self.assertEqual(path.read_text(), "const value = bar();\n")

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            runner = SidecarRunner(SidecarConfig.from_environment(root, environ={}), executor=lambda *a, **k: (0, "", ""))
            with self.assertRaises(ValueError):
                runner.ast_grep_search("$X", "python", outside)

    def test_missing_binary_returns_unavailable_without_raising(self):
        with tempfile.TemporaryDirectory() as root:
            runner = SidecarRunner(SidecarConfig.from_environment(root, environ={}), executor=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
            result = runner.ast_grep_search("$X", "python", ".")
            self.assertEqual(result.status, SidecarStatus.UNAVAILABLE)
            self.assertIn("not found", result.error.lower())

    def test_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={"SERENA_V8_SIDECAR_TIMEOUT_MS": "250"})
            def timeout_executor(*args, **kwargs):
                raise TimeoutError("expired")
            runner = SidecarRunner(config, executor=timeout_executor)
            result = runner.ast_grep_search("$X", "python", ".")
            self.assertEqual(result.status, SidecarStatus.TIMEOUT)
            self.assertEqual(result.timeout_ms, 250)

    def test_cgc_commands_are_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={"SERENA_V8_CGC_BIN": "cgc-test"})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "graph\n", ""))
            self.assertEqual(runner.cgc_index().status, SidecarStatus.OK)
            self.assertEqual(runner.last_command[0:5], ("cgc-test", "--database", "kuzudb", "--path", config.cgc_db_path))
            callers = runner.cgc_callers("leaf", "src/sample.py")
            self.assertEqual(callers.status, SidecarStatus.OK)
            self.assertIn("callers", runner.last_command)
            self.assertIn(str(Path(root).resolve() / "src" / "sample.py"), runner.last_command)
            callees = runner.cgc_callees("root")
            self.assertEqual(callees.status, SidecarStatus.OK)
            self.assertIn("calls", runner.last_command)
            self.assertEqual(runner.last_cwd, str(Path(root).resolve()))

    def test_cgc_command_template_remains_supported(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={
                "SERENA_V8_CGC_COMMAND": "cgc query --project {workspace_root}",
            })
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "graph\n", ""))
            result = runner.cgc_query("find callers")
            self.assertEqual(result.status, SidecarStatus.OK)
            self.assertIn(str(Path(root).resolve()), runner.last_command)

    def test_background_index_has_workspace_scoped_job_status(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "indexed", ""))
            indexer = WorkspaceCgcIndexer(runner)
            job_id = indexer.submit(path=".")
            result = indexer.wait(job_id, timeout=2)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["workspace_root"], str(Path(root).resolve()))
            self.assertEqual(result["result"]["status"], "ok")
            self.assertEqual(indexer.status(job_id)["job_id"], job_id)
            indexer.shutdown()

        with tempfile.TemporaryDirectory() as root:
            runner = SidecarRunner(SidecarConfig.from_environment(root, environ={}), executor=lambda *a, **k: (0, "{}", ""))
            payload = runner.ast_grep_search("$X", "python", ".").to_dict()
            json.dumps(payload)
            self.assertEqual(payload["workspace_root"], str(Path(root).resolve()))


if __name__ == "__main__":
    unittest.main()
