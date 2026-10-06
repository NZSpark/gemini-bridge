"""工具调用注入与解析的回归测试。

载体（carrier）已从纯文本 ``TOOL_CALL: {...}`` 行改为 ```tool_call 代码围栏
（见 ``doc/code_block_fence.md``）。本文件同时守两件事：

* 新载体：注入文案必须给出围栏模板，且该模板能反哺解析器（提示词与解析器同源）；
* 旧载体：纯文本行仍作为历史数据兼容，相关用例保留不删（它们现在是兼容性护栏）。
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest import mock  # noqa: E402

from gemini_web import config  # noqa: E402
from gemini_web.toolcalls import (  # noqa: E402
    _iter_balanced_objects,
    _normalize_tool_entry,
    _tool_names,
    edit_markdown_spec,
    execute_edit_markdown,
    format_tool_call_emphasis,
    format_tools_instruction,
    parse_tool_calls,
    should_register_edit_markdown,
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

    def test_contains_fenced_call_template(self):
        text = format_tools_instruction(TOOLS)
        self.assertIn("```tool_call", text)
        self.assertIn("info string is exactly", text)

    def test_no_longer_asks_for_plain_text_line(self):
        """载体已换成围栏：不得再残留「写成纯文本行 / 不要代码围栏」的旧措辞。"""
        text = format_tools_instruction(TOOLS)
        self.assertNotIn("plain text lines", text)
        self.assertNotIn("no code fences", text)
        self.assertNotIn("do not add ```", text)

    def test_template_is_stable(self):
        self.assertEqual(
            format_tools_instruction(TOOLS), format_tools_instruction(TOOLS)
        )

    def test_empty_tools(self):
        self.assertIsInstance(format_tools_instruction([]), str)


# 真机抓取样例（2026-10-07，Gemini 网页版 HEADLESS=false，无 tools 的运输层回声；
# 采集命令见 doc/code_block_fence.md §9）。同一段 92 字节 payload，三种载体从 DOM 取回后的
# 原文，逐字节照抄。关键差异：
#   * plain_line：网页版把这一行当 markdown 段落渲染，**反斜杠转义被吃掉**
#     （`\"` → `"`），JSON 不再合法——只能用修复启发式勉强救回，命令内容已损坏；
#   * code_only（只给围栏）：JSON 逐字节保留，但围栏与 info string 都**不进
#     innerText**（DOM 里只剩 UI 标题 ``Code snippet``），回复里没有任何可识别标记；
#   * hybrid（标记行 + 围栏）：两者兼得——标记行可识别，围栏保真。这是当前注入格式。
_CAPTURED_COMMAND_ESCAPED = 'printf \\"hi\\"; echo done\\n    x = 1\\n        y = 2'
_CAPTURED_COMMAND_EATEN = 'printf "hi"; echo done\\n    x = 1\\n        y = 2'
# json.loads 之后应该拿到的东西（字面量 \\n 变成真换行）
_CAPTURED_COMMAND_EXPECTED = 'printf "hi"; echo done\n    x = 1\n        y = 2'

DOM_SAMPLE_HYBRID = (
    "TOOL_CALL:\n\nCode snippet\n"
    '{"name":"bash","arguments":{"command":"' + _CAPTURED_COMMAND_ESCAPED + '"}}'
)
DOM_SAMPLE_CODE_ONLY = (
    "Code snippet\n"
    '{"name":"bash","arguments":{"command":"' + _CAPTURED_COMMAND_ESCAPED + '"}}'
)
DOM_SAMPLE_PLAIN_LINE = (
    'TOOL_CALL: {"name":"bash","arguments":{"command":"' + _CAPTURED_COMMAND_EATEN + '"}}'
)


class FencedCarrierTests(unittest.TestCase):
    """新载体（``TOOL_CALL:`` 标记行 + ```tool_call 围栏）的注入—解析闭环。

    载体为什么是混合形态、以及“只给围栏”为什么在本桥不可用，见 doc/code_block_fence.md。
    """

    def test_injection_asks_for_both_parts(self):
        """注入文案必须同时点名标记行与围栏：缺任何一半都会整体失效。"""
        for text in (format_tools_instruction(TOOLS), format_tool_call_emphasis()):
            self.assertIn("TOOL_CALL:", text)
            self.assertIn("```tool_call", text)

    def test_instruction_example_roundtrips_through_parser(self):
        """注入块里的示例必须是合法 JSON 围栏（提示词与解析器同源，不会各说各话）。"""
        text = format_tools_instruction(TOOLS)
        match = re.search(r"```tool_call\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(match, "注入块里缺少 ```tool_call 示例")
        example = json.loads(match.group(1))
        self.assertIn("name", example)
        self.assertIn("arguments", example)

        real = "```tool_call\n" + json.dumps(
            {"name": "get_weather", "arguments": {"city": "SF"}}
        ) + "\n```"
        calls = parse_tool_calls(real, {"get_weather"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], {"city": "SF"})
        # 示例本身只用了占位工具名，不应被当成可用调用泄漏出去
        self.assertEqual(parse_tool_calls(text, {"get_weather"}), [])

    def test_emphasis_block_mandates_fence(self):
        """新会话的格式强调块必须点名混合载体的两半，且不得再出现旧措辞。"""
        block = format_tool_call_emphasis()
        self.assertIn("```tool_call", block)
        self.assertIn("marker line must be exactly `TOOL_CALL:`", block)
        self.assertIn("fence label must be exactly `tool_call`", block)
        self.assertNotIn("plain text lines", block)
        self.assertNotIn("no code fences", block)

    def test_edit_markdown_spec_uses_fenced_carrier(self):
        spec = edit_markdown_spec()
        self.assertIn("TOOL_CALL:\n```tool_call", spec)
        # 示例里的 <int> 只是占位符，但结构合法；关键是围栏能被解析到 edit_markdown
        calls = parse_tool_calls(spec, {"edit_markdown"})
        self.assertEqual([c["name"] for c in calls], ["edit_markdown"])

    def test_raw_fence_still_parses(self):
        """围栏未被渲染（客户端把回复原样贴回 / 模型输出未渲染）时的主路径。"""
        text = '```tool_call\n{"name": "get_weather", "arguments": {"city": "SF"}}\n```'
        calls = parse_tool_calls(text, {"get_weather"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")

    def test_multiple_fences_are_all_consumed(self):
        """多个围栏＝并行多调用（一个围栏一条）。"""
        text = (
            '```tool_call\n{"name": "a", "arguments": {}}\n```\n'
            '```tool_call\n{"name": "b", "arguments": {}}\n```'
        )
        calls = parse_tool_calls(text, {"a", "b"})
        self.assertEqual([c["name"] for c in calls], ["a", "b"])

    def test_rendered_fence_label_line_parses(self):
        """网页 DOM 形态：围栏被渲染掉，只剩 ``tool_call`` 标签行 + JSON。

        这是本桥真实的取回形态（见 doc/code_block_fence.md），必须走裸标签兑底。
        """
        text = 'tool_call\n{"name": "get_weather", "arguments": {"city": "SF"}}'
        calls = parse_tool_calls(text, {"get_weather"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], {"city": "SF"})

    def test_rendered_fence_preserves_escapes_and_indentation(self):
        """载体保真：围栏内按字面保留，转义与缩进必须逐字节到达命令。

        这是整个改造的全部理由：同一段 payload，只把“承载它的那半”从纯文本行换成代码
        围栏，就能让 ``\\\"`` 与 4/8 空格缩进活着穿过网页渲染。
        """
        command = 'printf \"hi\"; echo done\n    x = 1\n        y = 2'
        payload = json.dumps({"name": "bash", "arguments": {"command": command}})
        for text in (
            "```tool_call\n" + payload + "\n```",          # 围栏未渲染
            "tool_call\n" + payload,                       # 只剩裸标签
            "TOOL_CALL:\n\nCode snippet\n" + payload,      # 真机 DOM 形态
        ):
            calls = parse_tool_calls(text, {"bash"})
            self.assertEqual(len(calls), 1, text)
            self.assertEqual(calls[0]["arguments"]["command"], command)

    def test_captured_gemini_dom_hybrid_is_byte_exact(self):
        """真机样本（关键回归）：混合载体的 DOM 形态必须逐字节还原命令，

        而且**不需要任何修复启发式**——围栏内取回的就是合法 JSON。
        """
        candidate = next(iter(_iter_balanced_objects(DOM_SAMPLE_HYBRID)), "")
        self.assertEqual(json.loads(candidate)["arguments"]["command"], _CAPTURED_COMMAND_EXPECTED)
        calls = parse_tool_calls(DOM_SAMPLE_HYBRID, {"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        self.assertEqual(
            calls[0]["arguments"]["command"], _CAPTURED_COMMAND_EXPECTED
        )

    def test_captured_gemini_dom_plain_line_is_not_valid_json(self):
        """真机样本（反例）：同一 payload 走纯文本行载体，DOM 里转义已被消费。

        这条锁住“为什么必须把载荷搬进围栏”：取回文本里 ``\\\"`` 已经全部消失，
        严格 ``json.loads`` 直接失败——整条调用只能交给修复启发式去猜（而正文裸引号
        在 JSON 里本质有歧义，长命令下会猜错、截断）。对比见
        :meth:`test_captured_gemini_dom_hybrid_is_byte_exact`：换进围栏后连修复都不需要。
        """
        self.assertEqual(DOM_SAMPLE_PLAIN_LINE.count('\\"'), 0)
        candidate = next(iter(_iter_balanced_objects(DOM_SAMPLE_PLAIN_LINE)), "")
        self.assertTrue(candidate, "样本里应当能扫出一个括号平衡的对象")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(candidate)
        # 启发式确实能救回一条（护栏没误杀），但这条路径是“猜”出来的
        calls = parse_tool_calls(DOM_SAMPLE_PLAIN_LINE, {"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["command"], _CAPTURED_COMMAND_EXPECTED)

    def test_captured_gemini_dom_code_only_has_no_marker(self):
        """真机样本（反例）：只给围栏时，DOM 里连一个可用标记都没有 → 0 条。

        网页版把围栏渲染成 code-snippet 组件，围栏与 info string 都不进 innerText
        （只剩 UI 标题 ``Code snippet``）。因此**标记行不可省**——这条用例守的就是这
        个前提，防止有人把注入格式“简化”回围栏单体。
        """
        self.assertNotIn("```", DOM_SAMPLE_CODE_ONLY)
        self.assertNotIn("TOOL_CALL", DOM_SAMPLE_CODE_ONLY.upper())
        self.assertEqual(parse_tool_calls(DOM_SAMPLE_CODE_ONLY, {"bash"}), [])

    def test_multiline_json_inside_fence_parses(self):
        """代码块保留换行 → 围栏内 JSON 合法跨行（token 之间的换行）也必须能解析。"""
        text = (
            "```tool_call\n"
            "{\n"
            '  "name": "get_weather",\n'
            '  "arguments": {\n    "city": "SF"\n  }\n'
            "}\n"
            "```"
        )
        calls = parse_tool_calls(text, {"get_weather"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], {"city": "SF"})

    def test_rendered_json_label_is_not_taken_as_call(self):
        """负向：DOM 只剩 ``json`` 标签行时必须**不**执行（安全取舍）。

        对话里正常展示的 JSON 代码块若恰好有 name/arguments，被当成调用执行就是
        「模型可控文本触发本机命令」。因此 info string 只认 tool_call。
        """
        text = 'json\n{"name": "get_weather", "arguments": {"city": "SF"}}'
        self.assertEqual(parse_tool_calls(text, {"get_weather"}), [])

    def test_warns_when_marker_but_no_call(self):
        """可观测性：有载体标记却解析不出调用时，必须留下 warning（原先完全静默）。"""
        with self.assertLogs("gemini_web.toolcalls", level="WARNING") as ctx:
            calls = parse_tool_calls('```tool_call\n{"name": "get_weather", "argu', {"get_weather"})
        self.assertEqual(calls, [])
        self.assertTrue(any("未产出任何可用调用" in line for line in ctx.output))

    def test_no_warning_for_plain_answer(self):
        with self.assertNoLogs("gemini_web.toolcalls", level="WARNING"):
            self.assertEqual(parse_tool_calls("just a normal answer"), [])

    def test_legacy_plain_text_carrier_still_parses(self):
        """历史数据兼容：旧纯文本行载体不再注入，但解析侧必须继续认。"""
        text = 'TOOL_CALL: {"name": "get_weather", "arguments": {"city": "NY"}}'
        calls = parse_tool_calls(text, {"get_weather"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], {"city": "NY"})


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

    def test_multiline_command_with_inner_quotes(self):
        # Real newlines plus unescaped inner quotes inside a JSON string value.
        raw_command = "\n".join([
            "python3 -c '",
            "import os, glob",
            'files = [y for x in os.walk(".") for y in glob.glob(os.path.join(x[0], "*"))]',
            "print('done')",
            "'",
        ])
        text = 'TOOL_CALL: {"name": "bash", "arguments": {"command": "' + raw_command + '"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        cmd = calls[0]["arguments"]["command"]
        self.assertIn('os.walk(".")', cmd)
        self.assertIn("glob.glob", cmd)


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

    def test_fenced_content_with_inner_object_recovered(self):
        # 值内既有 markdown 围栏又有裸 `{...}` 时，平衡扫描会错位；
        # 锚点式 salvage 应按 name/arguments 取到对象收尾，保留完整内容。
        text = (
            'TOOL_CALL: {"name": "write", "arguments": {"content": '
            '"see ```json\n{\\"a\\":1}\n``` end"}}'
        )
        calls = parse_tool_calls(text, valid_names={"write"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "write")
        self.assertEqual(
            calls[0]["arguments"]["content"],
            'see ```json\n{"a":1}\n``` end',
        )

    def test_multi_arg_fenced_content_with_raw_newline_recovered(self):
        # write 工具常见形态：path + content，content 是多行围栏且值内带双引号。
        text = (
            'TOOL_CALL: {"name": "write", "arguments": {"path": "README.md", '
            '"content": "```python\nclient = OpenAI(base_url=\\"http://x\\")\n```"}}'
        )
        calls = parse_tool_calls(text, valid_names={"write"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["path"], "README.md")
        self.assertEqual(
            calls[0]["arguments"]["content"],
            '```python\nclient = OpenAI(base_url="http://x")\n```',
        )

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

    def test_body_quote_before_bracket_kept(self):
        # Gemini 网页渲染会把 \" 消耗掉，DOM 取回后值里是裸引号：
        # [contenteditable="true"]。true 后面的引号紧跟 ]，再下一个是正文字符，
        # 不能被当成字符串结束，否则字符串提前闭合、整条调用被丢弃。
        text = (
            'TOOL_CALL: {"name": "edit", "arguments": {"edits": [{"oldText": '
            '"- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定可输入。"}], '
            '"path": "doc/design.md"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["edits"][0]["oldText"],
            '- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定可输入。',
        )
        self.assertEqual(calls[0]["arguments"]["path"], "doc/design.md")

    def test_body_quote_with_raw_newlines_repaired(self):
        # 裸引号 + 真实换行（多行 Markdown 未转义）叠加：仍应完整还原。
        old = '- `A`：x\n- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定。\n- `B`：y'
        text = (
            'TOOL_CALL: {"name": "edit", "arguments": {"edits": [{"oldText": "'
            + old +
            '"}]}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["edits"][0]["oldText"], old)


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


class ToolCallLineContractTests(unittest.TestCase):
    """首选形态 TOOL_CALL: 的“一行一调用”契约与重复消费回归（update_codex §2.3）。"""

    def test_multi_line_multi_calls(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}}\n'
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "pwd"}}\n'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [c["arguments"]["cmd"] for c in calls], ["ls", "pwd"]
        )

    def test_two_objects_same_line_second_dropped(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}} '
            '{"name": "exec_command", "arguments": {"cmd": "pwd"}}\n'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "ls")

    def test_marker_without_object_does_not_duplicate_next(self):
        # 第一个标记后没跟对象，只有解释文字；第二个标记才有对象。
        # 修复前：第一个标记会消费第二个标记的对象，导致同一调用出现两次。
        text = (
            "TOOL_CALL: 我改主意了，先说明一下\n"
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}}\n'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "ls")


class ToToolCallModelsTests(unittest.TestCase):
    def test_arguments_serialized_as_json_string(self):
        calls = [{"name": "f", "arguments": {"a": 1}}]
        models = to_tool_call_models(calls)
        self.assertEqual(models[0].function.name, "f")
        self.assertEqual(json.loads(models[0].function.arguments), {"a": 1})

    def test_id_prefix(self):
        models = to_tool_call_models([{"name": "f", "arguments": {}}])
        self.assertTrue(models[0].id.startswith("call_"))


class ShouldRegisterEditMarkdownTests(unittest.TestCase):
    """T7.6：内置工具只在客户端已声明工具时才注入（否则每请求多付 ~970 tokens）。"""

    def test_not_registered_when_client_has_no_tools(self):
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", True), \
                mock.patch.object(config, "EDIT_MARKDOWN_ALWAYS_REGISTER", False):
            self.assertFalse(should_register_edit_markdown(None))
            self.assertFalse(should_register_edit_markdown([]))

    def test_registered_when_client_has_tools(self):
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", True), \
                mock.patch.object(config, "EDIT_MARKDOWN_ALWAYS_REGISTER", False):
            self.assertTrue(should_register_edit_markdown(TOOLS))

    def test_not_registered_when_client_already_declares_it(self):
        tools = list(TOOLS) + [{
            "type": "function",
            "function": {"name": "edit_markdown", "parameters": {}},
        }]
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", True), \
                mock.patch.object(config, "EDIT_MARKDOWN_ALWAYS_REGISTER", False):
            self.assertFalse(should_register_edit_markdown(tools))

    def test_not_registered_when_local_execution_disabled(self):
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", False), \
                mock.patch.object(config, "EDIT_MARKDOWN_ALWAYS_REGISTER", True):
            self.assertFalse(should_register_edit_markdown(TOOLS))

    def test_escape_hatch_restores_old_behaviour(self):
        with mock.patch.object(config, "EDIT_MARKDOWN_LOCAL", True), \
                mock.patch.object(config, "EDIT_MARKDOWN_ALWAYS_REGISTER", True):
            self.assertTrue(should_register_edit_markdown(None))


class EditMarkdownPathGuardTests(unittest.TestCase):
    """T8.9：本地 edit_markdown 只能碰工作区根内的文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "doc").mkdir()
        (self.root / "doc" / "a.md").write_text("# A\n\nbody\n", encoding="utf-8")
        patcher = mock.patch.object(config, "EDIT_MARKDOWN_ROOT", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _edit(self, path, **kw):
        args = {"path": path, "start": 1, "end": 1, "new_text": "# B"}
        args.update(kw)
        return execute_edit_markdown(args, backup_dir=str(self.root / "backups"))

    def test_relative_path_inside_root_is_allowed(self):
        result = self._edit("doc/a.md")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["written"])  # 默认 dry-run
        self.assertEqual(Path(result["path"]), (self.root / "doc" / "a.md").resolve())

    def test_dotdot_escape_is_rejected(self):
        result = self._edit("../../etc/hosts")
        self.assertFalse(result["ok"])
        self.assertIn("路径越界", result["error"])

    def test_absolute_path_outside_root_is_rejected(self):
        result = self._edit("/etc/hosts")
        self.assertFalse(result["ok"])
        self.assertIn("路径越界", result["error"])

    def test_write_true_still_rejects_escape(self):
        """dry-run 与 write=true 必须用同一道关卡，不能只在读取时校验。"""
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.md"
            outside.write_text("# outside\n", encoding="utf-8")
            result = self._edit(str(outside), write=True)
            self.assertFalse(result["ok"])
            self.assertIn("路径越界", result["error"])
            # 未被改动：闸门必须在写盘之前生效
            self.assertEqual(outside.read_text(encoding="utf-8"), "# outside\n")

    def test_write_true_inside_root_persists(self):
        result = self._edit("doc/a.md", write=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["written"])
        self.assertTrue(result["backup"])
        self.assertTrue((self.root / "backups").exists())
        first_line = (self.root / "doc" / "a.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "# B")

    def test_directory_target_returns_error_instead_of_raising(self):
        """目标是目录（或根本读不动）时也必须返回结构化 error，不能抛给上层。"""
        result = self._edit(".")
        self.assertFalse(result["ok"])
        self.assertIn("读取失败", result["error"])


if __name__ == "__main__":
    unittest.main()
