"""把一次上游对话编码成 OpenAI 兼容的 SSE 流。"""

import asyncio
import json
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

from . import config
from .driver import GeminiBusyError, GeminiContextLimitError, GeminiTimeoutError
from .models import ChatCompletionRequest
from .prompting import estimate_tokens
from .toolcalls import _tool_names, parse_tool_calls


def _chunk_text(text: str, size: int = 64) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def _stream_chat_completion(
    request: ChatCompletionRequest,
    prompt: str,
    driver,
    seeded_prompt: Optional[str] = None,
    session_key: Optional[str] = None,
):
    """以 OpenAI SSE 格式输出 chunk，兼容 Pi 的 openai-completions 流式解析。

    ``driver`` 由调用方（server）注入，避免与本模块形成循环依赖。
    ``seeded_prompt`` 在需要轮转到新会话时使用（重放历史）。
    ``session_key`` 为按任务隔离会话的桶（None = 默认桶）。
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model
    wants_tools = bool(request.tools) and request.tool_choice != "none"

    def encode(delta: Optional[Dict[str, Any]], finish: Optional[str] = None,
               choices: Optional[list] = None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices if choices is not None else [
                {"index": 0, "delta": delta or {}, "finish_reason": finish}
            ],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 先发 role 头
    yield encode({"role": "assistant"})

    queue: "asyncio.Queue[tuple]" = asyncio.Queue()

    async def on_delta(piece: str):
        await queue.put(("delta", piece))

    async def runner():
        try:
            # 需要工具时先缓冲（等解析出 tool_calls 再决定输出形态），因此不实时吐字
            reply, blocks = await driver.send_chat(
                prompt,
                on_delta=None if wants_tools else on_delta,
                seeded_prompt=seeded_prompt,
                key=session_key,
            )
            await queue.put(("done", (reply, blocks, None, None)))
        except GeminiContextLimitError as exc:
            # 给客户端一个可区分的类型，而不是笼统的 server_error
            print("\n[ERR] 网页会话已达上下文长度上限:")
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "context_length_exceeded")))
        except GeminiBusyError as exc:
            # 本地排队保护：同一会话桶已有请求在跑且等锁超时，对应 HTTP 503 / upstream_busy
            print(f"\n[繁忙] {exc}")
            await queue.put(("done", (None, [], str(exc), "upstream_busy")))
        except GeminiTimeoutError as exc:
            # 与 server.py 的非流式分支保持一致：超时是 504/timeout，而不是 500
            print("\n[ERR] 等待 Gemini 回复超时（已重试）:")
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "timeout")))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "server_error")))

    task = asyncio.create_task(runner())

    # 已下发给客户端的文本（用于收尾对账，见下方 T6.1）
    streamed = ""
    reply_content = ""
    error: Optional[str] = None
    error_type = "server_error"
    keepalives = 0

    keepalive_s = config.CHAT_KEEPALIVE_S
    while True:
        try:
            kind, payload = await asyncio.wait_for(
                queue.get(), timeout=keepalive_s if keepalive_s and keepalive_s > 0 else None
            )
        except asyncio.TimeoutError:
            # 网页版生成较慢，发送 SSE 注释保活，避免 Pi 侧超时断连。
            # 带 tools 时回复必须先完整缓冲才能判断是不是 tool_calls，
            # 因此这段时间客户端看不到内容 —— 用注释保活 + 日志保持可观测。
            keepalives += 1
            if config.DEBUG:
                print(
                    f"[debug] 等待上游回复中（已发 {keepalives} 次 keep-alive，"
                    f"工具模式={wants_tools}）"
                )
            yield ": keep-alive\n\n"
            continue

        if kind == "delta":
            streamed += payload
            yield encode({"content": payload})
        else:
            reply_content, _blocks, error, error_type = payload
            break

    await task

    if error:
        yield f"data: {json.dumps({'error': {'message': error, 'type': error_type}}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    tool_calls = parse_tool_calls(reply_content, _tool_names(request.tools)) if wants_tools else []

    if tool_calls:
        for index, call in enumerate(tool_calls):
            yield encode({
                "tool_calls": [{
                    "index": index,
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": ""},
                }]
            })
            arguments_str = json.dumps(call["arguments"], ensure_ascii=False)
            for piece in _chunk_text(arguments_str):
                yield encode({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
        yield encode(None, finish="tool_calls")
    else:
        # 收尾对账（T6.1）：必须保证客户端最终持有的文本 == reply_content。
        #
        # 生成中途回复节点可能被整体替换，此时 _delta_piece 会停发增量（避免拼出错乱
        # 文本），但旧实现只在「从未发过任何增量」时才补全文，导致客户端少一截且
        # **没有任何报错**。这里按与已发内容的前缀关系补齐差额。
        final_text = reply_content if reply_content is not None else streamed
        if final_text and final_text != streamed:
            if final_text.startswith(streamed):
                missing = final_text[len(streamed):]
            else:
                # 追加语义无法修复（已下发的内容不是最终内容的前缀）。SSE 没有「撤回」
                # 语义，只能补发全文：宁可重复，也绝不静默丢尾。
                print("[流式] 回复被整体改写，已补发全文（客户端可能看到重复内容）。")
                missing = final_text
            for piece in _chunk_text(missing):
                yield encode({"content": piece})
        yield encode(None, finish="stop")

    include_usage = isinstance(request.stream_options, dict) and bool(
        request.stream_options.get("include_usage")
    )
    if include_usage:
        # usage 用真正发出去的 prompt 估算（driver 可能选了播种版 / 中途轮转过）。
        # 按会话桶读取，并发时不会拿到别的 Agent 的 prompt；回退到入参 prompt 兼容假 driver。
        sent_prompt = driver.sent_prompt(session_key) or prompt
        completion_tokens = estimate_tokens(reply_content)
        usage_payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": estimate_tokens(sent_prompt),
                "completion_tokens": completion_tokens,
                "total_tokens": estimate_tokens(sent_prompt) + completion_tokens,
            },
        }
        yield f"data: {json.dumps(usage_payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"
