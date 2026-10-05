"""`/v1/responses` 路由级测试（tasks.md T9.3）。

覆盖 update.md §6 指出的空白：此前只有 `responses.py` 的内部函数测试
（`tests/test_responses.py`），**没有**从 HTTP 路由进出的用例，于是
「`ENABLE_RESPONSES_API=false` 忘了返回 404」「空 input 返回 500 而不是 400」
这类问题不会被守护。这里用 FastAPI TestClient + 假 driver，不联网、不起浏览器。
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
    """最小 driver：路由只用到 page / needs_seed / send_chat / sent_prompt。"""

    def __init__(self, reply="pong", page=True):
        self.reply = reply
        self.page = object() if page else None

    def needs_seed(self, key=None):
        return False

    def sent_prompt(self, key=None):
        return "prompt"

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None):
        return self.reply, []


class ResponsesRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.driver = FakeDriver()
        # 任务快照会在 user_data/ 下落盘；路由测试不需要它，关掉以免污染工作区
        patcher = mock.patch.object(config, "TASK_SNAPSHOT_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, payload, **kwargs):
        with mock.patch("gemini_web.server.driver", self.driver):
            return self.client.post("/v1/responses", json=payload, **kwargs)

    # ---------- 开关 ----------

    def test_disabled_returns_404(self):
        with mock.patch.object(config, "ENABLE_RESPONSES_API", False):
            res = self._post({"model": "gemini-chat", "input": "hi"})
        self.assertEqual(res.status_code, 404)
        self.assertIn("ENABLE_RESPONSES_API", res.json()["detail"])

    # ---------- input 校验 ----------

    def test_empty_input_returns_400_not_500(self):
        res = self._post({"model": "gemini-chat", "input": ""})
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_none_string_input_is_coerced_not_crashed(self):
        """`input: "null"` 这类怪值是合法 JSON 字符串，应按普通文本处理而不是 500。"""
        res = self._post({"model": "gemini-chat", "input": "null"})
        self.assertEqual(res.status_code, 200)

    def test_browser_not_ready_returns_503(self):
        self.driver = FakeDriver(page=False)
        res = self._post({"model": "gemini-chat", "input": "hi"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"]["type"], "upstream_error")

    # ---------- 非流式结构 ----------

    def test_non_stream_structure(self):
        res = self._post({"model": "gemini-chat", "input": "hi", "stream": False})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["model"], "gemini-chat")
        self.assertTrue(body["id"].startswith("resp_"))
        message = body["output"][0]
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"][0]["type"], "output_text")
        self.assertEqual(message["content"][0]["text"], "pong")
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertIn(key, body["usage"])

    def test_input_items_list_are_accepted(self):
        """Codex 会发 `input` 数组（含 message item），必须能正常映射。"""
        payload = {
            "model": "gemini-chat",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
        res = self._post(payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["output"][0]["content"][0]["text"], "pong")


class ResponsesStreamRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.driver = FakeDriver()
        patcher = mock.patch.object(config, "TASK_SNAPSHOT_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stream_keeps_named_event_sequence(self):
        payload = {"model": "gemini-chat", "input": "hi", "stream": True}
        with mock.patch("gemini_web.server.driver", self.driver):
            res = self.client.post("/v1/responses", json=payload)
        self.assertEqual(res.status_code, 200)
        self.assertIn("event: response.created", res.text)
        self.assertIn("event: response.output_text.done", res.text)
        self.assertIn("event: response.completed", res.text)
        self.assertNotIn("[DONE]", res.text)  # Responses 用命名事件，不用 chat 的 [DONE]


if __name__ == "__main__":
    unittest.main()
