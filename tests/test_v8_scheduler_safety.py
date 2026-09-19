"""Regression coverage for scheduler mutation and lifetime safety."""
import threading
import unittest

from serena_v8.scheduler import Lane, SmartScheduler


class SchedulerSafetyTests(unittest.TestCase):
    def test_completed_request_uses_no_per_request_timer(self):
        from unittest.mock import patch

        scheduler = SmartScheduler()
        try:
            with patch("serena_v8.scheduler.threading.Timer", side_effect=AssertionError("per-request timer created")):
                future, _ = scheduler.submit("read_file", {}, "project", lambda: "done")
                self.assertEqual(future.result(1), "done")
            self.assertEqual(scheduler.stats()["pending_deadlines"], 0)
        finally:
            scheduler.shutdown()

    def test_running_write_cannot_report_timeout_before_its_last_side_effect(self):
        scheduler = SmartScheduler()
        release = threading.Event()
        started = threading.Event()
        effects = []

        def work():
            started.set()
            release.wait(2)
            effects.append("written")
            return "written"

        future, _ = scheduler.submit("replace_content", {}, "project", work, timeout=0.03)
        self.assertTrue(started.wait(1))
        try:
            # Waiting may time out, but the operation itself must remain running:
            # Python cannot kill its thread or roll back its eventual mutation.
            with self.assertRaises(TimeoutError):
                future.result(0.1)
            self.assertFalse(future.done())
            self.assertFalse(future.cancel())
        finally:
            release.set()
        self.assertEqual(future.result(2), "written")
        self.assertEqual(effects, ["written"])

    def test_reads_and_writes_never_overlap_in_either_order(self):
        for first_tool, second_tool in (("read_file", "replace_content"),
                                        ("replace_content", "find_symbol"),
                                        ("replace_content", "replace_content")):
            with self.subTest(first=first_tool, second=second_tool):
                scheduler = SmartScheduler()
                release = threading.Event()
                started = threading.Event()
                second_started = threading.Event()

                def first_work():
                    started.set()
                    release.wait(2)

                first, _ = scheduler.submit(first_tool, {"n": 1}, "project", first_work)
                self.assertTrue(started.wait(1))
                second, _ = scheduler.submit(second_tool, {"n": 2}, "project", second_started.set)
                try:
                    self.assertFalse(second_started.wait(0.05))
                finally:
                    release.set()
                    first.result(3)
                    second.result(3)
                self.assertTrue(second_started.is_set())

    def test_unknown_mutations_are_exclusive_and_never_deduplicated(self):
        for tool in ("write_memory", "insert_at_line", "execute_shell_command",
                     "activate_project", "restart_language_server", "ast_grep_rewrite",
                     "cgc_index", "future_plugin_tool"):
            with self.subTest(tool=tool):
                scheduler = SmartScheduler()
                release = threading.Event()
                calls = []

                def work():
                    calls.append(tool)
                    release.wait(2)
                    return len(calls)

                first, _ = scheduler.submit(tool, {}, "project", work)
                second, _ = scheduler.submit(tool, {}, "project", work)
                try:
                    self.assertIsNot(first, second)
                    self.assertEqual(scheduler.classify(tool), Lane.WRITE_REFACTOR)
                finally:
                    release.set()
                    first.result(3)
                    second.result(3)
                self.assertEqual(len(calls), 2)
