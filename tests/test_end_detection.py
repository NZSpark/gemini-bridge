"""`_send_chat_locked` 结束判定的回归测试（用假 page 驱动，不开浏览器）。

覆盖真实踩过的坑：
  * 选择器命中的末节点文本与发送前相同（Gemini 原地复用/替换节点），
    但页面确实在生成：旧代码只看「末节点文本 != before_text」会一直
    判不到「回复已出现」，空转到 180s 超时（poll 刷屏 + HTTP 000 time 180）。
    只有把「已观测到生成中」也算作已出现，才能正确收尾。
  * 结束判定：观测到生成中、随后停止按钮消失即结束。
  * 永远判不到结束（也无生成中信号）时，必须超时报错而非无限空转。
  * Web 端失去响应时末节点读到的仍是发送前的旧回复 / 刚提交 prompt 的回显：
    绝不能当成本轮回复返回（客户端会把旧指令再执行一遍 → 提示词反复叠加的死循环），
    必须按超时中止并让重试阶梯轮转会话。
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

    async def insert_text(self, text):
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
    """真实故障回归：末节点文本等于 before_text，靠「生成中」判定本轮已开始。

    同时守住一条底线：**没有新正文就不能收尾**。旧实现在生成信号消失后
    会把 `before_text`（上一轮的回复）当成本轮结果返回——Web 端失去响应时，
    上一轮回复往往正是一条「执行指令」（工具调用），客户端于是把同一条指令
    再执行一遍，同一 prompt 被反复提交进网页会话，形成提示词叠加的死循环。
    """

    def test_generating_without_new_text_fails_instead_of_returning_stale(self):
        # 末节点始终是「旧答案」（== before_text）、节点数不变，generating 过后变 False：
        # 绝不能把「旧答案」当本轮回复返回，必须按超时失败。
        page = FakePage(
            baseline=["旧答案"],
            script=[["旧答案"], ["旧答案"], ["旧答案"], ["旧答案"], ["旧答案"]],
            generating=[True, True, False, False, False],
        )
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
            with self.assertRaises(GeminiTimeoutError):
                self.run_chat(self.driver_for(page))

    def test_generating_with_new_text_still_finishes(self):
        # 观测到生成中，且末节点确实变成了新正文：照常收尾（原保护不丢）。
        page = FakePage(
            baseline=["旧答案"],
            script=[["旧答案"], ["新答案"], ["新答案"], ["新答案"]],
            generating=[True, True, False, False],
        )
        text, _ = self.run_chat(self.driver_for(page))
        self.assertEqual(text, "新答案")


class StaleContentGuardTests(EndDetectionTestCase):
    """Web 端失去响应（没有新回复）时，旧内容 / prompt 回显绝不能当作回复返回。"""

    def test_trailing_empty_container_does_not_blind_the_stale_guard(self):
        """基线必须取「最后一个有正文的节点」。

        旧实现的基线取 nodes[-1]：末尾若是尚未渲染的空容器（Web 端不响应时的
        常见形态），基线就成了空串，轮询回退读到上一轮旧回复时会被当成新回复返回。
        """
        page = FakePage(
            baseline=["旧答案", ""],
            script=[["旧答案", ""]],
            generating=[False],
        )
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
            with unittest.mock.patch.object(config, "STALL_POLLS", 2):
                with self.assertRaises(GeminiTimeoutError):
                    self.run_chat(driver)

    def test_prompt_echo_is_not_accepted_as_reply(self):
        """末节点若是刚提交 prompt 的回显（选择器命中用户消息节点），不能当回复。"""
        echo = "请执行以下指令：" + "x" * 400
        page = FakePage(
            baseline=["旧答案"],
            script=[[echo], [echo], [echo], [echo]],
            generating=[False],
        )
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
            with unittest.mock.patch.object(config, "STALL_POLLS", 2):
                with self.assertLogs("gemini_web.chat_io", level="WARNING") as ctx:
                    with self.assertRaises(GeminiTimeoutError):
                        self.run_chat(driver, prompt=echo)
        self.assertIn("回显", "\n".join(ctx.output))

    def test_lossy_prompt_echo_is_still_recognized(self):
        """回显可能被编辑器有损渲染（丢换行 / 少量字符）：仍须识别为回显。"""
        prompt = ("请执行以下指令：\n" + "step\n" * 100).strip()
        flattened = prompt.replace("\n", "")
        lossy = flattened[: int(len(flattened) * 0.95)]
        page = FakePage(
            baseline=["旧答案"],
            script=[[lossy], [lossy]],
            generating=[False],
        )
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
            with unittest.mock.patch.object(config, "STALL_POLLS", 2):
                with self.assertRaises(GeminiTimeoutError):
                    self.run_chat(driver, prompt=prompt)


class ClosedPageGuardTests(EndDetectionTestCase):
    """页面已被关闭（标签被手工关闭 / 崩溃）时，报错要指向真因。

    旧行为：已关闭的页面上 ``wait_for_selector`` 抛错被当作“选择器没命中”，
    最终报「无法找到对话输入框，请检查登录状态」——排查方向完全被带偏
    （真机：OpenAI SDK 的 ``ua:openai`` 桶永久 502，而其它桶正常）。
    """

    def test_closed_page_reports_closed_not_login(self):
        page = FakePage(baseline=["旧"], script=[["旧"]])
        page.is_closed = lambda: True

        with self.assertRaises(RuntimeError) as ctx:
            self.run_chat(self.driver_for(page))

        message = str(ctx.exception)
        self.assertIn("已关闭", message)
        self.assertNotIn("登录", message)


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

    async def click(self, timeout=None):
        self.state["focus_clicks"] += 1
        return None

    async def evaluate(self, script, arg=None):
        if arg is not None and "insertText" in script:  # _INSERT_TEXT_JS（分块写入退路）
            self.state["text"] = (self.state["text"] or "") + arg
            return True
        if "selectNodeContents" in script:  # _CLEAR_COMPOSER_JS
            self.state["text"] = ""
            return True
        if "el.value" in script:  # _COMPOSER_TEXT_JS
            if self.state["unverifiable"]:
                return True  # 非字符串 ⇒ 读不到 ⇒ 无法验证
            return "" if self.state["sent"] else self.state["text"]
        if "activeElement" in script:  # _IS_ACTIVE_JS
            return True
        if "KeyboardEvent" in script:  # _ENTER_JS（合成事件）
            self.state["enters"] += 1
            self.state["synthetic_enters"] += 1
            if self.state["enter_works"]:
                self.state["sent"] = True
            return True
        return True

    async def dispatch_event(self, name):
        return True

    async def dispatch_event(self, name):
        self.state["enters"] += 1
        if self.state["enter_works"]:
            self.state["sent"] = True
        return True


class _StateKeyboard:
    """假“真实键盘通道”（对应 CDP `keyboard.insert_text` / `press`）。"""

    def __init__(self, state):
        self.state = state
        self.presses = []  # 记录真实按键，供断言“首选键盘 Enter”

    async def insert_text(self, text):
        self.state["text"] = (self.state["text"] or "") + text

    async def press(self, key):
        self.presses.append(key)
        if key == "Backspace":
            self.state["text"] = ""
        elif key == "Enter":
            self.state["enters"] += 1
            if self.state["enter_works"]:
                self.state["sent"] = True


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
            "text": "", "sent": False, "enters": 0, "clicks": 0, "focus_clicks": 0,
            "synthetic_enters": 0,
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
        page.keyboard = _StateKeyboard(state)

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

    def test_real_keyboard_enter_is_preferred_over_synthetic(self):
        """提交首选**真实键盘 Enter**（CDP）：合成事件 isTrusted=false 常被编辑器忽略。

        参照姊妹项目 ChatGPTBridge 的 `_keyboard_enter`。
        """
        page = self._page(enter_works=True)
        self._run(page)
        self.assertEqual(page.keyboard.presses, ["Enter"])  # 只用了真实键盘
        self.assertEqual(page.state["synthetic_enters"], 0)  # 合成事件一次都没用
        self.assertEqual(page.state["clicks"], 0)  # 也没点按钮

    def test_synthetic_enter_is_last_resort(self):
        # 键盘 Enter 无效且没有发送按钮 → 才用合成 Enter（最后一级）
        page = self._page(enter_works=False, button=False)
        with self.assertRaises(RuntimeError):
            self._run(page)
        self.assertEqual(page.keyboard.presses.count("Enter"), 1)  # 第一级：真实键盘
        self.assertEqual(page.state["synthetic_enters"], 1)  # 末级：合成事件

    def test_unverifiable_composer_does_not_fail_or_retry(self):
        # 读不到输入框内容（页面差异）：只按旧行为派发一次 Enter，不报错、不空等
        page = self._page(enter_works=False, button=True, unverifiable=True)
        text, _ = self._run(page)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.state["enters"], 1)


class _ChunkyKeyboard:
    """假“真实键盘通道”（对应 CDP `keyboard.insert_text` / `press`）。"""

    def __init__(self, page):
        self.page = page

    async def insert_text(self, text):
        if self.page.keyboard_broken:
            raise RuntimeError("keyboard channel unavailable")
        self.page.record_insert(text, source="keyboard")

    async def press(self, key):
        self.page.presses.append(key)
        if key == "Backspace":  # 模拟“全选 + 删除”
            self.page.store["text"] = ""
        elif key == "Enter" and self.page.enter_works:  # 真实键盘 Enter = 提交
            self.page.submitted_text = self.page.store["text"]
            self.page.store["text"] = ""


class _ChunkyInput:
    """假输入框：只接受**小区块**插入；一次塞太长就报“元素不可编辑”（模拟主线程卡死）。"""

    def __init__(self, page, state):
        self.page = page
        self.state = state

    @property
    def store(self):
        return self.page.store

    async def click(self, timeout=None):
        self.page.focus_clicks += 1
        return None

    async def evaluate(self, script, arg=None):
        if arg is not None and "insertText" in script:  # _INSERT_TEXT_JS
            self.page.record_insert(arg, source="execCommand")
            return True
        if "collapse(false)" in script:  # _SET_CARET_JS（折到末尾）
            self.page.caret_sets += 1
            self.page.caret_in_composer = True
            return True
        if "selectNodeContents" in script:  # _CLEAR_COMPOSER_JS
            self.store["text"] = ""
            return True
        if "el.value" in script:  # _COMPOSER_TEXT_JS
            if self.page.unreadable:
                return True
            if self.page.normalize_on_read:
                # 编辑器把换行渲染成块级节点：读回时换行消失
                return self.store["text"].replace("\n", " ")
            if self.page.lossy_readback:
                # 更接近真机：换行被渲染掉 + markdown 标记被拿掉 → 读回明显变短
                return self.store["text"].replace("\n", "").replace("**", "")
            return self.store["text"]
        if "activeElement" in script:  # _IS_ACTIVE_JS
            return True
        if "KeyboardEvent" in script:  # _ENTER_JS（合成事件）
            self.page.synthetic_enters += 1
            if self.page.enter_works:
                self.page.submitted_text = self.store["text"]
                self.store["text"] = ""
            return True
        return True

    async def fill(self, text, timeout=None):
        # 整段一次写入。fill_writes_then_raises 模拟**真机签名**：文本其实已经写进去了
        # （真机上输入框长出了 child_nodes: 616），fill 却报超时。
        self.page.whole_fills += 1
        if len(text) > self.page.max_chunk and not self.page.fill_writes_then_raises:
            raise TimeoutError("waiting for element to be visible, enabled and editable")
        self.store["text"] = text
        if self.page.fill_writes_then_raises:
            raise TimeoutError("waiting for element to be visible, enabled and editable")
        return None

    async def dispatch_event(self, name):
        return True


class _ChunkyPage(FakePage):
    """分块写入用的假页面（同一个 store 保存输入框文本，重建句柄不会丢文本）。"""

    def __init__(
        self, *, max_chunk=4000, fail_at=None, unreadable=False, enter_works=True,
        require_caret=True, ignore_inserts=False, fill_writes_then_raises=False,
        normalize_on_read=False, lossy_readback=False,
        baseline=None, script=None, generating=None,
    ):
        # baseline / script / generating 可按需覆盖：默认是「发送前只有旧回复，
        # 提交后出现新回复」这一轮；连发多轮的用例需要自带每轮的基线快照。
        super().__init__(
            baseline=["旧"] if baseline is None else list(baseline),
            script=[["新答案"], ["新答案"]] if script is None else [list(p) for p in script],
            generating=[True, False, False] if generating is None else list(generating),
        )
        self.store = {"text": ""}
        self.input = _ChunkyInput(self, self.store)
        self.max_chunk = max_chunk
        self.fail_at = fail_at
        self.failed_once = False
        self.unreadable = unreadable
        self.enter_works = enter_works
        self.inserts = 0
        self.pieces = []
        self.sources = []
        self.presses = []  # 记录真实键盘按键（Enter / Control+A / Backspace）
        self.oversized = 0
        self.whole_fills = 0
        self.submitted_text = None
        self.focus_clicks = 0
        self.synthetic_enters = 0
        self.keyboard_broken = False
        # 真机根因模型：插入原语只作用于**当前选区**；输入框里没有落在编辑器内的光标时，
        # 两种插入都「不报错、也一个字不写」（这就是生产日志里 0 字符的签名）。
        self.require_caret = require_caret
        self.caret_in_composer = False
        self.caret_sets = 0
        self.silent_noops = 0
        # 真机故障模型：两种原语都静默失效（连光标也救不了），用于验证整段 fill 回退。
        self.ignore_inserts = ignore_inserts
        self.fill_writes_then_raises = fill_writes_then_raises
        # 真机现象：富文本编辑器把换行渲染成块级节点，读回的 textContent 不含换行。
        self.normalize_on_read = normalize_on_read
        # 真机现象：编辑器把换行渲染成块级节点、把 markdown 标记变成格式 —— 读回**变短**，
        # 真机上 9967 字符的 prompt 读回来只有 9834。
        self.lossy_readback = lossy_readback
        self.keyboard = _ChunkyKeyboard(self)

    def record_insert(self, text: str, *, source: str) -> None:
        """记录一次插入（两个原语都走这里）：模块级共享的“超长就卡死”行为。"""
        if self.ignore_inserts:
            self.silent_noops += 1
            return
        if self.require_caret and not self.caret_in_composer:
            self.silent_noops += 1
            return
        if self.failed_once is False and self.fail_at is not None and self.inserts == self.fail_at:
            self.failed_once = True
            raise RuntimeError("Execution context was destroyed (composer remounted)")
        if len(text) > self.max_chunk:
            self.oversized += 1
            raise TimeoutError("waiting for element to be visible, enabled and editable")
        self.inserts += 1
        self.pieces.append(len(text))
        self.sources.append(source)
        self.store["text"] += text

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        # 模拟重挂载：每次重定位都返回一个新句柄，但文本框内容存在 store 里
        self.input = _ChunkyInput(self, self.store)
        return self.input

    async def query_selector(self, selector):
        return None

    async def evaluate(self, script):
        if "stop" in script or "\u505c\u6b62" in script:
            return bool(self.submitted_text is not None)
        return ""


class ChunkedInsertTests(unittest.TestCase):
    """真实故障回归：超长工具结果（如 find 输出）把网页输入框卡死。

    用户现象：客户端 find 的超长结果导致网页输入框卡死，桥报
    「写入输入框连续失败 3 次（单次超时 10000ms）：命中的元素始终不处于
    「可见 / 可编辑」状态」。

    原因：一次性 `fill()` 把几万字符排成网页主线程上的一个长任务（React 重渲染 +
    富文本编辑器同步），期间连“元素是否可编辑”都探测不到。
    修法：分块插入 + 每块前重读输入框文本续写（可重挂载后继续、不重复写）。
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
            ("FILL_TIMEOUT_MS", 200),
            ("FILL_RETRIES", 2),
            ("FILL_CHUNK_CHARS", 4000),
            ("RETRY_BACKOFF_S", 0),
            ("SUBMIT_VERIFY_MS", 40),
            ("PROMPT_MAX_CHARS", 100000),
            ("MAX_UPSTREAM_RETRIES", 1),
        ):
            p = unittest.mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, page, prompt):
        driver = GeminiWebDriver()
        driver.page = page
        return asyncio.run(driver.send_chat(prompt))

    def test_long_prompt_is_split_into_chunks(self):
        page = _ChunkyPage()
        prompt = "A" * 12000
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        # 先试整段 fill（本机实测可用的原语），它没写进去才分块；每块都不超过 FILL_CHUNK_CHARS
        self.assertEqual(page.whole_fills, 1)
        self.assertEqual(page.oversized, 0)
        self.assertEqual(page.pieces, [4000, 4000, 4000])
        # 原语：真实键盘 insert_text 优先（参照姊妹项目 ChatGPTBridge）
        self.assertEqual(set(page.sources), {"keyboard"})
        # 写入前真实 click 聚焦（否则键盘事件会落到别处），提交用真实键盘 Enter
        self.assertGreaterEqual(page.focus_clicks, 1)
        self.assertEqual(page.presses, ["Enter"])
        self.assertEqual(page.synthetic_enters, 0)
        # 提交的正是完整 prompt（逐字一致）
        self.assertEqual(page.submitted_text, prompt)

    def test_resumes_after_composer_remount(self):
        # 第 3 块时句柄失效（重挂载）：必须重新定位后**续写**，不重复也不丢
        page = _ChunkyPage(fail_at=2)
        prompt = "B" * 10000
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.submitted_text, prompt)

    def test_partial_insert_from_previous_attempt_is_not_duplicated(self):
        # 上一轮已经写入前 6000 字符：本轮应从 6000 继续，而不是从头再来
        page = _ChunkyPage()
        page.store["text"] = "C" * 6000
        prompt = "C" * 9000
        self._run(page, prompt)
        self.assertEqual(page.submitted_text, prompt)
        self.assertEqual(len(page.pieces), 1)  # 只补了剩下 3000

    def test_unreadable_composer_still_trusts_fill(self):
        # 读不到输入框文本（页面差异）时无法读回校验，只能信任 fill 的返回（旧行为）
        page = _ChunkyPage(unreadable=True)
        text, _ = self._run(page, "go")
        self.assertEqual(text, "新答案")
        self.assertEqual(page.whole_fills, 1)

    def test_keyboard_primary_falls_back_to_exec_command(self):
        # 真实键盘通道不可用时，退到 execCommand，仍然能完整写入并提交
        page = _ChunkyPage()
        page.keyboard_broken = True
        prompt = "D" * 6000
        self._run(page, prompt)
        self.assertEqual(set(page.sources), {"execCommand"})
        self.assertEqual(page.submitted_text, prompt)

    def test_caret_is_placed_before_writing(self):
        # 真机根因守护：插入原语只作用于「当前选区」，写之前必须显式把光标放进输入框；
        # 否则两种插入都静默无效（不报错、0 字符）——这正是本轮生产故障的签名。
        page = _ChunkyPage()
        prompt = "F" * 5000
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertGreaterEqual(page.caret_sets, 1)
        self.assertEqual(page.silent_noops, 0)
        self.assertEqual(page.submitted_text, prompt)

    def test_whole_fill_is_tried_first(self):
        # 顺序校正（真机数据）：fill() 是本机实测可用的原语（probe_prompt_limit 用它写进过
        # 100K），必须优先；分块只在 fill 确实没写进去时才用。
        page = _ChunkyPage()
        prompt = "G" * 3000
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.whole_fills, 1)
        self.assertEqual(page.pieces, [])  # 没有走分块
        self.assertEqual(page.submitted_text, prompt)

    def test_fill_timeout_with_text_landed_is_treated_as_success(self):
        # 真机签名：fill(36265) 报超时，而输入框同时长出 child_nodes: 616——文本其实进去了。
        # 必须读回确认并按成功继续提交，而不是重写一遍或直接放弃。
        page = _ChunkyPage(fill_writes_then_raises=True)
        prompt = "H" * 9000
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.whole_fills, 1)  # 只试了一次 fill
        self.assertEqual(page.pieces, [])  # 也没有回头去分块
        self.assertEqual(page.submitted_text, prompt)

    def test_failed_fill_falls_back_to_chunked_insert(self):
        # fill 确实没写进去（长文本超时、输入框仍为空）时才走分块
        page = _ChunkyPage()
        prompt = "I" * 9000
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.whole_fills, 1)
        self.assertEqual(page.pieces, [4000, 4000, 1000])
        self.assertEqual(page.submitted_text, prompt)

    def test_editor_normalized_readback_is_not_treated_as_residue(self):
        # 真机根因守护：读回不含换行（编辑器会这样）时，`prompt.startswith(current)` 判 False，
        # 旧逻辑于是把**刚写好的整条 prompt 清掉重写**——真机里 child_nodes 从 616 掉到 1、
        # 每轮都报「已写入 0/36265 字符」。必须容忍空白规范化，绝不能清掉已有内容。
        page = _ChunkyPage(fill_writes_then_raises=True, normalize_on_read=True)
        prompt = "\n".join(f"line{i}" for i in range(600))
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.submitted_text, prompt)  # 原文完整发出（没有被清空重写）
        self.assertEqual(page.pieces, [])  # 读回确认后直接提交，没有回头去分块
        self.assertNotIn("Backspace", page.presses)  # 没有触发清空

    def test_send_log_distinguishes_seed_from_delta(self):
        # 可观测性守护：`[长度] 播种内容超出 SEED_MAX_CHARS` 是**构建期**日志，每轮都会打
        # （server.py 无条件 build 两份：增量 + 播种），不能据此判断本轮用了哪一份。
        # 真正发出去的是哪份、有多长，必须由 `[发送]` 行自己说清楚。
        driver = GeminiWebDriver()
        driver.page = _ChunkyPage()
        with self.assertLogs("gemini_web.chat_io", level="INFO") as logs:
            asyncio.run(driver.send_chat("delta-one", seeded_prompt="SEED-BODY-XY"))
            # 第二轮：同一个网页会话里上一条回复还在（基线），提交后出现**新**回复。
            # FakePage 的 script 按 query 次数推进，所以这里换一个带正确基线的假页面
            # ——若让第二轮复用同一页，它会把上一轮的回复当基线（新判定下不属新正文）。
            driver.page = _ChunkyPage(
                baseline=["新答案"],
                script=[["新答案", "第二个答案"], ["新答案", "第二个答案"]],
                generating=[False, False],
            )
            asyncio.run(driver.send_chat("delta-two", seeded_prompt="SEED-BODY-XY"))
        sent = [r.getMessage() for r in logs.records if r.getMessage().startswith("[发送]")]
        # 首次：桶里没有历史 -> 发播种（长度是播种那份的）
        self.assertEqual(sent[0], "[发送] bucket=default 用播种 prompt=12 字符")
        # 第二次：已有历史 -> 发增量
        self.assertEqual(sent[1], "[发送] bucket=default 用增量 prompt=9 字符")

    def test_lossy_readback_after_successful_fill_is_trusted(self):
        # 真机回归守护：fill() 成功、读回 9834 / prompt 9967（编辑器有损渲染的正常损耗）。
        # 绝不能据此判失败去分块——那会清空重写，把本来已经写好的 prompt 弄坏，
        # 最终报出「写入输入框连续失败」（真机 11:23 那一轮就是如此）。
        page = _ChunkyPage(max_chunk=20000, lossy_readback=True)
        prompt = "\n".join(f"**line{i}**" for i in range(600))
        text, _ = self._run(page, prompt)
        self.assertEqual(text, "新答案")
        self.assertEqual(page.whole_fills, 1)  # 只写了一次
        self.assertEqual(page.pieces, [])  # 没有回头去分块
        self.assertNotIn("Backspace", page.presses)  # 没有清空重写
        self.assertEqual(page.submitted_text, prompt)  # 原文完整发出

    def test_unwritable_composer_raises_instead_of_hanging(self):
        # 两种插入原语都静默失效、fill 也写不进去：必须抛出可行动错误，而不是空等到客户端超时
        page = _ChunkyPage(ignore_inserts=True)
        with self.assertRaises(RuntimeError):
            self._run(page, "J" * 5000)
        self.assertGreaterEqual(page.silent_noops, 1)

    def test_residue_is_cleared_before_writing(self):
        # 输入框里有上一次没发出去的草稿：必须先清干净，否则新 prompt 会被拼接
        page = _ChunkyPage()
        page.store["text"] = "OLD-RESIDUE-" * 50
        prompt = "E" * 5000
        self._run(page, prompt)
        self.assertEqual(page.submitted_text, prompt)
        self.assertNotIn("OLD-RESIDUE", page.submitted_text)
        # 清空手法：全选 + 删除（参照姊妹项目 ChatGPTBridge 的 _clear_input）
        self.assertIn("Backspace", page.presses)


if __name__ == "__main__":
    unittest.main()
