import json
import unittest

from serena.tools.query_project_tools import (
    ListQueryableProjectsTool,
    _resolve_registered_project,
)


class _RegisteredProject:
    def __init__(self, name, root):
        self.project_name = name
        self.project_root = root


class _Backend:
    def is_jetbrains(self):
        return False


class _Config:
    def __init__(self, projects, active_root):
        self.projects = projects
        self.active_root = active_root

    def get_registered_project(self, value):
        matches = [p for p in self.projects if p.project_name == value]
        if len(matches) > 1:
            raise ValueError("Multiple projects found with name 'tp-copydesign'")
        return matches[0] if matches else None


class _Agent:
    def __init__(self, projects, active_root):
        self.serena_config = _Config(projects, active_root)
        self._active_project = type(
            "ActiveProject", (), {"project_name": "tp-copydesign", "project_root": active_root}
        )()

    def get_language_backend(self):
        return _Backend()

    def get_active_project(self):
        return self._active_project


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.primary = _RegisteredProject("tp-copydesign", "/home/user/SuperProjects/tp-copydesign")
        self.temp = _RegisteredProject("tp-copydesign", "/tmp/tp-stable-workflow")
        self.agent = _Agent([self.primary, self.temp], self.primary.project_root)

    def test_list_queryable_projects_prefers_active_root_for_duplicate_name(self):
        tool = object.__new__(ListQueryableProjectsTool)
        tool.agent = self.agent

        result = json.loads(tool.apply())

        self.assertEqual(result["tp-copydesign"], self.primary.project_root)

    def test_query_project_resolves_duplicate_name_to_active_root(self):
        resolved = _resolve_registered_project(self.agent, "tp-copydesign")

        self.assertIs(resolved, self.primary)


if __name__ == "__main__":
    unittest.main()
