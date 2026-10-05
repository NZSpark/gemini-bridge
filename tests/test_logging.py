"""统一日志配置测试（tasks.md T8.1）。

除了级别映射与 handler 幂等，这里还有一条**结构性守护**：``gemini_web/`` 下
不允许再出现裸 ``print(``。T8.1 的验收标准就是「全仓 print 换成 logging」，
否则半年后有人加一行 print，观测面又会裂开。
"""

import contextlib
import io
import logging
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.logging_setup import (  # noqa: E402
    PACKAGE_LOGGER_NAME,
    resolve_level,
    setup_logging,
)

ROOT = Path(__file__).resolve().parent.parent


def _bridge_handlers(logger):
    return [h for h in logger.handlers if getattr(h, "_gemini_bridge_handler", False)]


class LevelTests(unittest.TestCase):
    def test_debug_flag_selects_debug_level(self):
        self.assertEqual(resolve_level(True), logging.DEBUG)
        self.assertEqual(resolve_level(False), logging.INFO)

    def test_default_follows_config(self):
        with mock.patch.object(config, "DEBUG", True):
            self.assertEqual(resolve_level(), logging.DEBUG)
        with mock.patch.object(config, "DEBUG", False):
            self.assertEqual(resolve_level(), logging.INFO)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        self._saved_handlers = list(self.logger.handlers)
        self._saved_level = self.logger.level
        self._saved_propagate = self.logger.propagate
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        for handler in self._saved_handlers:
            self.logger.addHandler(handler)
        self.logger.setLevel(self._saved_level)
        self.logger.propagate = self._saved_propagate

    def test_forced_setup_installs_exactly_one_handler(self):
        setup_logging(force=True)
        setup_logging(force=True)
        self.assertEqual(len(_bridge_handlers(self.logger)), 1)

    def test_level_and_propagation(self):
        with mock.patch.object(config, "DEBUG", True):
            setup_logging(force=True)
        self.assertEqual(self.logger.level, logging.DEBUG)
        # 已由包级 handler 输出，不应再冒泡到 root（否则与 uvicorn 重复打印）
        self.assertFalse(self.logger.propagate)

    def test_records_are_formatted_with_module_name(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            setup_logging(force=True)
            logging.getLogger("gemini_web.page_pool").warning("关闭页面失败：%s", "boom")
        out = buffer.getvalue()
        self.assertIn("WARNING", out)
        self.assertIn("gemini_web.page_pool", out)
        self.assertIn("关闭页面失败：boom", out)


class NoBarePrintTests(unittest.TestCase):
    _PRINT_RE = re.compile(r"(?<![.\w])print\(")

    def test_package_has_no_print_calls(self):
        offenders = []
        for path in sorted((ROOT / "gemini_web").glob("*.py")):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self._PRINT_RE.search(line):
                    offenders.append(f"{path.name}:{line_no}")
        self.assertEqual(
            offenders, [],
            "T8.1 要求统一走 logging，以下位置仍有裸 print：" + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
