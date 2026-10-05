"""OpenAI 兼容数据模型的回归测试：宽松校验与结构默认值。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web.models import (  # noqa: E402
    SUPPORTED_MODELS,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    FunctionCall,
    ModelCard,
    ModelListResponse,
    ToolCall,
    Usage,
)


class ChatMessageTests(unittest.TestCase):
    def test_minimal(self):
        msg = ChatMessage(role="user", content="hi")
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "hi")
        self.assertIsNone(msg.tool_calls)

    def test_tool_call_id_and_name(self):
        msg = ChatMessage(role="tool", content="x", tool_call_id="abc", name="fn")
        self.assertEqual(msg.tool_call_id, "abc")
        self.assertEqual(msg.name, "fn")

    def test_content_parts_array(self):
        msg = ChatMessage(role="user", content=[{"type": "text", "text": "hi"}])
        self.assertIsInstance(msg.content, list)

    def test_none_content(self):
        self.assertIsNone(ChatMessage(role="assistant", content=None).content)


class RequestTests(unittest.TestCase):
    def test_minimal_defaults(self):
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(req.model, "gemini-chat")
        self.assertFalse(req.stream)
        # None = 客户端未指定，实际是否落盘由 config.SAVE_FILES 决定
        self.assertIsNone(req.save_files)
        self.assertIsNone(req.output_dir)

    def test_unknown_fields_never_rejected(self):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            reasoning_effort="high",
            some_future_field={"nested": True},
        )
        self.assertEqual(req.temperature, 0.7)

    def test_tools_and_tool_choice(self):
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "f"}}],
            tool_choice="auto",
        )
        self.assertEqual(len(req.tools), 1)
        self.assertEqual(req.tool_choice, "auto")

    def test_missing_messages_is_error(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ChatCompletionRequest()


class ResponseStructureTests(unittest.TestCase):
    def test_ids_and_object(self):
        msg = ChoiceMessage(role="assistant", content="hello")
        resp = ChatCompletionResponse(model="gemini-chat", choices=[Choice(message=msg)])
        self.assertTrue(resp.id.startswith("chatcmpl-"))
        self.assertEqual(resp.object, "chat.completion")
        self.assertEqual(resp.choices[0].message.content, "hello")

    def test_finish_reason_default(self):
        msg = ChoiceMessage(role="assistant", content="x")
        self.assertEqual(Choice(message=msg).finish_reason, "stop")

    def test_tool_call_shape(self):
        call = ToolCall(function=FunctionCall(name="f", arguments='{"a": 1}'))
        self.assertTrue(call.id.startswith("call_"))
        self.assertEqual(call.type, "function")
        self.assertEqual(call.function.name, "f")

    def test_function_call_default_arguments(self):
        self.assertEqual(FunctionCall(name="f").arguments, "{}")

    def test_default_usage_and_saved_files(self):
        msg = ChoiceMessage(role="assistant", content="x")
        resp = ChatCompletionResponse(model="m", choices=[Choice(message=msg)])
        self.assertEqual(resp.usage.total_tokens, 0)
        self.assertEqual(resp.saved_files, [])

    def test_explicit_usage(self):
        usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        self.assertEqual(usage.total_tokens, 3)


class ModelListTests(unittest.TestCase):
    def test_supported_ids_present(self):
        ids = {m["id"] for m in SUPPORTED_MODELS}
        self.assertIn("gemini-chat", ids)

    def test_context_window_is_not_hardcoded_in_models(self):
        """T8.7：唯一权威数值是 SESSION_MAX_TOKENS，由 /v1/models 透出。

        旧实现把 65536 写在 SUPPORTED_MODELS 里，与 README 示例（1000000）
        互相矛盾，而端点又不透出该字段。现在模型表只保留 id。
        """
        for model in SUPPORTED_MODELS:
            self.assertNotIn("context_window", model)
        card = ModelCard(id="gemini-chat", context_window=123)
        self.assertEqual(card.context_window, 123)

    def test_model_card_defaults(self):
        card = ModelCard(id="gemini-chat")
        self.assertEqual(card.object, "model")
        self.assertEqual(card.owned_by, "gemini-web-bridge")

    def test_model_list_object(self):
        listing = ModelListResponse(data=[ModelCard(id="x")])
        self.assertEqual(listing.object, "list")


if __name__ == "__main__":
    unittest.main()
