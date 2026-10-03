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

# 当前注入格式：行首 ``TOOL_CALL:`` 纯文本标记（大小写不敏感）。
# 只认行首（允许前导空白），避免正文里偶然出现的 "TOOL_CALL:" 被误触发；
# 后续 JSON 由 _iter_balanced_objects 从冒号之后开始扫。
_TOOL_CALL_LINE_RE = re.compile(r"^[ \t]*TOOL_CALL\s*:\s*", re.IGNORECASE | re.MULTILINE)
# 仅匹配 "tool_call" / "tool-call" 围栏，避免误伤普通 ```json 代码块。
# 允许围栏被 DOM/引用符号包裹： ``> ```tool_call `` 这类形态也要能识别。
_TOOL_CALL_FENCE_RE = re.compile(r"```\s*(tool[-_]call)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# Gemini 网页版 markdown 渲染会在标识符里插入转义反斜杠：
#   TOOL_CALL -> TOOL\_CALL, exec_command -> exec\_command
# 取回 inner_text 时就带着这些反斜杠。解析前先去掉“反斜杠 + 下划线”的转义，
# 否则行首标记正则匹配不到，整条调用被丢弃。
_MD_ESCAPED_CHAR_RE = re.compile(r"\\([_*`~\[\]()#+.!\-])")
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

# DSML **结构化**形态（Gemini 原生工具 DSL，模型不听指令时会退回这种写法）：
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="bash">
#   <｜｜DSML｜｜ parameter name="command" string="true">cd /tmp && ls</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
#   </｜｜DSML｜｜ calls>
# 竖线数量不固定（DOM 提取后 1~3 个都出现过），标签内允许空白；
# 闭标签还可能缺失/错位（回复被截断），解析时按开标签切块兜底。
_BAR = r"[｜|]{1,4}"
_DSML_INVOKE_OPEN_RE = re.compile(rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\b([^>]*)>", re.IGNORECASE)
_DSML_INVOKE_CLOSE_RE = re.compile(rf"<\s*/\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\s*>", re.IGNORECASE)
_DSML_PARAM_OPEN_RE = re.compile(rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\b([^>]*)>", re.IGNORECASE)
_DSML_PARAM_CLOSE_RE = re.compile(rf"<\s*/\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\s*>", re.IGNORECASE)
# 参数值的 JSON 标量识别（string="true" 时不参与）
_DSML_SCALAR_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null", re.IGNORECASE)
# Gemini 偶尔不用我们的工具名，而用 bash/shell 这类通用名；只有能唯一对应时才映射
_DSML_GENERIC_NAMES = {
    "bash", "sh", "shell", "terminal", "command", "cmd", "exec", "execute",
    "run_command", "run_commands", "execute_command",
}
_SHELL_NAME_KEYWORDS = ("shell", "exec", "bash", "command", "term")


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

    # 统一使用 `TOOL_CALL: {json}` 纯文本行，**不要**用 ```tool_call 代码围栏。
    # 原因：Gemini 网页版会把 markdown 代码围栏渲染成 Code snippet 组件，
    # 取回 DOM 文本时围栏/换行被破坏，tool_call 解析失败；而普通文本行不会被
    # 渲染成代码块，能原样取回。解析侧 _TOOL_CALL_LINE_RE 已把该形态列为首选。
    lines += [
        "",
        "需要调用工具时，只输出一个或多个如下格式的**纯文本行**（不要用代码围栏、"
        "不要加 ```）：",
        "TOOL_CALL: {\"name\": \"工具名\", \"arguments\": {参数对象}}",
        "arguments 必须是合法 JSON：字符串里的双引号必须转义成 \\\"（反斜杠+引号），"
        "不能直接写裸的双引号；",
        "一行一个调用；一次可输出多行以并行调用多个工具；TOOL_CALL 行之外不要输出多余解释。",
        "如果不需要调用任何工具，请直接给出最终回答，不要输出 TOOL_CALL 行。",
    ]
    return "\n".join(lines)


def format_tool_call_emphasis() -> str:
    """新会话 / 重置会话时，放在播种 prompt **开头**的格式强调块。

    新 bucket 没有“示范过正确格式”的历史轮次，模型最容易在这时候
    退回原生 DSML 标记；把带围栏示例的完整格式再点一遍，双保险。
    """
    return "\n".join([
        "[输出格式强调] 这是一个新会话（或刚被重置），以下规则本会话持续有效：",
        "需要调用工具时，只输出如下格式的**纯文本行**（不要用代码围栏、不要加 ```）：",
        "TOOL_CALL: {\"name\": \"工具名\", \"arguments\": {参数对象}}",
        "arguments 必须是合法 JSON：字符串里的双引号必须转义成 \\\"（反斜杠+引号），"
        "不能直接写裸的双引号；否则网页端会把内容当代码渲染、导致参数被截断。",
        "工具名必须逐字使用 [工具调用说明] 中列出的名字，不要自造 bash / shell 之类的通用名。",
        "禁止输出 <｜DSML｜ ...>、<invoke>/<parameter>、<tool_calls> 等 XML/DSL 标记——它们不会被执行。",
    ])


def _dsml_blocks(open_re, close_re, text: str):
    """按 DSML 开标签切出 (属性串, 块体)。

    闭标签缺失/错位（回复被截断、DOM 吞标签）时，退化为取到下一个开标签或文末。
    """
    pos = 0
    while True:
        opened = open_re.search(text, pos)
        if not opened:
            return
        start = opened.end()
        closed = close_re.search(text, start)
        nxt = open_re.search(text, start)
        if closed and (nxt is None or closed.start() < nxt.start()):
            yield opened.group(1), text[start:closed.start()]
            pos = closed.end()
        elif nxt:
            yield opened.group(1), text[start:nxt.start()]
            pos = nxt.start()
        else:
            yield opened.group(1), text[start:]
            return


def _dsml_attr(attrs: str, key: str) -> Optional[str]:
    """从开标签的属性串里取 ``key="value"``。"""
    match = re.search(rf'(?:^|\s){re.escape(key)}\s*=\s*"([^"]*)"', attrs)
    return match.group(1) if match else None


def _dsml_param_value(raw: str, string_attr: Optional[str]) -> Any:
    """DSML parameter 内容 -> Python 值。"""
    value = raw.strip()
    if (string_attr or "").strip().lower() == "true":
        return value
    if value[:1] in "{[" or _DSML_SCALAR_RE.fullmatch(value):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _resolve_dsml_name(name: str, valid_names: Optional[set]) -> str:
    """把 DSML invoke 的工具名对齐到客户端工具名；对不上就原样返回（后续过滤）。"""
    if not name or not valid_names:
        return name
    if name in valid_names:
        return name
    lowered = name.strip().lower()
    for candidate in valid_names:
        if candidate.lower() == lowered:
            return candidate
    if lowered in _DSML_GENERIC_NAMES:
        hits = [c for c in valid_names if any(k in c.lower() for k in _SHELL_NAME_KEYWORDS)]
        if len(hits) == 1:
            return hits[0]
    return name


def _parse_dsml_invokes(text: str, valid_names: Optional[set] = None) -> List[Dict[str, Any]]:
    """解析 DSML 结构化工具调用（invoke/parameter 形态）。"""
    calls: List[Dict[str, Any]] = []
    for attrs, body in _dsml_blocks(_DSML_INVOKE_OPEN_RE, _DSML_INVOKE_CLOSE_RE, text):
        name = _dsml_attr(attrs, "name")
        if not name:
            continue
        arguments: Dict[str, Any] = {}
        for pattrs, pvalue in _dsml_blocks(_DSML_PARAM_OPEN_RE, _DSML_PARAM_CLOSE_RE, body):
            pname = _dsml_attr(pattrs, "name")
            if not pname:
                continue
            arguments[pname] = _dsml_param_value(pvalue, _dsml_attr(pattrs, "string"))
        calls.append({"name": _resolve_dsml_name(name, valid_names), "arguments": arguments})
    return calls


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


def _strip_redundant_value_quotes(raw: str) -> str:
    """折叠字符串值边界上多余的引号：``"cmd": ""git ...""`` -> ``"cmd": "git ..."``。

    模型（尤其 Gemini）常把参数值**又用一对引号包了一层**，或在值首/尾多写一个
    引号。这类输入括号是平衡的，但 JSON 非法；表现就是参数值被从第一个引号处
    截断（解析成空串）或整体解析失败。这里只在**值的开头**（``:`` 之后）和
    **值的结尾**（``,``/``}``/``]`` 之前）各折叠连续引号，正文中的引号不动。
    """
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        # 值开头：冒号后跳过空白，若连续 >=2 个引号则只留一个（保留真正的开引号）
        if ch == ":":
            out.append(ch)
            i += 1
            # 跳过空白
            while i < n and raw[i] in " \t\r\n":
                out.append(raw[i])
                i += 1
            if i < n and raw[i] == '"':
                j = i
                while j < n and raw[j] == '"':
                    j += 1
                if j - i >= 2:
                    # 折叠为单个开引号
                    out.append('"')
                    i = j
                    continue
            continue
        # 值结尾：连续 >=2 个引号且后面是结构符/结尾，只留一个（真正的闭引号）。
        # 注意：若前一个输出字符是反斜杠（转义引号），说明这是正文引号，跳过折叠。
        if ch == '"':
            j = i
            while j < n and raw[j] == '"':
                j += 1
            run = j - i
            k = j
            while k < n and raw[k] in " \t\r\n":
                k += 1
            escaped_prefix = bool(out) and out[-1] == "\\"
            if run >= 2 and (k >= n or raw[k] in ",}]") and not escaped_prefix:
                out.append('"')
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_json_quotes(raw: str) -> Optional[Any]:
    """尽力修复模型输出的非法 JSON。

    两类常见损伤：
      1. 字符串值里未转义的裸引号：
         {"cmd": "git commit -m "Update logic" && git push"}
      2. 值边界多余引号（模型给值又包了一层）：
         {"cmd": ""git status && git log""}
         表现为参数值从第一个引号处被截断、或整体解析失败。

    处理顺序：先折叠边界冗余引号，再用转义状态机修内层裸引号。
    只在首次 json.loads 失败后调用，避免影响合法输入。
    """
    raw = _strip_redundant_value_quotes(raw)
    out = []
    in_string = False
    escaped = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
                escaped = False
            i += 1
            continue
        # 处于字符串内部
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if ch == '"':
            # 向后看第一个非空白字符，判断是否为字符串真结束
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j >= n or raw[j] in ":,}]":
                out.append(ch)
                in_string = False
            else:
                # 正文引号：转义后保留
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    repaired = "".join(out)
    try:
        return json.loads(repaired)
    except Exception:
        return None


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

    需要兼容多种形态（新→旧）：
      0. **首选**：行首 ``TOOL_CALL: {...}`` 纯文本标记（当前注入格式，无尖括号、
         无围栏，模型无法脑补出 ``>`` 造成历史污染）；
      1. 带围栏的 ```tool_call ... ```代码块（模型直接输出 markdown 时）；
      2. **无围栏**的 ``tool_call`` 标签 + JSON 对象——这是从 Gemini 网页 DOM
         提取 inner_text 后的常见形态：代码块被渲染成 <pre>，围栏退化为标题文字，
         于是只剩 ``tool_call`` 标签与裸 JSON。
    """
    if not text:
        return []

    # Gemini 网页版 markdown 渲染会在下划线等字符前插入反斜杠（TOOL\_CALL、
    # exec\_command），取回 inner_text 时带着这些转义。先还原，再解析。
    text = _MD_ESCAPED_CHAR_RE.sub(r"\1", text)

    calls: List[Dict[str, Any]] = []

    def _consume(raw: str, allow_bare_object: bool) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            # 模型常把 shell 命令里的引号原样写进 JSON 字符串（未转义），
            # 标准解析失败；退回尽力修复（见 _repair_json_quotes）。
            data = _repair_json_quotes(raw)
            if data is None:
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

    # 0. 首选形态：行首 TOOL_CALL: 后跟一个平衡 JSON 对象。
    #    用 _iter_balanced_objects 逐对象解析，一行一个，天然支持多调用。
    for match in _TOOL_CALL_LINE_RE.finditer(text):
        segment = text[match.end():]
        objs = list(_iter_balanced_objects(segment))
        if not objs:
            # 平衡扫描失败：多半是值边界多/少了一个引号，导致字符串状态错乱、
            # depth 回不到 0。先用边界引号归一化再扫一遍。
            objs = list(_iter_balanced_objects(_strip_redundant_value_quotes(segment)))
        for obj in objs:
            _consume(obj, allow_bare_object=True)
            break

    if not calls:
        for match in _TOOL_CALL_FENCE_RE.finditer(text):
            _consume(match.group(2), allow_bare_object=True)

    if not calls:
        # DSML 风格 XML 包裹的工具调用（网页版偶发输出）
        for match in _DSML_TOOL_RE.finditer(text):
            _consume(match.group(1), allow_bare_object=True)

    if not calls:
        calls.extend(_parse_dsml_invokes(text, valid_names))

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
        # 兜底：无围栏的 "tool_call" 标签 + 平衡 JSON 对象（网页 DOM 提取后的形态）。
        # Gemini 把 ``` 围栏渲染成 <pre> 后 inner_text 常退化成：
        #   > tool_call            （markdown 引用/渲染残留）
        #   tool_call\n{...}
        #   ｜｜tool_call｜｜\n{...}
        # 因此 marker 与 JSON 之间可能夹着 > 、竖线、空白等噪声，需要跳过它们再找 JSON。
        # 不能用 \b：中文/全角字符（如 丨 ｜）在 Python re 里算 \w，
        # 会让 "tool_call丨" 这种边界匹配失败。改用「后面不是 ASCII 标识符字符」判定。
        marker_re = re.compile(r"tool[-_]?call(?![A-Za-z0-9_])", re.IGNORECASE)
        pos = 0
        while True:
            match = marker_re.search(text, pos)
            if not match:
                break
            segment = text[match.end():]
            # marker 与 JSON 之间可能夹着任意噪声：`">`、`>`、竖线、`` ` ``、
            # "Copy"/"Download" 渲染文字、空白换行……不要逐种枚举，
            # 直接跳到第一个 `{`，从那里起用平衡扫描找 JSON 对象。
            brace = segment.find("{")
            if brace < 0:
                pos = match.end()
                continue
            probe = segment[brace:]
            parsed = False
            for obj in _iter_balanced_objects(probe):
                _consume(obj, allow_bare_object=True)
                pos = match.end() + brace + probe.index(obj) + len(obj)
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
