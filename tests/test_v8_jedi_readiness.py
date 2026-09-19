from __future__ import annotations

import unittest
from unittest.mock import patch

from solidlsp.language_servers.jedi_server import JediServer


class V8JediReadinessTests(unittest.TestCase):
    @staticmethod
    def _server() -> JediServer:
        server = object.__new__(JediServer)
        server._has_waited_for_cross_file_references = False
        return server

    def test_jedi_has_no_blind_cross_file_delay(self) -> None:
        server = self._server()

        self.assertEqual(server._get_wait_time_for_cross_file_referencing(), 0.0)

    def test_inherited_cross_file_gate_marks_jedi_ready_without_sleeping(self) -> None:
        server = self._server()

        with patch("solidlsp.ls.sleep") as sleep:
            server._wait_for_cross_file_references_if_needed()

        sleep.assert_called_once_with(0.0)
        self.assertTrue(server._has_waited_for_cross_file_references)


if __name__ == "__main__":
    unittest.main()
