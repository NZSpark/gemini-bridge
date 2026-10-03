"""会话状态内存缓存的有界化（对照 doc/update_codex.md §2.4）。"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.driver import DEFAULT_SESSION_KEY, GeminiWebDriver  # noqa: E402


class SessionCacheEvictionTests(unittest.TestCase):
    def setUp(self):
        self.driver = GeminiWebDriver(user_data_dir="/tmp/gemini-test-noprofile")
        self._patch = mock.patch.object(config, "SESSION_FILE", Path("/tmp/gemini-test-state.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path("/tmp/gemini-test-state.json").unlink(missing_ok=True)

    def test_evicts_oldest_bucket_keeps_default(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 2):
            self.driver._state(DEFAULT_SESSION_KEY)
            self.driver._page_last_used["b1"] = 100.0
            self.driver._state("b1")
            self.driver._page_last_used["b2"] = 200.0
            self.driver._state("b2")
            self.assertNotIn("b1", self.driver._sessions)
            self.assertIn("b2", self.driver._sessions)
            self.assertIn(DEFAULT_SESSION_KEY, self.driver._sessions)

    def test_default_bucket_never_evicted(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 1):
            self.driver._state(DEFAULT_SESSION_KEY)
            for i in range(5):
                self.driver._page_last_used[f"b{i}"] = float(i)
                self.driver._state(f"b{i}")
            self.assertIn(DEFAULT_SESSION_KEY, self.driver._sessions)

    def test_last_prompt_evicted_with_state(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 1):
            self.driver._state(DEFAULT_SESSION_KEY)
            self.driver._last_prompts["b1"] = "x"
            self.driver._page_last_used["b1"] = 1.0
            self.driver._state("b1")
            self.driver._page_last_used["b2"] = 2.0
            self.driver._state("b2")
            self.assertNotIn("b1", self.driver._last_prompts)

    def test_zero_disables_eviction(self):
        with mock.patch.object(config, "MAX_SESSION_STATE_CACHE", 0):
            for i in range(10):
                self.driver._page_last_used[f"b{i}"] = float(i)
                self.driver._state(f"b{i}")
            self.assertEqual(len(self.driver._sessions), 10)


if __name__ == "__main__":
    unittest.main()
