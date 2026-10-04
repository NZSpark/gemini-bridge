import unittest




class ToolResultFidelityTests(unittest.TestCase):
    """工具执行结果必须逐字节保留，供 edit 工具做 oldText 精确匹配。"""

    def test_tool_result_preserves_trailing_newline(self):
        from gemini_web.prompting import _render_message
        from gemini_web.models import ChatMessage

        raw = "1  # Title\n2  \n3  ## Section\n4  text\n"
        m = ChatMessage(role="tool", content=raw, tool_call_id="call_1")
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertEqual(body, raw)

    def test_tool_result_preserves_leading_blank_lines(self):
        from gemini_web.prompting import _render_message
        from gemini_web.models import ChatMessage

        raw = "\n\n# Title\n"
        m = ChatMessage(role="tool", content=raw)
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertTrue(body.startswith("\n\n# Title"))
