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

    def test_project_epoch_changes_cache_key_after_invalidation(self):
        cache = _V8QueryCache()
        project = "/tmp/workspace-epoch"
        old_key = make_v8_cache_key("find", project, "Widget")
        cache.put(old_key, ["old"])

        cache.invalidate_project(project)

        new_key = make_v8_cache_key("find", project, "Widget")
        self.assertNotEqual(old_key, new_key)
        self.assertIsNone(cache.get(old_key))
        cache.put(new_key, ["new"])
        self.assertEqual(cache.get(new_key), ["new"])


if __name__ == "__main__":
    unittest.main()
