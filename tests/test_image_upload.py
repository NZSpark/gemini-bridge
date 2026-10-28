"""图片直传：抽取 / data URI 解码 / 去重 / 粘贴事件派发 / 模型能力声明。

对应 doc/image_upload.md §4.1。重点是三条容易被忽略的硬规则：
  * 文本引用行**不含完整 base64**（隐私 + prompt 体积）；
  * 图片只在首次出现的那一轮被抽取（``only_delta=True``）；
  * 粘贴 = 合成 ``paste`` 事件 + ``DataTransfer`` + ``File``，失败不阻断提交。
"""

import asyncio
import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.chat_io import ChatIOMixin  # noqa: E402
from gemini_web.models import ModelCard  # noqa: E402
from gemini_web.prompting import (  # noqa: E402
    _content_to_text,
    _parse_data_uri,
    extract_images,
)


def _data_uri(payload: bytes = b"\x89PNG\r\n", mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _user_with_images(*urls):
    content = [{"type": "text", "text": "看图"}]
    for url in urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": content}


class ExtractImagesTests(unittest.TestCase):
    def test_base64_data_uri_becomes_bytes(self):
        payload = b"\x89PNG\r\n\x1a\n-fake-body"
        images = extract_images([_user_with_images(_data_uri(payload))])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, payload)
        self.assertEqual(images[0].mime, "image/png")
        self.assertEqual(images[0].name, "image.png")

    def test_remote_url_is_kept_lazy(self):
        images = extract_images([_user_with_images("https://x/y.jpg?a=1")])
        self.assertEqual(len(images), 1)
        self.assertIsNone(images[0].data)
        self.assertEqual(images[0].url, "https://x/y.jpg?a=1")
        self.assertEqual(images[0].name, "y.jpg")

    def test_image_url_may_be_plain_string_or_bare_url(self):
        content = [
            {"type": "image_url", "image_url": "https://x/a.png"},
            {"type": "image", "url": "https://x/b.png"},
        ]
        images = extract_images([{"role": "user", "content": content}])
        self.assertEqual([i.name for i in images], ["a.png", "b.png"])

    def test_empty_base64_is_skipped(self):
        images = extract_images([_user_with_images("data:image/png;base64,")])
        self.assertEqual(images, [])

    def test_only_delta_ignores_history_images(self):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "旧"},
                {"type": "image_url", "image_url": {"url": _data_uri(b"old")}},
            ]},
            {"role": "assistant", "content": "收到"},
            {"role": "user", "content": [
                {"type": "text", "text": "新"},
                {"type": "image_url", "image_url": {"url": _data_uri(b"new")}},
            ]},
        ]
        self.assertEqual(len(extract_images(messages)), 2)
        delta = extract_images(messages, only_delta=True)
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0].data, b"new")

    def test_only_delta_without_assistant_keeps_all(self):
        messages = [_user_with_images(_data_uri(b"a")), _user_with_images(_data_uri(b"b"))]
        self.assertEqual(len(extract_images(messages, only_delta=True)), 2)

    def test_accepts_chat_message_objects(self):
        from gemini_web.models import ChatMessage

        msg = ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": _data_uri(b"obj")}},
        ])
        images = extract_images([msg])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, b"obj")


class DataUriTests(unittest.TestCase):
    def test_parses_mime_and_bytes(self):
        mime, data = _parse_data_uri("data:image/jpeg;base64," + base64.b64encode(b"xy").decode())
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(data, b"xy")

    def test_missing_base64_marker_rejected(self):
        self.assertIsNone(_parse_data_uri("data:image/png,raw"))

    def test_empty_payload_rejected(self):
        self.assertIsNone(_parse_data_uri("data:image/png;base64,"))


class ReferenceLineTests(unittest.TestCase):
    def test_reference_does_not_leak_base64(self):
        payload = b"secret-bytes" * 100
        text = _content_to_text(_user_with_images(_data_uri(payload))["content"])
        self.assertIn("[图片]", text)
        self.assertNotIn(base64.b64encode(payload).decode(), text)

    def test_reference_reports_approx_size(self):
        text = _content_to_text(_user_with_images(_data_uri(b"x" * 4096))["content"])
        self.assertIn("image/png", text)
        self.assertIn("base64", text)

    def test_url_reference_is_plain(self):
        text = _content_to_text(_user_with_images("https://x/y.jpg")["content"])
        self.assertIn("[图片] https://x/y.jpg", text)


class FakePage:
    """记录 ``evaluate`` 调用，验证粘贴脚本的入参与组合方式。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        return self.ok


class _Paster(ChatIOMixin):
    def __init__(self):
        self.focused = []

    async def _focus_composer(self, page, handle):
        self.focused.append(handle)


class PasteImagesTests(unittest.TestCase):
    def _run(self, images, page=None, enabled=True):
        paster = _Paster()
        page = page or FakePage()
        with mock.patch.object(config, "IMAGE_PASTE_ENABLED", enabled):
            count = asyncio.run(paster._paste_images(page, object(), images))
        return count, page, paster

    def test_dispatches_paste_event_with_bytes(self):
        images = extract_images([_user_with_images(_data_uri(b"abc"))])
        count, page, paster = self._run(images)
        self.assertEqual(count, 1)
        self.assertEqual(len(paster.focused), 1)
        script, arg = page.calls[0]
        self.assertIn("ClipboardEvent('paste'", script)
        self.assertIn("DataTransfer", script)
        name, mime, bytes_arg = arg
        self.assertEqual(name, "image.png")
        self.assertEqual(mime, "image/png")
        self.assertEqual(bytes(bytes_arg), b"abc")

    def test_disabled_switch_pastes_nothing(self):
        images = extract_images([_user_with_images(_data_uri(b"abc"))])
        count, page, _ = self._run(images, enabled=False)
        self.assertEqual(count, 0)
        self.assertEqual(page.calls, [])

    def test_failure_is_non_fatal(self):
        images = extract_images([_user_with_images(_data_uri(b"abc"))])
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(side_effect=RuntimeError("boom"))
        count, _, _ = self._run(images, page=page)
        self.assertEqual(count, 0)

    def test_missing_bytes_and_url_skipped(self):
        from gemini_web.prompting import ImageAttachment

        count, page, _ = self._run([ImageAttachment(name="x.png", mime="image/png")])
        self.assertEqual(count, 0)
        self.assertEqual(page.calls, [])


class ModelModalityTests(unittest.TestCase):
    def test_model_card_declares_image_input(self):
        card = ModelCard(id="gemini-chat")
        self.assertIn("image", card.input_modalities)
        self.assertIn("text", card.input_modalities)
        self.assertEqual(card.output_modalities, ["text"])
        self.assertEqual(card.architecture["modality"], "text+image->text")


if __name__ == "__main__":
    unittest.main()
