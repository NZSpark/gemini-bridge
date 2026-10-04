"""把客户端的消息数组转换成网页输入框里的一整段文本，以及若干纯文本工具函数。"""

from typing import Any, Dict, List, Optional

from . import config
from .models import ChatMessage
from .toolcalls import format_tool_call_emphasis, format_tools_instruction


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
    """估算 token 数（仅用于填充 OpenAI 的 usage 字段，不是精确值）。

    CJK 字符约 1 char/token，其余字符约 4 char/token。
    不引入 tiktoken：那是 OpenAI 的分词器，算 Gemini 的 token 只会
    得到一个“看起来很精确但其实是错的”数字，反而更容易误导客户端做上下文裁剪。
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
        if per_system_limit and len(text) > per_system_limit:
            text = text[:per_system_limit] + "…（系统提示已截断）"
            truncated = True
        size = len(text)
        if kept_systems and system_used + size > system_budget:
            truncated = True
            break
        kept_systems.append(ChatMessage(role="system", content=text))
        system_used += size
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
    """
    if seed:
        systems, kept, truncated = _seed_messages(messages, seed_max_chars or DEFAULT_SEED_MAX_CHARS)
        parts: List[str] = [
            "[上下文重建] 这是一个新会话。以下是本次任务此前的对话记录，请据此继续，不要从头重做。"
        ]
        if task_block:
            parts.append(task_block)
        if truncated:
            parts.append("（更早的部分因长度限制已省略，如需可向我确认。）")
        parts.extend(_render_message(m) for m in systems + kept)
    else:
        parts = [_render_message(m) for m in _run_messages(messages)]

    use_tools = bool(tools) and tool_choice != "none"
    if seed and use_tools:
        # 新 bucket / 重置后的第一轮：把带围栏示例的格式强调块放在播种开头，
        # 模型最容易在这种时候退回原生 DSML 标记，双保险。
        parts.insert(1, format_tool_call_emphasis())

    prompt = "\n\n".join(part for part in parts if part).strip()

    # 去重：入站 system 消息可能已带 [工具调用说明]（harness 会内联一份），
    # 再追加一遍会造成同一 prompt 出现两份说明、互相干扰。
    if use_tools and "[工具调用说明]" not in prompt:
        prompt = (prompt + "\n\n" + format_tools_instruction(tools)).strip()

    if use_tools and any(
        (t.get("function", t) or {}).get("name") == "edit_markdown" for t in tools
    ):
        from .toolcalls import edit_markdown_spec

        if "[edit_markdown 说明]" not in prompt:
            prompt = (prompt + "\n\n" + edit_markdown_spec()).strip()

    return prompt
