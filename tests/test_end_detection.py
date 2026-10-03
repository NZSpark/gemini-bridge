"""`_send_chat_locked` 结束判定的回归测试（用假 page 驱动，不开浏览器）。

覆盖真实踩过的坑：
  * 选择器命中的末节点文本与发送前相同（Gemini 原地复用/替换节点），
    但页面确实在生成：旧代码只看「末节点文本 != before_text」会一直
    判不到「回复已出现」，空转到 180s 超时（poll 刷屏 + HTTP 000 time 180）。
    只有把「已观测到生成中」也算作已出现，才能正确收尾。
  * 结束判定：观测到生成中、随后停止按钮消失即结束。
  * 永远判不到结束（也无生成中信号）时，必须超时报错而非无限空转。

运行：.venv/bin/python -m pytest tests/ -q
"""

import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.driver import GeminiTimeoutError, GeminiWebDriver  # noqa: E402


class FakeInput:
    async def fill(self, text):
        return None


class FakeKeyboard:
    async def press(self, key):
        return None


class FakeNode:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text

    async def query_selector_all(self, selector):
        return []

    async def query_selector(self, selector):
        return None

    async def get_attribute(self, name):
        return None


class FakePage:
    """query_selector_all 第一次调用是发送前的 baseline，之后按脚本返回。"""

    def __init__(self, baseline, script, generating=(False,)):
        self.baseline = list(baseline)
        self.script = list(script)
        self.generating = list(generating)
        self.query_calls = 0
        self.eval_calls = 0
        self.url = "https://gemini.google.com/app"
        self.keyboard = FakeKeyboard()

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        return FakeInput()

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            return [FakeNode(text) for text in self.baseline]
        texts = self.script[min(index - 1, len(self.script) - 1)]
        return [FakeNode(text) for text in texts]

    async def evaluate(self, script):
        # 生成中探测脚本返回 bool；上下文到顶探测脚本返回页面文本（这里恒为空串）。
        if "stop" in script or "\u505c\u6b62" in script:
            index = self.eval_calls
            self.eval_calls += 1
            return self.generating[min(index, len(self.generating) - 1)]
        return ""


class EndDetectionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_session = Path(self.id().replace(".", "_") + ".session")
        patch = unittest.mock.patch.object(config, "SESSION_FILE", self._tmp_session)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(lambda: self._tmp_session.exists() and self._tmp_session.unlink())

        for name, value in (
            ("POLL_INTERVAL_S", 0),
            ("RESPONSE_TIMEOUT_S", 5.0),
            ("PARALLEL_BUCKETS", False),
            ("BUCKET_LOCK_TIMEOUT_S", 0),
        ):
            p = unittest.mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def driver_for(page):
        driver = GeminiWebDriver()
        driver.page = page
        return driver

    def run_chat(self, driver, prompt="go", on_delta=None):
        return asyncio.run(driver.send_chat(prompt, on_delta))


class ReplySeenViaGeneratingTests(EndDetectionTestCase):
    """真实故障回归：末节点文本始终等于 before_text，靠「生成中」判定本轮已出现。"""

    def test_generating_proves_reply_seen_and_finishes(self):
        # 末节点文本始终为「旧答案」（== before_text），节点数也不变。
        # 旧代码：reply_seen 永远 False → 空转到超时。
        # 新代码：观测到 generating=True 后 reply_seen=True，
        #         随后 generating 变 False 即判定结束并返回内容。
        page = FakePage(
            baseline=["旧答案"],
            script=[["旧答案"], ["旧答案"], ["旧答案"], ["旧答案"]],
            generating=[True, True, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "旧答案")


class GeneratingStateTests(EndDetectionTestCase):
    def test_stop_button_disappearing_ends_immediately(self):
        page = FakePage(
            baseline=["旧"],
            script=[["答案1"], ["答案2"], ["答案3"], ["答案4"]],
            generating=[True, True, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "答案3")

    def test_replaced_content_detected_without_node_growth(self):
        page = FakePage(
            baseline=["旧答案", "旧答案"],
            script=[["旧答案", "新答案一段"], ["旧答案", "新答案一段"]],
            generating=[False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "新答案一段")

    def test_no_signal_times_out_instead_of_spinning(self):
        # 文本永远等于 before_text、也无生成中信号：必须超时，而不是无限空转。
        page = FakePage(
            baseline=["旧答案"],
            script=[["旧答案"]],
            generating=[False],
        )
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "RESPONSE_TIMEOUT_S", 0.05):
            with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
                with self.assertRaises(GeminiTimeoutError):
                    self.run_chat(driver)


if __name__ == "__main__":
    unittest.main()
