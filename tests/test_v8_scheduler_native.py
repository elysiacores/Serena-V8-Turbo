"""Tool dispatch must share the native agent executor, including non-tool tasks."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from serena.task_executor import TaskExecutor
from serena.tools.tools_base import Tool


class ProbeTool(Tool):
    def apply(self):
        return self.agent.work()


class NativeSchedulerTests(unittest.TestCase):
    def make_agent(self, work, timeout=1):
        executor = TaskExecutor("scheduler-safety-test")
        return SimpleNamespace(
            issue_task=executor.issue_task,
            executor=executor,
            serena_config=SimpleNamespace(tool_timeout=timeout),
            get_active_project=lambda: SimpleNamespace(project_root="test-project"),
            tool_is_active=lambda name: True,
            record_tool_usage=lambda *args: None,
            get_language_server_manager=lambda: None,
            work=work,
        )

    def test_tools_share_native_queue_with_agent_background_tasks(self):
        release = threading.Event()
        background_started = threading.Event()
        tool_started = threading.Event()
        agent = self.make_agent(lambda: tool_started.set() or "ok")

        def background():
            background_started.set()
            release.wait(3)

        background_task = agent.issue_task(background)
        self.assertTrue(background_started.wait(1))
        with patch("serena.v8_runtime.record_tool_call"), ThreadPoolExecutor(1) as clients:
            result = clients.submit(ProbeTool(agent).apply_ex, log_call=False)
            try:
                self.assertFalse(tool_started.wait(0.1))
            finally:
                release.set()
                background_task.result(2)
            self.assertEqual(result.result(2), "ok")
        self.assertTrue(tool_started.is_set())
