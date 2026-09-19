from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from serena.config.serena_config import ProjectConfig, SerenaConfig
from solidlsp.ls_config import LanguageServerId


class V8ProjectAutodetectionTests(unittest.TestCase):
    def test_bundled_jedi_is_default_python_autodetection_backend(self) -> None:
        config = SerenaConfig(web_dashboard=False)

        self.assertGreater(
            config.get_ls_priority(LanguageServerId.PYTHON_JEDI),
            config.get_ls_priority(LanguageServerId.PYTHON),
        )

        with tempfile.TemporaryDirectory() as root:
            Path(root, "sample.py").write_text("def hello():\n    return 'world'\n")
            detected = ProjectConfig._determine_project_language_servers(
                root,
                interactive=False,
                serena_config=config,
            )

        self.assertEqual(detected, [LanguageServerId.PYTHON_JEDI])

    def test_async_autogeneration_never_persists_empty_placeholder(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def detect(_project_root: str, interactive: bool, serena_config: SerenaConfig):
            del interactive, serena_config
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return [LanguageServerId.PYTHON_JEDI]

        with tempfile.TemporaryDirectory() as root:
            config = SerenaConfig(web_dashboard=False)
            project_yml = Path(config.get_project_yml_location(root))
            with patch.object(ProjectConfig, "_determine_project_language_servers", side_effect=detect):
                generated = ProjectConfig.autogenerate(
                    root,
                    serena_config=config,
                    asynchronous=True,
                    save_to_disk=True,
                )
                self.assertTrue(started.wait(timeout=5))
                self.assertFalse(project_yml.exists())
                release.set()
                generated.await_asynchronous_completion()

            self.assertTrue(project_yml.is_file())
            self.assertEqual(generated.language_servers, [LanguageServerId.PYTHON_JEDI])
            self.assertIn("- python_jedi", project_yml.read_text())

    def test_user_priority_override_can_disable_bundled_jedi(self) -> None:
        config = SerenaConfig(
            web_dashboard=False,
            ls_priorities={
                LanguageServerId.PYTHON_JEDI.value: 0,
                LanguageServerId.PYTHON.value: 4,
            },
        )

        self.assertEqual(config.get_ls_priority(LanguageServerId.PYTHON_JEDI), 0)
        self.assertEqual(config.get_ls_priority(LanguageServerId.PYTHON), 4)


if __name__ == "__main__":
    unittest.main()
