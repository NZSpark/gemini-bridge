"""OpenAI Responses API（Codex CLI 专用）兼容层。

Codex CLI 只发送 ``POST /v1/responses``（Responses API），不再支持 chat。
这里把 Responses 请求翻译成本项目内部的 Chat 语义，复用现有 driver / 会话分桶 /
工具解析，再把结果包装回 Responses 对象或命名 SSE 事件。

设计依据：doc/codex_support.md（§2 请求映射 / §3 响应映射 / §9 Pi 兼容性保障）。
硬约束：不改动 /v1/chat/completions，不修改共享组件的签名与行为。
"""

import json
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from . import config
from .driver import (
    GeminiBusyError,
    GeminiContextLimitError,
    GeminiTimeoutError,
)
from .driver import DEFAULT_SESSION_KEY
from .models import ChatCompletionRequest, ChatMessage, ToolCall
from .prompting import build_prompt, estimate_tokens
from . import tasks
from .toolcalls import _tool_names, parse_tool_calls


# ==================== 请求模型（宽松接收）====================


class ResponsesRequest(BaseModel):
    """Responses API 请求。用 extra="allow" 吞掉未知字段，绝不因 422 打断 Codex。"""

    model_config = ConfigDict(extra="allow")

    model: str = "gemini-chat"
    input: Optional[Any] = None
    instructions: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    max_output_tokens: Optional[int] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    # 本地扩展（Codex 不传，保持与 chat 一致的默认）
    save_files: Optional[bool] = None
    output_dir: Optional[str] = None


# ==================== 请求转换：Responses -> Chat ====================


def _tool_to_chat(tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Responses 的扁平工具 -> Chat 的嵌套 function 工具。strict 丢弃。"""
    if not isinstance(tool, dict):
        return None
    if tool.get("type") == "function" or "name" in tool:
        name = tool.get("name")
        if not name:
            return None
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            },
        }
    # 已是嵌套格式，直接透传
    if "function" in tool:
        return tool
    return None


def _content_parts_to_text(content: Any) -> str:
    """把 Responses 的 content（string / item 数组）里的文本抽出为纯文本。"""
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
                if part.get("type") in ("input_text", "output_text", "text"):
                    text = part.get("text")
                    if isinstance(text, str):
                        pieces.append(text)
        return "\n".join(pieces)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return str(content)


def _item_to_message(item: Dict[str, Any]) -> Optional[ChatMessage]:
    """单个 Responses input item -> ChatMessage。未知类型返回 None（宽松跳过）。"""
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    if itype == "message" or ("role" in item and "content" in item):
        role = item.get("role", "user")
        text = _content_parts_to_text(item.get("content"))
        return ChatMessage(role=role, content=text)
    if itype == "function_call":
        call = ToolCall(
            id=item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:16]}",
            function={"name": item.get("name", ""), "arguments": item.get("arguments", "{}")},
        )
        return ChatMessage(role="assistant", content=None, tool_calls=[call])
    if itype == "function_call_output":
        return ChatMessage(
            role="tool",
            content=item.get("output", ""),
            tool_call_id=item.get("call_id"),
        )
    return None


def to_chat_request(req: ResponsesRequest) -> ChatCompletionRequest:
    """Responses 请求 -> 内部 ChatCompletionRequest。"""
    messages: List[ChatMessage] = []
    if req.instructions:
        messages.append(ChatMessage(role="system", content=req.instructions))

    raw_input = req.input
    if isinstance(raw_input, str):
        messages.append(ChatMessage(role="user", content=raw_input))
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append(ChatMessage(role="user", content=item))
                continue
            converted = _item_to_message(item)
            if converted is not None:
                messages.append(converted)
            else:
                print(f"[responses] 跳过未知 input item: {item!r}")
    elif raw_input is None:
        pass
    else:
        messages.append(ChatMessage(role="user", content=str(raw_input)))

    tools = None
    if req.tools:
        tools = [t for t in (_tool_to_chat(x) for x in req.tools) if t]

    return ChatCompletionRequest(
        model=req.model,
        messages=messages,
        stream=req.stream,
        tools=tools,
        tool_choice=req.tool_choice,
        max_tokens=req.max_output_tokens,
        save_files=req.save_files,
        output_dir=req.output_dir,
    )


# ==================== 响应构造：Chat -> Responses ====================


def _usage_dict(prompt_tokens: int, completion_tokens: int) -> Dict[str, int]:
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def from_chat_response(
    reply: str,
    model: str,
    prompt_tokens: int,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把 driver 的回复构造为 Responses 非流式响应。"""
    rid = f"resp_{uuid.uuid4().hex[:16]}"
    output: List[Dict[str, Any]] = []

    if tool_calls:
        for call in tool_calls:
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:12]}",
                "call_id": f"call_{uuid.uuid4().hex[:16]}",
                "name": call["name"],
                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                "status": "completed",
            })
    else:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": reply, "annotations": []}],
        })

    completion_tokens = estimate_tokens(reply)
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": _usage_dict(prompt_tokens, completion_tokens),
    }


# ==================== 错误映射 ====================


def _error_payload(message: str, err_type: str) -> Dict[str, Any]:
    return {"error": {"message": message, "type": err_type}}


def _map_exception(exc: Exception) -> Tuple[int, str]:
    if isinstance(exc, GeminiContextLimitError):
        return 400, "context_length_exceeded"
    if isinstance(exc, GeminiBusyError):
        return 503, "upstream_busy"
    if isinstance(exc, GeminiTimeoutError):
        return 504, "timeout"
    if isinstance(exc, RuntimeError):
        return 502, "upstream_error"
    return 500, "server_error"


# ==================== 共享执行（流式/非流式都走这里）====================


def _resolve_session(request: ChatCompletionRequest, session_key: Optional[str], seed: bool) -> str:
    delta = build_prompt(request.messages, request.tools, request.tool_choice)
    seeded = build_prompt(
        request.messages,
        request.tools,
        request.tool_choice,
        seed=True,
        seed_max_chars=config.SEED_MAX_CHARS,
    )
    return seeded if seed else delta


async def run_chat(
    request: ChatCompletionRequest,
    driver,
    session_key: Optional[str],
    on_delta=None,
):
    """执行一次上游对话，返回 (reply, code_blocks, tool_calls)。

    与 server.py 的 chat 路径共用 driver / prompting / toolcalls，但不改其代码。
    """
    # 任务快照：记录本轮 messages，轮转播种时用它续接任务（不丢任务目标）。
    bucket = session_key or DEFAULT_SESSION_KEY
    tasks.record(bucket, request.messages)
    task_block = tasks.resume_block(bucket)

    delta_prompt = build_prompt(request.messages, request.tools, request.tool_choice)
    seeded_prompt = build_prompt(
        request.messages,
        request.tools,
        request.tool_choice,
        seed=True,
        seed_max_chars=config.SEED_MAX_CHARS,
        task_block=task_block,
    )
    prompt = seeded_prompt if driver.needs_seed(session_key) else delta_prompt
    if not prompt:
        raise ValueError("需要包含至少一条 user / tool 消息")

    wants_tools = bool(request.tools) and request.tool_choice != "none"
    reply, blocks = await driver.send_chat(
        prompt,
        on_delta=on_delta,
        seeded_prompt=seeded_prompt,
        key=session_key,
    )
    tool_calls = parse_tool_calls(reply, _tool_names(request.tools)) if wants_tools else []
    sent_prompt = driver.sent_prompt(session_key) or prompt
    return reply, blocks, tool_calls, sent_prompt


# ==================== 非流式入口 ====================


async def handle_responses(
    req: ResponsesRequest,
    session_key: Optional[str],
    driver,
) -> Any:
    """处理 /v1/responses。返回 dict 或 StreamingResponse。"""
    from fastapi.responses import JSONResponse

    if driver.page is None:
        return JSONResponse(
            status_code=503,
            content=_error_payload(
                "浏览器尚未就绪，请确认已完成登录。", "upstream_error"
            ),
        )

    chat_req = to_chat_request(req)
    if not chat_req.messages:
        return JSONResponse(
            status_code=400,
            content=_error_payload("input 不能为空", "invalid_request_error"),
        )

    if req.stream:
        from fastapi.responses import StreamingResponse

        return StreamingResponse(
            stream_responses(chat_req, driver, session_key),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        reply, _blocks, tool_calls, sent_prompt = await run_chat(chat_req, driver, session_key)
    except GeminiContextLimitError as exc:
        print("\n[ERR] responses: 上下文超限:")
        traceback.print_exc()
        return JSONResponse(status_code=400, content=_error_payload(str(exc), "context_length_exceeded"))
    except GeminiBusyError as exc:
        return JSONResponse(status_code=503, content=_error_payload(str(exc), "upstream_busy"))
    except GeminiTimeoutError as exc:
        return JSONResponse(status_code=504, content=_error_payload(str(exc), "timeout"))
    except ValueError as exc:
        return JSONResponse(status_code=400, content=_error_payload(str(exc), "invalid_request_error"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=_error_payload(str(exc), "upstream_error"))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse(status_code=500, content=_error_payload(str(exc), "server_error"))

    return from_chat_response(
        reply, req.model, estimate_tokens(sent_prompt), tool_calls or None
    )


# ==================== 流式 SSE（命名事件）====================


def _sse(event_type: str, payload: Dict[str, Any]) -> str:
    """命名 SSE 事件：event 名 + data（data 载荷自带 type，Codex 反序列化要求）。"""
    data = dict(payload)
    data["type"] = event_type
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_responses(
    request: ChatCompletionRequest,
    driver,
    session_key: Optional[str],
):
    """以 Responses 命名 SSE 事件流输出。"""
    import asyncio

    rid = f"resp_{uuid.uuid4().hex[:16]}"
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model

    base_response = {
        "id": rid,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": model,
        "output": [],
    }

    seq = 0

    def evt(event_type: str, payload: Dict[str, Any]) -> str:
        nonlocal seq
        payload = dict(payload)
        payload["sequence_number"] = seq
        seq += 1
        return _sse(event_type, payload)

    yield evt("response.created", {"response": base_response})

    wants_tools = bool(request.tools) and request.tool_choice != "none"

    # 工具模式默认先缓冲（要判断是不是 tool_calls）。可用 RESPONSES_TOOL_BUFFER=false
    # 关闭缓冲：此时工具模式下也会实时吐字，但若最终解析出 tool_calls，已发的文本事件
    # 不会撤回（Codex 会以 function_call 为准）。
    buffer_tools = wants_tools and config.RESPONSES_TOOL_BUFFER
    queue: "asyncio.Queue[tuple]" = asyncio.Queue()

    async def on_delta(piece: str):
        await queue.put(("delta", piece))

    async def runner():
        try:
            reply, blocks, tool_calls, sent_prompt = await run_chat(
                request, driver, session_key,
                on_delta=None if buffer_tools else on_delta,
            )
            await queue.put(("done", (reply, blocks, tool_calls, sent_prompt, None, None)))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            status, err_type = _map_exception(exc)
            await queue.put(("done", (None, [], [], None, str(exc), err_type)))

    task = asyncio.create_task(runner())

    # 文本形态的 output_item / content_part **延迟到确定本轮不是 tool_calls 之后再发**。
    # 早先无条件先发 message item 会让工具分支也占用 output_index 0，
    # 与 function_call 的 index 撞车（Codex 按 index 关联 item）。
    message_item_opened = False

    def open_message_item():
        """发 message item 的 added + content_part.added（幂等，只发一次）。"""
        nonlocal message_item_opened
        if message_item_opened:
            return []
        message_item_opened = True
        return [
            evt("response.output_item.added", {
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": msg_id,
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }),
            evt("response.content_part.added", {
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }),
        ]

    # 非工具模式：文本一定会走 message，先开 item，客户端才能流式收到 delta。
    if not wants_tools:
        for line in open_message_item():
            yield line

    streamed = ""
    result = None
    keepalive_s = config.RESPONSES_KEEPALIVE_S
    while result is None:
        try:
            kind, payload = await asyncio.wait_for(
                queue.get(),
                timeout=keepalive_s if keepalive_s and keepalive_s > 0 else None,
            )
        except asyncio.TimeoutError:
            yield ": keep-alive\n\n"
            continue
        if kind == "delta":
            streamed += payload
            if not message_item_opened:
                # 工具模式 + RESPONSES_TOOL_BUFFER=false：实时吐字时先补发 message item
                for line in open_message_item():
                    yield line
            yield evt("response.output_text.delta", {
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "delta": payload,
            })
        else:
            result = payload

    await task
    reply, blocks, tool_calls, sent_prompt, error, err_type = result

    if error:
        yield evt("response.failed", {
            "response": {
                **base_response,
                "status": "failed",
                "error": {"message": error, "type": err_type},
            }
        })
        return

    if tool_calls:
        # 工具调用：不发 message item（此前未开），每个 call 一个独立 output_index。
        # item id / call_id 全程复用同一对，added / delta / done / final_output 一致。
        final_output: List[Dict[str, Any]] = []
        for index, call in enumerate(tool_calls):
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            item_id = f"fc_{uuid.uuid4().hex[:12]}"
            args_str = json.dumps(call["arguments"], ensure_ascii=False)
            yield evt("response.output_item.added", {
                "output_index": index,
                "item": {
                    "type": "function_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": call["name"],
                    "arguments": "",
                    "status": "in_progress",
                },
            })
            yield evt("response.function_call_arguments.delta", {
                "item_id": item_id,
                "output_index": index,
                "delta": args_str,
            })
            yield evt("response.function_call_arguments.done", {
                "item_id": item_id,
                "output_index": index,
                "arguments": args_str,
            })
            final_item = {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": call["name"],
                "arguments": args_str,
                "status": "completed",
            }
            final_output.append(final_item)
            # 每个 function_call item 都单独发 done，index 与 added 对齐
            yield evt("response.output_item.done", {
                "output_index": index,
                "item": final_item,
            })
    else:
        # 文本形态：确保 message item 已开（工具模式缓冲后此处才补开）
        for line in open_message_item():
            yield line
        full = reply if reply is not None else streamed
        if not streamed and full:
            # 工具模式缓冲后未流式吐字，这里补发
            for i in range(0, len(full), 64):
                yield evt("response.output_text.delta", {
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": full[i:i + 64],
                })
        yield evt("response.output_text.done", {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "text": full,
        })
        yield evt("response.content_part.done", {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": full, "annotations": []},
        })
        final_output = [{
            "type": "message",
            "id": msg_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full, "annotations": []}],
        }]
        yield evt("response.output_item.done", {
            "output_index": 0,
            "item": final_output[0],
        })

    completed = {
        **base_response,
        "status": "completed",
        "output": final_output,
        "usage": _usage_dict(
            estimate_tokens(sent_prompt or ""), estimate_tokens(reply or streamed or "")
        ),
    }
    yield evt("response.completed", {"response": completed})
