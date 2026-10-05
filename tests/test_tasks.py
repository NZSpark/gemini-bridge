"""`tasks.py` 表驱动单测（tasks.md T9.4）。

守护两件「静默丢任务」的事故：

1. **环境包装块**（``<environment_context>`` / ``<skills_instructions>`` /
   ``<permissions_instructions>`` / ``<collaboration_mode>`` …）不能被当成任务目标，
   否则轮转播种后「任务目标 = 另一个项目的 cwd」；
2. **元提示**（Codex 的标题生成、上下文压缩交接 JSON、字数约束自测）不能污染 goal，
   否则新会话会以「任务目标：」的口吻被要求输出摘要 JSON，真实任务因此中断。

用例以表驱动写，新增 harness 注入块只需往表里加一行。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config, tasks  # noqa: E402
from gemini_web.models import ChatMessage  # noqa: E402

# (用例名, 文本, 期望是否是「环境包装块」)
ENV_WRAPPER_CASES = [
    (
        "codex_environment_context",
        "<environment_context>\n  cwd: /Users/x/other-project\n  approval: never\n</environment_context>",
        True,
    ),
    (
        "unclosed_multiline_block",
        "<environment_context>\n cwd: /Users/x\n",
        True,
    ),
    (
        "system_instructions",
        "<system_instructions>\nYou are a coding agent.\n</system_instructions>",
        True,
    ),
    (
        "permissions_instructions_with_space",
        "<permissions instructions>\n- allow read\n</permissions instructions>",
        True,
    ),
    (
        "collaboration_mode",
        "<collaboration_mode>plan</collaboration_mode>",
        True,
    ),
    (
        "env_block",
        "<env>\nPATH=/usr/bin\n</env>",
        True,
    ),
    (
        "user_instructions",
        "<user_instructions>prefer terse answers</user_instructions>",
        True,
    ),
    ("plain_request", "把这个仓库重构为插件式架构", False),
    ("request_mentions_tag", "解释 <environment_context> 这个标签的用途", False),
    (
        # 标签后面直接跟正文 = 用户真实请求被包了一层，不能整条丢掉
        "tag_with_inline_prose",
        "<environment_context>请修复登录失败的问题",
        False,
    ),
    ("unknown_tag_block", "<unknown_tag>\n x\n</unknown_tag>", False),
    ("empty_text", "", False),
]

# (用例名, 文本, 期望是否是「元提示」)
META_PROMPT_CASES = [
    ("codex_title_prompt", "Generate a concise, single-line task title for this conversation.", True),
    ("codex_title_prompt_zh", "只生成这个任务的一句话标题", True),
    ("catch_up_summary", "Write a brief catch-up summary of the conversation so far.", True),
    ("handoff_json", "Return JSON with summary and next_action fields.", True),
    ("do_not_answer", "Do not answer the request, only output the title.", True),
    ("word_count_constraint", "只回复两个字：收到", True),
    ("word_count_constraint_4", "只输出三个字", True),
    ("real_request", "更新README.md文件", False),
    ("smalltalk", "Who are you?", False),
    ("output_verb_but_normal", "请只输出下面这一行，不要任何解释、不要加代码围栏", False),
    ("empty_text", "", False),
]


class EnvironmentWrapperTests(unittest.TestCase):
    def test_table(self):
        for name, text, expected in ENV_WRAPPER_CASES:
            with self.subTest(case=name):
                self.assertEqual(tasks._is_environment_wrapper(text), expected, name)


class MetaPromptTests(unittest.TestCase):
    def test_table(self):
        for name, text, expected in META_PROMPT_CASES:
            with self.subTest(case=name):
                self.assertEqual(tasks._is_meta_prompt(text), expected, name)


class GoalFromMessagesTests(unittest.TestCase):
    def test_skips_wrappers_and_meta_prompts(self):
        messages = [
            ChatMessage(role="system", content="you are helpful"),
            ChatMessage(role="user", content="<environment_context>\n cwd: /x\n</environment_context>"),
            ChatMessage(role="user", content="Generate a concise, single-line task title."),
            ChatMessage(role="user", content="把 doc/tasks.md 里未完成的任务做完"),
        ]
        self.assertEqual(tasks._goal_from_messages(messages), "把 doc/tasks.md 里未完成的任务做完")

    def test_falls_back_to_environment_block_when_nothing_else(self):
        """整段对话只有环境块时，宁可把它当兜底目标，也不要让 goal 为空。"""
        block = "<environment_context>\n cwd: /x\n</environment_context>"
        self.assertEqual(tasks._goal_from_messages([ChatMessage(role="user", content=block)]), block)

    def test_goal_is_truncated(self):
        with mock.patch.object(config, "TASK_GOAL_MAX_CHARS", 10):
            goal = tasks._goal_from_messages([ChatMessage(role="user", content="x" * 100)])
        self.assertEqual(len(goal), 10)


class SnapshotRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, "TASK_FILE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        ns = mock.patch.object(config, "TASK_NAMESPACE", "gemini")
        ns.start()
        self.addCleanup(ns.stop)

    def test_record_then_resume_block_carries_goal_and_recent(self):
        messages = [
            ChatMessage(role="user", content="把 output/ 清理策略补上"),
            ChatMessage(role="assistant", content="好的，正在做"),
        ]
        tasks.record("default", messages)
        block = tasks.resume_block("default")
        self.assertIn("任务目标：把 output/ 清理策略补上", block)
        self.assertIn("[assistant] 好的，正在做", block)

    def test_goal_is_written_once_and_survives_later_turns(self):
        tasks.record("default", [ChatMessage(role="user", content="目标A")])
        tasks.record("default", [ChatMessage(role="user", content="后来的追问")])
        self.assertIn("任务目标：目标A", tasks.resume_block("default"))

    def test_other_namespace_snapshot_is_ignored(self):
        """同机多个「桥」项目共用目录时不能串台。"""
        tasks.record("default", [ChatMessage(role="user", content="目标A")])
        with mock.patch.object(config, "TASK_NAMESPACE", "deepseek"):
            self.assertEqual(tasks.resume_block("default"), "")

    def test_recent_item_is_truncated(self):
        with mock.patch.object(config, "TASK_RECENT_ITEM_MAX_CHARS", 5):
            tasks.record("default", [
                ChatMessage(role="user", content="目标A"),
                ChatMessage(role="user", content="Z" * 50),
            ])
            block = tasks.resume_block("default")
        self.assertIn("ZZZZZ…（已截断）", block)
        self.assertNotIn("Z" * 6, block)

    def test_disabled_snapshot_writes_nothing(self):
        with mock.patch.object(config, "TASK_SNAPSHOT_ENABLED", False):
            tasks.record("default", [ChatMessage(role="user", content="目标A")])
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
