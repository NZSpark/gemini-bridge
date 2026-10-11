"""FastAPI 应用与路由（OpenAI 兼容层）。"""

import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager, suppress
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import bridge_commands, config
from .driver import (
    DEFAULT_SESSION_KEY,
    GeminiBusyError,
    GeminiContextLimitError,
    GeminiTimeoutError,
    GeminiWebDriver,
)
from .models import (
    advertised_models,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    ModelCard,
    ModelListResponse,
    Usage,
)
from .prompting import build_prompt, estimate_tokens
from .responses import ResponsesRequest, handle_responses
from . import tasks
from .streaming import _stream_chat_completion, _stream_command_reply
from .toolcalls import (
    _tool_names,
    EDIT_MARKDOWN_TOOL,
    EDIT_MARKDOWN_TOOL_NAME,
    execute_edit_markdown,
    parse_tool_calls,
    should_register_edit_markdown,
    to_tool_call_models,
)
from .chat_io import prune_output_dir
from .logging_setup import setup_logging

logger = logging.getLogger(__name__)

# 统一日志面（T8.1）：GEMINI_DEBUG=1 → DEBUG，否则 INFO；格式含时间 / 级别 / 模块名。
# 幂等，且在 pytest 下不自动接管（测试输出保持干净）。
setup_logging()

driver = GeminiWebDriver()


def _prune_output_now() -> None:
    """启动 / 后台周期调用的落盘目录清理（T7.2）。"""
    try:
        removed = prune_output_dir(config.OUTPUT_DIR)
        if removed:
            logger.info("[清理] 已从 %s 回收 %s 个文件。", config.OUTPUT_DIR, removed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理落盘目录失败（%s）：%s", config.OUTPUT_DIR, exc)


async def _output_prune_loop() -> None:
    """按 OUTPUT_PRUNE_INTERVAL_S 周期清理（0 = 关闭，只保留启动清理）。"""
    interval = config.OUTPUT_PRUNE_INTERVAL_S
    if not interval or interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        _prune_output_now()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await driver.init()
        driver.init_error = None
    except Exception as exc:  # noqa: BLE001
        # 浏览器起不来时也让服务先启动：便于用 /healthz 定位问题，
        # 并让 /v1/chat/completions 返回可读错误，而不是整个进程直接挂掉
        driver.init_error = str(exc)
        logger.warning(
            "浏览器初始化失败：%s\n服务仍会启动，可用 GET /healthz 查看状态。", exc
        )
    # 启动时清理一次落盘目录；随后交给后台周期任务（T7.2）。
    _prune_output_now()
    prune_task = asyncio.create_task(_output_prune_loop())
    try:
        yield
    finally:
        prune_task.cancel()
        with suppress(asyncio.CancelledError):
            await prune_task
        await driver.close()


def _error_response(status_code: int, message: str, err_type: str):
    """以 OpenAI 兼容的 error 结构返回错误，而不是裸 500 字符串。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": status_code}},
    )


app = FastAPI(title="Gemini Web-to-API Bridge", lifespan=lifespan)


def _client_from_ua(ua: str) -> Optional[str]:
    """从 User-Agent 中提取客户端标识，用于自动按客户端分桶。

    不同的 AI 编程助手（Cline、Cursor、Aider 等）会发送各自独有的 User-Agent，
    例如 ``cline/3.2.1``、``cursor/0.48``。提取出名字部分即可作为桶标识，
    使不同客户端自动隔离到不同的 Gemini 会话，无需手动配置。

    返回值带 ``ua:`` 前缀，与显式传入的 session key 区分开，避免冲突。
    """
    # FastAPI 直接调用（单元测试）时，未被注入的 Header 默认值是 Header 对象而非 str，
    # 这里做类型防御，保证只接受真正的字符串 UA。
    if not isinstance(ua, str) or not ua:
        return None
    ua_lower = ua.lower()
    # 常见 AI 编程助手 / SDK 的关键词。用**词边界**匹配，避免子串误命中
    # （例如 "continue" 出现在别的 UA 里、"openai" 被 python SDK 泛化命中）。
    _KNOWN_CLIENTS = (
        "roo-code", "windsurf", "cline", "cursor", "aider",
        "copilot", "continue", "antigravity", "openai", "anthropic",
    )
    for name in _KNOWN_CLIENTS:
        if re.search(rf"(?:^|[^a-z0-9]){re.escape(name)}(?:[/\s;]|$)", ua_lower):
            return f"ua:{name}"
    # 通用 fallback：取 UA 第一个 token 的 product 部分
    # e.g. "python-httpx/0.27.0" → "ua:python-httpx"
    first_token = ua.split()[0] if ua else ""
    if "/" in first_token:
        product = first_token.split("/", 1)[0].strip().lower()
        # 只接受形如合法产品名的短串，避免把奇怪 UA 变成非法桶名
        if re.fullmatch(r"[a-z0-9._-]{1,32}", product):
            return f"ua:{product}"
    return None


def _session_key(
    request: ChatCompletionRequest,
    header_value: Optional[str],
    user_agent: Optional[str] = None,
) -> Optional[str]:
    """确定本次请求属于哪个会话桶（按任务隔离会话）。

    优先级（高 → 低）：

    1. ``X-Gemini-Session`` 请求头（可用 ``SESSION_KEY_HEADER`` 改名）；
    2. OpenAI 的 ``user`` 字段；
    3. **自动识别**：从 ``User-Agent`` 提取客户端名称，使不同客户端自动隔离。

    前两者都没有、且 User-Agent 也无法识别时返回 None（默认桶，全局共用）。
    取值会被消毒（只保留 ``[\\w.\\-:]``）并限长，避免变成非法文件名 / 超长 JSON 键。
    """
    if not config.SESSION_SCOPING:
        return None
    raw = header_value or ""
    if not raw:
        user = getattr(request, "user", None)
        raw = user if isinstance(user, str) else ""
    raw = raw.strip()
    if not raw and config.SESSION_SCOPING_BY_UA:
        # 自动按 User-Agent 分桶：不同客户端自动隔离到不同会话（可由参数关闭）
        raw = _client_from_ua(user_agent or "") or ""
    if not raw:
        return None
    sanitized = re.sub(r"[^\w.\-:]", "_", raw)[: max(1, config.SESSION_KEY_MAX_LEN)]
    return sanitized or None


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """健康检查：Pi 等客户端可用来探活。"""
    ready = driver.page is not None
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "browser_ready": ready,
            "headless": config.HEADLESS,
            "session": driver.session_stats(),
            "session_keys": driver.session_keys(),
            "session_scoping": config.SESSION_SCOPING,
            "cluster": driver.cluster_stats(),
            "init_error": driver.init_error,
        },
    )


@app.post("/session/reset", include_in_schema=False)
async def reset_session(
    session: Optional[str] = None,
    x_reset_token: Optional[str] = Header(None, alias="X-Reset-Token"),
):
    """手动逃生口：让指定会话桶的下一轮开新会话（历史会用“播种”重放，不丢上下文）。

    ``session`` 省略时重置默认桶；也可用 ``GEMINI_NEW_SESSION=true`` 在启动时重置。
    设置了 ``RESET_TOKEN`` 时必须带 ``X-Reset-Token`` 头，否则 403。
    """
    if config.RESET_TOKEN and x_reset_token != config.RESET_TOKEN:
        raise HTTPException(status_code=403, detail="RESET_TOKEN 校验失败")
    key = (session or "").strip() or None
    driver.reset_session(key)
    bucket = key or DEFAULT_SESSION_KEY
    return JSONResponse(
        content={
            "status": "ok",
            "session": bucket,
            "session_stats": driver.session_stats(bucket),
        }
    )


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Gemini Web-to-API Bridge",
        "openai_compatible": True,
        "endpoints": [
            "/v1/models",
            "/v1/chat/completions",
            "/v1/responses",
            "/healthz",
            "/debug/dom",
            "/session/reset",
        ],
    }


@app.get("/v1/models", response_model=ModelListResponse)
async def list_models():
    """Pi (models.json) 会用该端点做模型发现。

    ``context_window`` 透出 ``SESSION_MAX_TOKENS``（会话轮转预算），作为**唯一**权威
    数值：客户端据此裁剪上下文即可，不会再出现“代码 65536 / README 1000000 /
    端点又不透出”的三方矛盾（T8.7）。
    """
    return ModelListResponse(
        data=[
            ModelCard(id=m["id"], context_window=config.SESSION_MAX_TOKENS)
            for m in advertised_models()
        ]
    )


async def _require_bridge_token(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> None:
    """可选 Bearer 鉴权（T8.10）；``BRIDGE_TOKEN`` 留空时完全不校验（默认）。

    只保护**有副作用的生成端点**（``/v1/chat/completions``、``/v1/responses``）；
    ``/healthz`` 与 ``/v1/models`` 保持开放，便于探活与模型发现。
    默认关闭，因此对 Pi / Codex 的现有配置零影响。
    """
    token = config.BRIDGE_TOKEN
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(
            status_code=401,
            detail="BRIDGE_TOKEN 校验失败",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.post("/v1/responses", dependencies=[Depends(_require_bridge_token)])
async def responses(
    request: ResponsesRequest,
    x_gemini_session: Optional[str] = Header(None, alias=config.SESSION_KEY_HEADER),
    user_agent: Optional[str] = Header(None, alias="User-Agent"),
):
    """OpenAI Responses API（Codex CLI 专用）。

    复用 /v1/chat/completions 的会话分桶与 driver；具体转换与响应构造在
    ``gemini_web.responses``。此路由绝不改动 chat 路径的行为。
    """
    if not config.ENABLE_RESPONSES_API:
        raise HTTPException(status_code=404, detail="Responses API 未启用（ENABLE_RESPONSES_API=false）")
    session_key = _session_key(request, x_gemini_session, user_agent)
    if config.DEBUG:
        logger.debug("responses session_key=%r", session_key)
    return await handle_responses(request, session_key, driver)


@app.get("/debug/dom", include_in_schema=False)
async def debug_dom():
    """诊断用：返回当前页面「回复节点」与「疑似停止按钮控件」的结构（**不含正文**）。

    默认关闭（返回 404）：它会把会话的节点数量 / 长度 / 哈希暴露出去，
    属于信息泄露面。需要用 `GEMINI_DEBUG=1` 启动才启用。

    用法：在 Pi 发起一轮对话、Gemini 正在生成时反复 curl 该端点，
    即可看出两个结束判定信号（停止按钮 / 文本稳定）究竟有没有生效。
    """
    if not config.DEBUG:
        raise HTTPException(
            status_code=404,
            detail="调试端点默认关闭；请用 GEMINI_DEBUG=1 启动服务。",
        )
    if driver.page is None:
        raise HTTPException(status_code=503, detail="浏览器尚未初始化")

    nodes = await driver.page.query_selector_all(config.RESPONSE_SELECTORS)
    last_text = await nodes[-1].inner_text() if nodes else ""
    node_summaries: List[dict] = []
    for index, node in enumerate(nodes):
        try:
            node_text = await node.inner_text()
        except Exception:
            node_text = ""
        try:
            cls = await node.get_attribute("class") or ""
        except Exception:
            cls = ""
        node_summaries.append({
            "index": index,
            "class": cls,
            "text_length": len(node_text),
            "sha1": hashlib.sha1(node_text.encode("utf-8")).hexdigest(),
        })
    # 注意：**不回显正文**。判断“结束判定是否生效”只需要长度与 sha1
    # （生成中变化、结束后稳定），所以这里不再输出 head / tail。
    return {
        "response_node_count": len(nodes),
        "nodes": node_summaries,
        "last_node": {
            "text_length": len(last_text),
            "sha1": hashlib.sha1(last_text.encode("utf-8")).hexdigest(),
        },
        "generating": await driver._page_is_generating(),
        "stop_candidates": await driver.debug_stop_candidates(),
    }


def _run_local_edit_markdown(tool_calls):
    """本地执行 edit_markdown，把结构化结果挂回对应调用。未开启时原样返回。"""
    if not config.EDIT_MARKDOWN_LOCAL or not tool_calls:
        return tool_calls
    out = []
    for call in tool_calls:
        if call.get("name") == EDIT_MARKDOWN_TOOL_NAME:
            result = execute_edit_markdown(
                call.get("arguments") or {},
                backup_dir=config.EDIT_MARKDOWN_BACKUP_DIR,
            )
            out.append({**call, "result": result})
        else:
            out.append(call)
    return out


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(_require_bridge_token)],
)
async def chat_completions(
    request: ChatCompletionRequest,
    x_gemini_session: Optional[str] = Header(None, alias=config.SESSION_KEY_HEADER),
    user_agent: Optional[str] = Header(None, alias="User-Agent"),
):
    if not request.messages:
        return _error_response(400, "messages 不能为空", "invalid_request_error")

    # 桥内命令（``/bridge ...``）由桥自己应答、**不经过网页版**：浏览器没起来也必须能答，
    # 页面失效 / 登录过期时它们正是用户唯一的排障入口。普通提问仍按下面的检查 503。
    if driver.page is None and not bridge_commands.is_command(request.messages):
        return _error_response(
            503,
            "浏览器尚未就绪，请确认已完成登录、且没有另一个实例占用 user_data。"
            f"初始化错误：{driver.init_error or '无'}",
            "unavailable",
        )

    # 按任务隔离会话：同一客户端 / 同一 X-Gemini-Session 取值的请求共用一条网页会话
    session_key = _session_key(request, x_gemini_session, user_agent)
    if config.DEBUG:
        logger.debug("session_key=%r", session_key)
    bucket = session_key or DEFAULT_SESSION_KEY

    # 命令在这里求值一次（有副作用的命令不能被求值两次）：命中就由桥直接回、不走上游。
    command_reply = await bridge_commands.handle_command(request.messages, driver, session_key)
    if command_reply is not None:
        logger.info("[命令] %s 已由桥直接应答（session_key=%r）。", bridge_commands.BRIDGE_COMMAND, bucket)
        if request.stream:
            return StreamingResponse(
                _stream_command_reply(request, command_reply),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return bridge_commands.command_chat_response(request, command_reply)

    # 任务快照：记录本轮 messages，供轮转播种时续接任务（不丢任务目标）。
    tasks.record(bucket, request.messages)
    task_block = tasks.resume_block(bucket)

    if should_register_edit_markdown(request.tools):
        request.tools = list(request.tools or []) + [EDIT_MARKDOWN_TOOL]

    # 两份文本：增量版（现有会话已有上下文）与播种版（新会话 / 轮转后需要重放历史）。
    # 到底用哪份由 driver 决定（只有它知道当前网页会话是否还有历史）。
    delta_prompt = build_prompt(request.messages, request.tools, request.tool_choice)
    seeded_prompt = build_prompt(
        request.messages,
        request.tools,
        request.tool_choice,
        seed=True,
        seed_max_chars=config.SEED_MAX_CHARS,
        task_block=task_block,
    )

    # 预判：会话没有历史时 driver 会用“播种”prompt。仅用于下面的空输入快速失败；
    # 真正发出去的那份（以及 usage）以 driver.sent_prompt(session_key) 为准。
    prompt = seeded_prompt if driver.needs_seed(session_key) else delta_prompt
    # 真正要发的那份是空的就直接报错。不能拖到发出去再等：
    # 空输入会让网页版什么都不做，客户端只能等到 180s 超时，很难排查。
    if not prompt:
        return _error_response(400, "需要包含至少一条 user / tool 消息", "invalid_request_error")

    # ---------- 流式分支（Pi 默认 stream=true）----------
    if request.stream:
        return StreamingResponse(
            _stream_chat_completion(request, prompt, driver, seeded_prompt, session_key),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------- 非流式分支 ----------
    try:
        reply_content, code_blocks = await driver.send_chat(
            prompt, seeded_prompt=seeded_prompt, key=session_key
        )
    except GeminiContextLimitError as exc:
        logger.warning("网页会话已达上下文长度上限：%s", exc)
        return _error_response(400, str(exc), "context_length_exceeded")
    except GeminiBusyError as exc:
        # 本地保护：同一会话桶已有请求在跑且等锁超时。稍后重试即可，不是上游故障。
        logger.warning("上游繁忙：%s", exc)
        return _error_response(503, str(exc), "upstream_busy")
    except GeminiTimeoutError as exc:
        logger.error("等待 Gemini 回复超时（已重试）：%s", exc, exc_info=True)
        return _error_response(504, str(exc), "timeout")
    except RuntimeError as exc:
        # 浏览器不可用 / 找不到输入框等上游问题
        logger.error("上游浏览器不可用：%s", exc, exc_info=True)
        return _error_response(502, str(exc), "upstream_error")
    except Exception as exc:  # noqa: BLE001
        logger.error("处理请求失败：%s", exc, exc_info=True)
        return _error_response(500, str(exc), "server_error")

    # usage 用真正发出去的 prompt 估算（driver 可能选了播种版 / 中途轮转过）。
    # 按会话桶读取，并发时不会拿到别的 Agent 的 prompt；回退到预判值兼容假 driver。
    sent_prompt = driver.sent_prompt(session_key) or prompt
    wants_tools = bool(request.tools) and request.tool_choice != "none"
    tool_calls = parse_tool_calls(reply_content, _tool_names(request.tools)) if wants_tools else []
    tool_calls = _run_local_edit_markdown(tool_calls)

    if tool_calls:
        return ChatCompletionResponse(
            model=request.model,
            choices=[Choice(
                index=0,
                message=ChoiceMessage(role="assistant", content=None, tool_calls=to_tool_call_models(tool_calls)),
                finish_reason="tool_calls",
            )],
            usage=Usage(
                prompt_tokens=estimate_tokens(sent_prompt),
                completion_tokens=estimate_tokens(reply_content),
                total_tokens=estimate_tokens(sent_prompt) + estimate_tokens(reply_content),
            ),
        )

    saved_files = []
    # 优先级：请求里显式的 save_files > 本桶偏好（/bridge settings save-files）> SAVE_FILES
    save_files = bridge_commands.save_files_enabled(driver, session_key, request.save_files)
    if save_files:
        saved_files = driver.save_extracted_files(
            reply_content, code_blocks, request.output_dir or config.OUTPUT_DIR
        )

    return ChatCompletionResponse(
        model=request.model,
        choices=[Choice(
            index=0,
            message=ChoiceMessage(role="assistant", content=reply_content),
            finish_reason="stop",
        )],
        usage=Usage(
            prompt_tokens=estimate_tokens(sent_prompt),
            completion_tokens=estimate_tokens(reply_content),
            total_tokens=estimate_tokens(sent_prompt) + estimate_tokens(reply_content),
        ),
        saved_files=saved_files,
    )
