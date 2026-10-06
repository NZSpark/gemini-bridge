"""播种 prompt 不应被 harness 注入的巨型系统提示 / 元提示 / 环境块灌满。

回归背景：用 Codex 只发一句「更新README.md」，通过 bridge 时 prompt 却非常长。
原因是 ``_seed_messages`` 把 harness 每轮携带的完整系统提示（Codex CLI spec，
上万字）原样重放，且把「生成任务标题」这类元提示、``<environment_context>``
环境块也一并播种。本测试锁定修复后的行为。
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config, prompting  # noqa: E402
from gemini_web.models import ChatMessage  # noqa: E402


BIG_SYSTEM = "You are a coding agent running in the Codex CLI. " * 2000
TITLE_META = (
    "Generate a concise, single-line task title of at most 36 characters. "
    "Do not answer the request. User prompt: 更新README.md文件"
)
ENV_BLOCK = "<environment_context>cwd=/tmp/proj</environment_context>"
USER_ASK = "更新README.md文件"


def _codex_like_messages():
    return [
        ChatMessage(role="system", content=BIG_SYSTEM),
        ChatMessage(role="system", content=TITLE_META),
        ChatMessage(role="user", content=ENV_BLOCK),
        ChatMessage(role="user", content=USER_ASK),
    ]


class SeedPromptInflationTests(unittest.TestCase):
    def test_seed_prompt_is_bounded(self):
        prompt = prompting.build_prompt(
            _codex_like_messages(), seed=True, seed_max_chars=12000
        )
        # 修复前这里会是 ~90k+ 字符（完整系统提示全量重放）
        self.assertLess(len(prompt), 12000)

    def test_seed_prompt_keeps_user_request(self):
        prompt = prompting.build_prompt(
            _codex_like_messages(), seed=True, seed_max_chars=12000
        )
        self.assertIn(USER_ASK, prompt)

    def test_seed_prompt_drops_title_meta_prompt(self):
        prompt = prompting.build_prompt(
            _codex_like_messages(), seed=True, seed_max_chars=12000
        )
        self.assertNotIn("single-line task title", prompt)

    def test_seed_prompt_drops_environment_block(self):
        prompt = prompting.build_prompt(
            _codex_like_messages(), seed=True, seed_max_chars=12000
        )
        self.assertNotIn("environment_context", prompt)

    def test_system_block_is_truncated(self):
        prompt = prompting.build_prompt(
            _codex_like_messages(), seed=True, seed_max_chars=12000
        )
        limit = config.SEED_SYSTEM_MAX_CHARS
        # 巨型 system 被截断，且带有截断标记
        self.assertIn("系统提示已截断", prompt)
        self.assertLess(len(prompt), len(BIG_SYSTEM))
        self.assertLessEqual(limit, 12000)

    def test_giant_system_message_is_clamped_even_when_per_message_limit_is_off(self):
        """SEED_SYSTEM_MAX_CHARS=0（“不限制”）也不能让第一条巨型 system 原样进 prompt。

        参照姊妹项目 ChatGPTBridge 的 `_seed_messages`：单条上限与剩余总预算取小，
        否则一条 10 万字的系统提示会把播种变成“巨型 fill”，直接撞输入框上限。
        """
        with unittest.mock.patch.object(config, "SEED_SYSTEM_MAX_CHARS", 0):
            prompt = prompting.build_prompt(
                _codex_like_messages(), seed=True, seed_max_chars=12000
            )
        # 系统部分最多占一半预算（6000），再留出用户请求的位置
        self.assertIn("系统提示已截断", prompt)
        self.assertLess(len(prompt), 12000)
        self.assertIn(USER_ASK, prompt)

    def test_delta_prompt_only_sends_new_user_message(self):
        # 非播种（已有会话）时只发增量：不应包含任何系统提示
        prompt = prompting.build_prompt(_codex_like_messages(), seed=False)
        self.assertIn(USER_ASK, prompt)
        self.assertNotIn("Codex CLI", prompt)
        self.assertNotIn("single-line task title", prompt)


class SeedBudgetAccountingTests(unittest.TestCase):
    """`SEED_MAX_CHARS` 的计量口径：预算只算原始文本，成品会超出（且现在有日志）。

    排查结论（对应「prompt 长度控制没生效」）：播种预算在*拼装前*对原始文本计算，
    而真正填进输入框的是**渲染后**的成品——单条最新消息就算超出预算也会被无条件保留、
    工具结果另受 `TOOL_RESULT_MAX_CHARS` 截断、任务块与工具说明都在预算之后追加。
    实测：`SEED_MAX_CHARS=6000` 时成品可达 27726 字符（含任务块）/ 29112（含工具声明）。
    """

    def _messages(self):
        return [
            ChatMessage(role="system", content="S" * 30000),
            ChatMessage(role="user", content="请读这些文件并修 bug"),
            ChatMessage(role="assistant", content="tool ran"),
            ChatMessage(role="tool", content="file body line\n" * 2500, tool_call_id="c1"),
        ]

    def test_seed_truncation_is_logged_with_the_budget(self):
        with self.assertLogs("gemini_web.prompting", level="INFO") as ctx:
            prompting.build_prompt(self._messages(), seed=True, seed_max_chars=6000)
        joined = "\n".join(ctx.output)
        self.assertIn("SEED_MAX_CHARS=6000", joined)
        self.assertIn("丢弃历史", joined)

    def test_seed_budget_is_measured_before_rendering(self):
        prompt = prompting.build_prompt(self._messages(), seed=True, seed_max_chars=6000)
        # 口径差异：成品 > 预算（预算算的是原始文本，渲染标签/任务块/工具说明都在之后叠加）。
        # 若将来把预算改成按成品计量，这里应改为 assertLessEqual(len(prompt), 6000)。
        self.assertGreater(len(prompt), 6000)
        # 但也不会无限膨胀：单条上限依然兜住了巨型内容
        self.assertLess(len(prompt), 60000)


class MetaPromptDetectionTests(unittest.TestCase):
    def test_codex_title_prompt_detected_as_meta(self):
        from gemini_web import tasks

        self.assertTrue(tasks._is_meta_prompt(TITLE_META))

    def test_real_user_message_is_not_meta(self):
        from gemini_web import tasks

        self.assertFalse(tasks._is_meta_prompt("更新README.md文件"))
        self.assertFalse(tasks._is_meta_prompt("Who are you?"))

    def test_output_constraint_prompts_are_meta(self):
        # 联网自测常用“只回复N个字”这类输出约束；它不是用户的真实任务，
        # 一旦被存成 goal，播种时会以「任务目标：」口吻注入并与真实请求冲突。
        from gemini_web import tasks

        self.assertTrue(tasks._is_meta_prompt("只回复两个字：你好"))
        self.assertTrue(tasks._is_meta_prompt("只回复四个字：联网测试通过"))
        self.assertTrue(tasks._is_meta_prompt("只输出三个字"))

    def test_normal_request_with_output_verb_is_not_meta(self):
        # “请只输出下面这一行……”是正常用户请求，不能被误判成元提示而整条丢弃。
        from gemini_web import tasks

        self.assertFalse(
            tasks._is_meta_prompt("请只输出下面这一行，不要任何解释、不要加代码围栏")
        )
        self.assertFalse(tasks._is_meta_prompt("更新README.md文件"))


class ResumeBlockSanitizesPollutedGoalTests(unittest.TestCase):
    """被污染成“只回复N个字”的 goal，不应再以「任务目标：」口吻注入。"""

    def test_polluted_goal_is_dropped(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from gemini_web import tasks

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "TASK_FILE_DIR", tmp), mock.patch.object(
                config, "TASK_SNAPSHOT_ENABLED", True
            ):
                path = tasks._file("polluted")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "namespace": tasks._namespace(),
                            "bucket": "polluted",
                            "goal": "只回复两个字：你好",
                            "recent": [],
                            "turns": 1,
                            "updated_at": 0,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                block = tasks.resume_block("polluted")
        # 污染的 goal 被丢弃后，没有 goal 也没有 recent → 返回空串
        self.assertEqual(block, "")


if __name__ == "__main__":
    unittest.main()
