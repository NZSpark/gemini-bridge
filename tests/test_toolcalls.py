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

    def test_unescaped_inner_quotes_repaired(self):
        # 模型把 shell 命令里的引号原样写进 JSON 字符串（未转义），
        # 标准 json.loads 会失败；解析器应尽力修复并保留引号原意。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git commit -m "Update logic" && git push"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git commit -m "Update logic" && git push',
        )

    def test_unescaped_inner_quotes_repaired_full_command(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": '
            '"git add a.py b.py && git commit -m "msg here" && git push"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "exec_command")
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git add a.py b.py && git commit -m "msg here" && git push',
        )

    def test_wellformed_json_still_parses(self):
        # 修复逻辑只在解析失败时触发，合法输入不受影响。
        text = 'TOOL_CALL: {"name": "a", "arguments": {"cmd": "echo \\"hi\\""}}'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"]["cmd"], 'echo "hi"')

    def test_markdown_escaped_marker_recovered(self):
        # Gemini 网页版 markdown 渲染会插入反斜杠：TOOL\_CALL / exec\_command
        text = 'TOOL\\_CALL: {"name": "exec\\_command", "arguments": {"cmd": "mkdir -p doc"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "exec_command")
        self.assertEqual(calls[0]["arguments"]["cmd"], "mkdir -p doc")

    def test_properly_escaped_inner_quotes(self):
        # 注入指令要求模型把值内双引号转义为 \" ；这是首选、合法的形态。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "python -c \\"import os\\""}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], 'python -c "import os"')

    def test_redundant_boundary_quotes_stripped(self):
        # 模型给值又包了一层引号："cmd": ""git status""；应还原为无多余引号。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": ""git status && git log -n 5 --oneline""}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git status && git log -n 5 --oneline',
        )

    def test_single_stray_open_quote_recovered(self):
        # 值开头多一个引号（平衡扫描会失败）："cmd": ""git status"
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": ""git status && git log -n 5 --oneline"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git status && git log -n 5 --oneline',
        )

    def test_single_stray_close_quote_recovered(self):
        # 值结尾多一个引号："cmd": "git status""
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git status && git log -n 5 --oneline""}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git status && git log -n 5 --oneline',
        )


class ControlCharRepairTests(unittest.TestCase):
    """多行命令（heredoc）里的真实换行：JSON 字符串内裸控制字符需转义。"""

    def test_heredoc_newlines_repaired(self):
        # 模型把多行命令的真实换行直接写进 JSON 字符串值。
        # 手工拼一个"含裸换行"的 JSON（不走 json.dumps，否则会转义掉换行）。
        cmd = "cat << 'EOF' > x.py\nprint('hi')\nEOF\n"
        raw = '{"name": "exec_command", "arguments": {"cmd": "' + cmd + '"}}'
        calls = parse_tool_calls("TOOL_CALL: " + raw, {"exec_command"})
        self.assertEqual(len(calls), 1)
        got = calls[0]["arguments"]["cmd"]
        self.assertIn("cat << 'EOF' > x.py\n", got)
        self.assertIn("print('hi')\n", got)

    def test_tab_control_char_repaired(self):
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "echo\thi"}}'
        calls = parse_tool_calls(text, {"exec_command"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "echo\thi")

    def test_wellformed_multiline_still_parses(self):
        # 已正确转义 \n 的合法输入不受影响。
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "a\\nb"}}'
        calls = parse_tool_calls(text, {"exec_command"})
        self.assertEqual(calls[0]["arguments"]["cmd"], "a\nb")


class ShellGuardTests(unittest.TestCase):
    """护栏：shell 类命令引号不配对时丢弃，避免把坏命令发给 bash。"""

    def test_unbalanced_double_quote_dropped(self):
        # 模型/解析把命令尾部闭引号弄丢："git commit -m "msg"
        # 引号数为奇数 → 丢弃（否则 bash 报 unexpected EOF）。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git commit -m "msg"}}'
        )
        self.assertEqual(parse_tool_calls(text), [])

    def test_balanced_double_quotes_kept(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git commit -m "msg here" && git push"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "exec_command")

    def test_single_quoted_command_kept(self):
        # prompt 建议命令内部用单引号：这是首选形态，不应被护栏误伤。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            "{\"cmd\": \"git commit -m 'msg here'\"}}"
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "git commit -m 'msg here'")

    def test_non_shell_tool_not_guarded(self):
        # 非 shell 工具的字符串参数即使引号不配对也不受影响（不被护栏丢弃）。
        text = 'TOOL_CALL: {"name": "write_note", "arguments": {"text": "a \\" b"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["text"], 'a " b')


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
