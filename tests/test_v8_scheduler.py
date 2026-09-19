import threading
import time
import unittest
from concurrent.futures import TimeoutError

from serena_v8.scheduler import Lane, LaneConfig, QueueFullError, SmartScheduler


class SchedulerTests(unittest.TestCase):
    def test_request_executes_without_deadlock(self):
        scheduler = SmartScheduler({
            Lane.FAST_READ: LaneConfig(1, 2, 1),
            Lane.SEMANTIC_READ: LaneConfig(1, 2, 1),
            Lane.WRITE_REFACTOR: LaneConfig(1, 2, 1),
        })
        future, _ = scheduler.submit("list_dir", {}, "/tmp/project", lambda: "ok")
        self.assertEqual(future.result(timeout=2), "ok")
        self.assertEqual(scheduler.stats()["active"][Lane.FAST_READ.value], 0)

    def test_same_read_is_single_flight(self):
        scheduler = SmartScheduler()
        release = threading.Event()
        calls = []

        def work():
            calls.append(1)
            release.wait(2)
            return "shared"

        first, _ = scheduler.submit("list_dir", {"path": "."}, "/tmp/project", work)
        second, request_id = scheduler.submit("list_dir", {"path": "."}, "/tmp/project", work)
        self.assertTrue(request_id.startswith("dedup:"))
        release.set()
        self.assertEqual(first.result(timeout=2), "shared")
        self.assertEqual(second.result(timeout=2), "shared")
        self.assertEqual(len(calls), 1)

    def test_queue_is_bounded(self):
        scheduler = SmartScheduler({
            Lane.FAST_READ: LaneConfig(1, 1, 1),
            Lane.SEMANTIC_READ: LaneConfig(1, 1, 1),
            Lane.WRITE_REFACTOR: LaneConfig(1, 1, 1),
        })
        release = threading.Event()
        scheduler.submit("list_dir", {"n": 1}, "/tmp/project", lambda: release.wait(2))
        scheduler.submit("list_dir", {"n": 2}, "/tmp/project", lambda: "queued")
        with self.assertRaises(QueueFullError):
            scheduler.submit("list_dir", {"n": 3}, "/tmp/project", lambda: "rejected")
        release.set()

    def test_timeout_marks_future(self):
        scheduler = SmartScheduler({
            Lane.FAST_READ: LaneConfig(1, 2, 0.05),
            Lane.SEMANTIC_READ: LaneConfig(1, 2, 1),
            Lane.WRITE_REFACTOR: LaneConfig(1, 2, 1),
        })
        future, _ = scheduler.submit("list_dir", {}, "/tmp/project", lambda: time.sleep(0.2))
        with self.assertRaises(TimeoutError):
            future.result(timeout=1)
        self.assertGreaterEqual(scheduler.stats()["timeout"], 1)


if __name__ == "__main__":
    unittest.main()
