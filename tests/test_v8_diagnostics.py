import unittest
from types import SimpleNamespace

from serena.symbol import LanguageServerSymbolRetriever
from solidlsp.ls import SolidLanguageServer


class _TestLanguageServer(SolidLanguageServer):
    def _create_base_initialize_params(self):
        return {}

    def _start_server(self):
        return None


class DiagnosticsFastPathTests(unittest.TestCase):
    def test_new_empty_publication_is_a_valid_result(self):
        server = object.__new__(_TestLanguageServer)
        calls = []
        server._wait_for_published_diagnostics = lambda **kwargs: (calls.append(kwargs) or [])
        server._get_published_diagnostics_generation = lambda uri: 2
        result = server._wait_for_relevant_published_diagnostics(
            uri="file:///workspace/example.ts", after_generation=1, timeout=0.1
        )
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 1)

    def test_no_publication_still_returns_none(self):
        server = object.__new__(_TestLanguageServer)
        server._wait_for_published_diagnostics = lambda **kwargs: None
        server._get_cached_published_diagnostics = lambda uri: None
        result = server._wait_for_relevant_published_diagnostics(
            uri="file:///workspace/example.ts", after_generation=1, timeout=0.01
        )
        self.assertIsNone(result)

    def test_symbol_retriever_uses_published_cache_only_when_allowed(self):
        cached = [{"message": "cached"}]
        fresh_calls = []
        language_server = SimpleNamespace(
            get_cached_published_text_document_diagnostics=lambda **kwargs: cached,
            request_text_document_diagnostics=lambda **kwargs: (fresh_calls.append(kwargs) or [{"message": "fresh"}]),
        )
        manager = SimpleNamespace(get_language_server=lambda _path: language_server)
        project = SimpleNamespace(
            get_language_server_manager_or_raise=lambda: manager,
            ls_sync_file_system_changes=lambda: 0,
        )
        retriever = LanguageServerSymbolRetriever(project)

        self.assertEqual(
            retriever.get_file_diagnostics("sample.py", allow_cached=True),
            cached,
        )
        self.assertEqual(fresh_calls, [])
        self.assertEqual(
            retriever.get_file_diagnostics("sample.py", allow_cached=False),
            [{"message": "fresh"}],
        )
        self.assertEqual(len(fresh_calls), 1)


if __name__ == "__main__":
    unittest.main()
