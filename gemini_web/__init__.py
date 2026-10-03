"""Gemini 网页版 -> OpenAI 兼容 API 桥接服务的实现包。

模块划分：
    config      配置加载（.env）与所有可调参数
    models      OpenAI 兼容的 Pydantic 数据模型
    toolcalls   工具注入与解析（模拟 function calling）
    prompting   消息 -> 网页输入框文本
    driver      Playwright 浏览器 Driver + 会话持久化
    streaming   SSE 流式编码
    server      FastAPI 应用与路由

入口仍为项目根目录的 ``gemini_api_server.py``（薄封装，向后兼容）。
"""

from . import config, tasks
from .driver import (
    DEFAULT_SESSION_KEY,
    GeminiContextLimitError,
    GeminiTimeoutError,
    GeminiWebDriver,
    SessionState,
)
from .models import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from .prompting import build_prompt, estimate_tokens
from .responses import ResponsesRequest, handle_responses
from .toolcalls import parse_tool_calls, to_tool_call_models

__all__ = [
    "config",
    "tasks",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatMessage",
    "DEFAULT_SESSION_KEY",
    "GeminiContextLimitError",
    "GeminiTimeoutError",
    "GeminiWebDriver",
    "SessionState",
    "build_prompt",
    "estimate_tokens",
    "parse_tool_calls",
    "to_tool_call_models",
    "ResponsesRequest",
    "handle_responses",
]
