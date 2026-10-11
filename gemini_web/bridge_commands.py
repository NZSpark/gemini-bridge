"""Bridge 聊天命令（``/bridge ...``）：在对话里查询桥接状态、管理当前会话桶。

设计约定见 ``doc/bridge_command.md``（姊妹项目 ChatGPTBridge 的同名文档 + 本项目的落地版），
要点：

* **精确匹配**：只有「整条消息就是那一行命令」才执行——历史消息里的旧命令不重放，
  正文 / 多行文本 / 代码块里出现的命令字样一律当普通提问（见 :func:`parse_command`）。
* **只作用于当前会话桶**：命令**没有**「指定别的桶」的参数；桶由 ``X-Gemini-Session`` /
  ``user`` / User-Agent 决定（见 ``server._session_key``），多用户 / 多 Agent 不会互相影响。
* **本地应答**：命令由桥直接回复、**不发给 Gemini 网页版**，因此句柄失效、浏览器
  不可用时这些命令仍然可用（只读命令尤其如此；路由层的可用性检查见 :func:`is_command`）。
* **不泄露**：回复只含状态摘要，不打印 ``.env`` 值、登录 Cookie、令牌或其它桶的信息。
* **复用现有实现**：``session reset`` 走 ``driver.reset_session``、``session reseed`` 走
  ``driver.reseed_session``、``settings save-files`` 走 driver 的按桶偏好，聊天命令与
  HTTP 端点 / 请求路径共用同一套业务逻辑，不另写一套规则。

与本项目无关的 ChatGPTBridge 命令**没有**实现，避免造出假的语义：``session link`` /
``unlink``（本项目不做网页会话绑定，每桶始终新开对话 + 播种历史）、``settings think``
（Gemini 侧没有可靠的思考模式开关）、``/reset``、``/clear``、``/retry``、``/cancel``、
``/shell`` 等（理由见文档 §4）。
"""

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from . import config, models
from .errors import DEFAULT_SESSION_KEY
from .models import ChatCompletionResponse, Choice, ChoiceMessage, Usage
from .prompting import _content_to_text, estimate_tokens

logger = logging.getLogger(__name__)

#: 命令命名空间。只识别这一个前缀；其它 ``/`` 开头的文本照旧交给网页版。
BRIDGE_COMMAND = "/bridge"
#: ``/bridge help`` 支持的主题；``session`` / ``settings`` 分别对应下面两组命令。
HELP_TOPICS = ("session", "settings")
#: ``/bridge settings`` 支持的设置项名字。
SETTING_NAMES = ("save-files",)
#: ``/bridge settings <name>`` 的取值：on / off = 本桶偏好；default = 跟随全局配置。
SETTING_VALUES = ("on", "off", "default", "status")
#: ``/bridge session`` 的子命令。
SESSION_SUBCOMMANDS = ("status", "reset", "reseed")

_USAGE = (
    f"{BRIDGE_COMMAND} 用法：\n"
    f"  {BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]     查看命令列表 / 分主题帮助\n"
    f"  {BRIDGE_COMMAND} status                    桥接与当前会话桶状态摘要\n"
    f"  {BRIDGE_COMMAND} models                    本桥对外公布的模型名（同 /v1/models）\n"
    f"  {BRIDGE_COMMAND} session [status]          当前桶的会话状态摘要\n"
    f"  {BRIDGE_COMMAND} session reset             下一轮开新网页会话并播种完整历史\n"
    f"  {BRIDGE_COMMAND} session reseed            下一轮把完整历史再播种一遍（一次性）\n"
    f"  {BRIDGE_COMMAND} settings save-files [status|on|off|default]\n"
    f"                                             按桶保存的落盘偏好（不带取值 = 查看当前值）"
)


@dataclass(frozen=True)
class BridgeCommand:
    """一条已被识别的 ``/bridge`` 命令。"""

    action: str  # "help" | "status" | "models" | "session" | "settings" | "invalid"
    subaction: str = ""  # help 主题 / session 子命令 / settings 的查询或写入
    name: str = ""  # settings 名字（save-files）
    value: Optional[bool] = None  # settings 目标值（None = 跟随全局默认）
    reason: str = ""  # 命令未执行的原因（invalid 时展示给用户）


# ==================== 命令清单（帮助的唯一来源）====================


@dataclass(frozen=True)
class CommandSpec:
    """帮助里的一个条目。只列当前版本**真正注册**的命令，避免帮助与实现漂移。"""

    syntax: str
    summary: str
    group: str  # "" = 顶层；"session" / "settings" = 分主题
    readonly: bool  # True = 只读；False = 会改变会话桶状态


REGISTRY: tuple[CommandSpec, ...] = (
    CommandSpec(f"{BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]",
                "查看命令列表 / 分主题帮助", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} status",
                "桥接、浏览器与当前会话桶的状态摘要", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} models",
                "本桥对外公布的模型名（与 GET /v1/models 同源）", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} session [status]",
                "当前桶的会话状态摘要", "session", True),
    CommandSpec(f"{BRIDGE_COMMAND} session reset",
                "下一轮开新网页会话，并把客户端历史播种进去", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session reseed",
                "下一轮把完整历史再播种一遍（一次性标记，落盘）", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} settings save-files [{'|'.join(SETTING_VALUES)}]",
                "代码块落盘偏好（按桶持久化；不带取值 = 查看）", "settings", False),
)


# ==================== 解析 ====================


def latest_user_text(messages: Optional[Sequence[Any]]) -> Optional[str]:
    """最后一条消息的文本，仅当它是 ``user`` 消息时返回（否则 ``None``）。

    只看最后一条：历史里的旧命令是**已经执行过**的会话内容，不能被重放执行。
    """
    if not messages:
        return None
    last = messages[-1]
    if getattr(last, "role", None) != "user":
        return None
    return _content_to_text(getattr(last, "content", None))


def parse_command(text: str) -> Optional[BridgeCommand]:
    """整条消息是否是一条 ``/bridge`` 命令；不是则返回 ``None``（照常发给网页版）。

    只有「整条消息就是这一行」才算命令：混在正文里、出现在多行文本里、或出现在历史
    消息里都当作普通提问交给模型。保留命名空间内**语法已匹配但参数不合法**的写法
    返回 ``invalid``（带用法），因为 ``/bridge`` 是桥自己保留的前缀，回一条用法提示
    比把它发给网页版更可解释；其它任何 ``/`` 开头的文本一律不是命令。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    tokens = lines[0].split()
    if not tokens or tokens[0].lower() != BRIDGE_COMMAND:
        return None

    tail = tokens[1:]
    if not tail:
        # 裸 `/bridge` = 命令发现入口（帮助），而不是「未知命令」
        return BridgeCommand("help")
    head, rest = tail[0].lower(), tail[1:]

    if head == "help":
        if len(rest) > 1:
            return _invalid(f"{BRIDGE_COMMAND} help 最多接受一个主题。")
        topic = rest[0].lower() if rest else ""
        if topic and topic not in HELP_TOPICS:
            listed = "、".join(HELP_TOPICS)
            return _invalid(f"没有这个帮助主题：{rest[0]}（可用主题：{listed}）。")
        return BridgeCommand("help", subaction=topic)
    if head == "status":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} status 不接受参数。")
        return BridgeCommand("status")
    if head == "models":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} models 不接受参数。")
        return BridgeCommand("models")
    if head == "session":
        return _parse_session(rest)
    if head == "settings":
        return _parse_settings(rest)
    return _invalid(f"未知子命令：{tail[0]}。")


def _invalid(reason: str) -> BridgeCommand:
    return BridgeCommand("invalid", reason=f"{reason}\n{_USAGE}")


def _parse_session(rest: List[str]) -> BridgeCommand:
    if not rest:
        return BridgeCommand("session", subaction="status")
    sub, args = rest[0].lower(), rest[1:]
    if sub not in SESSION_SUBCOMMANDS:
        listed = "、".join(SESSION_SUBCOMMANDS)
        return _invalid(f"session 没有这个子命令：{rest[0]}（可用：{listed}）。")
    if args:
        return _invalid(f"{BRIDGE_COMMAND} session {sub} 不接受参数。")
    return BridgeCommand("session", subaction=sub)


def _parse_settings(rest: List[str]) -> BridgeCommand:
    if not rest:
        listed = "、".join(SETTING_NAMES)
        return _invalid(f"settings 还需要一个设置名（可用：{listed}）。")
    name, args = rest[0].lower(), rest[1:]
    if name not in SETTING_NAMES:
        listed = "、".join(SETTING_NAMES)
        return _invalid(f"没有这个设置项：{rest[0]}（可用：{listed}）。")
    if not args or (len(args) == 1 and args[0].lower() == "status"):
        return BridgeCommand("settings", subaction="status", name=name)
    if len(args) > 1:
        return _invalid(f"settings {name} 最多接受一个取值。")
    value_token = args[0].lower()
    if value_token not in SETTING_VALUES:
        listed = "、".join(SETTING_VALUES)
        return _invalid(f"settings {name} 不支持取值 {args[0]}（可用：{listed}）。")
    if value_token == "status":
        return BridgeCommand("settings", subaction="status", name=name)
    # on / off = 本桶偏好；default = 跟随全局配置（值为 None）
    value: Optional[bool] = {"on": True, "off": False, "default": None}[value_token]
    return BridgeCommand("settings", subaction="set", name=name, value=value)


def is_command(messages: Optional[Sequence[Any]]) -> bool:
    """这批消息的最后一条是否会由桥自己应答（整条消息就是 ``/bridge ...``）。

    路由层用它把「桥内命令」从「需要浏览器才能处理的普通提问」里分出来：浏览器尚未
    初始化时命令仍应答得出来（这正是用户排障时最需要它们的时候）。
    """
    text = latest_user_text(messages)
    if text is None:
        return False
    return parse_command(text) is not None


# ==================== 落盘偏好的解析顺序（请求字段 > 本桶偏好 > 配置）====================


def save_files_enabled(driver, session_key: Optional[str], requested: Optional[bool]) -> bool:
    """本轮是否把回复里的代码块落盘。

    优先级：请求体里**显式**的 ``save_files`` > 该桶偏好（``/bridge settings save-files``，
    落盘持久化）> ``config.SAVE_FILES``（默认 false）。默认仍然关闭：落盘是本地扩展
    能力，标准 OpenAI 客户端并不知情。
    """
    if requested is not None:
        return bool(requested)
    getter = getattr(driver, "save_files_preference", None)
    if callable(getter):
        try:
            preference = getter(session_key)
        except Exception:  # noqa: BLE001
            preference = None
        # 只采信真正的三态值：假 driver / 替身返回的其它对象一律当作“未设置”，
        # 否则一次意外的真值就会在用户机器上静默写出文件。
        if isinstance(preference, bool):
            return preference
    return bool(config.SAVE_FILES)


# ==================== 执行 ====================


async def handle_command(
    messages: Optional[Sequence[Any]], driver, session_key: Optional[str] = None
) -> Optional[str]:
    """识别并执行聊天命令，返回桥自己给出的应答；不是命令时返回 ``None``。

    这是 ``/bridge`` 命令的**统一入口**：Chat Completions 与 Responses 两条协议路径
    都调用它。调用方拿到应答后直接回给客户端，**不会**把这条消息发给网页版；返回
    ``None`` 时则表示这是普通提问，照常走上游。
    """
    text = latest_user_text(messages)
    if text is None:
        return None
    command = parse_command(text)
    if command is None:
        return None

    bucket = session_key or DEFAULT_SESSION_KEY
    try:
        return await _execute(command, driver, bucket)
    except Exception as exc:  # noqa: BLE001
        # 失败要可解释，但不把内部堆栈 / 敏感信息发给客户端
        logger.warning("[命令] 执行 %s 失败：%r", BRIDGE_COMMAND, exc, exc_info=True)
        return f"[Bridge] 执行命令失败：{exc!r}"


async def _execute(command: BridgeCommand, driver, bucket: str) -> str:
    if command.action == "help":
        return _help_reply(command.subaction)
    if command.action == "status":
        return _status_reply(driver, bucket)
    if command.action == "models":
        return _models_reply()
    if command.action == "invalid":
        return f"[Bridge] 命令未执行：{command.reason}"
    if command.action == "session":
        return _session_reply(command, driver, bucket)
    if command.action == "settings":
        return _settings_reply(command, driver, bucket)
    return f"[Bridge] 命令未执行：未知命令。\n{_USAGE}"  # 防御：注册表与执行分支必须同步


def command_chat_response(request, reply: str) -> ChatCompletionResponse:
    """把命令应答包装成一条普通的 Chat Completions 回复。

    usage 按「没有上游 prompt」计（``prompt_tokens=0``），避免把桥自己的应答算成
    网页版用量。``/v1/responses`` 走 ``run_chat`` 的短路分支，同样不算上游用量。
    """
    completion_tokens = estimate_tokens(reply)
    return ChatCompletionResponse(
        model=getattr(request, "model", "gemini-chat"),
        choices=[Choice(
            index=0,
            message=ChoiceMessage(role="assistant", content=reply),
            finish_reason="stop",
        )],
        usage=Usage(
            prompt_tokens=0,
            completion_tokens=completion_tokens,
            total_tokens=completion_tokens,
        ),
    )


def _help_reply(topic: str) -> str:
    """分主题帮助：只列当前版本真正注册的命令，并标明是否只读。"""
    lines = [f"[Bridge] {BRIDGE_COMMAND} 命令列表（当前版本）："]
    for spec in REGISTRY:
        if topic and spec.group != topic:
            continue
        mark = "只读" if spec.readonly else "改状态"
        lines.append(f"  {spec.syntax}\n      {spec.summary}（{mark}）")
    if not topic:
        lines.append(
            f"  分主题帮助：{'、'.join(f'{BRIDGE_COMMAND} help {item}' for item in HELP_TOPICS)}"
        )
    lines.append("只读命令不导航网页、不触发生成；改状态的命令只影响当前会话桶。")
    lines.append(
        f"这些命令由桥本地应答，不发给 Gemini 网页版——浏览器未就绪时同样可用。"
    )
    return "\n".join(lines)


def _models_reply() -> str:
    """本桥对外公布的模型名：与 ``GET /v1/models`` 用同一份数据（models.advertised_models）。"""
    ids = [card["id"] for card in models.advertised_models()]
    lines = ["[Bridge] 本桥对外公布的模型（与 GET /v1/models 同源）："]
    lines.extend(f"  {model_id}" for model_id in ids)
    lines.append(
        "说明：这些是 API 层的模型名，供客户端配置使用；它们**不强制**切换 Gemini "
        "网页端实际选用的模型（那由登录账号与网页端设置决定）。"
    )
    return "\n".join(lines)


def _status_reply(driver, bucket: str) -> str:
    """桥接与当前桶的状态摘要：只读，不导航、不触发生成。

    「浏览器是否就绪」看的是**浏览器级**的 ``driver.page``，不是当前桶的页面：桶页面是
    惰性创建的，第一个请求到达前必然不存在——拿它当“未就绪”会谎报服务不可用（实测踩到过：
    浏览器明明好着，``/bridge status`` 却回「请求处理：不可用（浏览器尚未初始化）」）。
    """
    browser_page = getattr(driver, "page", None)
    bucket_page = _bucket_page(driver, bucket)
    init_error = getattr(driver, "init_error", None)
    stats = _session_stats(driver, bucket)
    cluster = _cluster_stats(driver)
    ready = browser_page is not None
    lines = ["[Bridge] 桥接状态："]
    if init_error:
        lines.append(f"  请求处理：不可用（浏览器初始化失败：{_one_line(init_error)}）")
    elif not ready:
        lines.append("  请求处理：不可用（浏览器尚未初始化）")
    else:
        lines.append("  请求处理：可用")
    lines.append(f"  浏览器：{'已就绪' if ready else '未初始化'}")
    lines.append(f"  当前桶：{bucket}")
    if bucket_page is not None:
        lines.append("  当前桶页面：已打开")
    else:
        lines.append("  当前桶页面：未打开（该桶有请求时按需惰性创建，不代表服务不可用）")
    lines.append(f"  会话分桶：{'开启' if config.SESSION_SCOPING else '关闭'}")
    lines.append(
        f"  按桶并行：{'开启' if config.PARALLEL_BUCKETS else '关闭'}"
        f"；桶上限：{config.MAX_SESSION_BUCKETS}"
        f"；已打开页面：{cluster.get('open_pages', 0)}"
    )
    if stats.get("last_error"):
        lines.append(f"  上次错误：{_one_line(stats['last_error'])}")
    if not ready:
        lines.append(
            "  下一步：检查登录状态与 user_data 是否被另一个实例占用；"
            "或看 GET /healthz 的 init_error。（只读命令在浏览器不可用时仍可用）"
        )
    return "\n".join(lines)


def _session_reply(command: BridgeCommand, driver, bucket: str) -> str:
    sub = command.subaction
    if sub == "status":
        return _session_status_reply(driver, bucket)
    if sub == "reset":
        return _session_reset_reply(driver, bucket)
    if sub == "reseed":
        return _session_reseed_reply(driver, bucket)
    return f"[Bridge] 命令未执行：未知会话子命令。\n{_USAGE}"


def _session_status_reply(driver, bucket: str) -> str:
    """当前桶的会话状态摘要。

    只查当前桶：**不**列出其它桶（``session_stats`` 里的 ``buckets`` 也不打印），
    否则多用户 / 多 Agent 场景会泄露别的会话使用情况。
    """
    stats = _session_stats(driver, bucket)
    turns = int(stats.get("turns") or 0)
    est_tokens = int(stats.get("est_tokens") or 0)
    lines = [f"[Bridge] 会话桶 {bucket} 的会话状态："]
    if stats.get("pending_rotation"):
        lines.append("  网页会话：已请求重置（下一轮开新网页会话并把完整历史播种进去）")
    elif stats.get("needs_seed") and turns > 0:
        lines.append(
            "  网页会话：已排队重新播种（下一轮把完整历史再发一遍——"
            "会话里会出现重复内容，这不是删除旧消息）"
        )
    elif stats.get("needs_seed"):
        lines.append("  网页会话：需要播种（该桶还没有网页会话历史，下一轮会重放客户端历史）")
    else:
        lines.append("  网页会话：已有上下文（之后只发增量）")
    lines.append(
        f"  轮数：{turns}；估算 tokens：{est_tokens}"
        f"（轮转阈值：{config.SESSION_MAX_TURNS} 轮 / {config.SESSION_MAX_TOKENS} tokens，0 = 禁用）"
    )
    if stats.get("cap_hit"):
        lines.append("  上次生成：因上下文长度上限中断（下一轮会轮转到新会话）")
    if stats.get("last_error"):
        lines.append(f"  上次错误：{_one_line(stats['last_error'])}")
    lines.append(f"  任务快照：{'开启' if config.TASK_SNAPSHOT_ENABLED else '关闭'}")
    lines.append(
        f"  管理：`{BRIDGE_COMMAND} session reset`（换新网页会话）、"
        f"`{BRIDGE_COMMAND} session reseed`（在当前会话里重播历史）"
    )
    return "\n".join(lines)


def _session_reset_reply(driver, bucket: str) -> str:
    """重置当前桶的**网页会话**：复用 driver.reset_session，只改状态、不碰页面。"""
    driver.reset_session(bucket)
    logger.info("[命令] key=%s 已按 %s session reset 请求重置网页会话。", bucket, BRIDGE_COMMAND)
    return (
        f"[Bridge] 已把会话桶 {bucket} 标记为「下一轮开新网页会话」。\n"
        "  影响范围：只影响这个会话桶，其它桶不动。\n"
        "  语义：下一轮会新开一条网页会话，并把客户端传来的完整历史**播种**进去（上下文不丢）。\n"
        "  注意：这不是删除客户端的对话历史，也不清除 Gemini 账户里的数据或远程会话记录。\n"
        f"  HTTP 等价入口：POST /session/reset?session={bucket}"
        "（设置了 RESET_TOKEN 时需带 X-Reset-Token）。"
    )


def _session_reseed_reply(driver, bucket: str) -> str:
    """排队「下一轮重新播种」：标记落盘，重启 / 并发都不会丢，成功发送后自动清除。"""
    driver.reseed_session(bucket)
    logger.info("[命令] key=%s 已按 %s session reseed 排队重新播种。", bucket, BRIDGE_COMMAND)
    return (
        f"[Bridge] 会话桶 {bucket} 已排队「下一轮重新播种」。\n"
        "  下一轮会把客户端传来的完整历史再发进**当前**网页会话"
        "（一次性标记，已落盘，重启后仍有效；成功发送后自动清除）。\n"
        "  注意：这不是清除旧消息，而是**再次发送**上下文——那条会话里会出现重复内容。\n"
        f"  要换一条新网页会话请用 `{BRIDGE_COMMAND} session reset`。"
    )


# ==================== 设置项（按桶持久化，不改进程全局配置）====================


def _preference_label(value: Optional[bool]) -> str:
    if value is None:
        return "跟随全局配置"
    return "开启" if value else "关闭"


def _settings_reply(command: BridgeCommand, driver, bucket: str) -> str:
    """``settings save-files``：本桶偏好 + 当前生效值 + 落盘行为，三件事分开讲。"""
    name = command.name
    if command.subaction == "set":
        _set_preference(driver, name, command.value, bucket)
        logger.info(
            "[命令] key=%s 已把设置 %s 记为 %s（%s）。",
            bucket, name, _preference_label(command.value), BRIDGE_COMMAND,
        )
        lines = [
            f"[Bridge] 已记录设置「{name}」（会话桶 {bucket}）：{_preference_label(command.value)}"
            "（按桶持久化，只有这个桶受影响）。"
        ]
    else:
        lines = [f"[Bridge] 设置「{name}」（会话桶 {bucket}）："]
    lines.extend(_save_files_lines(driver, bucket))
    lines.append(f"  用法：`{BRIDGE_COMMAND} settings {name} [{'|'.join(SETTING_VALUES)}]`")
    return "\n".join(lines)


def _get_preference(driver, bucket: str) -> Optional[bool]:
    getter = getattr(driver, "save_files_preference", None)
    if not callable(getter):
        return None
    try:
        value = getter(bucket)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, bool) else None


def _set_preference(driver, name: str, value: Optional[bool], bucket: str) -> None:
    setter = getattr(driver, "set_save_files_preference", None)
    if not callable(setter):
        raise RuntimeError("当前 driver 不支持按桶保存落盘偏好。")
    setter(value, bucket)


def _save_files_lines(driver, bucket: str) -> List[str]:
    """落盘行为的说明：写到哪、会不会覆盖、怎么关。（正文默认不落盘。）"""
    preference = _get_preference(driver, bucket)
    effective = bool(config.SAVE_FILES if preference is None else preference)
    lines = [
        f"  代码块落盘偏好：{_preference_label(preference)}"
        + ("（SAVE_FILES）" if preference is None else "（按桶持久化）"),
        f"  当前生效：{'开启' if effective else '关闭'}"
        "（优先级：请求的 save_files 字段 > 本桶偏好 > SAVE_FILES 配置）",
    ]
    if effective:
        lines.append(f"  落盘目录：{config.OUTPUT_DIR}（请求可用 output_dir 覆盖）")
        lines.append(
            "  落盘行为：每次回复**新建**文件（code_<时间戳>_<序号>_<随机>.<扩展名>；"
            "没有代码块时 response_<时间戳>_<随机>.md），不会覆盖已有文件。"
            "仅 /v1/chat/completions 的非流式回复会落盘（流式与 /v1/responses 不落盘）。"
        )
    lines.append(
        f"  关闭方式：`{BRIDGE_COMMAND} settings save-files off`（本桶）或请求里显式传 save_files=false。"
    )
    return lines


# ==================== driver 取值（对假 driver / 替身保持宽容）====================


def _bucket_page(driver, bucket: str):
    """当前桶的页面句柄；driver 没有该能力（测试替身）时退回 ``driver.page``。"""
    for name in ("bucket_page", "_page_for"):
        getter = getattr(driver, name, None)
        if callable(getter):
            try:
                return getter(bucket)
            except Exception:  # noqa: BLE001
                return None
    return getattr(driver, "page", None)


def _session_stats(driver, bucket: str) -> dict:
    getter = getattr(driver, "session_stats", None)
    if not callable(getter):
        return {}
    try:
        stats = getter(bucket)
    except Exception:  # noqa: BLE001
        return {}
    return stats if isinstance(stats, dict) else {}


def _cluster_stats(driver) -> dict:
    getter = getattr(driver, "cluster_stats", None)
    if not callable(getter):
        return {}
    try:
        stats = getter()
    except Exception:  # noqa: BLE001
        return {}
    return stats if isinstance(stats, dict) else {}


def _one_line(text: str, limit: int = 200) -> str:
    """只保留错误的第一行有效内容并限长。

    回复里不带换行，也不会把整段堆栈 / 多行内部报告倒进对话：只需要足以定位问题的
    一句话，其余细节留在日志里。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    flat = " ".join(lines[0].split()) if lines else ""
    return flat[:limit] + ("…" if len(flat) > limit else "")
