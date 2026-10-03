"""播种 prompt 不应被 harness 注入的巨型系统提示 / 元提示 / 环境块灌满。

回归背景：用 Codex 只发一句「更新README.md」，通过 bridge 时 prompt 却非常长。
原因是 ``_seed_messages`` 把 harness 每轮携带的完整系统提示（Codex CLI spec，
上万字）原样重放，且把「生成任务标题」这类元提示、``<environment_context>``
环境块也一并播种。本测试锁定修复后的行为。
"""

import sys
import unittest
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

    def test_delta_prompt_only_sends_new_user_message(self):
        # 非播种（已有会话）时只发增量：不应包含任何系统提示
        prompt = prompting.build_prompt(_codex_like_messages(), seed=False)
        self.assertIn(USER_ASK, prompt)
        self.assertNotIn("Codex CLI", prompt)
        self.assertNotIn("single-line task title", prompt)


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
        self.assertTrue(tasks._is_meta_prompt("仅输出 OK"))


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
