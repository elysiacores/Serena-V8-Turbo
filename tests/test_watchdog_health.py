import importlib.util
import inspect
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workspace-watchdog-v8.py"


class WatchdogHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("workspace_watchdog_v8", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(cls.module)

    def test_unauthorized_control_plane_is_unhealthy_even_with_open_port(self):
        health = self.module.evaluate_tunnel_health(
            active=True,
            port_open=True,
            recent_log="401 Unauthorized tunnel_use_forbidden Access denied",
            poll_age_seconds=5,
            mcp_running=True,
        )
        self.assertFalse(health["ok"])
        self.assertEqual(health["reason"], "control_plane_unauthorized")

    def test_stale_poll_is_unhealthy(self):
        health = self.module.evaluate_tunnel_health(
            active=True,
            port_open=True,
            recent_log="",
            poll_age_seconds=180,
            mcp_running=True,
        )
        self.assertFalse(health["ok"])
        self.assertEqual(health["reason"], "control_plane_poll_stale")

    def test_healthy_tunnel_requires_all_layers(self):
        health = self.module.evaluate_tunnel_health(
            active=True,
            port_open=True,
            recent_log="poller recovered; polling operational",
            poll_age_seconds=10,
            mcp_running=True,
        )
        self.assertTrue(health["ok"])
        self.assertEqual(health["reason"], "ok")

    def test_zero_poll_timestamp_is_unknown_not_stale(self):
        result = self.module.evaluate_tunnel_health(
            active=True,
            port_open=True,
            recent_log="tunnel-client started",
            poll_age_seconds=0,
            mcp_running=True,
        )
        self.assertTrue(result["ok"], result)

    def test_serena_dev_tunnel_is_monitored(self):
        self.assertEqual(self.module.TUNNEL_PORTS["serena-v8-dev-tunnel.service"], 8792)

    def test_serena_dev_tunnel_has_default_functional_probe(self):
        expected = str(SCRIPT.parents[1])
        self.assertEqual(self.module.MCP_PROBE_PROJECTS["serena-v8-dev-tunnel.service"], expected)

    def test_functional_probe_timeout_is_bounded(self):
        timeout = inspect.signature(self.module.Watchdog.functional_probe).parameters["timeout"].default
        self.assertEqual(timeout, 45.0)

    def test_zero_metric_is_reported_as_zero_age(self):
        watchdog = self.module.Watchdog()
        watchdog._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="commands_poll_last_successful_timestamp_seconds 0\n", stderr=""
        )
        self.assertEqual(watchdog.poll_age_seconds(8789), 0)


if __name__ == "__main__":
    unittest.main()
