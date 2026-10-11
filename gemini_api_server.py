"""Gemini Web-to-API Bridge —— 入口（薄封装）。

真正的实现已拆分到 ``gemini_web`` 包：

    gemini_web/config.py      配置加载（.env）与全部可调参数
    gemini_web/models.py      OpenAI 兼容的 Pydantic 数据模型
    gemini_web/toolcalls.py   工具注入与解析（模拟 function calling）
    gemini_web/prompting.py   消息 -> 网页输入框文本
    gemini_web/driver.py      Playwright 浏览器 Driver
    gemini_web/streaming.py   SSE 流式编码
    gemini_web/bridge_commands.py  /bridge 聊天命令（解析 / 执行 / 帮助 / 状态摘要）
    gemini_web/server.py      FastAPI 应用与路由

本文件仅做两件事：把历史公开名字重新导出，以及在直接运行时启动服务。

.. warning::
   下面列出的 **配置常量是重新导出的快照**。运行期所有模块都通过
   ``gemini_web.config.<NAME>`` 取属性，因此改写 ``gemini_api_server.RESPONSE_TIMEOUT_S``
   之类的名字不会生效；需要覆盖时请改 ``config`` 模块属性或用环境变量 / ``.env``。
"""

# ---- 配置（快照式重新导出，勿就地改写）----
from gemini_web import config
from gemini_web.config import (  # noqa: F401
    CODE_BLOCK_SELECTOR,
    CODE_TAG_SELECTOR,
    DEBUG,
    HEADLESS,
    HOST,
    INPUT_SELECTORS,
    LEN_STABLE_POLLS,
    MAX_SESSION_BUCKETS,
    MAX_UPSTREAM_RETRIES,
    NEW_CHAT_SELECTOR,
    OUTPUT_DIR,
    POLL_INTERVAL_S,
    PORT,
    READY_SELECTOR,
    RESPONSE_SELECTORS,
    RESPONSE_TIMEOUT_S,
    RETRY_BACKOFF_S,
    SESSION_FILE,
    SESSION_KEY_HEADER,
    SESSION_KEY_MAX_LEN,
    SESSION_SCOPING,
    STABLE_POLLS,
    USER_DATA_DIR,
    env_bool,
    env_float,
    env_int,
    env_str,
)

# ---- 数据模型 ----
from gemini_web.models import (  # noqa: F401
    advertised_models,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    FunctionCall,
    ModelCard,
    ModelListResponse,
    SUPPORTED_MODELS,
    ToolCall,
    Usage,
)

# ---- 工具调用 ----
from gemini_web.toolcalls import (  # noqa: F401
    _iter_balanced_objects,
    _normalize_tool_entry,
    _tool_names,
    format_tools_instruction,
    parse_tool_calls,
    to_tool_call_models,
)

# ---- 提示词与文本工具 ----
from gemini_web.prompting import (  # noqa: F401
    _content_to_text,
    _delta_piece,
    build_prompt,
    estimate_tokens,
)

# ---- 聊天命令（``/bridge ...``）----
from gemini_web import bridge_commands  # noqa: F401
from gemini_web.bridge_commands import (  # noqa: F401
    BRIDGE_COMMAND,
    handle_command,
    is_command,
    parse_command,
)

# ---- Driver / 流式 / 路由 ----
from gemini_web.driver import (  # noqa: F401
    DEFAULT_SESSION_KEY,
    GeminiBusyError,
    GeminiContextLimitError,
    GeminiTimeoutError,
    GeminiWebDriver,
    SessionState,
)
from gemini_web.streaming import _chunk_text, _stream_chat_completion  # noqa: F401
from gemini_web.server import (  # noqa: F401
    _session_key,
    app,
    chat_completions,
    debug_dom,
    driver,
    healthz,
    lifespan,
    list_models,
    reset_session,
    root,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
