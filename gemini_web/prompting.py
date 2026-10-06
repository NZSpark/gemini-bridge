"""把客户端的消息数组转换成网页输入框里的一整段文本，以及若干纯文本工具函数。"""

import logging
from typing import Any, Dict, List, Optional

from . import config
from .models import ChatMessage
from .toolcalls import format_tool_call_emphasis, format_tools_instruction

logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    """把 OpenAI 的 content 归一化为纯文本。

    content 可能是：
      - None
      - 字符串
      - 内容分片数组，例如 [{"type": "text", "text": "hi"}]
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return str(content)


def estimate_tokens(text: str) -> int:
    """**量级估算**（order-of-magnitude）token 数，不是精确值，也不追求精确。

    CJK 字符约 1 char/token，其余字符约 4 char/token。
    不引入 tiktoken：那是 OpenAI 的分词器，算 Gemini 的 token 只会
    得到一个“看起来很精确但其实是错的”数字，反而更容易误导客户端做上下文裁剪。

    它同时充当三个地方的“计价单位”，三处必须用同一个函数，不得各算各的：

    * ``usage.prompt_tokens`` / ``completion_tokens``（OpenAI 兼容字段）；
    * ``SESSION_MAX_TOKENS`` 会话轮转预算（超过即轮转，见 ``chat_io``）；
    * ``/v1/models`` 的 ``context_window``（既然对外报了这个数，就必须同源）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff")
    other = len(text) - cjk
    return max(1, cjk + other // 4)


def _delta_piece(streamed: str, current: str) -> tuple[Optional[str], str]:
    """计算 ``current`` 相对「已经发给客户端的内容」真正新增的部分。

    网页版在生成中可能重排 / 替换回复节点，导致 ``current`` 不再以之前的内容为前缀。
    此时有两种做法：

    * 退回「公共前缀之后的部分」追加——但这会让客户端把**改写后的旧内容**
      拼在旧内容后面，得到重复 / 错乱的文本（旧尾巴不会撤回）；
    * **停止发送增量**（返回 ``(None, streamed)``）——保留已发内容不动，
      等本轮结束由调用方一次性给全量文本，客户端最终拿到的是完整且不重复的回复。

    这里选后者：宁可少发几次增量，也不要让客户端拼出错乱文本。真正的
    前缀扩展（最常见情形）仍然逐块下发。

    :return: (需要补发的内容, 客户端补发后实际拥有的内容)；无新增 / 已停发时为 None。
    """
    if current == streamed:
        return None, streamed
    if current.startswith(streamed):
        return current[len(streamed):], current
    # 非前缀：节点被整体替换 / 重排。停发增量，保留已发内容，交由收尾补全。
    return None, streamed


# 播种时的默认字符预算（server 会传入 config.SEED_MAX_CHARS 覆盖）
DEFAULT_SEED_MAX_CHARS = 12000

# 成品级预算压缩时，单条工具结果至少保留多少**原始**字符。
# 压缩策略（用户定）：超长的客户端结果不整块丢掉，而是只留开头一段，
# 并在 prompt 里注明“结果太长已被截短”——模型知道自己看的是片段，
# 也不会因为中间凭空消失而答非所问。
MIN_TOOL_RESULT_KEEP_CHARS = 2000

_SEED_HEADER = "[上下文重建] 这是一个新会话。以下是本次任务此前的对话记录，请据此继续，不要从头重做。"
_SEED_TRUNCATED_NOTE = "（更早的部分因长度限制已省略，如需可向我确认。）"


def _join_segments(segments) -> str:
    """把 ``(文本, 来源消息)`` 分段拼成最终 prompt（空段跳过，首尾去空白）。"""
    return "\n\n".join(text for text, _ in segments if text).strip()


def _render_tool_with_keep(message: ChatMessage, keep: int) -> str:
    """渲染工具结果，只保留开头 ``keep`` 个字符并**注明已被截短**（成品预算用）。"""
    raw = _content_to_text(message.content)
    tag = f" {message.tool_call_id}" if message.tool_call_id else ""
    if keep >= len(raw):
        return f"[工具执行结果{tag}]\n{raw}"
    return (
        f"[工具执行结果{tag}]\n{raw[:keep]}"
        f"\n…（工具结果过长，已截断 {len(raw) - keep} 字符）"
    )


def _longest_tool_index(segments, skip) -> Optional[int]:
    """当前最长的、且还没压到下限的工具结果分段下标；没有则 None。"""
    best_index: Optional[int] = None
    best_len = 0
    for index, (text, message) in enumerate(segments):
        if index in skip or message is None or message.role != "tool":
            continue
        if len(text) > best_len:
            best_index, best_len = index, len(text)
    return best_index


def _fit_segments_to_budget(segments, limit: int) -> tuple[bool, int]:
    """把拼装后的**成品**压进预算：优先压缩超长的工具结果（保留开头 + 标注）。

    为什么需要它：``TOOL_RESULT_MAX_CHARS`` 只管单条，一次请求带入多条工具结果时
    （Pi / Codex 并行读文件很常见）成品仍可超预算，而原先唯一的兜底是发送侧的
    头尾截断——那会把中间那段**凭空**抽走，模型看不到任何“这里被截过”的提示。

    这里改成：每轮挑当前最长的工具结果砍一半，直到进预算或所有工具结果都到了
    ``MIN_TOOL_RESULT_KEEP_CHARS`` 下限；每段都会带上一行截断说明。

    :return: (是否发生压缩, 压缩后的成品长度)
    """
    total = len(_join_segments(segments))
    if not limit or limit <= 0 or total <= limit:
        return False, total
    before = total
    kept: dict = {}
    floor_hit: set = set()
    shrunk = 0
    for _ in range(64):
        if len(_join_segments(segments)) <= limit:
            break
        index = _longest_tool_index(segments, floor_hit)
        if index is None:
            break
        message = segments[index][1]
        raw_len = len(_content_to_text(message.content))
        current = kept.get(index, min(raw_len, config.TOOL_RESULT_MAX_CHARS or raw_len))
        if current <= MIN_TOOL_RESULT_KEEP_CHARS:
            floor_hit.add(index)
            continue
        keep = max(MIN_TOOL_RESULT_KEEP_CHARS, current // 2)
        kept[index] = keep
        segments[index] = (_render_tool_with_keep(message, keep), message)
        shrunk += 1
    total = len(_join_segments(segments))
    logger.warning(
        "[长度] 成品超预算：已压缩 %s 条工具结果（只保留开头 + 标注截断），"
        "%s → %s 字符（上限 %s%s）。若模型说“看不到完整内容”，这是预期行为。",
        shrunk, before, total, limit,
        "，仍有超出" if total > limit else "",
    )
    return shrunk > 0, total


def _render_message(message: ChatMessage) -> str:
    """把单条消息渲染成喂给网页版的一段文本。

    ``role == "tool"`` 的正文必须逐字节保留：Pi/Codex 的 read 结果会原样
    作为 edit 工具的 oldText，任何 .strip() 抹掉的首尾空行/换行都会导致
    "Could not find the exact text ... including all whitespace and newlines"。
    其它角色仍是历史对话文本，去掉首尾空白无副作用。
    """
    raw = _content_to_text(message.content)
    if message.role == "tool":
        tag = f" {message.tool_call_id}" if message.tool_call_id else ""
        limit = config.TOOL_RESULT_MAX_CHARS
        if limit and len(raw) > limit:
            dropped = len(raw) - limit
            raw = (
                raw[:limit]
                + f"\n…（工具结果过长，已截断 {dropped} 字符）"
            )
        return f"[工具执行结果{tag}]\n{raw}"
    content = raw.strip()
    if message.role == "system":
        return f"[系统指令]\n{content}"
    if message.role == "assistant":
        return f"[你之前的回复]\n{content}"
    return content


def _last_assistant_index(messages: List[ChatMessage]) -> int:
    last = -1
    for index, message in enumerate(messages):
        if message.role == "assistant":
            last = index
    return last


def _is_harness_noise(m: ChatMessage) -> bool:
    """harness 注入的元提示（如 Codex 的“生成任务标题”请求）与环境包装块
    都不是真实对话内容，播种/增量时都不应重放。"""
    from .tasks import _is_environment_wrapper, _is_meta_prompt

    text = _content_to_text(m.content)
    return _is_meta_prompt(text) or _is_environment_wrapper(text)


def _run_messages(messages: List[ChatMessage]) -> List[ChatMessage]:
    """取出“最后一条 assistant 之后”的新增消息。

    harness（Codex / Pi）每轮会把完整系统提示作为 system 消息重新发来，也会
    内联“生成任务标题”之类的元提示与 ``<environment_context>`` 环境块。这些
    都不是用户真正说的话，增量发送时必须丢弃，否则每轮都会把它们当成新指令
    重发一遍。
    """
    last_assistant = _last_assistant_index(messages)
    delta = messages[last_assistant + 1:] if last_assistant >= 0 else messages
    # 增量模式下 system 消息一律丢弃：harness 每轮重发完整系统提示，而网页
    # 会话早已带着它，没必要也不应该把上万字的系统提示当新指令再发一遍。
    delta = [
        m
        for m in delta
        if m.role != "system" and not _is_harness_noise(m)
    ]
    if not delta:
        # 兜底：没有新消息时，退回最后一条真实 user 消息
        delta = [
            m
            for m in messages
            if m.role == "user" and not _is_harness_noise(m)
        ][-1:]
    return delta


def _seed_messages(messages: List[ChatMessage], max_chars: int):
    """为新会话准备“播种”内容：尽量带上完整上下文，超出预算时保留最近的。

    网页会话一旦轮转（新开会话），后端的上下文就清空了。此时如果还只发增量，
    模型会收到一条“没有前因”的孤立消息——不报错，但会胡编。

    :return: (system 消息, 保留的其余消息, 是否发生了截断)
    """
    systems = [m for m in messages if m.role == "system" and not _is_harness_noise(m)]
    rest = [m for m in messages if m.role != "system" and not _is_harness_noise(m)]

    truncated = False

    # system 消息同样计入预算。harness（Codex / Pi）每轮都把完整系统提示作为
    # system 消息发来，常达上万字；若像以前那样“原样全发”，播种 prompt 就会被
    # 这段系统提示灌满，用户的真实请求被淹没。这里逐条按 SEED_SYSTEM_MAX_CHARS
    # 截断，并从最旧的开始丢弃，直到 system 总量不超过总预算的一半。
    system_budget = max_chars // 2
    per_system_limit = config.SEED_SYSTEM_MAX_CHARS
    systems = list(reversed(systems))  # 保留最近的 system
    kept_systems: List[ChatMessage] = []
    system_used = 0
    for message in systems:
        text = _content_to_text(message.content)
        # 剩余预算：既要满足单条 SEED_SYSTEM_MAX_CHARS，也不能突破 system_budget。
        # 取两者较小值，**第一条巨型 system 也会被截断**（而不是无条件放行）——
        # 否则 SEED_SYSTEM_MAX_CHARS=0（“不限制”）时，一条 10 万字的系统提示会
        # 原样进 prompt，把播种变成“巨型 fill”。参照姊妹项目 ChatGPTBridge 的
        # ``_seed_messages``（其 limit = min(per_system_limit, remaining)）。
        remaining = system_budget - system_used
        limit = per_system_limit or len(text)
        limit = min(limit, remaining) if remaining > 0 else 0
        if limit <= 0:
            truncated = True
            break
        if len(text) > limit:
            text = text[:limit] + "…（系统提示已截断）"
            truncated = True
        kept_systems.append(ChatMessage(role="system", content=text))
        system_used += len(text)
    kept_systems.reverse()

    kept: List[ChatMessage] = []
    used = system_used
    for message in reversed(rest):
        size = len(_content_to_text(message.content))
        if kept and used + size > max_chars:
            truncated = True
            break
        kept.append(message)
        used += size
    kept.reverse()
    return kept_systems, kept, truncated


def _warn_if_over_budget(
    prompt: str,
    *,
    seed: bool,
    history_chars: int,
    tool_result_chars: int,
    instruction_chars: int,
) -> None:
    """拼装完成就已超出 ``PROMPT_MAX_CHARS`` 时留一条 warning（此前完全静默）。

    为什么需要它：``PROMPT_MAX_CHARS`` 一直是**发送侧的事后兜底**
    （``chat_io._clamp_prompt`` 对整段文本做头尾截断），而各条预算都管不到拼装后的成品——
    ``TOOL_RESULT_MAX_CHARS`` 只管单条工具结果、``SEED_MAX_CHARS`` 只算原始文本
    （渲染标签 / 工具说明 / 任务块都在预算之后叠加）、``role="user"`` 完全没有上限。
    于是「把旋钮调小也没用」：一次请求带入多条工具结果时，成品照样远超预算，
    最后只剩兜底截断把中间内容砍掉（此前连日志都没有，无从发现）。

    这行日志回答的是「到底谁把 prompt 撑大的」，是排查「长 prompt 导致模型不响应 /
    答非所问」的第一现场证据。
    """
    limit = config.PROMPT_MAX_CHARS
    if not limit or len(prompt) <= limit:
        return
    logger.warning(
        "[长度] 拼装即超预算：prompt=%s 字符 > PROMPT_MAX_CHARS=%s（%s 路径；"
        "历史 %s 字符，其中工具结果 %s；工具说明 %s）。发送侧将截掉中间 %s 字符——"
        "若模型答非所问 / 丢上下文，先看这一行。",
        len(prompt), limit, "seed" if seed else "delta",
        history_chars, tool_result_chars, instruction_chars, len(prompt) - limit,
    )


def build_prompt(
    messages: List[ChatMessage],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    seed: bool = False,
    seed_max_chars: Optional[int] = None,
    task_block: Optional[str] = None,
) -> str:
    """把客户端发来的完整 OpenAI 消息数组，转换成要发给网页输入框的文本。

    * ``seed=False``（默认）：网页版是一个持续存在的会话，无需每轮重发全部历史，
      只发送“最后一条 assistant 消息之后”的新增消息（新的 user 指令或 tool 结果）。
    * ``seed=True``：当前会话是**新开的**，必须把既有上下文一次性播种进去，
      否则模型会收到一条没有前因的孤立消息。
    * ``task_block``：任务快照（见 ``tasks.resume_block``）。仅在 ``seed=True`` 时
      生效，会被放在**历史之前、上下文重建头之后**，因此**不会**被
      ``seed_max_chars`` 的尾部截断逻辑丢掉——这是“轮转不丢任务”的关键。
    * 拼装成成品后，若总长超过 ``PROMPT_MAX_CHARS``，**优先压缩超长的工具结果**
      （只保留开头一段 + 一行截断说明，见 ``_fit_segments_to_budget``），
      而不是等发送侧把头尾之外的内容凭空抽走。
    """
    budget = seed_max_chars or DEFAULT_SEED_MAX_CHARS
    if seed:
        systems, kept, truncated = _seed_messages(messages, budget)
        source: List[ChatMessage] = systems + kept
        if truncated:
            logger.info(
                "[长度] 播种内容超出 SEED_MAX_CHARS=%s：已从最旧的开始丢弃历史"
                "（保留 system=%s 条 / 其余=%s 条）。注意该预算只计原始文本，"
                "渲染标签、任务块与工具说明都在预算之后叠加。",
                budget, len(systems), len(kept),
            )
        segments: List[tuple] = [(_SEED_HEADER, None)]
        if task_block:
            segments.append((task_block, None))
        if truncated:
            segments.append((_SEED_TRUNCATED_NOTE, None))
        segments.extend((_render_message(m), m) for m in source)
    else:
        source = _run_messages(messages)
        segments = [(_render_message(m), m) for m in source]

    use_tools = bool(tools) and tool_choice != "none"
    if seed and use_tools:
        # 新 bucket / 重置后的第一轮：把带围栏示例的格式强调块放在播种开头，
        # 模型最容易在这种时候退回原生 DSML 标记，双保险。
        segments.insert(1, (format_tool_call_emphasis(), None))

    base = _join_segments(segments)
    # 去重：入站 system 消息可能已带 [工具调用说明]（harness 会内联一份），
    # 再追加一遍会造成同一 prompt 出现两份说明、互相干扰。
    appended: List[str] = []
    if use_tools and "[工具调用说明]" not in base:
        appended.append(format_tools_instruction(tools))
    if use_tools and any(
        (t.get("function", t) or {}).get("name") == "edit_markdown" for t in tools
    ):
        from .toolcalls import edit_markdown_spec

        if "[edit_markdown 说明]" not in base:
            appended.append(edit_markdown_spec())

    # 工具结果合计长度：预算是逐条的，这里把「合计」单独记下来（排查用）
    tool_result_chars = sum(
        len(text) for text, message in segments if message is not None and message.role == "tool"
    )

    # 按**成品**预算压缩：预留后面要追加的工具说明的位置
    limit = config.PROMPT_MAX_CHARS
    if limit and limit > 0:
        _fit_segments_to_budget(segments, limit - sum(len(a) + 2 for a in appended))

    prompt = _join_segments(segments)
    history_chars = len(prompt)                                         # 历史/正文部分
    instruction_chars = sum(len(a) + 2 for a in appended)               # 追加的说明部分
    for piece in appended:
        prompt = (prompt + "\n\n" + piece).strip()

    _warn_if_over_budget(
        prompt,
        seed=seed,
        history_chars=history_chars,
        tool_result_chars=tool_result_chars,
        instruction_chars=instruction_chars,
    )
    return prompt
