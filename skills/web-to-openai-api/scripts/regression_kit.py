#!/usr/bin/env python3
"""假 page/driver 回归工具箱：让「事故档案」里的每一条反模式都能**不联网、不启浏览器**地验证。

配套文件是 `references/regression-templates.md`（25 条可直接粘贴的用例）。分工：

- **本文件**提供假件（fake clock / page / composer / pool / watcher / metrics）+ 每条反模式
  **修好之后的判据**（参考实现）。这些判据就是「可观察判据」那几列的可执行版本。
- **模板文件**提供 `def test_*()` 用例，只依赖本文件与标准库，可直接粘进 `tests/`。

两个设计点决定了它有用而不是「又一份文档」：

1. **旧行为可以换回来**：环境变量 `TEMPLATE_VARIANT=<bug_id>` 会把某条反模式的旧代码路径
   打开。此时对应用例**必须变红**——这就是「区分力实验」的机械化版本，不再靠人回忆。
   `TEMPLATE_VARIANT=all` 打开全部旧行为（用于一次性证明整组用例都不是空断言）。
2. **只依赖标准库**（Python ≥3.9），所以 `scripts/check_templates.py` 与 `scripts/gate.py` 可以
   在没有 pytest 的机器上直接跑它；用例写成 `test_*` 函数，放进 pytest 也照跑。

用法::

    cp regression_kit.py tests/            # 与你的用例放同一目录（或调 import）
    TEMPLATE_VARIANT=fixed python3 check_templates.py          # 期望全绿
    TEMPLATE_VARIANT=p0a   python3 check_templates.py          # 期望且仅 p0a 用例变红

接进你自己的项目时，把下面这些函数换成本项目真实实现里的符号（对照
`references/regression-templates.md` 第 3 节「接入你自己的实现」）；
`check_templates.py` 只用 BUG_TESTS 做覆盖与区分力核对。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

__all__ = [
    "BUG_TESTS",
    "CHALLENGE_PATTERNS",
    "CAP_PATTERNS",
    "ComposerError",
    "ComposerNotFound",
    "FakeClock",
    "FakeComposer",
    "FakeConversation",
    "FakeLock",
    "FakeMetrics",
    "FakePage",
    "FakePagePool",
    "FakeWatcher",
    "IdleClock",
    "PageRegistry",
    "Record",
    "ReplyStore",
    "SessionState",
    "UnknownFieldError",
    "UnrecoverableSubmission",
    "VARIANT",
    "cancel_and_release",
    "cap_notice_hit",
    "check_acceptance_markers",
    "classify_failure",
    "describe_usage",
    "find_dead_keys",
    "is_challenged",
    "is_done",
    "is_generating",
    "is_page_lost",
    "is_retryable",
    "make_registry",
    "parse_tool_calls",
    "pick_composer",
    "pick_new_reply",
    "probe_ok",
    "reconcile_after_goto",
    "recover_after_ambiguous_submit",
    "same_conversation",
    "should_seed",
    "system_prompt",
    "text_without_subtrees",
    "write_prompt",
]

# ---------------------------------------------------------------- 变异开关


VARIANT = os.environ.get("TEMPLATE_VARIANT", "fixed")


def _bug(bug_id: str) -> bool:
    """True = 把这条反模式的**旧行为**换回来（区分力实验：对应用例必须变红）。"""
    return VARIANT in ("all", bug_id)


# 反模式 → 对应用例名。check_templates.py 用它核对「事故档案是否每条都有模板」以及
# 「把旧行为换回来后是否**只有**对应用例变红」。新增反模式时必须同时加进来，否则门禁报缺。
BUG_TESTS = {
    "p0a": "test_p0a_echo_is_never_a_reply",
    "p0b": "test_p0b_persist_callable_on_instance_and_class",
    "p0c": "test_p0c_reap_skips_bucket_with_inflight_request",
    "p0d": "test_p0d_dead_page_is_rebuilt_on_same_session",
    "p0e": "test_p0e_landing_url_decides_has_history",
    "p0f": "test_p0f_cap_notice_ignores_quoted_transcript",
    "p0g": "test_p0g_decorative_stop_class_is_not_generating",
    "p0h": "test_p0h_fenced_tool_call_survives_rendering",
    "p0i": "test_p0i_system_prompt_never_negates_capabilities",
    "a01": "test_a01_seed_flag_is_bound_to_live_page",
    "a02": "test_a02_ambiguous_submit_never_resends",
    "a03": "test_a03_unrecoverable_error_is_not_retried",
    "a04": "test_a04_challenge_needs_dead_composer",
    "a05": "test_a05_probe_requires_navigation_ok",
    "a06": "test_a06_usage_limit_popup_is_attributed",
    "a07": "test_a07_dead_config_keys_are_found",
    "a08": "test_a08_subtree_text_removed_everywhere",
    "a09": "test_a09_lossy_readback_does_not_veto_fill",
    "a10": "test_a10_unknown_usage_field_is_rejected",
    "a11": "test_a11_zombie_composer_is_skipped",
    "a12": "test_a12_conversation_url_exact_match",
    "a13": "test_a13_registry_is_not_a_process_singleton",
    "a14": "test_a14_acceptance_marks_unverified_items",
    "a15": "test_a15_idle_window_is_seconds_not_ticks",
    "a16": "test_a16_cancel_does_not_await_watcher",
}


# ---------------------------------------------------------------- 异常


class ComposerError(RuntimeError):
    """输入区写入失败（真的没写进去，不是读回有损）。"""


class ComposerNotFound(LookupError):
    """候选全枚举后仍没有可写入的输入区 → 502 composer_not_found（可恢复）。"""


class UnrecoverableSubmission(RuntimeError):
    """提交无法确认且没有安全恢复路径：明确失败，不冒险重发。"""


class UnknownFieldError(LookupError):
    """引用了不存在的字段（照抄建议前的核实动作）。"""


# ---------------------------------------------------------------- 时钟


class FakeClock:
    """只有秒，没有「轮询次数」——阈值单位一旦是次数，调快轮询就会等比削弱保护（A15）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class IdleClock:
    """「内容静默窗口」按秒判定：记录内容最后一次变化的时刻。"""

    def __init__(self, clock: FakeClock, idle_s: float = 3.0) -> None:
        self.clock = clock
        self.idle_s = idle_s
        self.last_change: float | None = None
        self.ticks = 0
        self._last_text: str | None = None

    def tick(self, text: str) -> IdleClock:
        self.ticks += 1
        if text != self._last_text:
            self._last_text = text
            self.last_change = self.clock.now()
        return self

    def stalled(self) -> bool:
        if _bug("a15"):
            # 反模式：把 idle_s 当「轮询次数」用 → 轮询一调快就提前判结束，读到半截
            return self.ticks >= self.idle_s
        if self.last_change is None:
            return False
        return (self.clock.now() - self.last_change) >= self.idle_s


# ---------------------------------------------------------------- 假页面 / 假输入区


class FakePage:
    """最小页面替身：url / 存活状态 / 真实落点（可模拟被重定向回首页）。"""

    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.closed = False
        self.crashed = False
        self.gotos: list[str] = []
        self.redirect_to: str | None = None

    def goto(self, url: str) -> None:
        if self.is_lost():
            raise RuntimeError("page is gone")  # 真实 Playwright 对已关闭页面的行为
        self.gotos.append(url)
        self.url = self.redirect_to or url

    def close(self) -> None:
        self.closed = True

    def is_lost(self) -> bool:
        return self.closed or self.crashed


def is_page_lost(page: FakePage) -> bool:
    """判活：标签被关 / 渲染进程崩溃。死页面留在池里 = 该桶永久 502（P0-D）。"""
    return page.is_lost()


class FakeComposer:
    """受控富文本输入区替身，复刻三种真机行为：

    1. 写入是**复合动作**：没有选区时裸插入是静默空操作（不报错、0 字符）；
    2. 读回是**有损**的（这里模拟空白收敛 / 丢换行）；
    3. `accepts_input=False` 表示页面退化时的僵尸节点：attached、visible，但吃不进字符。
    """

    def __init__(
        self,
        *,
        attached: bool = True,
        visible: bool = True,
        lossy: bool = True,
        accepts_input: bool = True,
    ) -> None:
        self.attached = attached
        self.visible = visible
        self.lossy = lossy
        self.accepts_input = accepts_input
        self.text = ""
        self.caret: int | None = None
        self.fills = 0
        self.clears = 0

    def place_caret(self) -> None:
        self.caret = len(self.text)

    def insert_text(self, chunk: str) -> int:
        if self.caret is None or not self.accepts_input:
            return 0  # 静默空操作：这正是「写不进」最容易被误当成功的地方
        self.text = self.text[: self.caret] + chunk + self.text[self.caret:]
        self.caret += len(chunk)
        return len(chunk)

    def fill(self, chunk: str) -> int:
        """真实 `fill()` = 建立选区 + insertText（复合动作，不是一次朴素赋值）。"""
        self.place_caret()
        self.fills += 1
        return self.insert_text(chunk)

    def clear(self) -> None:
        self.clears += 1
        self.text = ""
        self.caret = None

    def read_back(self) -> str:
        if not self.lossy:
            return self.text
        return re.sub(r"\s+", " ", self.text)


def write_prompt(composer: FakeComposer, prompt: str, chunk_chars: int = 4000) -> dict:
    """写入 = 放置选区 + 分块插入；读回**只能发现失败、不能否决成功**（A9 / 硬规则 13-14）。"""
    composer.place_caret()
    written = 0
    for i in range(0, len(prompt), chunk_chars):
        written += composer.insert_text(prompt[i : i + chunk_chars])
    if written != len(prompt):
        # 只有「真的没写进去」才失败：清空重来一次，仍失败就明确报错（不静默空操作）
        composer.clear()
        composer.place_caret()
        written = 0
        for i in range(0, len(prompt), chunk_chars):
            written += composer.insert_text(prompt[i : i + chunk_chars])
        if written != len(prompt):
            raise ComposerError("输入区写入失败：分块插入 0 字符（没有选区 / 僵尸节点？）")
    readback = composer.read_back()
    if not readback.strip():
        raise ComposerError("写入后读回为空")
    if _bug("a09") and readback != prompt:
        # 反模式：拿有损读回否决一次成功的写入，再清空重写（反而把写好的 prompt 弄坏）
        composer.clear()
        composer.fill(prompt)
    return {"written": written, "readback": readback, "lossy": composer.lossy}


def pick_composer(candidates: list[FakeComposer]) -> FakeComposer:
    """候选**全枚举** + 可见可编辑优先 + 逐个试填校验读回；「第一个 attached」会填进僵尸节点（A11）。"""
    if _bug("a11"):
        for c in candidates:
            if c.attached:
                return c
        raise ComposerNotFound("没有 attached 的输入区")
    for c in sorted(candidates, key=lambda c: (not c.visible, not c.attached)):
        if not c.attached or not c.visible:
            continue
        c.place_caret()
        if c.insert_text("x") != 1 or "x" not in c.read_back():
            continue  # 僵尸节点：吃得进元素但写不进字符
        c.clear()
        return c
    raise ComposerNotFound("候选全枚举后仍无可写入的输入区")


# ---------------------------------------------------------------- 回复判定


def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _last_with_text(nodes: list[str]) -> str:
    for text in reversed(nodes):
        if (text or "").strip():
            return text.strip()
    return ""


def pick_new_reply(baseline: list[str], after: list[str], prompt: str) -> str | None:
    """结束时的「新正文」判据：跳过空节点、跳过本轮 prompt 回显、跳过上一轮内容（P0-A）。

    返回 None 表示**没有可确认的新正文** → 调用方应继续等待直到超时，而不是把旧内容当本轮回复。
    """
    if _bug("p0a"):
        return _last_with_text(after) or None  # 反模式：最后一个非空节点就是回复（可能是回显）
    before = _strip_ws(_last_with_text(baseline))
    grid = _strip_ws(prompt)
    for text in reversed(after):
        body = (text or "").strip()
        if not body or _strip_ws(body) == grid:
            continue
        if before and _strip_ws(body) == before:
            continue
        return body
    return None


# ---------------------------------------------------------------- 页面池（P0-C / P0-D）


@dataclass
class Record:
    page: FakePage
    session_url: str


class FakePagePool:
    """桶 → 页面；回收判据是**「有请求在飞」的可重入计数**，不是「锁被谁拿着」（P0-C）。"""

    def __init__(self, max_pages: int = 2) -> None:
        self.max_pages = max_pages
        self.pages: dict[str, FakePage] = {}
        self.inflight: dict[str, int] = {}
        self.reaped: list[str] = []

    def acquire(self, bucket: str) -> FakePage:
        if bucket not in self.pages and len(self.pages) >= self.max_pages:
            self.reap(keep=(bucket,))
        page = self.pages.get(bucket) or FakePage(url=f"session://{bucket}")
        self.pages[bucket] = page
        self.inflight[bucket] = self.inflight.get(bucket, 0) + 1
        return page

    def release(self, bucket: str) -> None:
        self.inflight[bucket] = max(0, self.inflight.get(bucket, 0) - 1)

    def reap(self, keep: tuple[str, ...] = ()) -> list[str]:
        for bucket, page in list(self.pages.items()):
            if bucket in keep:
                continue
            if _bug("p0c"):
                # 反模式：看「桶锁在谁手里」→ 建页与拿锁之间请求就算在飞也照回收 → 随机 502
                protected = False
            else:
                protected = self.inflight.get(bucket, 0) > 0
            if protected:
                continue
            page.close()
            self.reaped.append(bucket)
            del self.pages[bucket]
        return self.reaped


@dataclass
class PageRegistry:
    """桶 → 页面句柄 + 该桶的会话 URL；死页面必须**重建并回到同一条会话**（P0-D）。"""

    session_url_for: dict[str, str] = field(default_factory=dict)
    records: dict[str, Record] = field(default_factory=dict)
    rebuilds: int = 0
    new_pages: list[FakePage] = field(default_factory=list)

    def get_or_rebuild(self, bucket: str) -> tuple[FakePage, bool]:
        session_url = self.session_url_for.get(bucket, f"https://chat.example.com/c/{bucket}")
        rec = self.records.get(bucket)
        if _bug("p0d"):
            if rec is not None:
                return rec.page, False  # 反模式：记录里有页面就用它，不判活 → 永久 502
        elif rec is not None and not is_page_lost(rec.page):
            return rec.page, False
        self.rebuilds += 1
        page = FakePage()
        page.goto(session_url)
        self.new_pages.append(page)
        self.records[bucket] = Record(page=page, session_url=session_url)
        return page, True


# ---------------------------------------------------------------- 会话与状态


@dataclass
class SessionState:
    has_history: bool = False
    url: str = ""
    seeded_page: FakePage | None = None


def should_seed(state: SessionState, page: FakePage) -> bool:
    """播种条件必须**同时**看「磁盘说有过历史」与「内存里播种的那张页面就是现在这张」（A1）。"""
    if _bug("a01"):
        return not state.has_history
    return not (state.has_history and state.seeded_page is page)


def same_conversation(url_a: str, url_b: str) -> bool:
    """会话 URL 比较 = 主机 + 路径**精确匹配**；前缀比较会把末位不同的会话判成同一条（A12）。"""
    if _bug("a12"):
        return bool(url_a) and bool(url_b) and url_a.startswith(url_b)
    a, b = urlparse(url_a or ""), urlparse(url_b or "")
    if not a.hostname or not b.hostname:
        return False
    return (a.hostname, a.path.rstrip("/")) == (b.hostname, b.path.rstrip("/"))


def reconcile_after_goto(state: SessionState, page: FakePage) -> dict:
    """`goto` 之后用**真实落点**重算 has_history：状态里的 url 只是意图（P0-E）。"""
    landing = page.url
    if _bug("p0e"):
        has_history = bool(state.url)
    else:
        has_history = same_conversation(landing, state.url)
    return {"landing": landing, "has_history": has_history}


def recover_after_ambiguous_submit(confirmed: bool, existing_reply: str | None) -> str:
    """提交无法确认时只做**只读**恢复；无条件重发会创建重复对话（A2）。"""
    if _bug("a02"):
        return "resend"
    if confirmed:
        return "submitted"
    if existing_reply:
        return "read_existing"
    raise UnrecoverableSubmission("无法确认提交，且页面没有可读回复：不冒险重发")


class FakeConversation:
    """记录「新建对话」次数，用来证明没有重发（重发 = 多出一条重复对话）。"""

    def __init__(self) -> None:
        self.created = 0
        self.turns = 0

    def new(self) -> FakeConversation:
        self.created += 1
        return self

    def turn(self) -> FakeConversation:
        self.turns += 1
        return self


# ---------------------------------------------------------------- 错误语义


RETRYABLE = frozenset({"upstream_error", "timeout"})
UNRECOVERABLE = frozenset(
    {
        "composer_not_found",
        "unavailable",
        "invalid_request_error",
        "usage_limit_reached",
        "context_length_exceeded",
        "challenge_required",
    }
)


def is_retryable(error_code: str) -> bool:
    """不可恢复错误（输入区缺失 / 未登录 / 额度用尽）命中即冒泡，不进重试阶梯（A3）。"""
    if _bug("a03"):
        return error_code != "invalid_request_error"
    return error_code not in UNRECOVERABLE and error_code in RETRYABLE


CHALLENGE_PATTERNS = [
    re.compile(r"security verification", re.I),
    re.compile(r"verif(?:y|ying) you are human", re.I),
    re.compile(r"checking your browser", re.I),
]

CAP_PATTERNS = [
    re.compile(r"maximum length", re.I),
    re.compile(r"reached the (maximum|limit)", re.I),
    re.compile(r"(长度|上下文|对话)上限"),
]

LIMIT_PATTERNS = [
    re.compile(r"reached (your|the) (usage )?limit", re.I),
    re.compile(r"out of messages", re.I),
    re.compile(r"额度|配额"),
]


def text_without_subtrees(page_text: str, subtrees: list[str]) -> str:
    """在**克隆体上剪掉子树**后再读文本；字符串替换必须删掉**每一处**（P0-F / A8）。"""
    out = page_text
    for subtree in subtrees:
        if not subtree:
            continue
        if _bug("a08"):
            out = out.replace(subtree, "", 1)  # 反模式：同一段文本出现两次只剔掉一处
        else:
            out = out.replace(subtree, "")
    return out


def cap_notice_hit(page_text: str, transcript: str) -> bool:
    """「到顶」提示只在**剔除会话正文后**的页面外壳里扫（P0-F）。"""
    scan = page_text if _bug("p0f") else text_without_subtrees(page_text, [transcript])
    return any(p.search(scan) for p in CAP_PATTERNS)


def is_challenged(page_text: str, transcript: str, composer_ok: bool) -> bool:
    """挑战页三条件与门：页面 chrome 命中 + 命中可见 + **输入区不可用**（A4）。"""
    if _bug("a04"):
        scan = page_text
    else:
        scan = text_without_subtrees(page_text, [transcript])
    hit = any(p.search(scan) for p in CHALLENGE_PATTERNS)
    if _bug("a04"):
        return hit  # 反模式：命中就 503，正文里引用了这句话也照报
    return hit and not composer_ok


def probe_ok(nav_status: int, dom_hits: int) -> bool:
    """「页面可用」的判据必须含 navigation_ok：403 的挑战壳里仍可能命中输入框（A5）。"""
    if _bug("a05"):
        return dom_hits > 0
    return 200 <= nav_status < 400 and dom_hits > 0


def classify_failure(visible_texts: list[str], stalled: bool, parsed_zero: bool = False) -> str:
    """额度用尽弹窗必须归因 usage_limit_reached；报成「停滞 / 解析失败」会让客户端无限重试（A6）。"""
    hit = any(p.search(text) for text in visible_texts for p in LIMIT_PATTERNS)
    if _bug("a06"):
        hit = False
    if hit:
        return "usage_limit_reached"
    if stalled:
        return "stalled"
    if parsed_zero:
        return "parse_error"
    return "unknown"


# ---------------------------------------------------------------- 生成中信号与结束判定


STOP_TEXTS = re.compile(r"^\s*(stop|stop generating|停止生成)\s*$", re.I)
STOP_CLASS_RE = re.compile(r"(^|[-_])stop([-_]|$)", re.I)


def is_generating(stop_hits: list[dict]) -> bool:
    """「生成中」= 存在**可见**且真的是停止控件的元素；装饰类名 stopAtEnd-<hash> 不算（P0-G）。"""
    for hit in stop_hits:
        if not hit.get("visible"):
            continue
        cls = hit.get("cls") or ""
        text = hit.get("text") or ""
        if _bug("p0g"):
            if "stop" in cls.lower():
                return True
            continue
        if STOP_TEXTS.search(text) or STOP_CLASS_RE.search(cls):
            return True
    return False


def is_done(signals: dict, idle: IdleClock) -> bool:
    """结束判定是**多信号与门**：停止控件消失 + 生成标记为假 + 内容静默窗口（硬规则 1 / 16）。"""
    return (
        not signals.get("stop_visible", False)
        and not signals.get("busy", True)
        and bool(signals.get("has_text"))
        and idle.stalled()
    )


# ---------------------------------------------------------------- 工具调用载体


FENCE_RE = re.compile(r"```(?:tool_call|tool-call)\s*(\{.*?\})\s*```", re.S)
MARKER_FENCE_RE = re.compile(r"^\s*TOOL_CALL\s*```\s*(\{.*?\})\s*```", re.S | re.M)
LEGACY_LINE_RE = re.compile(r"^\s*TOOL_CALL\s+(\{.*\})\s*$", re.M)


def parse_tool_calls(rendered: str) -> list[dict]:
    """代码围栏载体优先，兼容「标记行 + 围栏」退化形态与旧纯文本行（P0-H）。"""
    import json

    out: list[dict] = []
    patterns = [LEGACY_LINE_RE] if _bug("p0h") else [FENCE_RE, MARKER_FENCE_RE, LEGACY_LINE_RE]
    for pattern in patterns:
        for match in pattern.finditer(rendered):
            try:
                call = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(call, dict):
                out.append(call)
        if out:
            break
    return out


def system_prompt(tools: list[dict], tools_supported: bool = True) -> str:
    """只描述链路、不描述模型自身能力；否定式措辞会被读成能力事实题而拒答（P0-I）。"""
    lines = [
        "你是语言模型。这条对话会被用户电脑上的本地客户端读取；"
        "客户端会真实执行你写出的调用并返回输出。",
    ]
    if tools_supported and tools:
        names = ", ".join(str(t.get("name", "?")) for t in tools)
        lines.append(f"可用工具：{names}。需要工具时，把调用写成 ```tool_call 围栏。")
    if _bug("p0i"):
        lines.append("你没有 shell，你无法访问网络，工具已挂载。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 指标 / 状态注册表


class FakeMetrics:
    """逐测试注入的计数器；进程单例会让测试互相串味（A13）。"""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def get(self, name: str) -> int:
        return self.counters.get(name, 0)


_SHARED_METRICS = FakeMetrics()


def make_registry() -> FakeMetrics:
    if _bug("a13"):
        return _SHARED_METRICS
    return FakeMetrics()


# ---------------------------------------------------------------- 取消路径


class FakeWatcher:
    """模拟「不可取消的 receive()」：被取消后再 await 会抛 CancelledError。"""

    def __init__(self) -> None:
        self.cancelled = False
        self.awaited_after_cancel = 0

    def cancel(self) -> None:
        self.cancelled = True

    async def wait(self) -> None:
        if self.cancelled:
            self.awaited_after_cancel += 1
            raise asyncio.CancelledError("watcher 已被取消")
        await asyncio.sleep(0)


class FakeLock:
    def __init__(self) -> None:
        self.locked = False

    def acquire(self) -> None:
        self.locked = True

    def release(self) -> None:
        self.locked = False


async def cancel_and_release(
    watcher: FakeWatcher, page: FakePage, lock: FakeLock, registry: FakeMetrics
) -> dict:
    """客户端断开：取消 + 归还页面 + 释放桶锁；**不 await 被取消的 watcher**（A16）。"""
    watcher.cancel()
    if _bug("a16"):
        await watcher.wait()  # 反模式：卡在不可取消的 receive()，把整条路由拖死
    page.close()
    lock.release()
    registry.incr("cancelled")
    return {
        "cancelled": watcher.cancelled,
        "page_closed": page.closed,
        "lock_released": not lock.locked,
    }


# ---------------------------------------------------------------- 落盘 / usage / 配置 / 文档


class ReplyStore:
    """落盘方法：漏 `self`/`@staticmethod` 会让每个非流式请求最后一步 500（P0-B）。"""

    def __init__(self, root: str = "output") -> None:
        self.root = root
        self.saved: list[str] = []

    if VARIANT in ("all", "p0b"):

        def persist(self, reply: str) -> str:
            # 反模式：漏了 @staticmethod → 实例调用正常，类/被引用调用直接 500
            return f"{self.root}/reply.md"

    else:

        @staticmethod
        def persist(reply: str, root: str = "output") -> str:
            return f"{root}/reply.md"


def describe_usage(
    usage: object, fields: tuple[str, ...] = ("prompt_tokens", "total_tokens")
) -> dict:
    """`usage` 字段必须**真实存在**才算数；照抄建议时先核实（A10）。"""
    out: dict = {}
    for name in fields:
        exists = (isinstance(usage, dict) and name in usage) or hasattr(usage, name)
        if not exists:
            if _bug("a10"):
                out[name] = 0  # 反模式：不存在的字段用 0 顶替，于是「看起来有」
                continue
            raise UnknownFieldError(f"字段不存在：{name}")
        out[name] = usage[name] if isinstance(usage, dict) else getattr(usage, name)
    return out


CONFIG_KEY_RE = re.compile(r'env_(?:int|float|bool|str)\(\s*"([A-Z][A-Z0-9_]*)"')


def find_dead_keys(config_src: str, read_src: str) -> list[str]:
    """定义了却没人读的配置键（死键）= 虚假的保护感；要么接线，要么删（A7）。"""
    dead: list[str] = []
    for key in sorted(set(CONFIG_KEY_RE.findall(config_src))):
        if _bug("a07"):
            continue
        if not re.search(rf"\b{re.escape(key)}\b", read_src):
            dead.append(key)
    return dead


VERIFIED_MARKS = ("✅", "[x]", "✔")
UNVERIFIED_WORDS = ("未验证", "未复现", "未通过", "待验证", "已知限制")


def check_acceptance_markers(doc_text: str) -> list[str]:
    """未验证的项必须原样留成限制，且不得同时被打成通过（A14）。"""
    if _bug("a14"):
        return []
    problems: list[str] = []
    has_untracked = any(word in doc_text for word in UNVERIFIED_WORDS)
    unchecked = [ln.strip() for ln in doc_text.splitlines() if ln.strip().startswith("- [ ]")]
    if not has_untracked:
        problems.append("文档里没有任何「未验证 / 已知限制」记录：要么真全验过（罕见），要么在含糊")
    for line in doc_text.splitlines():
        if any(mark in line for mark in VERIFIED_MARKS) and any(
            word in line for word in UNVERIFIED_WORDS
        ):
            problems.append(f"同一行既标通过又写未验证：{line.strip()[:60]}")
    if unchecked and not has_untracked:
        problems.append(f"有 {len(unchecked)} 个未勾选项，却没有任何未验证说明")
    return problems
