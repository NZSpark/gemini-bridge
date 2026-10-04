"""Markdown 文档读写修改支持（T9.1–T9.3）。

设计见 ``doc/update.md``：核心是“按行读取 + 围栏扫描 + 行视图”，
后续的定位 / 编辑 / 写回都建立在带围栏掩码的行结构之上。

本模块为纯逻辑，无浏览器 / 网络依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# 围栏：行首最多 3 个空格缩进，≥3 个反引号或波浪号，反引号后不允许再出现反引号（信息串）
_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


@dataclass
class Line:
    """一行文本及其结构标记。``no`` 为 1-based 行号。"""

    no: int
    text: str
    in_fence: bool = False
    lang: str = ""
    indent: int = 0

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass
class Fence:
    """一段围栏代码块（含开/闭围栏行，1-based 闭区间）。"""

    start: int
    end: int
    lang: str
    marker: str  # ``` 或 ~~~
    length: int


@dataclass
class MdDoc:
    """读取后的 Markdown 文档。"""

    path: Optional[Path]
    lines: List[Line]
    newline: str  # "\\n" 或 "\\r\\n"
    trailing_newline: bool
    fences: List[Fence] = field(default_factory=list)

    @property
    def text(self) -> str:
        """还原为字符串（保留原换行风格与尾换行）。"""
        body = self.newline.join(line.text for line in self.lines)
        return body + self.newline if self.trailing_newline and self.lines else body


class MarkdownError(RuntimeError):
    """Markdown 结构错误（如未闭合围栏）。"""


def _detect_newline(raw: str) -> str:
    idx = raw.find("\n")
    if idx > 0 and raw[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def read_md(path) -> MdDoc:
    """按行读入 Markdown，保留换行风格与是否尾换行，并扫描围栏。"""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    return parse_md(raw, path=p)


def parse_md(raw: str, path=None) -> MdDoc:
    """从字符串解析（便于测试）。"""
    newline = _detect_newline(raw)
    # 统一按 \n 切分；记录是否以换行结尾
    if raw == "":
        trailing = False
        parts: List[str] = []
    else:
        trailing = raw.endswith("\n")
        if trailing:
            # 去掉整个行终止符：\r\n 去两字符，\n 去一字符
            body = raw[:-2] if raw.endswith("\r\n") else raw[:-1]
        else:
            body = raw
        # 统一剩余行尾：\r\n 与孤立 \r 都归一为 \n，避免残留 \r
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        parts = body.split("\n")

    lines = [Line(no=i + 1, text=t) for i, t in enumerate(parts)]
    fences = scan_fences(lines)
    _mark_fences(lines, fences)
    return MdDoc(
        path=Path(path) if path is not None else None,
        lines=lines,
        newline=newline,
        trailing_newline=trailing,
        fences=fences,
    )


def scan_fences(lines: List[Line]) -> List[Fence]:
    """逐行状态机扫描围栏代码块，返回 Fence 列表。

    规则（CommonMark 子集）：
    - 开启：行首 ≤3 空格 + ≥3 反引号或波浪号；反引号围栏的信息串不得含反引号。
    - 闭合：同字符、长度 ≥ 开启长度、行首 ≤3 空格、其后只有空白。
    - 4 空格缩进视为缩进代码，不算围栏。
    未闭合的开启围栏会抛 MarkdownError。
    """
    fences: List[Fence] = []
    open_fence: Optional[Tuple[str, int, str, int]] = None  # marker, length, lang, start_no

    for line in lines:
        m = _FENCE_RE.match(line.text)
        if open_fence is None:
            if not m:
                continue
            marker = m.group("fence")
            info = m.group("info").strip()
            char = marker[0]
            if char == "`" and "`" in info:
                continue  # 反引号围栏的信息串不得含反引号
            lang = info.split()[0] if info else ""
            open_fence = (char, len(marker), lang, line.no)
            continue

        # 在围栏内：只寻找闭合
        char, length, lang, start_no = open_fence
        if not m:
            continue
        marker = m.group("fence")
        if marker[0] != char:
            continue
        if len(marker) < length:
            continue
        if m.group("info").strip():
            continue  # 闭合围栏后只能是空白
        fences.append(Fence(start=start_no, end=line.no, lang=lang, marker=char, length=length))
        open_fence = None

    if open_fence is not None:
        char, length, lang, start_no = open_fence
        raise MarkdownError(f"未闭合的围栏：起始于第 {start_no} 行（{char * length}{lang}）")
    return fences


def _mark_fences(lines: List[Line], fences: List[Fence]) -> None:
    """把围栏信息写回每行：in_fence / lang。开/闭围栏行也标记为 in_fence。"""
    for fence in fences:
        for i in range(fence.start - 1, fence.end):
            lines[i].in_fence = True
            lines[i].lang = fence.lang


def text_lines(doc: MdDoc) -> List[Line]:
    """返回每行视图（带 in_fence / lang / indent / 是否空行）。"""
    for line in doc.lines:
        if not line.in_fence:
            line.indent = len(line.text) - len(line.text.lstrip(" "))
    return doc.lines


@dataclass
class Anchor:
    """定位锚点。kind ∈ {line, heading, fence, text}。"""

    kind: str
    start: Optional[int] = None   # line: 起始行号（1-based）
    end: Optional[int] = None     # line: 结束行号（闭区间）
    heading: str = ""             # heading: 标题文本（不含 # 前缀）
    level: Optional[int] = None   # heading: 限定层级
    lang: str = ""                # fence: 语言标签（空=任意）
    index: int = 0                # fence: 第几个（1-based；0 表示按 lang）
    text: str = ""                # text: 唯一片段


class LocateError(MarkdownError):
    """定位失败（未命中或多命中）。"""


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def locate(doc: MdDoc, anchor: Anchor) -> Tuple[int, int]:
    """按锚点定位，返回 1-based 闭区间 (start, end)。

    - line：直接校验范围。
    - heading：从该标题行到下一个同级/更高级标题之前；跳过围栏内的 # 行。
    - fence：按序号或语言标签定位整段围栏（含开/闭行）。
    - text：片段必须唯一命中非围栏行，否则报错并列候选行号。
    """
    lines = text_lines(doc)

    if anchor.kind == "line":
        if anchor.start is None or anchor.end is None:
            raise LocateError("line 锚点需要 start 与 end")
        if anchor.start < 1 or anchor.end > len(lines) or anchor.start > anchor.end:
            raise LocateError(f"行号越界：{anchor.start}-{anchor.end}（共 {len(lines)} 行）")
        return anchor.start, anchor.end

    if anchor.kind == "heading":
        target = anchor.heading.strip()
        hit = None
        for line in lines:
            if line.in_fence:
                continue
            m = _HEADING_RE.match(line.text)
            if not m:
                continue
            level = len(m.group(1))
            text = m.group(2).strip()
            if text == target and (anchor.level is None or level == anchor.level):
                hit = (line.no, level)
                break
        if hit is None:
            raise LocateError(f"未找到标题：{target}")
        start, level = hit
        end = len(lines)
        for line in lines[start:]:
            if line.in_fence:
                continue
            m = _HEADING_RE.match(line.text)
            if m and len(m.group(1)) <= level:
                end = line.no - 1
                break
        while end > start and lines[end - 1].is_blank:
            end -= 1
        return start, end

    if anchor.kind == "fence":
        candidates = [f for f in doc.fences if not anchor.lang or f.lang == anchor.lang]
        if anchor.index:
            if anchor.index > len(candidates):
                raise LocateError(f"没有第 {anchor.index} 个围栏（lang={anchor.lang or '*'!r}）")
            f = candidates[anchor.index - 1]
        else:
            if len(candidates) != 1:
                raise LocateError(
                    f"围栏锚点不唯一：命中 {len(candidates)} 个"
                    + (f"（lang={anchor.lang}）" if anchor.lang else "")
                )
            f = candidates[0]
        return f.start, f.end

    if anchor.kind == "text":
        needle = anchor.text
        if not needle:
            raise LocateError("text 锚点需要非空片段")
        hits = [line.no for line in lines if not line.in_fence and needle in line.text]
        if not hits:
            raise LocateError("片段未命中任何正文行")
        if len(hits) > 1:
            raise LocateError(f"片段多命中，候选行号：{hits}")
        return hits[0], hits[0]

    raise LocateError(f"未知锚点类型：{anchor.kind}")


def apply_edit(doc: MdDoc, start: int, end: int, new_text: str) -> MdDoc:
    """用 new_text 替换 [start, end] 行（1-based 闭区间），区间外原样保留。

    new_text 内部换行按 \n 处理；替换后重新扫描围栏。
    """
    if start < 1 or end > len(doc.lines) or start > end:
        raise MarkdownError(f"行号越界：{start}-{end}（共 {len(doc.lines)} 行）")

    replacement: List[str] = [] if new_text == "" else new_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    before = [line.text for line in doc.lines[: start - 1]]
    after = [line.text for line in doc.lines[end:]]
    merged = before + replacement + after

    lines = [Line(no=i + 1, text=t) for i, t in enumerate(merged)]
    fences = scan_fences(lines)
    _mark_fences(lines, fences)
    return MdDoc(
        path=doc.path,
        lines=lines,
        newline=doc.newline,
        trailing_newline=doc.trailing_newline,
        fences=fences,
    )


def write_md(doc: MdDoc, path=None, dry_run: bool = False) -> str:
    """写回文档；返回统一 diff。

    - ``dry_run=True`` 只返回 diff，不落盘。
    - 落盘用“临时文件 + 原子重命名”，失败不破坏原文件。
    """
    target = Path(path) if path is not None else doc.path
    if target is None:
        raise MarkdownError("write_md 需要 path")

    old = target.read_text(encoding="utf-8") if target.exists() else ""
    new = doc.text
    diff = _unified_diff(old, new, target)

    if dry_run or old == new:
        return diff

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(target)
    return diff


def _unified_diff(old: str, new: str, target) -> str:
    import difflib

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=str(target), tofile=str(target))
    )


@dataclass
class Issue:
    """校验发现的问题。"""

    code: str
    message: str
    line: Optional[int] = None


def verify(doc: MdDoc) -> List[Issue]:
    """校验：围栏配对、语言标签非空保留、围栏计数。

    说明：``parse_md`` 对未闭合围栏已抛错，所以这里主要覆盖
    “编辑后结构是否仍然健康”以及语言标签是否意外丢失。
    """
    issues: List[Issue] = []
    for fence in doc.fences:
        open_line = doc.lines[fence.start - 1]
        if fence.lang and not open_line.text.strip().endswith(fence.lang):
            issues.append(Issue("lang_lost", f"第 {fence.start} 行语言标签疑似丢失", fence.start))
        if fence.end < fence.start:
            issues.append(Issue("bad_range", f"围栏范围非法：{fence.start}-{fence.end}", fence.start))
    return issues


@dataclass
class Backup:
    """一次写回前的快照。"""

    path: Path
    backup_path: Path
    text: str


def _safe_fence(content: str, lang: str = "") -> str:
    """把内容包进围栏；若内容含 ``` 则自动加长外层围栏。"""
    body = content.replace("\r\n", "\n").replace("\r", "\n")
    longest = 0
    for token in re.findall(r"`+", body):
        longest = max(longest, len(token))
    length = max(3, longest + 1)
    marker = "`" * length
    return marker + lang + "\n" + body + "\n" + marker


def backup_md(path, backup_dir="output/backups") -> Backup:
    """写回前把原文件快照到 backup_dir（带时间戳）。"""
    import time

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    bdir = Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    bpath = bdir / f"{p.name}.{ts}.bak"
    bpath.write_text(text, encoding="utf-8")
    return Backup(path=p, backup_path=bpath, text=text)


def rollback_md(backup: Backup) -> None:
    """从快照恢复文件（原子重命名）。"""
    tmp = backup.path.with_name(backup.path.name + ".rollback.tmp")
    tmp.write_text(backup.text, encoding="utf-8")
    tmp.replace(backup.path)


def render_view(doc: MdDoc) -> str:
    """生成给模型的“带行号 + 围栏边界标注”视图。

    围栏起止行显式标注，让模型知道哪些行是代码块内部、不可当结构处理。
    """
    out: List[str] = []
    for line in doc.lines:
        prefix = f"L{line.no}"
        if line.in_fence:
            marker = "[[fence" + (f":{line.lang}" if line.lang else "") + "]]"
            out.append(f"{prefix} {marker} {line.text}")
        else:
            out.append(f"{prefix} {line.text}")
    return "\n".join(out)


def generate_edit(doc: MdDoc, instruction: str, llm) -> Tuple[int, int, str]:
    """让模型基于“带行号视图”产出 (start, end, new_text)。

    ``llm`` 是可调用对象：接收 prompt 字符串，返回模型输出文本。
    解析复用 ``toolcalls.parse_tool_calls`` 的 ``TOOL_CALL`` 通道，
    工具名约定为 ``edit_markdown``，参数为 ``{start, end, new_text}``。

    返回前会校验行号范围，越界即抛 MarkdownError；调用方应据此拒绝写入。
    """
    from .toolcalls import parse_tool_calls

    prompt = _build_edit_prompt(doc, instruction)
    raw = llm(prompt)
    calls = parse_tool_calls(raw, valid_names={"edit_markdown"})
    call = next((c for c in calls if c.get("name") == "edit_markdown"), None)
    if call is None:
        raise MarkdownError("模型未返回 edit_markdown 工具调用")

    args = call.get("arguments") or {}
    try:
        start = int(args["start"])
        end = int(args["end"])
    except (KeyError, TypeError, ValueError):
        raise MarkdownError("edit_markdown 参数缺少合法的 start/end")
    new_text = args.get("new_text", "")
    if not isinstance(new_text, str):
        raise MarkdownError("edit_markdown 的 new_text 必须是字符串")

    total = len(doc.lines)
    if start < 1 or end > total or start > end:
        raise MarkdownError(f"模型返回的行号越界：{start}-{end}（共 {total} 行）")
    return start, end, new_text


def _build_edit_prompt(doc: MdDoc, instruction: str) -> str:
    tools_doc = (
        "可用工具：edit_markdown\n"
        "调用格式（一行，严格 JSON）：\n"
        'TOOL_CALL: {"name": "edit_markdown", "arguments": {"start": <int>, "end": <int>, "new_text": "<替换内容>"}}\n'
        "start/end 为 1-based 闭区间行号，必须落在下面视图的行号范围内。\n"
        "代码围栏（[[fence...]] 标记的行）内部不要做结构改动，除非明确要求。"
    )
    return (
        f"{instruction}\n\n"
        f"{tools_doc}\n\n"
        f"--- 文档视图（每行前缀 L<行号>）---\n{render_view(doc)}\n--- 视图结束 ---"
    )
