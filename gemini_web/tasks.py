"""任务快照：让网页会话轮转（上下文到顶）后仍能无损续接任务。

背景：网页版会话一旦到顶，driver 会轮转到新会话并“播种”历史。但播种只依赖
**当前这一次请求**的 messages，且 ``SEED_MAX_CHARS`` 会从尾部截断历史——若任务目标
（“把这个仓库重构为 X”）出现在很早的消息里，轮转后就会被截掉，表现为“丢了任务”。

本模块为每个会话桶维护一份轻量快照：
  * ``goal``：该任务最初的目标（第一条 user 消息），**永不被截断**；
  * ``recent``：最近若干条消息的纯文本，作为历史的额外保险；
  * ``updated_at`` / ``turns``：观测用。

轮转播种时，``resume_block()`` 生成一段 ``[任务状态]`` 文本，由 prompting 放在
播种内容的最前面，确保目标始终传达给新会话。
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .models import ChatMessage
from .prompting import _content_to_text


def _namespace() -> str:
    """当前项目的任务命名空间（不同「桥」项目必须不同）。"""
    return getattr(config, "TASK_NAMESPACE", "") or "default"


def _dir() -> Path:
    # 按命名空间分子目录：即使多个项目共用同一个 TASK_FILE_DIR，也互不覆盖
    return Path(config.TASK_FILE_DIR) / _namespace()


def _file(bucket: str) -> Path:
    # bucket 已经过 _session_key 消毒（只保留 [\w.\-:]），这里再兜底一次文件名安全
    safe = "".join(ch if ch.isalnum() or ch in ".-_:" else "_" for ch in bucket) or "default"
    return _dir() / f"{safe}.json"


def _goal_from_messages(messages: List[ChatMessage]) -> str:
    """取任务目标：第一条**真正的** user 消息（跳过 system 与环境包装块）。

注意：Codex / 部分 Agent 会在每轮最前面自动注入 ``<environment_context>`` 之类的
环境元信息（cwd、权限、时间等）。它不是“用户想做的事”，若当成 goal 存下来，
resume 时就会看到“目标 = 另一个项目的 cwd”这种串台错觉。因此这里跳过纯包装块。
"""
    fallback = ""
    for message in messages:
        if message.role != "user":
            continue
        text = _content_to_text(message.content).strip()
        if not text:
            continue
        if _is_environment_wrapper(text):
            # 仅作为兜底：整段对话里若只有环境块，才用它，避免 goal 为空
            fallback = fallback or text
            continue
        if _is_meta_prompt(text):
            # 元提示（交接/摘要请求）不是任务目标，也不当兜底
            continue
        return text[: max(1, config.TASK_GOAL_MAX_CHARS)]
    return fallback[: max(1, config.TASK_GOAL_MAX_CHARS)]


# Agent 自动注入的环境/元信息包装：整条消息只由这些标签构成时，不算“任务目标”。
# 除 environment_context 外，Codex 还会注入 skills_instructions、permissions
# instructions、collaboration_mode 等——它们都不是用户的真实意图，必须一并排除，
# 否则会被当正文写进快照 recent，轮转播种时把 prompt 灌成一大段系统提示。
_ENV_WRAPPER_RE = re.compile(
    r"^\s*<(environment_context|user_instructions|system_instructions|env"
    r"|skills_instructions|permissions[_ ]instructions|collaboration_mode)\b",
    re.IGNORECASE,
)


def _wrap_recent_item(text: str) -> str:
    """把单条 recent 文本裁到 TASK_RECENT_ITEM_MAX_CHARS，避免超长系统块占满预算。"""
    limit = max(1, config.TASK_RECENT_ITEM_MAX_CHARS)
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def _is_environment_wrapper(text: str) -> bool:
    """消息是否只是 Agent 注入的环境元信息（而非用户真实意图）。"""
    stripped = text.strip()
    if not _ENV_WRAPPER_RE.match(stripped):
        return False
    # 环境块通常以同名闭合标签结尾；只要没有明显的自然语言正文就判定为包装
    return "\n" in stripped or stripped.endswith(">")


# 客户端注入的“元提示”（上下文压缩 / 交接摘要请求）：以 user 角色出现在历史里，
# 要求模型输出 {"summary": ..., "next_action": ...}。它不是用户的真实任务；
# 一旦被存成 goal，新 bucket 播种时会以「任务目标：」的口吻重新注入，
# 模型就会转去写摘要 JSON，真实任务因此中断。
_META_PROMPT_RE = re.compile(
    r"write a brief catch-up"
    r"|return json with summary"
    r"|\bnext_action\b"
    # Codex CLI 每轮会先发一条“生成任务标题”的元提示；它不是用户意图，
    # 一旦被当成 goal 存下，播种时会以「任务目标：」口吻重新注入，污染任务。
    r"|generate a concise,? single-line task title"
    r"|single-line task title"
    r"|do not answer the request"
    r"|只生成.*标题|生成.*任务标题"
    # “只回复/仅输出 N 个字”这类输出约束通常是 harness 自测/元任务的残留，
    # 不是用户的真实任务；一旦被存成 goal，播种时会以「任务目标：」口吻注入，
    # 与真实请求冲突（模型会照着字数约束只回一句话）。
    r"|只回复|仅回复|只输出|仅输出|只回复\S{0,4}字|回复\S{0,4}字",
    re.IGNORECASE,
)


def _is_meta_prompt(text: str) -> bool:
    """是否为客户端注入的摘要/交接类元提示（只看开头，避免长正文误伤）。"""
    return bool(_META_PROMPT_RE.search((text or "")[:800]))


def _recent_texts(messages: List[ChatMessage]) -> List[Dict[str, str]]:
    """最近 N 条消息（role + 文本），滚动保留。"""
    keep = max(0, config.TASK_KEEP_MESSAGES)
    picked = [m for m in messages if m.role != "system"][-keep:] if keep else []
    out: List[Dict[str, str]] = []
    for message in picked:
        text = _content_to_text(message.content).strip()
        if text and not _is_environment_wrapper(text) and not _is_meta_prompt(text):
            out.append({"role": message.role, "text": _wrap_recent_item(text)})
    return out


def load(bucket: str) -> Dict[str, Any]:
    try:
        raw = _file(bucket).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # 归属校验：旧版本可能把别的项目/别的命名空间的快照写在同一个位置，
    # 一旦发现 namespace 不匹配就视为无效，避免串台。
    owner = data.get("namespace")
    if owner is not None and owner != _namespace():
        return {}
    return data


def record(bucket: str, messages: List[ChatMessage]) -> None:
    """每轮把当前 messages 快照写入任务文件（goal 只首次写入，之后保留）。"""
    if not config.TASK_SNAPSHOT_ENABLED:
        return
    data = load(bucket)
    goal = data.get("goal") or ""
    if _is_meta_prompt(goal):
        # 自愈：早先把元提示误存成了 goal，丢弃并重新挑选真实目标
        goal = ""
    goal = goal or _goal_from_messages(messages)
    payload = {
        "namespace": _namespace(),
        "bucket": bucket,
        "goal": goal,
        "recent": _recent_texts(messages),
        "turns": int(data.get("turns") or 0) + 1,
        "updated_at": int(time.time()),
    }
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        _file(bucket).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def resume_block(bucket: str) -> str:
    """生成轮转播种用的“任务续接”块；无任务文件时返回空串。

    放在播种内容最前面，保证即使历史被 SEED_MAX_CHARS 截断，任务目标仍能传达。
    """
    data = load(bucket)
    goal = (data.get("goal") or "").strip()
    if _is_meta_prompt(goal):
        goal = ""  # 污染过的 goal 绝不能以「任务目标：」口吻注入，否则会中断任务
    recent = data.get("recent")
    if not goal and not recent:
        return ""
    lines = ["[任务状态] 这是一个正在进行的任务，请据此继续，不要从头重做。"]
    if goal:
        lines.append(f"任务目标：{goal}")
    if isinstance(recent, list) and recent:
        lines.append("最近进展：")
        for item in recent[-max(1, config.TASK_KEEP_MESSAGES):]:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "?"
            text = (item.get("text") or "").strip()
            if text:
                lines.append(f"- [{role}] {text}")
    return "\n".join(lines)
