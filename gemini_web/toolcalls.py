"""工具调用（function calling）桥接层。

Gemini 网页版并不原生支持 OpenAI 的 function calling，因此这里采用
“提示词注入 + 结构化解析”的方式模拟：
  1. 把客户端传来的 tools 描述注入到 prompt，要求模型用「`TOOL_CALL:` 标记行 +
     ```tool_call 代码块」回话；
  2. 解析模型输出里的标记与代码块，还原为 OpenAI 的 tool_calls；
  3. 下一轮请求里 role=tool 的执行结果再拼回 prompt 喂给网页版。

载体（carrier）为什么是「纯文本标记行 + 代码围栏」的混合形态：

* 纯文本 ``TOOL_CALL: {...}`` 一行装全部内容会被网页版当 markdown **段落**渲染——
  反斜杠转义被消费、连续空白被折叠，JSON 直接不再合法；
* 只用 ```tool_call 围栏也不够：Gemini 把代码块渲染成 code-snippet 组件，**围栏与
  info string 都不会进 innerText**（DOM 里只剩 UI 标题 ``Code snippet``），
  于是回复里连一个可识别的标记都没有，整条调用被静默丢弃。

所以：**标记行只负责“可识别”（载有无载荷，弄坏也没关系），围栏只负责“逐字节保真”**
（载有全部载荷，不需要它携带任何标记）。真机对照与兼容矩阵见 ``doc/code_block_fence.md``；
纯文本行 + JSON 的历史一体化载体仍在解析侧兼容，但不再被注入。
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .models import FunctionCall, ToolCall

logger = logging.getLogger(__name__)

# ``TOOL_CALL:`` 标记行（大小写不敏感）。两重用途：
#   * 当前注入载体的**标记行**（见 parse_tool_calls 分支 1）：后面跟的往往不是同一个
#     段落里的 JSON，而是网页版渲染后的 ``Code snippet`` 标题 + 围栏内容——直接对它
#     后面那段文字做平衡扫描就能拿到 JSON；
#   * 历史一体化载体（``TOOL_CALL: {json}`` 全写一行）的兼容路径。
# 只认行首（允许前导空白），避免正文里偶然出现的 "TOOL_CALL:" 被误触发；
# 后续 JSON 由 _iter_balanced_objects 从冒号之后开始扫。
_TOOL_CALL_LINE_RE = re.compile(r"^[ \t]*TOOL_CALL\s*:\s*", re.IGNORECASE | re.MULTILINE)
# ```tool_call 代码围栏（围栏内一条 JSON）。这是注入载体里**承载载荷**的那一半。
# 它只在“围栏没有被网页版渲染掉”时命中（客户端把回复原样贴回、或别的 DOM 形态）；
# 被渲染掉时靠标记行 + 裸标签兑底两条路径接手（见 doc/code_block_fence.md §4）。
#
# 只匹配 "tool_call" / "tool-call"，**刻意不认** ```json：客户端与模型的正常对话里
# 经常展示 JSON 代码块，只要其中恰好有 name/arguments 就会被当调用执行——模型可控
# 文本触发本机命令，这是不可接受的安全面，因此 info string 必须唯一。
# 允许围栏被 DOM/引用符号包裹： ``> ```tool_call `` 这类形态也要能识别。
_TOOL_CALL_FENCE_RE = re.compile(
    r"```[ \t]*(tool[-_]call)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE
)
# 诊断用：回复里是否出现了任何一种工具调用载体标记（含被渲染成裸标签的形态）。
_CARRIER_MARKER_RE = re.compile(r"tool[_\- ]?call", re.IGNORECASE)
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


# ==================== 内置工具：edit_markdown ====================
# 桥接层内置的 Markdown 锚点编辑工具。模型只需给出行号区间与新文本，
# 桥接层用 markdown_io 做围栏安全的定位/校验/保真写回，避免整段文本匹配。
EDIT_MARKDOWN_TOOL_NAME = "edit_markdown"

EDIT_MARKDOWN_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EDIT_MARKDOWN_TOOL_NAME,
        "description": (
            "按行号区间编辑本地 Markdown 文件：保留围栏代码块结构，"
            "只替换 [start, end] 行，区间外字节级保真。默认只返回 diff（dry-run），"
            "传 write=true 才落盘，落盘前自动备份。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "目标 Markdown 文件路径：必须是工作区内的相对路径，"
                        "不得使用 .. 或指向工作区之外的绝对路径"
                    ),
                },
                "start": {"type": "integer", "description": "起始行号（1-based，闭区间）"},
                "end": {"type": "integer", "description": "结束行号（1-based，闭区间）"},
                "new_text": {"type": "string", "description": "替换 [start, end] 的新文本"},
                "write": {
                    "type": "boolean",
                    "description": "true 才落盘；默认 false 仅返回 diff",
                },
            },
            "required": ["path", "start", "end", "new_text"],
        },
    },
}

# 所有可由桥接层本地执行的内置工具。供响应层按需注入/执行。
BUILTIN_TOOLS: List[Dict[str, Any]] = [EDIT_MARKDOWN_TOOL]


def builtin_tool_names() -> set:
    return {t["function"]["name"] for t in BUILTIN_TOOLS}


def should_register_edit_markdown(client_tools: Optional[List[Dict[str, Any]]]) -> bool:
    """本轮是否要把内置 ``edit_markdown`` 追加进工具列表。

    默认**只在客户端已经声明了工具**时才注入。原因（实测，见 doc/update.md P1-5）：
    无条件注入会给每个请求额外附带约 970 tokens 的脚手架（工具说明 + 格式强调 +
    edit_markdown 说明），既挤占网页会话预算，又可能让客户端收到自己从未声明过的
    ``tool_calls``（`finish_reason=tool_calls`）。

    需要恢复旧行为时设 ``EDIT_MARKDOWN_ALWAYS_REGISTER=true``。
    """
    if not config.EDIT_MARKDOWN_LOCAL:
        return False
    if EDIT_MARKDOWN_TOOL_NAME in _tool_names(client_tools):
        return False
    if config.EDIT_MARKDOWN_ALWAYS_REGISTER:
        return True
    return bool(client_tools)


def edit_markdown_spec() -> str:
    """Usage notes for edit_markdown injected into the prompt (anchors / fences caveats)."""
    example = json.dumps(
        {
            "name": "edit_markdown",
            "arguments": {
                "path": "README.md",
                "start": "<int>",
                "end": "<int>",
                "new_text": "<replacement text>",
            },
        },
        ensure_ascii=False,
    )
    return "\n".join([
        "[edit_markdown notes]",
        "When editing a Markdown file, prefer edit_markdown over rewriting the whole file and doing plain-text matching:",
        "TOOL_CALL:",
        "```tool_call",
        example,
        "```",
        "start/end are 1-based inclusive line numbers; content outside the range (including blank lines, indentation, trailing whitespace) is preserved verbatim.",
        "Do not touch ``` fence lines; content inside a fence does not participate in structural positioning.",
        "By default only a diff is returned; once confirmed, pass write=true to persist to disk.",
    ])


def _resolve_edit_path(raw_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """把 edit_markdown 的目标路径限制在工作区根内（T8.9）。

    工作区根 = ``config.EDIT_MARKDOWN_ROOT``（留空时默认为项目根目录）。
    路径先 ``resolve()`` 再校验归属，因此以下情形一律拒绝：

    * 相对路径里用 ``..`` 逃出根目录；
    * 绝对路径指向根目录之外（如 ``/etc/passwd``）；
    * 经软链接跳出根目录的路径。

    返回 ``(绝对路径, None)`` 或 ``(None, 错误信息)``，错误直接作为工具结果回传，
    不抛给上层（与其它结构错误一致）。
    """
    root = Path(config.EDIT_MARKDOWN_ROOT or config.PROJECT_ROOT).resolve()
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        return None, (
            f"路径越界：{raw_path!r} 不在工作区根 {root} 内。"
            "edit_markdown 只允许编辑工作区内的文件；如需放宽，请调整 EDIT_MARKDOWN_ROOT。"
        )
    return resolved, None


def execute_edit_markdown(args: Dict[str, Any], *, backup_dir: str = "output/backups") -> Dict[str, Any]:
    """桥接层本地执行 edit_markdown。返回可直接回传的结构化结果。

    - 默认 dry-run：只返回统一 diff，不落盘。
    - write=true 时先备份原文件，再原子写回。
    - 路径必须落在工作区根内（``EDIT_MARKDOWN_ROOT``，见 ``_resolve_edit_path``）。
    - 任何结构性错误（围栏不配对、行号越界、路径越界）都作为 error 返回，不抛给上层。
    """
    from . import markdown_io

    path = args.get("path")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "edit_markdown 需要 path"}
    resolved_path, path_error = _resolve_edit_path(path)
    if path_error:
        logger.warning("edit_markdown 路径被拒绝：%s", path)
        return {"ok": False, "error": path_error}
    # 读写 / 备份 / 回传统一用解析后的绝对路径，避免“校验时解析、落盘时又换一份”
    path = str(resolved_path)
    try:
        start = int(args["start"])
        end = int(args["end"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "edit_markdown 需要合法的 start/end"}
    new_text = args.get("new_text", "")
    if not isinstance(new_text, str):
        return {"ok": False, "error": "edit_markdown 的 new_text 必须是字符串"}
    do_write = bool(args.get("write", False))

    try:
        doc = markdown_io.read_md(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"文件不存在：{path}"}
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        # 目标是目录 / 权限不足 / 编码错误等：一律作为结构化错误回传，不抛给上层
        return {"ok": False, "error": f"读取失败：{exc}"}

    total = len(doc.lines)
    if start < 1 or end > total or start > end:
        return {"ok": False, "error": f"行号越界：{start}-{end}（共 {total} 行）"}

    try:
        edited = markdown_io.apply_edit(doc, start, end, new_text)
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    issues = markdown_io.verify(edited)
    if issues:
        return {
            "ok": False,
            "error": "编辑后结构校验未通过",
            "issues": [{"code": i.code, "message": i.message, "line": i.line} for i in issues],
        }

    backup_path = None
    if do_write:
        try:
            backup = markdown_io.backup_md(path, backup_dir=backup_dir)
            backup_path = str(backup.backup_path)
        except OSError as exc:
            return {"ok": False, "error": f"备份失败：{exc}"}

    diff = markdown_io.write_md(edited, path=path, dry_run=not do_write)
    return {
        "ok": True,
        "path": path,
        "start": start,
        "end": end,
        "written": do_write,
        "backup": backup_path,
        "diff": diff,
    }


def format_tools_instruction(tools: List[Dict[str, Any]]) -> str:
    """把 OpenAI tools 描述转换成注入网页版的自然语言指令。"""
    lines = [
        "[Tool Calling Instructions]",
        "You can call the following tools to complete the task (valid for this turn):",
    ]
    for tool in tools:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        lines.append(f"- {name}: {desc}")
        if params:
            lines.append(f"  parameters (JSON Schema): {json.dumps(params, ensure_ascii=False)}")

    # 载体：```tool_call 代码围栏（围栏内一条 JSON）。**不要**改回纯文本行：
    # 网页版会把它当 markdown 段落渲染——吃掉反斜杠转义、折叠缩进，长行还会丢收尾
    # 花括号，整条调用被静默丢弃（机制与真机对照见 doc/code_block_fence.md）。
    lines += [
        "",
        "To call a tool, output a plain-text marker line, then a fenced code block whose info "
        "string is exactly `tool_call`, containing ONE JSON object and nothing else:",
        "TOOL_CALL:",
        "```tool_call",
        '{"name": "TOOL_NAME", "arguments": {"ARG_NAME": "ARG_VALUE"}}',
        "```",
        "Rules:",
        "- Output BOTH parts, in this order: the marker line must be exactly `TOOL_CALL:` on its "
        "own line, and the fence label must be exactly `tool_call` (not json / text / empty). "
        "The JSON must live inside the fence. A call written as a single plain-text line "
        "(e.g. `TOOL_CALL: {...}` all on one line) will NOT be executed: the web UI eats its "
        "backslash escapes there (and may collapse indentation), so the JSON stops being valid.",
        "- Inside JSON strings, escape double quotes as \\\" and newlines as \\n; keep the JSON on one "
        "line inside the fence.",
        "- Paste command/script text into the JSON string verbatim - the fenced block preserves it. "
        "As a supplementary precaution, prefer single quotes inside shell commands "
        "(e.g. git commit -m 'msg'), so the command needs no escaped double quote at all.",
        "- You may output several fenced blocks to call several tools in parallel; do not output "
        "extra explanation outside the fenced blocks.",
        "If you do not need to call any tool, just give the final answer directly; do not output a fence.",
    ]
    return "\n".join(lines)


def format_tool_call_emphasis() -> str:
    """Format-emphasis block placed at the start of the seed prompt for a new / reset session.

    A new bucket has no history turns that demonstrate the correct format, so the model is
    most likely to fall back to native DSML markers at that point; repeating the full format
    is a second safeguard.
    """
    return "\n".join([
        "[Output Format Emphasis] This is a new session (or one that was just reset); the following rules stay in effect for this whole session:",
        "To call a tool, output a plain-text marker line, then a fenced code block whose info string is exactly `tool_call`, holding ONE JSON object:",
        "TOOL_CALL:",
        "```tool_call",
        '{"name": "TOOL_NAME", "arguments": {"ARG_NAME": "ARG_VALUE"}}',
        "```",
        "Output BOTH parts in this order (marker line first, then the fence); the marker line must "
        "be exactly `TOOL_CALL:` and the fence label must be exactly `tool_call`. The JSON must "
        "live inside the fence: a call written as a single plain-text line (e.g. `TOOL_CALL: {...}`) "
        "will NOT be executed, because the web UI eats its backslash escapes there.",
        "Inside JSON strings, escape double quotes as \\\" and newlines as \\n, and keep the JSON on one "
        "line inside the fence. Paste command text verbatim - the fenced block preserves it.",
        "As a supplementary precaution, prefer single quotes inside shell commands (e.g. git commit -m 'msg').",
        "Use the tool names exactly as listed in [Tool Calling Instructions]; do not invent generic names like bash / shell.",
        "Do not output XML/DSL markers such as <｜DSML｜ ...>, <invoke>/<parameter>, <tool_calls> - they will not be executed.",
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


def _escape_control_chars_in_strings(raw: str) -> str:
    """把 JSON 字符串**内部**的裸控制字符转义（`\\n`/`\\r`/`\\t` 等）。

    JSON 规范禁止字符串字面量里出现未转义的控制字符。模型写多行 shell
    命令（如 heredoc）时，常把真实换行直接写进值里，导致：
        json.loads: Invalid control character at ...
    这类损伤**没有歧义**——字符串内的裸控制字符一律转义即可，是安全修复。

    逐字符扫描，用 `in_string`/`escaped` 跟踪状态，只在字符串内部替换。
    """
    out = []
    in_string = False
    escaped = False
    for ch in raw:
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            out.append(ch)
            in_string = False
            continue
        # 字符串内部：裸控制字符转义
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


_NAME_ARG_COMMAND_RE = re.compile(
    r'^\s*{\s*"name"\s*:\s*"(?P<name>[^"\\]+)"\s*,\s*"arguments"\s*:\s*{\s*"(?P<argkey>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"',
    re.DOTALL,
)

# 前面已完整闭合的字符串参数对，形如 `"path": "README.md",`。
_STRING_ARG_PAIR_RE = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"(?P<val>(?:[^"\\]|\\.)*)"\s*,')


def _parse_complete_string_args(raw: str):
    """Parse leading ``"key": "value",`` pairs; return None if anything is off.

    Used by :func:`_salvage_string_args` to recover the well-formed arguments
    that precede an object's broken final string value.
    """
    text = raw.strip()
    if text.endswith(","):
        text = text[:-1].rstrip()
    if not text:
        return {}
    out = {}
    pos = 0
    while pos < len(text):
        m = _STRING_ARG_PAIR_RE.match(text, pos)
        if not m:
            return None
        try:
            out[m.group("key")] = json.loads('"' + m.group("val") + '"')
        except Exception:
            return None
        pos = m.end()
    return out


def _salvage_string_args(raw: str):
    """Salvage objects shaped like {"name": X, "arguments": {"<key>": "<body>"[, ...]}}.

    When a value string contains raw newlines or unescaped inner quotes (the
    common case for markdown / shell content) that defeat JSON repair, locate
    structurally: anchor ``name`` and the leading string-valued argument key(s)
    with a regex, take everything from that key's opening quote up to the
    object-closing quote before ``}}``, and re-serialize. Only objects whose
    preceding arguments are all well-formed quoted strings are accepted, which
    keeps multi-key shapes like the ``write`` tool (path + content) working
    while refusing to guess at nested/array values.
    """
    m = _NAME_ARG_COMMAND_RE.match(raw)
    if not m:
        return None
    name = m.group("name")
    argkey = m.group("argkey")
    body_start = m.end()  # first char of the anchored key's value
    # Prefix covers any fully-quoted args before the anchored key; it starts
    # right after the `{` that opens the arguments object (the one immediately
    # preceding the anchored key), so the slice holds `"path": "...",` pairs.
    brace = raw.rfind("{", 0, body_start)
    if brace < 0:
        return None
    head = brace + 1
    stripped = raw.rstrip()
    if not stripped.endswith("}}"):
        return None
    close = stripped.rindex('"')
    if close < body_start:
        return None

    # The prefix ends with the anchored key's own `"<argkey>": "` fragment
    # (the regex consumed up to its opening quote). Drop that trailing fragment
    # so only complete preceding `"key": "value",` pairs remain.
    prefix = raw[head:body_start]
    anchor_frag = '"' + argkey + '"'
    anchor_pos = prefix.rfind(anchor_frag)
    if anchor_pos >= 0:
        prefix = prefix[:anchor_pos]
    prefix_args = _parse_complete_string_args(prefix)
    if prefix_args is None:
        return None

    value = raw[body_start:close]
    try:
        value = json.loads('"' + value + '"')
    except Exception:
        escaped = value.replace("\\", "\\\\")
        escaped = escaped.replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        try:
            value = json.loads('"' + escaped + '"')
        except Exception:
            return None
    prefix_args[argkey] = value
    return {"name": name, "arguments": prefix_args}


def _repair_json_quotes(raw: str) -> Optional[Any]:
    """尽力修复模型输出的非法 JSON。

    常见损伤：
      1. 字符串值里未转义的裸引号：
         {"cmd": "git commit -m "Update logic" && git push"}
      2. 值边界多余引号（模型给值又包了一层）：
         {"cmd": ""git status && git log""}
         表现为参数值从第一个引号处被截断、或整体解析失败。
      3. 值内含嵌套的 shell 双引号，且末尾闭引号看似"丢失"：
         {"cmd": "git commit -m "msg"}
         逐字符启发式会把它当"字符串结束"而截断值、丢掉尾引号。
      4. 值内有裸控制字符（多行命令/heredoc 的真实换行）：
         json.loads: Invalid control character at ...
         这类无歧义，优先修复。

    处理顺序：先折叠边界冗余引号、转义字符串内控制字符，再用逐字符
    状态机修内层裸引号。只在首次 json.loads 失败后调用。
    """
    raw = _strip_redundant_value_quotes(raw)

    # 无歧义修复优先：字符串内裸控制字符（多行命令的真实换行等）。
    # 很多长指令只因这一项就无法解析，单独先试一次。
    ctrl_fixed = _escape_control_chars_in_strings(raw)
    if ctrl_fixed != raw:
        try:
            return json.loads(ctrl_fixed)
        except Exception:
            raw = ctrl_fixed  # 控制字符已修，继续尝试引号修复

    # 说明：曾尝试用"结构定位"重写值内嵌套引号，但 JSON 值内嵌 shell 双引号
    # 本质有歧义（无法区分"值的边界引号"与"正文引号"），实验版本会产出"合法
    # 但错误"的截断命令。改为在 prompt 层要求命令内部用单引号从源头消除歧义，
    # 解析层只保留确定性修复 + 下方护栏。
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
            if j >= n or raw[j] in ":,":
                out.append(ch)
                in_string = False
            elif raw[j] in "}]":
                # 引号后是 } / ] 时再看一层：值真结束时，} / ] 之后必然是
                # 结构符（, } ]）或输入结束；若是正文引号（如
                # `[contenteditable="true"]` 中 true 后面那个 `"`），紧跟的
                # 是正文字符，不能当作字符串结束——否则字符串被提前闭合、
                # 后续修复失败，整条工具调用被丢弃。
                k = j + 1
                while k < n and raw[k] in " \t\r\n":
                    k += 1
                if k >= n or raw[k] in ",}]":
                    out.append(ch)
                    in_string = False
                else:
                    out.append('\\"')
            else:
                # 正文引号：转义后保留
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    repaired = "".join(out)
    # 引号修复后字符串体内可能仍留着裸控制字符（多行命令的真实换行）：
    # 逐字符修复阶段不会转义它们，这里补一次，否则最终 json.loads 仍会报
    # "Invalid control character" 而返回 None、整条调用被丢弃。
    repaired = _escape_control_chars_in_strings(repaired)
    try:
        return json.loads(repaired)
    except Exception:
        return None


def _shell_quotes_balanced(cmd: str) -> bool:
    """粗判 shell 命令里的双引号是否成对（忽略 \\" 转义引号）。

    解析出的命令若引号不配对，几乎必然是 JSON 修复阶段把值截断/丢尾引号，
    直接发给 shell 只会得到 `unexpected EOF`。宁可在桥接层拦下，让模型重出。
    """
    count = 0
    i = 0
    n = len(cmd)
    while i < n:
        if cmd[i] == "\\":
            i += 2
            continue
        if cmd[i] == '"':
            count += 1
        i += 1
    return count % 2 == 0


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
      0. ```tool_call 代码围栏（注入载体中**承载载荷**的那一半；围栏没被渲染掉时的主路径）；
      1. ``TOOL_CALL:`` 标记行 + 其后第一个平衡 JSON 对象——**Gemini 渲染后的主路径**：
         围栏与 info string 都不会进 innerText，DOM 里只剩标记行、UI 标题 ``Code snippet``
         和保真保留的 JSON 本体，于是“标记忆号行的段”里扫出 JSON 就是调用。
         历史一体化载体（``TOOL_CALL: {json}`` 全写一行）也走同一条分支；
      2. **无围栏**的 ``tool_call`` 标签 + JSON 对象（另一些 DOM/引用形态下的兑底）；
      3. DSML / ```json 兑底（网页版偶发输出 / 历史数据）。
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
                salvaged = _salvage_string_args(raw)
                if salvaged is not None:
                    calls.append(salvaged)
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

    # 0. 首选载体：```tool_call 围栏（当前注入格式，未渲染时的主路径）。
    #    每个围栏一条调用，多个围栏＝并行多调用。围栏内不是单个 JSON（几个对象
    #    并列 / 夹着说明文字）时再退到平衡扫描，避免把合法单对象重复消费成两条。
    for match in _TOOL_CALL_FENCE_RE.finditer(text):
        before = len(calls)
        _consume(match.group(2), allow_bare_object=True)
        if len(calls) == before:
            for obj in _iter_balanced_objects(match.group(2)):
                _consume(obj, allow_bare_object=True)

    # 1. TOOL_CALL: 标记行 + 其后第一个平衡 JSON 对象。
    #
    #    这是 Gemini 渲染后**最常用**的一条：标记行之后跟的不是同一段落的 JSON，
    #    而是被渲染成代码块的内容（DOM 里表现为 ``Code snippet`` 标题 + 原样 JSON）；
    #    平衡扫描会跳过中间那些非 JSON 文字，直接拿到对象。
    #
    #    契约：**每个 TOOL_CALL: 标记只取其后第一个平衡 JSON 对象；一行一调用；
    #    多个调用必须写成多行**。同行第二个对象会被丢弃（这是有意为之——
    #    避免把标记之后的无关 {..} 当成调用吞进来）。
    #
    #    segment 必须截到**下一个 TOOL_CALL 标记之前**：否则某个标记后面若没跟
    #    对象（模型写了标记又改主意），它会把下一个标记的对象当成自己的消费掉，
    #    轮到下一个标记时又消费同一个对象 → 同一次调用重复出现两次。
    matches = list(_TOOL_CALL_LINE_RE.finditer(text)) if not calls else []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():next_start]
        objs = list(_iter_balanced_objects(segment))
        if not objs:
            # 平衡扫描失败：多半是值边界多/少了一个引号，导致字符串状态错乱、
            # depth 回不到 0。先用边界引号归一化再扫一遍。
            objs = list(_iter_balanced_objects(_strip_redundant_value_quotes(segment)))
        if not objs:
            # 值内出现裸 `{`（如 markdown 里的 {"a":1}）会让字符串状态提前错乱，
            # 平衡扫描切不出完整对象。改用锚点式 salvage：按 name/arguments/首个
            # 键定位，一直取到对象收尾，绕开括号配对。
            salvaged = _salvage_string_args(segment)
            if salvaged is not None:
                calls.append(salvaged)
                continue
        for obj in objs:
            _consume(obj, allow_bare_object=True)
            break

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

    # 可观测性：回复里分明出现了载体标记/围栏（``tool_call`` / ``TOOL_CALL:``），却一条
    # 可用调用都没交出去。以前这条路径**完全静默**——客户端只收到一段纯文本就当任务
    # 结束，日志也没线索。这里至少留下一行 warning 便于事后定位（截断 / 格式违规 /
    # 工具名不在客户端 tools 里）。
    if not calls and _CARRIER_MARKER_RE.search(text):
        logger.warning(
            "回复含工具调用标记/围栏，但未产出任何可用调用（截断、格式违规或工具名不合法）：%s",
            text[:200].replace("\n", "\\n"),
        )

    # 护栏：若传入了 valid_names，则过滤掉不在其中的幻觉工具名；否则保留全部解析出的工具调用。
    if valid_names is not None:
        calls = [c for c in calls if c.get("name") in valid_names]

    # 护栏：shell 类命令若双引号不配对，几乎必然是解析阶段把值截断/丢尾引号。
    # 这类命令发给 shell 只会得到 `unexpected EOF`，宁可在桥接层丢弃，
    # 让模型下一轮重新输出完整命令。
    calls = [c for c in calls if _call_args_sane(c)]

    return calls


def _call_args_sane(call: Dict[str, Any]) -> bool:
    """对 shell 类调用做最低限度健全性检查（当前：命令引号配对）。"""
    name = (call.get("name") or "").lower()
    if not any(k in name for k in _SHELL_NAME_KEYWORDS):
        return True
    args = call.get("arguments")
    if not isinstance(args, dict):
        return True
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and not _shell_quotes_balanced(value):
            return False
    return True


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
