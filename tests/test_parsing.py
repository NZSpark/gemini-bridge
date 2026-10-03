"""解析层与文本工具函数的回归测试。

只用标准库 unittest：

    .venv/bin/python -m unittest discover -s tests -t . -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gemini_api_server as srv  # noqa: E402


class ContentToTextTests(unittest.TestCase):
    def test_none_and_plain_string(self):
        self.assertEqual(srv._content_to_text(None), "")
        self.assertEqual(srv._content_to_text("hi"), "hi")

    def test_parts_array(self):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(srv._content_to_text(content), "a\nb")

    def test_plain_string_parts(self):
        self.assertEqual(srv._content_to_text(["a", "b"]), "a\nb")

    def test_non_text_parts_are_ignored(self):
        content = [{"type": "image_url", "image_url": {"url": "x"}}, {"text": "kept"}]
        self.assertEqual(srv._content_to_text(content), "kept")

    def test_dict_without_text(self):
        self.assertEqual(srv._content_to_text({"foo": 1}), "")

    def test_dict_with_text(self):
        self.assertEqual(srv._content_to_text({"text": "hi"}), "hi")

    def test_unknown_type_stringified(self):
        self.assertEqual(srv._content_to_text(123), "123")


class EstimateTokensTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(srv.estimate_tokens(""), 0)

    def test_english_is_about_quarter(self):
        text = "a" * 400
        self.assertEqual(srv.estimate_tokens(text), 100)

    def test_cjk_counts_per_char(self):
        self.assertEqual(srv.estimate_tokens("你好世界"), 4)

    def test_never_zero_for_content(self):
        self.assertGreaterEqual(srv.estimate_tokens("a"), 1)


class DeltaPieceTests(unittest.TestCase):
    def test_first_piece(self):
        self.assertEqual(srv._delta_piece("", "abc"), ("abc", "abc"))

    def test_no_growth(self):
        self.assertEqual(srv._delta_piece("abc", "abc")[0], None)

    def test_prefix_extension(self):
        self.assertEqual(srv._delta_piece("abc", "abcdef"), ("def", "abcdef"))

    def test_rewrite_stops_streaming_instead_of_appending(self):
        # 节点被整体替换：不再追加，避免客户端拼出重复 / 错乱文本；
        # 已发内容保持不动，收尾时由调用方补发全量。
        piece, streamed = srv._delta_piece("abc", "abd")
        self.assertIsNone(piece)
        self.assertEqual(streamed, "abc")

    def test_rewrite_with_no_common_prefix_stops(self):
        piece, streamed = srv._delta_piece("abc", "xyz")
        self.assertIsNone(piece)
        self.assertEqual(streamed, "abc")


class BalancedObjectTests(unittest.TestCase):
    def test_nested_braces(self):
        text = '{"a": {"b": 1}}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), [text])

    def test_brace_inside_string_is_not_counted(self):
        text = '{"cmd": "echo }"}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), [text])

    def test_escaped_quote(self):
        text = '{"cmd": "say \\"hi\\""}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), [text])

    def test_multiple_objects(self):
        text = '{"a": 1} {"b": 2}'
        self.assertEqual(list(srv._iter_balanced_objects(text)), ['{"a": 1}', '{"b": 2}'])

    def test_unbalanced_is_ignored(self):
        self.assertEqual(list(srv._iter_balanced_objects('{"a": 1')), [])


if __name__ == "__main__":
    unittest.main()
