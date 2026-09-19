"""Regression tests for audit snapshot races and resource ceilings."""
import os
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from serena_v8.sidecars import SidecarConfig, SidecarRunner, WorkspaceCgcIndexer


class SnapshotResourceTests(unittest.TestCase):
    def test_subprocess_output_is_drained_without_unbounded_capture(self):
        with tempfile.TemporaryDirectory() as root:
            tracemalloc.start()
            try:
                code, stdout, stderr = SidecarRunner._execute(
                    (sys.executable, "-c", "import os; [(os.write(1,b'x'*65536),os.write(2,b'y'*65536)) for _ in range(192)]"),
                    cwd=root, timeout=5, max_output_bytes=1024)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual((code, stdout, stderr), (0, "x" * 1024, "y" * 1024))
            self.assertLess(peak, 2 * 1024 * 1024)

    def test_index_queue_backpressure_and_completed_job_retention(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "sample.py").write_text("x=1")
            release = threading.Event()
            started = threading.Event()
            def execute(*a, **k):
                started.set()
                release.wait(2)
                return 0, "indexed", ""
            indexer = WorkspaceCgcIndexer(SidecarRunner(SidecarConfig(root), execute))
            indexer._max_pending_jobs = 2
            indexer._max_retained_jobs = 2
            try:
                first = indexer.submit(path="sample.py", force=True)
                self.assertTrue(started.wait(1))
                second = indexer.submit(path="sample.py", force=True)
                with self.assertRaisesRegex(RuntimeError, "queue.*full"):
                    indexer.submit(path="sample.py", force=True)
                self.assertEqual(len(indexer._jobs), 2)
                release.set()
                indexer.wait(first, 2)
                indexer.wait(second, 2)
                for _ in range(6):
                    indexer.wait(indexer.submit(path="sample.py"), 2)
                self.assertLessEqual(len(indexer._jobs), 2)
                self.assertEqual(set(indexer._jobs), set(indexer._job_meta))
                self.assertEqual(set(indexer._jobs), set(indexer._job_paths))
                with self.assertRaises(KeyError):
                    indexer.status(first)
            finally:
                release.set()
                indexer.shutdown()

    def test_cache_evicts_old_entries_and_sweeps_expired_unique_keys(self):
        with tempfile.TemporaryDirectory() as root:
            runner = SidecarRunner(replace(SidecarConfig(root), cgc_query_cache_ttl_ms=10),
                                   lambda *a, **k: (0, "x" * 32, ""))
            SidecarRunner._query_cache.clear()
            with patch.object(SidecarRunner, "_cache_max_entries", 3, create=True), \
                 patch.object(SidecarRunner, "_cache_max_bytes", 100, create=True):
                for i in range(8):
                    runner.cgc_query(str(i))
                self.assertLessEqual(len(runner._query_cache), 3)
                self.assertLessEqual(sum(len(v[1].stdout.encode()) + len(v[1].stderr.encode())
                                         for v in runner._query_cache.values()), 100)
                time.sleep(.03)
                runner.cgc_query("new")
                self.assertEqual(len(runner._query_cache), 1)
            SidecarRunner._query_cache.clear()

    def test_edit_during_index_remains_dirty_even_if_content_reverts(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "sample.py")
            source.write_text("version_one")
            original = source.stat()
            calls = []

            def execute(*args, **kwargs):
                calls.append(source.read_text())
                if len(calls) == 1:
                    source.write_text("version_two")
                return 0, "indexed", ""

            indexer = WorkspaceCgcIndexer(SidecarRunner(SidecarConfig(root), execute))
            try:
                result = indexer.wait(indexer.submit(path="sample.py"), 2)
                self.assertEqual(indexer.stale_paths(), ["sample.py"])
                self.assertEqual(result["state"], "dirty")
                source.write_text("version_one")
                os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertEqual(indexer.stale_paths(), ["sample.py"])
                self.assertEqual(indexer.wait(indexer.submit(path="sample.py"), 2)["state"], "completed")
                self.assertEqual(len(calls), 2)
                self.assertEqual(indexer.stale_paths(), [])
                self.assertEqual(indexer.wait(indexer.submit(path="sample.py"), 2)["state"], "skipped")
            finally:
                indexer.shutdown()
