import unittest

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "workspace_watchdog_v8", Path(__file__).parents[1] / "scripts" / "workspace-watchdog-v8.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
evaluate_tunnel_health = _module.evaluate_tunnel_health


class FunctionalProbeTests(unittest.TestCase):
    def test_failed_functional_probe_is_unhealthy(self):
        result = evaluate_tunnel_health(
            active=True, port_open=True, recent_log="", poll_age_seconds=1,
            mcp_running=True, probe_ok=False,
        )
        self.assertEqual(result["reason"], "mcp_functional_probe_failed")

    def test_unknown_probe_does_not_fail_legacy_callers(self):
        result = evaluate_tunnel_health(
            active=True, port_open=True, recent_log="", poll_age_seconds=1,
            mcp_running=True,
        )
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
