"""`_send_chat_locked` 结束判定的回归测试（用假 page 驱动，不开浏览器）。

覆盖真实踩过的坑：
  * 选择器命中的末节点文本与发送前相同（Gemini 原地复用/替换节点），
    但页面确实在生成：旧代码只看「末节点文本 != before_text」会一直
    判不到「回复已出现」，空转到 180s 超时（poll 刷屏 + HTTP 000 time 180）。
    只有把「已观测到生成中」也算作已出现，才能正确收尾。
  * 结束判定：观测到生成中、随后停止按钮消失即结束。
  * 永远判不到结束（也无生成中信号）时，必须超时报错而非无限空转。
  * 写入输入框失败（重挂载导致的失效句柄）：必须**重新定位再试**，
    且单次超时用 FILL_TIMEOUT_MS 而不是 Playwright 默认的 30s。
  * 长度护栏的可观测性：`PROMPT_MAX_CHARS` 截断时必须留下 warning，
    每次发送也必须记录 prompt 长度（否则「长 prompt → 不响应」无从定位）。

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
    async def fill(self, text, timeout=None):
        return None

    async def evaluate(self, script):
        # _dispatch_enter 的脚本会派发 Enter 键事件；假节点只回报“已派发”。
        return True


class FakeKeyboard:
    async def press(self, key):
        return None


class FakeNode:
    """假回复节点。

    ``pending`` 模拟 Gemini 逐 token 显现动画：尚未显现的 token 带 .pending，
    inner_text 取不到它们（只有显现后的前缀），但 text_content 能拿到全文——
    正是生产环境踩到的截断根因。
    """

    def __init__(self, text, pending=False):
        self._text = text
        self._pending = pending

    async def inner_text(self):
        # 模拟：有 pending token 时只能读到已显现的前缀。
        if self._pending:
            return self._text[: max(1, len(self._text) // 2)]
        return self._text

    async def text_content(self):
        return self._text

    async def evaluate(self, script):
        # _has_pending_tokens 的探测脚本形如 "(n) => !!n.querySelector('.pending...')"
        if "querySelector('.pending" in script:
            return self._pending
        # _complete_text 的 JS 返回完整文本
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
            out = []
            for item in self.baseline:
                out.append(FakeNode(item[0], pending=item[1]) if isinstance(item, tuple) else FakeNode(item))
            return out
        texts = self.script[min(index - 1, len(self.script) - 1)]
        nodes = []
        for item in texts:
            if isinstance(item, tuple):
                nodes.append(FakeNode(item[0], pending=item[1]))
            else:
                nodes.append(FakeNode(item))
        return nodes

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



class PendingTokenTests(EndDetectionTestCase):
    """真实故障回归：Gemini 逐 token 显现动画导致回复被截断。

    现象：停止按钮一消失就收尾，但此时后面还有 .pending token 未显现，
    inner_text 只读到半截 JSON（如 TOOL_CALL 的 arguments 被切掉）。
    修复：1) 读文本走 _complete_text（克隆节点去动画类后取全文）；
         2) 停止按钮消失时若仍有 pending token 则继续等待。
    """

    def test_pending_tokens_delay_finish_until_revealed(self):
        # poll1/2: 仍有 pending，停止按钮已消失也不能收尾；
        # poll3: pending 清除，读到完整文本。
        page = FakePage(
            baseline=["旧"],
            script=[
                [("TOOL_CALL: {\"cmd\":", True)],
                [("TOOL_CALL: {\"cmd\":", True)],
                [("TOOL_CALL: {\"cmd\": \"echo hi\"}", False)],
            ],
            generating=[True, False, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, 'TOOL_CALL: {"cmd": "echo hi"}')

    def test_complete_text_reads_full_text_when_pending(self):
        # 即便节点还有 pending，_complete_text 也应拿到全文（text_content 兜底）。
        driver = self.driver_for(FakePage(baseline=["x"], script=[["x"]]))
        node = FakeNode("a " + chr(34) + "b" + chr(34) + " c", pending=True)
        out = asyncio.run(driver._complete_text(node))
        self.assertEqual(out, "a " + chr(34) + "b" + chr(34) + " c")


class _BoundInput:
    """绑定到页面的输入框句柄：fill 成败由页面上的计数器决定。"""

    def __init__(self, page):
        self._page = page

    async def fill(self, text, timeout=None):
        self._page.fill_timeouts.append(timeout)
        if self._page.fail_fills_left > 0:
            self._page.fail_fills_left -= 1
            raise TimeoutError("ElementHandle.fill: Timeout 10000ms exceeded.")
        self._page.filled.append(text)
        return None

    async def evaluate(self, script):
        # _COMPOSER_DIAG_JS 含 contenteditable；_dispatch_enter 的脚本不含
        if "contenteditable" in script:
            return '{"tag":"RICH-TEXTAREA","ce":"true","size":"600x48"}'
        return True


class FlakyFillPage(FakePage):
    """wait_for_selector 每次返回**新句柄**（模拟重挂载）；前 N 次 fill 失败。"""

    def __init__(self, *, fail_fills: int, **kwargs):
        super().__init__(**kwargs)
        self.fail_fills_left = fail_fills
        self.locate_calls = 0
        self.fill_timeouts = []
        self.filled = []

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        self.locate_calls += 1
        return _BoundInput(self)


class FillRetryTests(unittest.TestCase):
    """真实故障回归：`fill` 挂在失效句柄上等 30s，客户端重试全部失败。

    参照姊妹项目 ChatGPTBridge 的 FILL_TIMEOUT_MS / FILL_RETRIES：单次超时收紧，
    失败后**重新定位**输入框（拿到重挂载后的新节点）再试。
    """

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
            ("FILL_TIMEOUT_MS", 1234),
            ("FILL_RETRIES", 3),
            ("RETRY_BACKOFF_S", 0),  # 测试不为退避等待
        ):
            p = unittest.mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _page(self, fail_fills: int) -> FlakyFillPage:
        return FlakyFillPage(
            fail_fills=fail_fills,
            baseline=["旧"],
            script=[["新答案"], ["新答案"]],
            generating=[True, False, False],
        )

    def _run(self, page):
        driver = GeminiWebDriver()
        driver.page = page
        return asyncio.run(driver.send_chat("go"))

    def test_relocates_and_succeeds_within_retries(self):
        page = self._page(fail_fills=2)
        text, _ = self._run(page)
        self.assertEqual(text, "新答案")
        # 1 次进门校验 + 3 次尝试（2 败 1 成）⇒ 每次尝试都重新定位
        self.assertEqual(page.locate_calls, 4)
        self.assertEqual(len(page.fill_timeouts), 3)
        self.assertEqual(page.filled, ["go"])

    def test_each_attempt_uses_configured_timeout(self):
        page = self._page(fail_fills=1)
        self._run(page)
        self.assertEqual(page.fill_timeouts, [1234, 1234])

    def test_gives_up_after_retries_with_actionable_error(self):
        page = self._page(fail_fills=99)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(page)
        message = str(ctx.exception)
        self.assertIn("写入输入框连续失败 3 次", message)
        self.assertIn("1234ms", message)
        # 只试 FILL_RETRIES 次，不多试；底层超时作为 cause 保留
        self.assertEqual(len(page.fill_timeouts), 3)
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)

    def test_failure_logs_composer_diagnostics(self):
        page = self._page(fail_fills=2)
        with self.assertLogs("gemini_web.chat_io", level="WARNING") as ctx:
            self._run(page)
        joined = "\n".join(ctx.output)
        self.assertIn("输入框状态=", joined)
        self.assertIn("RICH-TEXTAREA", joined)


class PromptLengthGuardTests(EndDetectionTestCase):
    """长度护栏的可观测性（用户报告「prompt 长度控制没生效」后的排查结论）。

    真机实测（``tests/e2e/probe_prompt_limit.py``，20K/60K/100K 单条 + 3×60K 同会话）
    显示网页版能吃下这些长度，所以问题不在「控制没触发」，而在**控制不可观测**：
    ``_clamp_prompt`` 截断时一行日志都不留（中间内容静默消失），成功发送的
    prompt 到底多长也无从得知。这两条守护测试锁住新的可观测性。
    """

    def test_send_logs_prompt_size(self):
        page = FakePage(
            baseline=["旧"], script=[["新答案"], ["新答案"]],
            generating=[True, False, False],
        )
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 100000):
            with self.assertLogs("gemini_web.chat_io", level="INFO") as ctx:
                self.run_chat(driver, prompt="x" * 1234)
        joined = "\n".join(ctx.output)
        self.assertIn("[发送]", joined)
        self.assertIn("prompt=1234 字符", joined)

    def test_clamp_is_not_silent_and_keeps_head_and_tail(self):
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 1000):
            with self.assertLogs("gemini_web.chat_io", level="WARNING") as ctx:
                out = GeminiWebDriver._clamp_prompt("h" * 2000 + "t" * 2000)
        joined = "\n".join(ctx.output)
        self.assertIn("已省略中间 3000 字符", joined)
        self.assertIn("PROMPT_MAX_CHARS=1000", joined)
        # 头（工具说明）与尾（最新指令）都保留，中间换成标记
        self.assertTrue(out.startswith("h"))
        self.assertTrue(out.endswith("t"))
        self.assertIn("已省略中间 3000 字符", out)
        self.assertLess(len(out), 1500)

    def test_clamp_under_limit_is_untouched_and_silent(self):
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 1000):
            with self.assertNoLogs("gemini_web.chat_io", level="WARNING"):
                out = GeminiWebDriver._clamp_prompt("x" * 999)
        self.assertEqual(out, "x" * 999)

    def test_clamp_disabled_by_zero(self):
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 0):
            out = GeminiWebDriver._clamp_prompt("x" * 5000)
        self.assertEqual(out, "x" * 5000)


class _ComposerInput:
    """假输入框：能读回自己的文本；“发送”只改变 sent 标记。"""

    def __init__(self, state):
        self.state = state

    async def fill(self, text, timeout=None):
        self.state["text"] = text
        return None

    async def evaluate(self, script):
        if "el.value" in script:  # _COMPOSER_TEXT_JS
            if self.state["unverifiable"]:
                return True  # 非字符串 ⇒ 读不到 ⇒ 无法验证
            return "" if self.state["sent"] else self.state["text"]
        if "KeyboardEvent" in script:  # _ENTER_JS
            self.state["enters"] += 1
            if self.state["enter_works"]:
                self.state["sent"] = True
            return True
        return True

    async def dispatch_event(self, name):
        self.state["enters"] += 1
        if self.state["enter_works"]:
            self.state["sent"] = True
        return True


class _SendButton:
    """假发送按钮：禁用时 click() 会抛超时（模拟 Playwright 的行为）。"""

    def __init__(self, state):
        self.state = state

    async def click(self, timeout=None):
        if not self.state["button_usable"]:
            raise TimeoutError("element is not enabled")
        self.state["clicks"] += 1
        self.state["sent"] = True

    async def dispatch_event(self, name):
        self.state["clicks"] += 1
        if self.state["button_usable"]:
            self.state["sent"] = True

    async def evaluate(self, script):
        return _json_like(self.state["button_usable"])


def _json_like(usable: bool) -> str:
    return '{"disabled": %s}' % ("false" if usable else "true")


class SubmitVerificationTests(unittest.TestCase):
    """真实故障回归：文字已经在输入框里，但消息**没被提交**。

    用户侧观察：prompt 在输入框里，发送按钮没被点击（或点了没反应），
    网页不再产生任何回复，客户端干等到超时。

    根因：`_dispatch_enter` 内嵌的 JS 只要把事件派发出去就返回 true，旧实现据此
    直接 return，**永远走不到点击发送按钮的兑底路径**。现在的阶梯是
    「Enter → 发送按钮 → Enter」，每次尝试后都用「输入框已清空 / 页面已开始生成」验证。
    """

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
            ("FILL_TIMEOUT_MS", 100),
            ("RETRY_BACKOFF_S", 0),
            ("SUBMIT_VERIFY_MS", 40),  # 测试不为验证窗口等待 3 秒
            ("PROMPT_MAX_CHARS", 100000),
        ):
            p = unittest.mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _page(self, *, enter_works, button=None, unverifiable=False):
        state = {
            "text": "", "sent": False, "enters": 0, "clicks": 0,
            "enter_works": enter_works, "button_usable": bool(button),
            "unverifiable": unverifiable, "gen_while_sent": 0,
        }
        page = FakePage(
            baseline=["旧"], script=[["新答案"], ["新答案"]],
            generating=[True, False, False],
        )
        page.input = _ComposerInput(state)
        page.state = state
        page.button = _SendButton(state) if button else None

        async def wait_for_selector(selector, timeout=0, **kwargs):
            return page.input

        async def query_selector(selector):
            return page.button

        async def evaluate(script):
            # 只有**真的发出去**了才会进入“生成中”；否则页面一直不动
            if "stop" in script or "\u505c\u6b62" in script:
                if not state["sent"]:
                    return False
                state["gen_while_sent"] += 1
                return state["gen_while_sent"] <= 2
            return ""

        page.wait_for_selector = wait_for_selector
        page.query_selector = query_selector
        page.evaluate = evaluate
        return page

    def _run(self, page, prompt="go"):
        driver = GeminiWebDriver()
        driver.page = page
        return asyncio.run(driver.send_chat(prompt))

    def test_send_button_fallback_when_enter_is_ignored(self):
        # 合成 Enter 被网页忽略，但点发送按钮有效：必须兑底成功而不是等到超时
        page = self._page(enter_works=False, button=True)
        with self.assertLogs("gemini_web.chat_io", level="INFO") as ctx:
            text, _ = self._run(page)
        self.assertEqual(text, "新答案")
        self.assertTrue(page.state["sent"])
        self.assertEqual(page.state["clicks"], 1)
        self.assertIn("第 2 次尝试（发送按钮）成功", "\n".join(ctx.output))

    def test_ladder_gives_up_with_actionable_error(self):
        # Enter 无效且没有发送按钮：必须报「未能提交」，不能静默等 180s 超时
        page = self._page(enter_works=False, button=False)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(page)
        message = str(ctx.exception)
        self.assertIn("未能提交", message)
        self.assertIn("输入框仍有", message)
        # 阶梯完整执行：Enter → 发送按钮 → Enter
        self.assertEqual(page.state["enters"], 2)

    def test_enter_success_short_circuits(self):
        page = self._page(enter_works=True, button=True)
        text, _ = self._run(page)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.state["enters"], 1)
        self.assertEqual(page.state["clicks"], 0)

    def test_unverifiable_composer_does_not_fail_or_retry(self):
        # 读不到输入框内容（页面差异）：只按旧行为派发一次 Enter，不报错、不空等
        page = self._page(enter_works=False, button=True, unverifiable=True)
        text, _ = self._run(page)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.state["enters"], 1)


if __name__ == "__main__":
    unittest.main()
