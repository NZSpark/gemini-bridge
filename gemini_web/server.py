"""FastAPI 应用与路由（OpenAI 兼容层）。"""

import hashlib
import re
import traceback
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import config
from .driver import (
    DEFAULT_SESSION_KEY,
    GeminiBusyError,
    GeminiContextLimitError,
    GeminiTimeoutError,
    GeminiWebDriver,
)
from .models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    ModelCard,
    ModelListResponse,
    SUPPORTED_MODELS,
    Usage,
)
from .prompting import build_prompt, estimate_tokens
from .responses import ResponsesRequest, handle_responses
from . import tasks
from .streaming import _stream_chat_completion
from .toolcalls import _tool_names, parse_tool_calls, to_tool_call_models

driver = GeminiWebDriver()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await driver.init()
        driver.init_error = None
    except Exception as exc:  # noqa: BLE001
        # 浏览器起不来时也让服务先启动：便于用 /healthz 定位问题，
        # 并让 /v1/chat/completions 返回可读错误，而不是整个进程直接挂掉
        driver.init_error = str(exc)
        print(
            f"\n[启动警告] 浏览器初始化失败：{exc}\n"
            "服务仍会启动，可用 GET /healthz 查看状态。\n"
        )
    yield
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
async def reset_session(session: Optional[str] = None):
    """手动逃生口：让指定会话桶的下一轮开新会话（历史会用“播种”重放，不丢上下文）。

    ``session`` 省略时重置默认桶；也可用 ``GEMINI_NEW_SESSION=true`` 在启动时重置。
    """
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
    """Pi (models.json) 会用该端点做模型发现。"""
    candidates = {m["id"]: m for m in SUPPORTED_MODELS}
    candidates.setdefault("gemini-chat", {"id": "gemini-chat"})
    return ModelListResponse(
        data=[ModelCard(id=m["id"]) for m in candidates.values()]
    )


@app.post("/v1/responses")
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
        print(f"[debug] responses session_key={session_key!r}")
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


@app.get("/debug/snippet", include_in_schema=False)
async def debug_snippet():
    """临时诊断：dump 最新回复节点里代码块 (pre/code) 的结构与复制按钮。

    仅 GEMINI_DEBUG=1 时可用。用于定位 Gemini 把 tool_call 渲染成 Code snippet 后，
    究竟把原始 JSON 放在哪里（inner_text / textContent / 复制按钮的剪贴板）。
    """
    if not config.DEBUG:
        raise HTTPException(status_code=404, detail="调试端点默认关闭")
    if driver.page is None:
        raise HTTPException(status_code=503, detail="浏览器尚未初始化")

    js = r"""
    (selectors) => {
      const nodes = document.querySelectorAll(selectors);
      let last = null;
      for (let i = nodes.length - 1; i >= 0; i--) {
        const t = (nodes[i].innerText || '').trim();
        if (t) { last = nodes[i]; break; }
      }
      if (!last) return {found: false};
      const pres = last.querySelectorAll('pre');
      const blocks = [];
      for (const pre of pres) {
        const code = pre.querySelector('code');
        const target = code || pre;
        // 收集 pre/code 附近（其父级往上两层内）的所有按钮类控件
        const scope = pre.closest('div') || pre.parentElement || pre;
        const btns = [];
        const cand = scope.querySelectorAll('button,[role=button],mat-icon,[aria-label],[data-test-id]');
        for (const b of cand) {
          btns.push({
            tag: b.tagName.toLowerCase(),
            cls: b.className || '',
            aria: b.getAttribute('aria-label') || '',
            title: b.getAttribute('title') || '',
            dataTestId: b.getAttribute('data-test-id') || '',
            text: (b.innerText || '').trim().slice(0, 40),
          });
        }
        blocks.push({
          codeClass: code ? (code.className || '') : null,
          innerText: target.innerText,
          textContent: target.textContent,
          preOuterHead: pre.outerHTML.slice(0, 300),
          buttons: btns,
        });
      }
      return {found: true, preCount: pres.length, blocks};
    }
    """
    data = await driver.page.evaluate(js, config.RESPONSE_SELECTORS)
    return data


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    x_gemini_session: Optional[str] = Header(None, alias=config.SESSION_KEY_HEADER),
    user_agent: Optional[str] = Header(None, alias="User-Agent"),
):
    if not request.messages:
        return _error_response(400, "messages 不能为空", "invalid_request_error")

    if driver.page is None:
        return _error_response(
            503,
            "浏览器尚未就绪，请确认已完成登录、且没有另一个实例占用 user_data。"
            f"初始化错误：{driver.init_error or '无'}",
            "unavailable",
        )

    # 按任务隔离会话：同一客户端 / 同一 X-Gemini-Session 取值的请求共用一条网页会话
    session_key = _session_key(request, x_gemini_session, user_agent)
    if config.DEBUG:
        print(f"[debug] session_key={session_key!r}")

    # 任务快照：记录本轮 messages，供轮转播种时续接任务（不丢任务目标）。
    bucket = session_key or DEFAULT_SESSION_KEY
    tasks.record(bucket, request.messages)
    task_block = tasks.resume_block(bucket)

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
        print("\n[ERR] 网页会话已达上下文长度上限:")
        traceback.print_exc()
        return _error_response(400, str(exc), "context_length_exceeded")
    except GeminiBusyError as exc:
        # 本地保护：同一会话桶已有请求在跑且等锁超时。稍后重试即可，不是上游故障。
        print(f"\n[繁忙] {exc}")
        return _error_response(503, str(exc), "upstream_busy")
    except GeminiTimeoutError as exc:
        print("\n[ERR] 等待 Gemini 回复超时（已重试）:")
        traceback.print_exc()
        return _error_response(504, str(exc), "timeout")
    except RuntimeError as exc:
        # 浏览器不可用 / 找不到输入框等上游问题
        print("\n[ERR] 上游浏览器不可用:")
        traceback.print_exc()
        return _error_response(502, str(exc), "upstream_error")
    except Exception as exc:  # noqa: BLE001
        print("\n[ERR] 处理请求失败:")
        traceback.print_exc()
        return _error_response(500, str(exc), "server_error")

    # usage 用真正发出去的 prompt 估算（driver 可能选了播种版 / 中途轮转过）。
    # 按会话桶读取，并发时不会拿到别的 Agent 的 prompt；回退到预判值兼容假 driver。
    sent_prompt = driver.sent_prompt(session_key) or prompt
    wants_tools = bool(request.tools) and request.tool_choice != "none"
    tool_calls = parse_tool_calls(reply_content, _tool_names(request.tools)) if wants_tools else []

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
    if request.save_files:
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
