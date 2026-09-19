from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from serena.agent import SerenaAgent


class V8AgentStartupTests(unittest.TestCase):
    def test_usage_reporting_is_scheduled_off_the_startup_thread(self) -> None:
        agent = object.__new__(SerenaAgent)
        agent._send_usage_info = Mock()
        thread = Mock()

        with patch("serena.agent.threading.Thread", return_value=thread) as thread_cls:
            agent._send_usage_info_async()

        thread_cls.assert_called_once_with(
            target=agent._send_usage_info,
            name="serena-usage-report",
            daemon=True,
        )
        thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
