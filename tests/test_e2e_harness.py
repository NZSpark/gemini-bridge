"""E2E 测试脚手架（tests/e2e/）的定向回归测试：不联网、不起浏览器。

覆盖本轮修复的三处真实缺陷：

1. ``test_parity._resolve_port`` —— ``PORT=0`` 这类非法端口必须回退默认端口。
   真实环境变量优先于 ``.env``，一个随手设置的 ``PORT=0`` 会让 BASE_URL 变成
   ``http://127.0.0.1:0``：探活失败 + 误起一个绑定随机端口的第二实例。
2. ``bridge.BridgeServer.ensure_started`` —— 端口非法时快速失败；自己拉起的实例
   若浏览器起不来（503 + init_error）必须立刻报错并清理进程，不白等 90s、不留孤儿。
3. ``direct.DirectGeminiClient.start`` —— 必须显式导航到 Gemini 入口；导航/就绪
   失败时必须关闭浏览器，避免留下一个停在 about:blank 的空白窗口。
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.e2e import direct as direct_mod  # noqa: E402
from tests.e2e.bridge import BridgeServer  # noqa: E402
from tests.e2e.direct import DirectGeminiClient  # noqa: E402
from tests.e2e.test_parity import _DEFAULT_PORT, _resolve_port  # noqa: E402


class ResolvePortTests(unittest.TestCase):
    def test_uses_valid_port(self):
        self.assertEqual(_resolve_port("8123"), 8123)
        self.assertEqual(_resolve_port(8123), 8123)
        self.assertEqual(_resolve_port(" 8123 "), 8123)

    def test_invalid_ports_fall_back(self):
        for bad in ("0", 0, -1, "65536", "", None, "abc", 3.5):
            with self.subTest(bad=bad):
                self.assertEqual(_resolve_port(bad), _DEFAULT_PORT)


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class EnsureStartedTests(unittest.TestCase):
    def test_reuses_external_service_without_spawning(self):
        server = BridgeServer("http://127.0.0.1:8001")
        with mock.patch.object(BridgeServer, "healthz", return_value=(200, {})), \
                mock.patch("tests.e2e.bridge.subprocess.Popen") as popen:
            self.assertFalse(server.ensure_started())
        self.assertFalse(popen.called, "外部服务已在跑时不得再拉一个实例")

    def test_invalid_port_fails_fast_without_spawning(self):
        server = BridgeServer("http://127.0.0.1:0")
        with mock.patch.object(BridgeServer, "healthz", return_value=None), \
                mock.patch("tests.e2e.bridge.subprocess.Popen") as popen:
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                server.ensure_started()
            elapsed = time.monotonic() - started
        self.assertIn("端口非法", str(ctx.exception))
        self.assertFalse(popen.called)
        self.assertLess(elapsed, 1.0, "非法端口必须快速失败，而不是等满启动超时")

    def test_spawned_but_browser_broken_fails_fast_and_cleans_up(self):
        """自己拉起的实例返回 503+init_error：立刻报错并杀掉进程（不留孤儿）。"""
        server = BridgeServer("http://127.0.0.1:8011")
        proc = _FakeProc()
        probes = [None, (503, {"init_error": "profile is already in use"})]

        def fake_healthz(timeout: float = 2.0):
            return probes.pop(0) if probes else (503, {"init_error": "profile is already in use"})

        started = time.monotonic()
        with mock.patch.object(BridgeServer, "healthz", side_effect=fake_healthz), \
                mock.patch("tests.e2e.bridge.subprocess.Popen", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                server.ensure_started()
        elapsed = time.monotonic() - started

        self.assertIn("init_error", str(ctx.exception))
        self.assertTrue(proc.terminated, "失败时必须清理自己拉起的 uvicorn 进程")
        self.assertIsNone(server.proc)
        self.assertLess(elapsed, 5.0, "浏览器未就绪必须快速失败，而不是等满 90s 超时")


# ---------------- DirectGeminiClient.start ----------------

class _FakePage:
    def __init__(self, landing_url=None, ready=True):
        self.url = "about:blank"
        self._landing_url = landing_url
        self._ready = ready
        self.goto_calls = []
        self.closed = False

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        self.url = self._landing_url if self._landing_url is not None else url

    def title(self):
        return "Google Gemini"

    def wait_for_selector(self, *args, **kwargs):
        if not self._ready:
            raise TimeoutError("selector not found")
        return object()

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self, context):
        self.chromium = mock.MagicMock()
        self.chromium.launch_persistent_context.return_value = context
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeSyncPlaywright:
    def __init__(self, playwright):
        self._playwright = playwright

    def start(self):
        return self._playwright


class DirectStartTests(unittest.TestCase):
    def _run(self, page, context=None):
        context = context or _FakeContext(page)
        playwright = _FakePlaywright(context)
        with mock.patch.object(
            direct_mod, "sync_playwright",
            return_value=_FakeSyncPlaywright(playwright),
        ):
            client = DirectGeminiClient("user_data_fake", headless=True)
            try:
                client.start()
                error = None
            except Exception as exc:  # noqa: BLE001  # 测试需要观察异常
                error = exc
        return client, context, playwright, error

    def test_navigates_to_gemini_entry(self):
        page = _FakePage()
        _client, _context, _pw, error = self._run(page)
        self.assertIsNone(error)
        self.assertIn("https://gemini.google.com/app", page.goto_calls)

    def test_blank_or_redirected_landing_is_an_error_and_closes_browser(self):
        page = _FakePage(landing_url="about:blank")
        client, context, playwright, error = self._run(page)
        self.assertIsNotNone(error, "没落在 Gemini 上必须报错，而不是留一个空白窗口")
        self.assertIn("未落在 Gemini", str(error))
        self.assertTrue(context.closed, "失败时必须关闭浏览器上下文")
        self.assertTrue(playwright.stopped, "失败时必须停止 playwright")
        self.assertIsNone(client.context)

    def test_login_page_landing_is_an_error(self):
        page = _FakePage(landing_url="https://accounts.google.com/signin")
        _client, context, _pw, error = self._run(page)
        self.assertIsNotNone(error)
        self.assertTrue(context.closed)

    def test_ready_selector_failure_closes_browser(self):
        page = _FakePage(ready=False)
        client, context, playwright, error = self._run(page)
        self.assertIsNotNone(error)
        self.assertTrue(context.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(client.context)


if __name__ == "__main__":
    unittest.main()
