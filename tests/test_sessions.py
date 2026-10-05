"""会话状态：内存缓存有界化（update_codex §2.4）与落盘正确性（tasks.md T6.2）。"""

import asyncio
import json
import sys
import tempfile
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


class StateFileTests(unittest.TestCase):
    """T6.2：状态落盘的并发不变量 + 原子写。

    实测结论（见 doc/update.md）：单事件循环下 `_save_session_state` 内**没有 await**，
    所以读-改-写天然互斥，不存在“跨桶竞态”。下面第一个用例把这个不变量锁住：
    一旦有人在临界区里引入 await，它会立刻变红。
    真正需要修的是**非原子写**——直接 write_text 被中断会留下截断的 JSON，
    而 `_read_state_file` 对解析失败一律返回 {}，表现为状态凭空清零。
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.state_file = self.tmpdir / "state.json"
        self._patch = mock.patch.object(config, "SESSION_FILE", self.state_file)
        self._patch.start()
        self.driver = GeminiWebDriver(user_data_dir="/tmp/gemini-test-noprofile")

    def tearDown(self):
        self._patch.stop()

    def _remember(self, bucket):
        """`_remember_session` 是 async 的；这里同步驱动它。"""
        asyncio.run(self.driver._remember_session(bucket))

    def test_concurrent_buckets_do_not_lose_updates(self):
        """8 个桶并发各写 20 轮：每个桶的 turns 都必须完整保留。"""
        buckets = 8
        rounds = 20

        async def worker(bucket: str):
            for _ in range(rounds):
                state = self.driver._state(bucket)
                state.turns += 1
                await asyncio.sleep(0)  # 强制让出事件循环，制造最大交错
                await self.driver._remember_session(bucket)
                await asyncio.sleep(0)

        async def run_all():
            await asyncio.gather(*(worker(f"b{i}") for i in range(buckets)))

        asyncio.run(run_all())

        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        sessions = data.get("sessions") or {}
        self.assertEqual(len(sessions), buckets)
        for name, payload in sessions.items():
            self.assertEqual(
                payload["turns"], rounds,
                f"{name} 的 turns 被覆盖（临界区里可能有人加了 await）",
            )

    def test_write_uses_atomic_replace_and_leaves_no_tmp(self):
        self.driver._state("b1").turns = 7
        self._remember("b1")
        self.assertTrue(self.state_file.exists())
        leftovers = [p.name for p in self.tmpdir.iterdir() if p.name != self.state_file.name]
        self.assertEqual(leftovers, [], "原子写不应留下临时文件")
        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["sessions"]["b1"]["turns"], 7)

    def test_default_bucket_and_named_buckets_coexist(self):
        self.driver._state(DEFAULT_SESSION_KEY).turns = 3
        self._remember(DEFAULT_SESSION_KEY)
        self.driver._state("b1").turns = 5
        self._remember("b1")

        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["turns"], 3)  # 默认桶写在顶层
        self.assertEqual(payload["sessions"]["b1"]["turns"], 5)

    def test_corrupt_state_file_is_ignored_not_fatal(self):
        self.state_file.write_text("{\"turns\": 3", encoding="utf-8")  # 截断的 JSON
        state = self.driver._state("b1")
        self.assertEqual(state.turns, 0)  # 回退为无状态，不得抛错


if __name__ == "__main__":
    unittest.main()
