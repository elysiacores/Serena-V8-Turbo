from __future__ import annotations

import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from serena.agent import SerenaAgent
from serena.project import Project


class V8LspStartupOwnershipTests(unittest.TestCase):
    @staticmethod
    def _project_shell(manager: object | None = None) -> Project:
        project = object.__new__(Project)
        project.language_server_manager = manager
        project._language_server_manager_init_error = None
        project._language_server_manager_init_lock = threading.RLock()
        project._agent = None
        return project

    def test_ensure_language_server_manager_reuses_existing_manager(self) -> None:
        manager = object()
        project = self._project_shell(manager)
        project.create_language_server_manager = Mock(side_effect=AssertionError("must not restart"))  # type: ignore[method-assign]

        self.assertIs(project.ensure_language_server_manager(), manager)
        project.create_language_server_manager.assert_not_called()  # type: ignore[attr-defined]

    def test_concurrent_ensure_creates_language_server_manager_once(self) -> None:
        project = self._project_shell()
        manager = object()
        create_count = 0
        count_lock = threading.Lock()

        def create() -> object:
            nonlocal create_count
            with count_lock:
                create_count += 1
            time.sleep(0.03)
            project.language_server_manager = manager  # type: ignore[assignment]
            return manager

        project.create_language_server_manager = create  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _index: project.ensure_language_server_manager(), range(4)))

        self.assertEqual(create_count, 1)
        self.assertTrue(all(result is manager for result in results))

    def test_agent_activation_uses_idempotent_lsp_startup(self) -> None:
        ensure = Mock(return_value=object())
        project = SimpleNamespace(ensure_language_server_manager=ensure)
        agent = object.__new__(SerenaAgent)
        agent._gui_log_viewer = None
        agent._dashboard_manager = None
        agent._active_project = project
        agent.get_language_backend = lambda: SimpleNamespace(is_lsp=lambda: True, is_jetbrains=lambda: False)  # type: ignore[method-assign]

        with patch.dict(os.environ, {"SERENA_V8_SKIP_PREWARM": "0"}):
            agent._init_active_project_language_backend()

        ensure.assert_called_once_with()
        agent._active_project = None

    def test_skip_prewarm_defers_lsp_startup(self) -> None:
        ensure = Mock(return_value=object())
        project = SimpleNamespace(ensure_language_server_manager=ensure)
        agent = object.__new__(SerenaAgent)
        agent._gui_log_viewer = None
        agent._dashboard_manager = None
        agent._active_project = project
        agent.get_language_backend = lambda: SimpleNamespace(is_lsp=lambda: True, is_jetbrains=lambda: False)  # type: ignore[method-assign]

        with patch.dict(os.environ, {"SERENA_V8_SKIP_PREWARM": "1"}):
            agent._init_active_project_language_backend()

        ensure.assert_not_called()
        agent._active_project = None


if __name__ == "__main__":
    unittest.main()
