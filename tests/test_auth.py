"""鉴权 / 调试端点的分支测试（tasks.md T9.5）。

守护三条容易“默默失效”的边界：

* 设了 ``RESET_TOKEN`` 后 ``/session/reset`` 必须校验 ``X-Reset-Token``（否则 403）；
* ``GEMINI_DEBUG=false`` 时 ``/debug/dom`` 必须 404（它是信息泄露面）；
* 设了 ``BRIDGE_TOKEN`` 后两个生成端点必须 401（T8.10），留空时完全不校验。

全部用 TestClient + 假 driver，不联网、不起浏览器。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from gemini_web import config  # noqa: E402
from gemini_web.server import app  # noqa: E402


class FakeDriver:
    def __init__(self):
        self.page = object()
        self.init_error = None

    def needs_seed(self, key=None):
        return False

    def sent_prompt(self, key=None):
        return "prompt"

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None):
        return "pong", []

    def reset_session(self, key=None):
        self.reset_keys = getattr(self, "reset_keys", [])
        self.reset_keys.append(key)

    def session_stats(self, key=None):
        return {"turns": 0}

    def session_keys(self):
        return []

    def cluster_stats(self):
        return {}


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.driver = FakeDriver()
        patcher = mock.patch.object(config, "TASK_SNAPSHOT_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        driver_patcher = mock.patch("gemini_web.server.driver", self.driver)
        driver_patcher.start()
        self.addCleanup(driver_patcher.stop)


class ResetTokenTests(AuthTestCase):
    def test_no_token_configured_allows_reset(self):
        with mock.patch.object(config, "RESET_TOKEN", ""):
            res = self.client.post("/session/reset")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    def test_missing_header_is_403(self):
        with mock.patch.object(config, "RESET_TOKEN", "s3cret"):
            res = self.client.post("/session/reset")
        self.assertEqual(res.status_code, 403)

    def test_wrong_header_is_403(self):
        with mock.patch.object(config, "RESET_TOKEN", "s3cret"):
            res = self.client.post("/session/reset", headers={"X-Reset-Token": "nope"})
        self.assertEqual(res.status_code, 403)

    def test_correct_header_passes(self):
        with mock.patch.object(config, "RESET_TOKEN", "s3cret"):
            res = self.client.post(
                "/session/reset?session=pi", headers={"X-Reset-Token": "s3cret"}
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["session"], "pi")
        self.assertEqual(self.driver.reset_keys, ["pi"])


class DebugDomTests(AuthTestCase):
    def test_disabled_returns_404(self):
        with mock.patch.object(config, "DEBUG", False):
            res = self.client.get("/debug/dom")
        self.assertEqual(res.status_code, 404)

    def test_enabled_but_browser_down_returns_503(self):
        self.driver.page = None
        with mock.patch.object(config, "DEBUG", True):
            res = self.client.get("/debug/dom")
        self.assertEqual(res.status_code, 503)


class BridgeTokenTests(AuthTestCase):
    CHAT = {"model": "gemini-chat", "messages": [{"role": "user", "content": "hi"}]}
    RESPONSES = {"model": "gemini-chat", "input": "hi"}

    def test_disabled_by_default(self):
        with mock.patch.object(config, "BRIDGE_TOKEN", ""):
            self.assertEqual(self.client.post("/v1/chat/completions", json=self.CHAT).status_code, 200)
            self.assertEqual(self.client.post("/v1/responses", json=self.RESPONSES).status_code, 200)

    def test_chat_requires_bearer(self):
        with mock.patch.object(config, "BRIDGE_TOKEN", "tok"):
            self.assertEqual(self.client.post("/v1/chat/completions", json=self.CHAT).status_code, 401)
            self.assertEqual(
                self.client.post(
                    "/v1/chat/completions", json=self.CHAT,
                    headers={"Authorization": "Bearer wrong"},
                ).status_code,
                401,
            )
            ok = self.client.post(
                "/v1/chat/completions", json=self.CHAT,
                headers={"Authorization": "Bearer tok"},
            )
        self.assertEqual(ok.status_code, 200)

    def test_responses_requires_bearer(self):
        with mock.patch.object(config, "BRIDGE_TOKEN", "tok"):
            self.assertEqual(self.client.post("/v1/responses", json=self.RESPONSES).status_code, 401)
            ok = self.client.post(
                "/v1/responses", json=self.RESPONSES,
                headers={"Authorization": "Bearer tok"},
            )
        self.assertEqual(ok.status_code, 200)

    def test_healthz_stays_open(self):
        """探活与模型发现不能被鉴权挡住，否则客户端连不上会误判为服务挂了。"""
        with mock.patch.object(config, "BRIDGE_TOKEN", "tok"):
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            self.assertEqual(self.client.get("/v1/models").status_code, 200)


if __name__ == "__main__":
    unittest.main()
