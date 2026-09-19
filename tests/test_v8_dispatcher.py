import time
import unittest

from serena_v8.runtime.dispatcher import V8Dispatcher
from serena_v8.scheduler import Lane, LaneConfig, SmartScheduler


class DispatcherTests(unittest.TestCase):
    def make_dispatcher(self):
        records = []
        scheduler = SmartScheduler({
            Lane.FAST_READ: LaneConfig(2, 10, 2),
            Lane.SEMANTIC_READ: LaneConfig(2, 10, 2),
            Lane.WRITE_REFACTOR: LaneConfig(1, 10, 2),
        })
        dispatcher = V8Dispatcher(scheduler=scheduler, recorder=lambda *a, **kw: records.append((a, kw)))
        return dispatcher, records

    def test_records_lane_queue_and_execution_stages(self):
        dispatcher, records = self.make_dispatcher()

        result = dispatcher.execute(
            tool_name="read_file",
            args={"relative_path": "a.py"},
            project="/tmp/project",
            operation=lambda: (time.sleep(0.01), "ok")[1],
            timeout=1,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(len(records), 1)
        args, fields = records[0]
        self.assertEqual(args[0], "read_file")
        self.assertGreaterEqual(args[1], fields["execution_ms"])
        self.assertGreater(fields["execution_ms"], 0)
        self.assertGreaterEqual(fields["queue_ms"], 0)
        self.assertEqual(fields["lane"], "fast_read")
        self.assertFalse(fields["error"])
        self.assertFalse(fields["timeout"])

    def test_write_uses_serialized_operation_but_read_does_not(self):
        dispatcher, _ = self.make_dispatcher()
        calls = []

        self.assertEqual(
            dispatcher.execute(
                tool_name="read_file",
                args={},
                project="/tmp/project",
                operation=lambda: calls.append("read-direct") or "read",
                serialized_operation=lambda: calls.append("read-serialized") or "wrong",
                timeout=1,
            ),
            "read",
        )
        self.assertEqual(
            dispatcher.execute(
                tool_name="replace_content",
                args={},
                project="/tmp/project",
                operation=lambda: calls.append("write-direct") or "wrong",
                serialized_operation=lambda: calls.append("write-serialized") or "write",
                timeout=1,
            ),
            "write",
        )
        self.assertEqual(calls, ["read-direct", "write-serialized"])

    def test_records_errors_once(self):
        dispatcher, records = self.make_dispatcher()

        def fail():
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            dispatcher.execute(
                tool_name="find_symbol",
                args={"name_path_pattern": "x"},
                project="/tmp/project",
                operation=fail,
                timeout=1,
            )

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0][1]["error"])
        self.assertFalse(records[0][1]["timeout"])


if __name__ == "__main__":
    unittest.main()
