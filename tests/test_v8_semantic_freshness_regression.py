"""Regressions for external edits; no language-server subprocess required."""
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from serena.ls_manager import LanguageServerFileChangeNotifier
from solidlsp.lsp_protocol_handler.lsp_types import FileChangeType


class SemanticFreshnessRegressionTests(unittest.TestCase):
    def test_find_does_not_reuse_results_after_external_create_edit_delete(self):
        from serena.symbol import LanguageServerSymbolRetriever
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def trees(**kwargs):
                return [dict(name=p.read_text(), kind=12, children=[]) for p in root.glob('*.py')]
            ls = SimpleNamespace(request_full_symbol_tree=trees)
            manager = SimpleNamespace(iter_language_servers=lambda: iter([ls]))
            project = SimpleNamespace(project_root=directory, get_language_server_manager_or_raise=lambda: manager)
            retriever = LanguageServerSymbolRetriever(project)
            self.assertEqual(retriever.find('alpha'), [])
            source = root / 'new.py'
            source.write_text('alpha')
            self.assertEqual([s.name for s in retriever.find('alpha')], ['alpha'])
            stamp = source.stat()
            source.write_text('bravo')
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(retriever.find('alpha'), [])
            self.assertEqual([s.name for s in retriever.find('bravo')], ['bravo'])
            source.unlink()
            self.assertEqual(retriever.find('bravo'), [])

    def test_references_requery_when_only_a_caller_changes(self):
        from serena.symbol import LanguageServerSymbolLocation, LanguageServerSymbolRetriever
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'target.py').write_text('def target(): pass\n')
            caller = root / 'caller.py'
            caller.write_text('target()')
            def references(**kwargs):
                return [SimpleNamespace(symbol=dict(name='caller', kind=12, children=[]), line=0, character=0)] if caller.exists() and caller.read_text() == 'target()' else []
            ls = SimpleNamespace(request_referencing_symbols=references)
            manager = SimpleNamespace(get_language_server=lambda p: ls)
            project = SimpleNamespace(project_root=directory, get_language_server_manager_or_raise=lambda: manager)
            retriever = LanguageServerSymbolRetriever(project)
            location = LanguageServerSymbolLocation('target.py', 0, 4)
            query = lambda: retriever.find_referencing_symbols_by_location(location)
            self.assertEqual(len(query()), 1)
            stamp = caller.stat()
            caller.write_text('other_()')
            os.utime(caller, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(query(), [])
            caller.write_text('target()')
            self.assertEqual(len(query()), 1)
            caller.unlink()
            self.assertEqual(query(), [])

    def test_external_same_size_same_or_backdated_mtime_is_changed(self):
        for delta in (0, -1_000_000_000):
            with self.subTest(delta=delta), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'module.py'
                source.write_text('value = 1\n')
                events = []
                ls = SimpleNamespace(
                    server=SimpleNamespace(notify=SimpleNamespace(did_change_watched_files=lambda p: events.extend(p['changes']))),
                    is_ignored_path=lambda *a, **kw: False,
                    open_file=lambda p: nullcontext(),
                )
                project = SimpleNamespace(project_root=directory, gather_source_files=lambda: [p.name for p in root.glob('*.py')])
                manager = SimpleNamespace(iter_language_servers=lambda: iter([ls]))
                notifier = LanguageServerFileChangeNotifier(project, manager)
                stamp = source.stat()
                source.write_text('value = 2\n')
                os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + delta))
                self.assertEqual(notifier.poll_and_notify(), 1)
                self.assertEqual(events[-1]['type'], FileChangeType.Changed)
                self.assertEqual(notifier.poll_and_notify(), 0)
                created = root / 'caller.py'
                created.write_text('from module import value\n')
                self.assertEqual(notifier.poll_and_notify(), 1)
                self.assertEqual(events[-1]['type'], FileChangeType.Created)
                created.unlink()
                self.assertEqual(notifier.poll_and_notify(), 1)
                self.assertEqual(events[-1]['type'], FileChangeType.Deleted)

    def test_known_change_notifies_without_workspace_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "module.py"
            source.write_text("value = 1\n")
            events = []
            ls = SimpleNamespace(
                server=SimpleNamespace(notify=SimpleNamespace(did_change_watched_files=lambda p: events.extend(p["changes"]))),
                is_ignored_path=lambda *a, **kw: False,
                open_file=lambda p: nullcontext(),
            )
            project = SimpleNamespace(
                project_root=directory,
                gather_source_files=lambda: (_ for _ in ()).throw(AssertionError("known edit must not scan workspace")),
            )
            manager = SimpleNamespace(iter_language_servers=lambda: iter([ls]))
            notifier = LanguageServerFileChangeNotifier(project, manager, initial_poll=False)

            self.assertEqual(notifier.notify_known_change("module.py"), 1)
            self.assertEqual(events[-1]["type"], FileChangeType.Created)
            source.write_text("value = 2\n")
            self.assertEqual(notifier.notify_known_change("module.py"), 1)
            self.assertEqual(events[-1]["type"], FileChangeType.Changed)
            source.unlink()
            self.assertEqual(notifier.notify_known_change("module.py"), 1)
            self.assertEqual(events[-1]["type"], FileChangeType.Deleted)

    def test_stable_tree_skips_recursive_rediscovery_but_keeps_edit_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "module.py"
            source.write_text("value = 1\n")
            events = []
            discoveries = 0

            def gather_tree():
                nonlocal discoveries
                discoveries += 1
                return [path.name for path in root.glob("*.py")], [""]

            ls = SimpleNamespace(
                server=SimpleNamespace(notify=SimpleNamespace(did_change_watched_files=lambda p: events.extend(p["changes"]))),
                is_ignored_path=lambda *a, **kw: False,
                open_file=lambda p: nullcontext(),
            )
            project = SimpleNamespace(
                project_root=directory,
                _gather_source_tree=gather_tree,
                gather_source_files=lambda: (_ for _ in ()).throw(AssertionError("fallback discovery should not run")),
            )
            manager = SimpleNamespace(iter_language_servers=lambda: iter([ls]))
            notifier = LanguageServerFileChangeNotifier(project, manager)

            self.assertEqual(discoveries, 1)
            self.assertEqual(notifier.poll_and_notify(), 0)
            self.assertEqual(discoveries, 1)

            stamp = source.stat()
            source.write_text("value = 2\n")
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(notifier.poll_and_notify(), 1)
            self.assertEqual(events[-1]["type"], FileChangeType.Changed)
            self.assertEqual(discoveries, 1)

            created = root / "caller.py"
            created.write_text("from module import value\n")
            root_stat = root.stat()
            os.utime(root, ns=(root_stat.st_atime_ns, root_stat.st_mtime_ns + 1_000_000))
            self.assertEqual(notifier.poll_and_notify(), 1)
            self.assertEqual(events[-1]["type"], FileChangeType.Created)
            self.assertEqual(discoveries, 2)
