"""配置层回归测试：默认值、.env 覆盖、`||` 回退链解析、布尔/数值容错。"""

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402


class EnvParserTests(unittest.TestCase):
    def test_env_str_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.env_str("NO_SUCH_KEY", "fallback"), "fallback")

    def test_env_str_override(self):
        with unittest.mock.patch.dict(os.environ, {"NO_SUCH_KEY": "value"}):
            self.assertEqual(config.env_str("NO_SUCH_KEY", "fallback"), "value")

    def test_env_int_default_and_override(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.env_int("NO_SUCH_KEY", 7), 7)
        with unittest.mock.patch.dict(os.environ, {"NO_SUCH_KEY": "9"}):
            self.assertEqual(config.env_int("NO_SUCH_KEY", 7), 9)

    def test_env_int_tolerates_garbage(self):
        with unittest.mock.patch.dict(os.environ, {"NO_SUCH_KEY": "not-an-int"}):
            self.assertEqual(config.env_int("NO_SUCH_KEY", 7), 7)

    def test_env_float_tolerates_garbage(self):
        with unittest.mock.patch.dict(os.environ, {"NO_SUCH_KEY": "xx"}):
            self.assertEqual(config.env_float("NO_SUCH_KEY", 1.5), 1.5)

    def test_env_bool_truthy_and_falsy(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with unittest.mock.patch.dict(os.environ, {"NO_SUCH_KEY": raw}):
                self.assertTrue(config.env_bool("NO_SUCH_KEY"), raw)
        for raw in ("0", "false", "no", "off", ""):
            with unittest.mock.patch.dict(os.environ, {"NO_SUCH_KEY": raw}):
                self.assertFalse(config.env_bool("NO_SUCH_KEY"), raw)

    def test_env_bool_default_when_absent(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(config.env_bool("NO_SUCH_KEY", True))
            self.assertFalse(config.env_bool("NO_SUCH_KEY", False))


class DefaultValueTests(unittest.TestCase):
    def test_listen_defaults(self):
        self.assertEqual(config.HOST, "127.0.0.1")
        self.assertIsInstance(config.PORT, int)

    def test_gemini_prefixed_keys(self):
        self.assertEqual(config.SESSION_KEY_HEADER, "X-Gemini-Session")
        self.assertTrue(config.DEBUG is False or config.DEBUG is True)

    def test_timeout_and_polling(self):
        self.assertGreater(config.RESPONSE_TIMEOUT_S, 0)
        self.assertGreater(config.POLL_INTERVAL_S, 0)
        self.assertGreaterEqual(config.STABLE_POLLS, 1)
        self.assertGreaterEqual(config.LEN_STABLE_POLLS, 1)

    def test_bucket_defaults(self):
        self.assertGreaterEqual(config.MAX_SESSION_BUCKETS, 0)
        self.assertGreaterEqual(config.BUCKET_LOCK_TIMEOUT_S, 0)

    def test_cap_notice_patterns_is_list(self):
        self.assertIsInstance(config.CAP_NOTICE_PATTERNS, list)
        self.assertTrue(all(isinstance(p, str) and p for p in config.CAP_NOTICE_PATTERNS))

    def test_selectors_are_gemini_not_deepseek(self):
        joined = " ".join(config.INPUT_SELECTORS) + config.READY_SELECTOR + config.NEW_CHAT_SELECTOR
        self.assertNotIn("ds-markdown", joined)
        self.assertNotIn("deepseek", joined.lower())
        self.assertIn("contenteditable", config.READY_SELECTOR)

    def test_response_selectors_are_gemini(self):
        self.assertIn("message-content", config.RESPONSE_SELECTORS)

    def test_new_chat_selector_has_fallback_chain(self):
        self.assertIn("||", config.NEW_CHAT_SELECTOR)

    def test_input_selectors_split_by_double_pipe(self):
        self.assertGreaterEqual(len(config.INPUT_SELECTORS), 1)
        for selector in config.INPUT_SELECTORS:
            self.assertNotIn("||", selector)


class EnvFileTests(unittest.TestCase):
    def test_env_file_does_not_clobber_existing_env(self):
        with unittest.mock.patch.dict(os.environ, {"PORT": "4321"}):
            config._load_env_file(config.ENV_FILE)
            self.assertEqual(os.environ["PORT"], "4321")

    def test_load_missing_file_is_noop(self):
        config._load_env_file(Path("/nonexistent/path/to/.env"))


if __name__ == "__main__":
    unittest.main()
