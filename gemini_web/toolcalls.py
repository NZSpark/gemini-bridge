"""工具调用（function calling）桥接层。

Gemini 网页版并不原生支持 OpenAI 的 function calling，因此这里采用
“提示词注入 + 结构化解析”的方式模拟：
  1. 把客户端传来的 tools 描述注入到 prompt，要求模型用 ```tool_call 代码块回话；
  2. 解析模型输出里的代码块，还原为 OpenAI 的 tool_calls；
  3. 下一轮请求里 role=tool 的执行结果再拼回 prompt 喂给网页版。
"""

import json
import re
from typing import Any, Dict, List, Optional

from .models import FunctionCall, ToolCall

# 仅匹配 "tool_call" / "tool-call" 围栏，避免误伤普通 ```json 代码块
_TOOL_CALL_FENCE_RE = re.compile(r"```(tool[-_]call)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# "json" 围栏仅在内容明显是工具调用时才采纳（兜底，兼容模型不听话的情况）
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# 网页版偶尔会输出 DSML 风格的工具调用 XML（全角竖线 ｜｜ 包裹的标签），
# 形如： <｜｜DSML｜｜ calls>{"tool_uses": [...]}</｜｜DSML｜｜ calls>
# 这里只取标签之间的 JSON 对象，交由 _consume 解析。
_DSML_TOOL_RE = re.compile(
    r"<\s*[｜|]{2}\s*DSML\s*[｜|]{2}[^>]*>(.*?)<\s*/\s*[｜|]{2}\s*DSML\s*[｜|]{2}",
    re.DOTALL | re.IGNORECASE,
)
# 无标签但带 "tool_uses" 键的裸 JSON 对象（DOM 提取后标签可能丢失）
_TOOL_USES_RE = re.compile(r"tool_uses\s*\"?\s*:", re.IGNORECASE)


def format_tools_instruction(tools: List[Dict[str, Any]]) -> str:
    """把 OpenAI tools 描述转换成注入网页版的自然语言指令。"""
    lines = [
        "[工具调用说明]",
        "你可以调用下列工具来完成任务（本轮对话中有效）：",
    ]
    for tool in tools:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        lines.append(f"- {name}: {desc}")
        if params:
            lines.append(f"  参数(JSON Schema): {json.dumps(params, ensure_ascii=False)}")

    lines += [
        "",
        "需要调用工具时，只输出一个或多个如下格式的代码块（arguments 必须是合法 JSON）：",
        "```tool_call",
        '{"name": "工具名", "arguments": {参数对象}}',
        "```",
        "一次可输出多个 tool_call 代码块以并行调用多个工具；代码块之外不要输出多余解释。",
        "如果不需要调用任何工具，请直接给出最终回答，不要输出 tool_call 代码块。",
    ]
    return "\n".join(lines)


def _normalize_tool_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    function = entry.get("function") or {}
    name = entry.get("name") or entry.get("tool") or function.get("name")
    arguments = entry.get("arguments")
    if arguments is None:
        arguments = entry.get("parameters")
    if arguments is None:
        arguments = function.get("arguments")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    if not name:
        return None
    return {"name": name, "arguments": arguments}


def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> set:
    """从 OpenAI tools 描述里收集合法工具名，用于过滤误报。"""
    names = set()
    for tool in tools or []:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name")
        if name:
            names.add(name)
    return names


def _iter_balanced_objects(text: str):
    """扫描文本，产出顶层、括号平衡的 JSON 对象字面量（能正确处理字符串与转义）。"""
    in_string = False
    escaped = False
    depth = 0
    start = -1
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = index
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:index + 1]
                    start = -1


def parse_tool_calls(text: str, valid_names: Optional[set] = None) -> List[Dict[str, Any]]:
    """从模型回复中解析出工具调用列表。返回 [{"name": ..., "arguments": {...}}, ...]

    需要兼容两种形态：
      1. 带围栏的 ```tool_call ... ```代码块（模型直接输出 markdown 时）；
      2. **无围栏**的 ``tool_call`` 标签 + JSON 对象——这是从 Gemini 网页 DOM
         提取 inner_text 后的常见形态：代码块被渲染成 <pre>，围栏退化为标题文字，
         于是只剩 ``tool_call`` 标签与裸 JSON。
    """
    if not text:
        return []

    calls: List[Dict[str, Any]] = []

    def _consume(raw: str, allow_bare_object: bool) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            entries = data["tool_calls"]
        elif isinstance(data, dict) and isinstance(data.get("tool_uses"), list):
            # DSML / 部分网页版形态：键名是 tool_uses
            entries = data["tool_uses"]
        elif isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            if not allow_bare_object:
                return
            entries = [data]
        else:
            return
        for entry in entries:
            normalized = _normalize_tool_entry(entry)
            if normalized:
                calls.append(normalized)

    for match in _TOOL_CALL_FENCE_RE.finditer(text):
        _consume(match.group(2), allow_bare_object=True)

    if not calls:
        # DSML 风格 XML 包裹的工具调用（网页版偶发输出）
        for match in _DSML_TOOL_RE.finditer(text):
            _consume(match.group(1), allow_bare_object=True)

    if not calls:
        for match in _JSON_FENCE_RE.finditer(text):
            _consume(match.group(1), allow_bare_object=True)

    if not calls and _TOOL_USES_RE.search(text):
        # 标签丢失、只剩 {"tool_uses": [...]} 的裸对象
        for obj in _iter_balanced_objects(text):
            _consume(obj, allow_bare_object=True)
            if calls:
                break

    if not calls:
        # 兜底：无围栏的 "tool_call" 标签 + 平衡 JSON 对象（网页 DOM 提取后的形态）
        marker_re = re.compile(r"tool[-_]?call\b", re.IGNORECASE)
        pos = 0
        while True:
            match = marker_re.search(text, pos)
            if not match:
                break
            segment = text[match.end():]
            parsed = False
            for obj in _iter_balanced_objects(segment):
                _consume(obj, allow_bare_object=True)
                pos = match.end() + segment.index(obj) + len(obj)
                parsed = True
                break
            if not parsed:
                pos = match.end()

    if valid_names:
        calls = [c for c in calls if c.get("name") in valid_names]

    return calls


def to_tool_call_models(calls: List[Dict[str, Any]]) -> List[ToolCall]:
    return [
        ToolCall(
            function=FunctionCall(
                name=call["name"],
                arguments=json.dumps(call["arguments"], ensure_ascii=False),
            )
        )
        for call in calls
    ]
