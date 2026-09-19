import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from serena_v8.sidecars import (
    SidecarConfig,
    SidecarKind,
    SidecarRunner,
    SidecarStatus,
    WorkspaceCgcIndexer,
    _CgcGatewayClient,
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
            def rewrite_executor(command, **_kwargs):
                if "--update-all" in command:
                    path.write_text(path.read_text().replace("foo()", "bar()"))
                return 0, "[]", ""

            runner = SidecarRunner(SidecarConfig.from_environment(root, environ={}), executor=rewrite_executor)
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

    def test_cgc_gateway_inherits_owner_process_group(self):
        with tempfile.TemporaryDirectory() as root:
            client = _CgcGatewayClient(SidecarConfig.from_environment(root, environ={}))
            process = Mock()
            process.poll.return_value = None
            with (
                patch("serena_v8.sidecars.subprocess.Popen", return_value=process) as popen,
                patch.object(client, "_request", return_value={}),
            ):
                client.start()

            kwargs = popen.call_args.kwargs
            self.assertFalse(kwargs.get("start_new_session", False))
            client._process = None

    def test_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={"SERENA_V8_SIDECAR_TIMEOUT_MS": "250"})
            def timeout_executor(*args, **kwargs):
                raise TimeoutError("expired")
            runner = SidecarRunner(config, executor=timeout_executor)
            result = runner.ast_grep_search("$X", "python", ".")
            self.assertEqual(result.status, SidecarStatus.TIMEOUT)
            self.assertEqual(result.timeout_ms, 250)

    def test_cgc_operation_timeouts_are_separate_and_configurable(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={
                "SERENA_V8_CGC_QUERY_TIMEOUT_MS": "7000",
                "SERENA_V8_CGC_INCREMENTAL_INDEX_TIMEOUT_MS": "11000",
                "SERENA_V8_CGC_FULL_INDEX_TIMEOUT_MS": "90000",
            })
            calls = []
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (calls.append(kwargs) or (0, "", "")))
            runner.cgc_callers("leaf")
            self.assertEqual(calls[-1]["timeout"], 7)
            runner.cgc_index(path="sample.py")
            self.assertEqual(calls[-1]["timeout"], 11)
            (Path(root) / "src").mkdir()
            runner.cgc_index(path="src")
            self.assertEqual(calls[-1]["timeout"], 90)
            runner.cgc_index(path=".")
            self.assertEqual(calls[-1]["timeout"], 90)

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

    def test_cgc_relationship_normalizes_qualified_method_when_path_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={"SERENA_V8_CGC_BIN": "cgc-test"})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "graph\n", ""))
            result = runner.cgc_callees("Example.execute", "src/sample.py")
            self.assertEqual(result.status, SidecarStatus.OK)
            self.assertIn("calls", runner.last_command)
            self.assertIn("execute", runner.last_command)
            self.assertNotIn("Example.execute", runner.last_command)

    def test_cgc_query_cache_reuses_successful_workspace_query(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            config = SidecarConfig.from_environment(root, environ={"SERENA_V8_CGC_QUERY_CACHE_TTL_MS": "5000"})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (calls.append(command) or (0, "graph", "")))
            first = runner.cgc_callers("leaf")
            second = runner.cgc_callers("leaf")
            self.assertEqual(first.status, SidecarStatus.OK)
            self.assertEqual(second.status, SidecarStatus.OK)
            self.assertEqual(len(calls), 1)

        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "Total scanned files | 4\nFunction nodes | 9\nCALLS edges | 12\n", ""))
            result = runner.cgc_index(path="sample.py")
            self.assertEqual(result.metrics["scanned_files"], 4)
            self.assertEqual(result.metrics["function_nodes"], 9)
            self.assertEqual(result.metrics["calls_edges"], 12)

    def test_subprocess_timeout_kills_sidecar(self):
        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={"SERENA_V8_SIDECAR_TIMEOUT_MS": "100"})
            runner = SidecarRunner(config)
            result = runner._run(SidecarKind.CGC, ("python", "-c", "import time; time.sleep(2)"))
            self.assertEqual(result.status, SidecarStatus.TIMEOUT)
            self.assertEqual(result.timeout_ms, 100)

        with tempfile.TemporaryDirectory() as root:
            config = SidecarConfig.from_environment(root, environ={})
            def index_executor(command, **kwargs):
                Path(command[4]).mkdir(parents=True, exist_ok=True)
                return 0, "indexed", ""
            runner = SidecarRunner(config, executor=index_executor)
            indexer = WorkspaceCgcIndexer(runner)
            job_id = indexer.submit(path=".")
            result = indexer.wait(job_id, timeout=2)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["workspace_root"], str(Path(root).resolve()))
            self.assertEqual(result["result"]["status"], "ok")
            self.assertEqual(indexer.status(job_id)["job_id"], job_id)
            indexer.shutdown()

    def test_unchanged_incremental_index_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.py"
            path.write_text("def value():\n    return 1\n")
            calls = []
            config = SidecarConfig.from_environment(root, environ={})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (calls.append(command) or (0, "indexed", "")))
            indexer = WorkspaceCgcIndexer(runner)
            first = indexer.wait(indexer.submit(path="sample.py"), timeout=2)
            self.assertEqual(first["state"], "completed")
            started = time.perf_counter()
            second = indexer.wait(indexer.submit(path="sample.py"), timeout=2)
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.assertEqual(second["state"], "skipped")
            self.assertEqual(second["result"]["metrics"]["skipped"], 1)
            self.assertLess(elapsed_ms, 100)
            self.assertEqual(len(calls), 1)
            indexer.shutdown()

        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "first.py"
            second = Path(root) / "second.py"
            first.write_text("a = 1\n")
            second.write_text("b = 1\n")
            config = SidecarConfig.from_environment(root, environ={})

            def executor(command, **kwargs):
                if "--path" in command:
                    db_path = Path(command[command.index("--path") + 1])
                    db_path.mkdir(parents=True, exist_ok=True)
                return 0, "indexed", ""

            runner = SidecarRunner(config, executor=executor)
            indexer = WorkspaceCgcIndexer(runner)
            self.assertEqual(indexer.wait(indexer.submit(path="."), timeout=2)["state"], "completed")
            self.assertEqual(indexer.wait(indexer.submit(path="first.py"), timeout=2)["state"], "completed")
            second.write_text("b = 2\n")
            self.assertEqual(indexer.stale_paths(), ["second.py"])
            indexer.shutdown()

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.py"
            path.write_text("def value():\n    return 1\n")
            config = SidecarConfig.from_environment(root, environ={})
            runner = SidecarRunner(config, executor=lambda command, **kwargs: (0, "indexed", ""))
            indexer = WorkspaceCgcIndexer(runner)
            job_id = indexer.submit(path="sample.py")
            self.assertEqual(indexer.wait(job_id, timeout=2)["state"], "completed")
            self.assertEqual(indexer.stale_paths(), [])
            path.write_text("def value():\n    return 2\n")
            self.assertEqual(indexer.stale_paths(), ["sample.py"])
            indexer.shutdown()

        with tempfile.TemporaryDirectory() as root:
            runner = SidecarRunner(SidecarConfig.from_environment(root, environ={}), executor=lambda *a, **k: (0, "{}", ""))
            payload = runner.ast_grep_search("$X", "python", ".").to_dict()
            json.dumps(payload)
            self.assertEqual(payload["workspace_root"], str(Path(root).resolve()))

    def test_cgc_snapshot_ignores_generated_python_cache_files(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "sample.py"
            source.write_text("value = 1\n")
            def executor(command, **kwargs):
                if "--path" in command:
                    Path(command[command.index("--path") + 1]).mkdir(parents=True, exist_ok=True)
                return 0, "indexed", ""

            runner = SidecarRunner(
                SidecarConfig.from_environment(root, environ={}),
                executor=executor,
            )
            indexer = WorkspaceCgcIndexer(runner)
            try:
                self.assertEqual(indexer.wait(indexer.submit(path="."), timeout=2)["state"], "completed")
                cache = Path(root) / "__pycache__"
                cache.mkdir()
                (cache / "sample.cpython-313.pyc").write_bytes(b"generated")
                self.assertEqual(indexer.stale_paths(), [])
            finally:
                indexer.shutdown()


if __name__ == "__main__":
    unittest.main()
