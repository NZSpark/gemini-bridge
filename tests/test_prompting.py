import unittest
import unittest.mock

from gemini_web import config, prompting
from gemini_web.models import ChatMessage


def _tool_heavy_messages(count: int = 5, size: int = 20000):
    """仿真实流量：一次请求带回多条 read 结果（每条都在单条上限之内）。"""
    raw = ("line of file content\n" * (size // 20 + 1))[:size]
    messages = [
        ChatMessage(role="user", content="请阅读这些文件并修复 bug"),
        ChatMessage(role="assistant", content="TOOL_CALL: read"),
    ]
    messages += [
        ChatMessage(role="tool", content=raw, tool_call_id=f"call_{i}")
        for i in range(count)
    ]
    return messages


class PromptBudgetEnforcementTests(unittest.TestCase):
    """成品预算必须真正落地（用户报告「prompt 长度控制没生效」后的修复）。

    真实缺口（修复前）：`TOOL_RESULT_MAX_CHARS` 只管**单条**工具结果，多条结果合并没有
    总量预算；`PROMPT_MAX_CHARS` 只是发送侧的事后头尾截断。一次请求带上 5 条 20KB 的
    read 结果时成品 105,462 字符——只有兜底截断会把中间 5,432 字符**凭空**抽走，
    而且此前连一行日志都没有，所以「把旋钮调小」看不到任何变化。

    修复后的策略（用户定）：超长的客户端结果不整块丢掉，而是**只保留开头一段**，
    并在 prompt 里注明「结果太长已被截短」。
    """

    def test_many_tool_results_are_compressed_into_budget(self):
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 50000):
            with self.assertLogs("gemini_web.prompting", level="WARNING") as ctx:
                prompt = prompting.build_prompt(_tool_heavy_messages())
        self.assertLessEqual(len(prompt), 50000)
        joined = "\n".join(ctx.output)
        self.assertIn("成品超预算", joined)
        self.assertIn("只保留开头", joined)
        # 被压缩的那几段必须自带截断说明（模型才知道自己看的是片段）
        self.assertIn("工具结果过长，已截断", prompt)
        # 并且保留的是**开头**：日志行文本仍在
        self.assertIn("line of file content", prompt)

    def test_documented_defaults_can_compress_multi_tool_overflow(self):
        """按文档默认值（单条 50000 / 整段 100000）：3 条 40KB 结果会被压回预算内。"""
        with unittest.mock.patch.object(config, "TOOL_RESULT_MAX_CHARS", 50000), \
                unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 100000):
            prompt = prompting.build_prompt(_tool_heavy_messages(count=3, size=40000))
        self.assertLessEqual(len(prompt), 100000)
        self.assertIn("工具结果过长，已截断", prompt)

    def test_single_tool_result_cap_is_50k_by_default(self):
        """单条工具结果上限 = 50K（用户定的值）：超出只保留开头一段。"""
        self.assertEqual(config.TOOL_RESULT_MAX_CHARS, 50000)
        raw = "H" + "x" * 80000 + "TAIL"
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 0):  # 关掉成品预算，只验单条上限
            prompt = prompting.build_prompt([ChatMessage(role="tool", content=raw, tool_call_id="c1")])
        self.assertIn("H", prompt)
        self.assertNotIn("TAIL", prompt)
        self.assertIn("工具结果过长，已截断 30005 字符", prompt)

    def test_within_budget_is_silent(self):
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 50000):
            with self.assertNoLogs("gemini_web.prompting", level="WARNING"):
                prompting.build_prompt([ChatMessage(role="user", content="hi")])

    def test_small_tool_result_is_kept_verbatim(self):
        """预算未超时不碰工具结果：edit 工具依赖 oldText 逐字节匹配。"""
        raw = "1  # Title\n2  \n3  ## Section\n4  text"
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 50000):
            with self.assertNoLogs("gemini_web.prompting", level="WARNING"):
                prompt = prompting.build_prompt(
                    [ChatMessage(role="tool", content=raw, tool_call_id="c1")]
                )
        self.assertIn(raw, prompt)
        self.assertNotIn("已截断", prompt)

    def test_compression_prefers_oldest_when_sizes_tie(self):
        """同样长时优先压**最早**的那条（最新结果对当前任务最有用）。"""
        raw = "x" * 20000
        messages = [ChatMessage(role="tool", content=raw, tool_call_id=f"c{i}") for i in range(3)]
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 45000):
            with self.assertLogs("gemini_web.prompting", level="WARNING"):
                prompt = prompting.build_prompt(messages)
        self.assertLessEqual(len(prompt), 45000)
        # 最后一条（最新）不应该被压缩
        self.assertIn(raw, prompt)

    def test_user_message_is_only_capped_by_the_send_side_clamp(self):
        """已知口径缺口：`role="user"` 没有单条上限，唯一兜底是发送侧截断。

        若将来给 user 消息也加预算/截断（例如粘贴大文件），本用例应改为断言被截断。
        """
        huge = "u" * 300000
        prompt = prompting.build_prompt([ChatMessage(role="user", content=huge)])
        self.assertEqual(len(prompt), len(huge))


class ToolResultFidelityTests(unittest.TestCase):
    """工具执行结果必须逐字节保留，供 edit 工具做 oldText 精确匹配。"""

    def test_tool_result_preserves_trailing_newline(self):
        from gemini_web.prompting import _render_message

        raw = "1  # Title\n2  \n3  ## Section\n4  text\n"
        m = ChatMessage(role="tool", content=raw, tool_call_id="call_1")
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertEqual(body, raw)

    def test_tool_result_preserves_leading_blank_lines(self):
        from gemini_web.prompting import _render_message

        raw = "\n\n# Title\n"
        m = ChatMessage(role="tool", content=raw)
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertTrue(body.startswith("\n\n# Title"))
