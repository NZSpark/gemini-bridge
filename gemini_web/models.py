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
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "gemini-web-bridge"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelCard]


# Pi 的 models.json 里引用的 id 需要与这里一致
SUPPORTED_MODELS = [
    {"id": "gemini-chat", "context_window": 65536},
    {"id": "gemini-reasoner", "context_window": 65536},
]
