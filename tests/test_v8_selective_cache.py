import unittest

from serena.symbol import _V8QueryCache, make_v8_cache_key


class SelectiveInvalidationTests(unittest.TestCase):
    def test_edit_invalidates_only_the_affected_project(self):
        cache = _V8QueryCache()
        project_a = "/tmp/workspace-a"
        project_b = "/tmp/workspace-b"
        key_a = make_v8_cache_key("find", project_a, "Widget", "src/a.py")
        key_b = make_v8_cache_key("find", project_b, "Widget", "src/a.py")
        cache.put(key_a, ["old-a"])
        cache.put(key_b, ["keep-b"])

        cache.invalidate_project_file("src/a.py", project_a)

        self.assertIsNone(cache.get(key_a))
        self.assertEqual(cache.get(key_b), ["keep-b"])


if __name__ == "__main__":
    unittest.main()
