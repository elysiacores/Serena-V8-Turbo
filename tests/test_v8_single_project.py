import os
import unittest
from unittest.mock import patch

from serena.agent import SerenaAgent


class SingleProjectInvariantTests(unittest.TestCase):
    def test_duplicate_name_matching_active_project_is_safe_noop(self):
        agent = object.__new__(SerenaAgent)

        class ActiveProject:
            project_name = "workspace-a"
            project_root = "/tmp/example-workspace-a"

            def shutdown(self, timeout=2.0):
                return None

        class Config:
            def get_project(self, value):
                if value == "workspace-a":
                    raise ValueError("Multiple projects found with name 'workspace-a'")
                return None

        agent._active_project = ActiveProject()
        agent._gui_log_viewer = None
        agent._dashboard_manager = None
        agent.serena_config = Config()

        with patch.dict(os.environ, {"SERENA_V8_SINGLE_PROJECT": "1"}):
            with patch.object(agent, "_activate_project", return_value=False) as activate:
                result = agent.activate_project_from_path_or_name("workspace-a")

        self.assertFalse(result)
        activate.assert_called_once()

    def test_duplicate_name_not_matching_active_project_still_rejects(self):
        agent = object.__new__(SerenaAgent)

        class ActiveProject:
            project_name = "workspace-b"
            project_root = "/tmp/test-workspace-b"

            def shutdown(self, timeout=2.0):
                return None

        class Config:
            def get_project(self, value):
                if value == "workspace-a":
                    raise ValueError("Multiple projects found with name 'workspace-a'")
                return None

        agent._active_project = ActiveProject()
        agent._gui_log_viewer = None
        agent._dashboard_manager = None
        agent.serena_config = Config()

        with patch.dict(os.environ, {"SERENA_V8_SINGLE_PROJECT": "1"}):
            with self.assertRaisesRegex(ValueError, "Multiple projects found"):
                agent.activate_project_from_path_or_name("workspace-a")

    def test_project_started_with_folder_rejects_switch_to_another_folder(self):
        agent = object.__new__(SerenaAgent)

        class ActiveProject:
            project_name = "workspace-a"
            project_root = "/tmp/test-workspace-a"

            def shutdown(self, timeout=2.0):
                return None

        class Config:
            def get_project(self, value):
                if value == "workspace-b":
                    return type("Project", (), {"project_name": "workspace-b", "project_root": "/tmp/test-workspace-b"})()
                return None

        agent._active_project = ActiveProject()
        agent._gui_log_viewer = None
        agent._dashboard_manager = None
        agent.serena_config = Config()

        with patch.dict(os.environ, {"SERENA_V8_SINGLE_PROJECT": "1"}):
            with self.assertRaisesRegex(ValueError, "single-project tunnel"):
                agent.activate_project_from_path_or_name("workspace-b")


if __name__ == "__main__":
    unittest.main()
