"""OpenAI 兼容的数据模型。

参考: https://platform.openai.com/docs/api-reference/chat
Pi Coding Agent (pi.dev) 通过 models.json 里的 "api": "openai-completions"
接入任何 OpenAI 兼容端点，因此这里需要完整支持 messages / tools / stream。
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class FunctionCall(BaseModel):
    name: str
    # OpenAI 的 arguments 是 JSON 字符串；这里以字符串对外暴露
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    # Pi 会发送 tool / tool_call_id / name 等字段，宽松接收避免校验失败
    model_config = ConfigDict(extra="allow")

    role: str
    # content 可能是字符串，也可能是内容分片数组
    # （如 [{"type": "text", "text": "hi"}]，Pi / 新版 OpenAI 客户端会这样发），
    # 因此用 Any 宽松接收，构建 prompt 时再归一化为纯文本。
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    # Pi 会附带大量标准字段（temperature、top_p、reasoning_effort、...）
    # 用 extra="allow" 全量吞掉，绝不因未知字段报 422
    model_config = ConfigDict(extra="allow")

    model: str = "gemini-chat"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    stream_options: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    # 网页版无法控制生成参数，以下字段仅作兼容占位（接收但不生效）
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Any] = None
    # ---- 本地扩展字段（Pi 不会传，保持默认即可）----
    # None 表示「未显式指定」，落盘与否交给 config.SAVE_FILES 决定（默认 false）；
    # 只有客户端显式传 true/false 时才覆盖。避免默认产生落盘副作用。
    save_files: Optional[bool] = None
    output_dir: Optional[str] = None  # None -> 使用 .env 的 OUTPUT_DIR


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "gemini-chat"
    choices: List[Choice]
    usage: Usage = Field(default_factory=Usage)
    saved_files: List[str] = []


class ModelCard(BaseModel):
    """模型卡。

    ``context_window`` 由会话轮转预算（``config.SESSION_MAX_TOKENS``）填充，
    由 ``/v1/models`` 透出：网页版没有公开的上下文数值，这里给出的是
    「桥接层在累计多少估算 token 后轮转会话」的上限，客户端据此裁剪即可
    与桥接层行为一致，不需要手填（T8.7）。
    """

    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "gemini-web-bridge"
    context_window: Optional[int] = None


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelCard]


# Pi 的 models.json 里引用的 id 需要与这里一致。
# 这里**不再**硬编码 context_window：旧的 65536 与 README 示例（1000000）互相矛盾，
# 而 /v1/models 又不透出该字段，三方各说各话（update.md P2-7）。
# 现在唯一权威数值 = config.SESSION_MAX_TOKENS，由 server.list_models 透出。
SUPPORTED_MODELS = [
    {"id": "gemini-chat"},
    {"id": "gemini-reasoner"},
]


def advertised_models() -> List[Dict[str, Any]]:
    """本桥对外公布的模型清单。

    ``GET /v1/models`` 与聊天命令 ``/bridge models`` 共用这一份数据，避免两边各写
    一套清单后悄悄漂移（帮助 / 回执文本里硬编码的模型名最容易过期）。
    """
    return [dict(card) for card in SUPPORTED_MODELS]
