from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId


class V8LanguageServerClassResolutionTests(unittest.TestCase):
    def test_bound_reverse_lookup_does_not_scan_other_language_servers(self) -> None:
        class BoundServer:
            _solidlsp_language_server_id = LanguageServerId.PYTHON_JEDI

        with patch.object(LanguageServerId, "get_ls_class", side_effect=AssertionError("must not scan enum")):
            self.assertIs(LanguageServerId.from_ls_class(BoundServer), LanguageServerId.PYTHON_JEDI)

    def test_create_binds_selected_language_server_class_before_construction(self) -> None:
        class FakeServer:
            def __init__(self, config, repository_root_path, solidlsp_settings):
                self.config = config
                self.repository_root_path = repository_root_path
                self.settings = solidlsp_settings

            def set_request_timeout(self, timeout):
                self.timeout = timeout

        config = SimpleNamespace(ls_id=LanguageServerId.PYTHON_JEDI)
        settings = SimpleNamespace()
        with patch.object(LanguageServerId, "get_ls_class", return_value=FakeServer):
            server = SolidLanguageServer.create(config, ".", timeout=7, solidlsp_settings=settings)

        self.assertIs(FakeServer._solidlsp_language_server_id, LanguageServerId.PYTHON_JEDI)
        self.assertEqual(server.timeout, 7)
        self.assertIs(server.settings, settings)


if __name__ == "__main__":
    unittest.main()
