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

    def test_session_over_budget_triggers_rotation(self):
        """当轮数或估算 token 达上限时，标记 pending_rotation 为 True。"""
        with mock.patch.object(config, "SESSION_MAX_TURNS", 3):
            with mock.patch.object(config, "SESSION_MAX_TOKENS", 0):
                state = self.driver._state("b1")
                state.turns = 2
                self.assertFalse(self.driver._session_over_budget("b1"))
                state.turns = 3
                self.assertTrue(self.driver._session_over_budget("b1"))

    def test_corrupt_state_file_is_ignored_not_fatal(self):
        self.state_file.write_text("{\"turns\": 3", encoding="utf-8")  # 截断的 JSON
        state = self.driver._state("b1")
        self.assertEqual(state.turns, 0)  # 回退为无状态，不得抛错


class _FakeContextPage:
    """最小假页面：只实现 ``_ensure_page`` 用到的接口。"""

    def __init__(self, closed=False, fail_goto=False):
        self._closed = closed
        self.fail_goto = fail_goto
        self.goto_calls = []

    def is_closed(self):
        return self._closed

    async def goto(self, url, wait_until=None):
        if self.fail_goto:
            raise RuntimeError("Page.goto: net::ERR_FAILED")
        self.goto_calls.append(url)

    async def wait_for_selector(self, selector, timeout=None, **kwargs):
        return None  # 找不到新建对话按钮 / 输入框时只警告，不影响重建流程

    async def close(self):
        self._closed = True


class _FakeContext:
    def __init__(self, pages=None):
        self.created = 0
        self._queue = list(pages or [])

    async def new_page(self):
        self.created += 1
        return self._queue.pop(0) if self._queue else _FakeContextPage()


class ClosedPageRecoveryTests(unittest.TestCase):
    """真机回归：会话桶的标签被手工关闭 / 崩溃后，该桶不能永久 502。

    现象：OpenAI SDK 请求（自动分桶 ``ua:openai``）一直报 502「无法找到对话输入框」
    而其它桶正常——因为已关闭的页面句柄永远留在 ``_pages`` 里，
    ``wait_for_selector`` 在死页面上抛错又被当成“选择器没命中”。
    """

    def setUp(self):
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path("/tmp/gemini-test-closed-page-state.json")
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: Path("/tmp/gemini-test-closed-page-state.json").unlink(missing_ok=True))
        self.driver = GeminiWebDriver(user_data_dir="/tmp/gemini-test-noprofile")
        self.driver.context = _FakeContext()

    def test_closed_bucket_page_is_recreated_and_seeded(self):
        dead = _FakeContextPage(closed=True)
        self.driver._pages["ua:openai"] = dead
        self.driver._page_last_used["ua:openai"] = 0.0

        asyncio.run(self.driver._ensure_page("ua:openai"))

        fresh = self.driver._pages["ua:openai"]
        self.assertIsNot(fresh, dead)
        self.assertFalse(fresh.is_closed())
        self.assertEqual(fresh.goto_calls, [config.WEBSITE])
        # 页面丢了 = 网页端上下文没了：必须让下一轮重新播种
        self.assertFalse(self.driver._state("ua:openai").has_history)

    def test_live_bucket_page_is_reused(self):
        live = _FakeContextPage()
        self.driver._pages["ua:x"] = live

        asyncio.run(self.driver._ensure_page("ua:x"))

        self.assertIs(self.driver._pages["ua:x"], live)
        self.assertEqual(self.driver.context.created, 0)

    def test_failed_navigation_does_not_keep_dead_handle(self):
        self.driver.context = _FakeContext(pages=[_FakeContextPage(fail_goto=True)])

        with self.assertRaises(RuntimeError):
            asyncio.run(self.driver._ensure_page("ua:boom"))

        self.assertNotIn("ua:boom", self.driver._pages)

    def test_default_bucket_page_is_not_touched(self):
        sentinel = object()
        self.driver.page = sentinel

        asyncio.run(self.driver._ensure_page(DEFAULT_SESSION_KEY))

        self.assertIs(self.driver.page, sentinel)


if __name__ == "__main__":
    unittest.main()
