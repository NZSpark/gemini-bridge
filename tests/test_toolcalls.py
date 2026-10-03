"""工具调用注入与解析的回归测试。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web.toolcalls import (  # noqa: E402
    _normalize_tool_entry,
    _tool_names,
    format_tools_instruction,
    parse_tool_calls,
    to_tool_call_models,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


class FormatInstructionTests(unittest.TestCase):
    def test_contains_name_and_description(self):
        text = format_tools_instruction(TOOLS)
        self.assertIn("get_weather", text)
        self.assertIn("查询天气", text)

    def test_contains_call_template(self):
        text = format_tools_instruction(TOOLS)
        self.assertIn("TOOL_CALL", text.upper())

    def test_template_is_stable(self):
        self.assertEqual(
            format_tools_instruction(TOOLS), format_tools_instruction(TOOLS)
        )

    def test_empty_tools(self):
        self.assertIsInstance(format_tools_instruction([]), str)


class ToolNamesTests(unittest.TestCase):
    def test_collects_names(self):
        self.assertEqual(_tool_names(TOOLS), {"get_weather"})

    def test_none_and_empty(self):
        self.assertEqual(_tool_names(None), set())
        self.assertEqual(_tool_names([]), set())


class NormalizeEntryTests(unittest.TestCase):
    def test_non_dict_returns_none(self):
        self.assertIsNone(_normalize_tool_entry("nope"))
        self.assertIsNone(_normalize_tool_entry(123))

    def test_missing_name_returns_none(self):
        self.assertIsNone(_normalize_tool_entry({"arguments": {}}))

    def test_flat_entry(self):
        out = _normalize_tool_entry({"name": "f", "arguments": {"a": 1}})
        self.assertEqual(out["name"], "f")
        self.assertEqual(out["arguments"], {"a": 1})

    def test_nested_function_key(self):
        entry = {"function": {"name": "f", "arguments": {"a": 1}}}
        out = _normalize_tool_entry(entry)
        self.assertEqual(out["name"], "f")
        self.assertEqual(out["arguments"], {"a": 1})

    def test_string_arguments_decoded(self):
        out = _normalize_tool_entry({"name": "f", "arguments": '{"a": 1}'})
        self.assertEqual(out["arguments"], {"a": 1})


class ParseToolCallsTests(unittest.TestCase):
    def test_fenced_call(self):
        text = '```tool_call\n{"name": "get_weather", "arguments": {"city": "SF"}}\n```'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["arguments"], {"city": "SF"})

    def test_multiple_fenced_calls(self):
        text = (
            '```tool_call\n{"name": "a", "arguments": {}}\n```\n'
            '```tool_call\n{"name": "b", "arguments": {}}\n```'
        )
        calls = parse_tool_calls(text)
        self.assertEqual([c["name"] for c in calls], ["a", "b"])

    def test_line_marker(self):
        text = 'TOOL_CALL: {"name": "get_weather", "arguments": {"city": "NY"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "get_weather")

    def test_tool_calls_wrapper_key(self):
        payload = {"tool_calls": [{"name": "a", "arguments": {"x": 1}}]}
        text = "```tool_call\n" + json.dumps(payload) + "\n```"
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "a")

    def test_tool_uses_wrapper_key(self):
        payload = {"tool_uses": [{"name": "a", "arguments": {}}]}
        text = "```tool_call\n" + json.dumps(payload) + "\n```"
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "a")

    def test_invalid_json_is_ignored(self):
        text = "```tool_call\n{not json}\n```"
        self.assertEqual(parse_tool_calls(text), [])

    def test_plain_text_returns_empty(self):
        self.assertEqual(parse_tool_calls("just a normal answer"), [])

    def test_empty_returns_empty(self):
        self.assertEqual(parse_tool_calls(""), [])

    def test_string_arguments_decoded(self):
        text = '```tool_call\n{"name": "a", "arguments": "{\\"x\\": 1}"}\n```'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"], {"x": 1})

    def test_valid_names_filter_drops_hallucinations(self):
        text = '```tool_call\n{"name": "ghost", "arguments": {}}\n```'
        self.assertEqual(parse_tool_calls(text, {"real"}), [])

    def test_valid_names_filter_keeps_known(self):
        text = '```tool_call\n{"name": "real", "arguments": {}}\n```'
        self.assertEqual(len(parse_tool_calls(text, {"real"})), 1)

    def test_parameters_key_alias(self):
        text = '```tool_call\n{"name": "a", "parameters": {"y": 2}}\n```'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"], {"y": 2})


class ToToolCallModelsTests(unittest.TestCase):
    def test_arguments_serialized_as_json_string(self):
        calls = [{"name": "f", "arguments": {"a": 1}}]
        models = to_tool_call_models(calls)
        self.assertEqual(models[0].function.name, "f")
        self.assertEqual(json.loads(models[0].function.arguments), {"a": 1})

    def test_id_prefix(self):
        models = to_tool_call_models([{"name": "f", "arguments": {}}])
        self.assertTrue(models[0].id.startswith("call_"))


if __name__ == "__main__":
    unittest.main()
