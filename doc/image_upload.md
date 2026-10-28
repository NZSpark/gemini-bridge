# 网页版 LLM 图片直传实现指南（以 GeminiBridge 为例）

> 本文记录「把图片真正粘贴进网页版 LLM 输入框」这一功能的完整实现过程：需求分析、
> 技术要点、具体实现、测试与验证。目标是让**其他类似的 web-to-API 桥接项目**能照此
> 复刻同样能力。参考项目：DeepseekBridge、DoubaoBridge。

---

## 1. 背景与需求分析

### 1.1 问题

把网页版 LLM（gemini.google.com）包装成 OpenAI 兼容 API 时，图片（`image_url` /
base64 data URI）最初只能被**降级成一行文本引用**：

```
[图片] (image/png, ~442KB base64)
```

这带来两个问题：

1. 网页输入框里**看不到图片本身**，模型无法理解图片内容；
2. 用户以为「上传了图」，实际模型只收到一句「这里本该有张图」的占位说明。

### 1.2 目标

让图片以**真实文件**的形式进入 Gemini 网页输入框（等价于用户手动粘贴或上传一张图），并满足：

- 网页端显示图片（缩略图 / 附件卡片）；
- 模型能读到图片**像素内容**；
- 上传失败时**不影响文本提交**（优雅降级）；
- 可配置开关与数量上限；
- 有单元测试覆盖事件派发与降级路径。

### 1.3 关键约束

- **输入框是 `contenteditable`，不是 `<input type=file>`**：无法用 `set_input_files`
  直接塞文件，必须模拟「粘贴」行为。
- **不能触发系统级剪贴板**：无头 / 服务环境下没有真实剪贴板，需在页面内合成事件。
- **保留文本语义**：文本 prompt 仍要携带 `[图片]` 引用行，作为兜底锚点。
- **不能每轮重复粘贴**：多轮对话里历史图片会随每轮请求重复出现，需去重（只对本轮新增消息抽图）。

---

## 2. 技术要点

### 2.1 核心思路：合成 `paste` 事件 + `DataTransfer` + `File`

Gemini 网页版的富文本编辑器（`contenteditable`）监听 `paste` 事件，从
`event.clipboardData.files` 里取文件。我们可以在页面上下文中：

1. 用原始字节构造 `File`；
2. 构造 `DataTransfer` 并 `items.add(file)`；
3. 派发一个携带该 `DataTransfer` 的 `ClipboardEvent('paste')`。

编辑器收到事件后，会像真实粘贴一样把图片挂载到输入框。

```js
const el = document.activeElement || document.querySelector('[contenteditable="true"]');
const file = new File([new Uint8Array(bytesArray)], name, {type: mime});
const dt = new DataTransfer();
dt.items.add(file);
el.dispatchEvent(new ClipboardEvent('paste', {
  clipboardData: dt, bubbles: true, cancelable: true, composed: true,
}));
```

关键点：

| 要点 | 说明 |
|---|---|
| `bubbles`/`composed` | 让事件穿过 shadow DOM 冒泡到编辑器的监听器 |
| `cancelable` | 允许编辑器 `preventDefault`，行为更接近真实粘贴 |
| `document.activeElement` | 优先用当前焦点元素，回退到第一个 `contenteditable` |
| 字节传输 | Playwright `evaluate` 只能传可序列化数据，`bytes` 需转成 `list[int]` |
| 粘贴时机 | 必须在文本已填入输入框、**提交之前**粘贴，图片才会附加到同一条消息 |

### 2.2 数据来源：两种图片形态

OpenAI 兼容请求里图片有两种形态，处理方式不同：

| 形态 | 例子 | 处理 |
|---|---|---|
| base64 data URI | `data:image/png;base64,....` | 直接解码为字节 |
| 远程 URL | `https://x/y.jpg` | 惰性下载为字节（走浏览器上下文，复用登录态） |

`_image_url()` 还兼容 `image_url` 为 dict / str、以及裸 `url` 字段三种写法。

### 2.3 优雅降级

任何一步失败（下载失败、focus 失败、事件派发异常）都**只记录日志**，不阻断文本提交。
因为文本 prompt 已经带了 `[图片]` 引用行，最差情况退回到「文本引用」语义。

### 2.4 只在图片首次出现时粘贴（关键坑）

多轮对话里，历史消息的图片会随每一轮请求被重复带上。若每轮都无脑粘贴，同一张图会被反复贴进输入框，造成重复附件。

GeminiBridge 采用**无状态**方案：只对「本轮新增消息」抽图。

```python
def _messages_since_last_assistant(messages):
    """返回最后一条 assistant 消息之后的「本轮新增」消息（兼容 dict / 对象）。"""
    last = -1
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "assistant":
            last = index
    if last < 0 or last + 1 >= len(messages):
        return messages          # 无 assistant / 无新增：退回全部（宁多贴不漏）
    return messages[last + 1:]
```

`extract_images(messages, only_delta=True)` 即只抽本轮图片。这样：

- 图片在它**首次出现的那一轮**被粘贴一次；
- 之后轮次重发整段 `messages` 时，历史图片只保留 `[图片]` 文本引用行，不再重复粘贴；
- 网页会话轮转（新会话）时，`only_delta` 依然是「本轮新增」，无需额外状态。

### 2.5 安全：不外泄完整 base64

文本引用行**绝不包含完整 base64 字节**，只记类型与估算大小，避免 prompt 爆炸与隐私泄漏。

---

## 3. 具体实现

实现分五层：**抽取 → 编排 → 粘贴 → 配置 → 声明**。

### 3.1 数据模型与抽取（`gemini_web/prompting.py`）

```python
@dataclass(slots=True)
class ImageAttachment:
    name: str
    mime: str
    data: bytes | None = None   # base64 已知时为原始字节
    url: str | None = None      # 远程 URL，惰性下载
```

```python
def extract_images(messages, only_delta: bool = False) -> list[ImageAttachment]:
    """从消息里收集图片分片；base64 解码为字节，URL 保留待下载。

    only_delta=True 时只看「本轮新增消息」——图片只粘贴一次。
    """
    if only_delta:
        messages = _messages_since_last_assistant(messages)
    attachments = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            if not (isinstance(part, dict) and _is_image_part(part)):
                continue
            url = _image_url(part)
            if not url:
                continue
            if url.startswith("data:"):
                parsed = _parse_data_uri(url)
                if parsed is None:
                    continue
                mime, data = parsed
                if not data:          # 空 / 非法 base64 解出 b""：跳过，不贴 0 字节文件
                    continue
                ext = mime.split("/")[-1] or "png"
                attachments.append(ImageAttachment(name=f"image.{ext}", mime=mime, data=data))
            else:
                name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "image.png"
                attachments.append(ImageAttachment(name=name, mime="image/png", url=url))
    return attachments
```

data URI 解析：

```python
def _parse_data_uri(url: str) -> tuple[str, bytes] | None:
    header, _, payload = url.partition(",")
    if ";base64" not in header or not payload:
        return None
    mime = header.removeprefix("data:").split(";", 1)[0] or "application/octet-stream"
    try:
        return mime, base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return None
```

文本引用（兜底锚点，注意**不含完整字节**）：

```python
def _image_reference(part: dict) -> str | None:
    url = _image_url(part)
    if url is None:
        return None
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        media = header.removeprefix("data:").split(";", 1)[0] or "image"
        approx_kb = max(1, len(payload) * 3 // 4 // 1024)
        return f"{MARK_IMAGE} ({media}, ~{approx_kb}KB base64)"
    return f"{MARK_IMAGE} {url}"
```

`_content_to_text()` 在遇到图片分片时插入引用行，文本分片照常拼接——所以历史图片仍会以 `[图片] …` 出现，但绝不含完整字节。

### 3.2 编排与数量上限（`gemini_web/server.py` / `responses.py` / `streaming.py`）

在构造 prompt 之后、调用 driver 之前收集图片，并做上限裁剪。三处入口都要传 `only_delta=True`：

```python
images = []
if config.IMAGE_PASTE_ENABLED:
    images = extract_images(request.messages, only_delta=True)[: config.IMAGE_PASTE_MAX]

try:
    reply_content, _code_blocks = await driver.send_chat(
        prompt, seeded_prompt=seeded_prompt, key=session_key, images=images
    )
except GeminiContextLimitError as exc:
    ...
```

- 非流式：`server.chat_completions`
- Responses API：`responses.handle_responses`
- 流式：`streaming._stream_chat_completion`（`extract_images(messages, only_delta=True)`）

### 3.3 浏览器粘贴（`gemini_web/chat_io.py`）

远程 URL 下载（走浏览器上下文，复用登录态 / cookie）：

```python
async def _download_image(self, url: str) -> bytes | None:
    try:
        response = await self.context.request.get(
            url, timeout=config.IMAGE_DOWNLOAD_TIMEOUT_MS
        )
        if response.ok:
            return await response.body()
        print(f"[图片] 下载失败 status={response.status} url={url[:80]}")
    except Exception as exc:  # noqa: BLE001 下载失败一律降级
        print(f"[图片] 下载异常 {type(exc).__name__}: url={url[:80]}")
    return None
```

粘贴派发脚本（`_PASTE_IMAGE_JS`）：

```js
([name, mime, bytesArray]) => {
  const el = document.activeElement
    || document.querySelector('[contenteditable="true"]');
  if (!el) return false;
  const file = new File([new Uint8Array(bytesArray)], name, {type: mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  el.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true, composed: true,
  }));
  return true;
}
```

```python
async def _paste_images(self, page, composer, images: list) -> int:
    """把真实图片文件以合成 paste 事件粘贴进输入框，返回成功派发的张数。"""
    if not images or not config.IMAGE_PASTE_ENABLED:
        return 0
    pasted = 0
    for image in images:
        data = getattr(image, "data", None)
        if data is None and getattr(image, "url", None):
            data = await self._download_image(image.url)
        if not data:
            continue
        name = getattr(image, "name", "image.png") or "image.png"
        mime = getattr(image, "mime", "image/png") or "image/png"
        try:
            # 粘贴前确保输入框聚焦：否则 document.activeElement 不是编辑器。
            if composer is not None:
                await self._focus_composer(page, composer)
            ok = await page.evaluate(self._PASTE_IMAGE_JS, [name, mime, list(data)])
            if ok:
                pasted += 1
                if config.DEBUG:
                    print(f"[图片] 已粘贴 {name}（{mime}, {len(data)} 字节）")
        except Exception as exc:  # noqa: BLE001 粘贴失败一律降级，不阻断提交
            print(f"[图片] 粘贴失败 {type(exc).__name__}: name={name}")
    return pasted
```

在 `_send_chat_locked()` 里，**填完文本、按 Enter 之前**调用（失败非致命）：

```python
chat_input = await self._fill_prompt(page, prompt)
# 图片必须在文本写进输入框之后、按 Enter 之前粘进去：编辑器此时已聚焦、
# 文本已在框内，合成 paste 会把图片附加到同一条消息上。
if images:
    await self._paste_images(page, chat_input, images)
await self._submit_prompt(page, chat_input, bucket, prompt=prompt)
```

### 3.4 配置（`gemini_web/config.py` / `.env.example`）

```python
IMAGE_PASTE_ENABLED = env_bool("IMAGE_PASTE_ENABLED", True)
IMAGE_PASTE_MAX = max(0, env_int("IMAGE_PASTE_MAX", 4))
IMAGE_DOWNLOAD_TIMEOUT_MS = max(500, env_int("IMAGE_DOWNLOAD_TIMEOUT_MS", 10_000))
```

| 配置 | 默认 | 说明 |
|---|---|---|
| `IMAGE_PASTE_ENABLED` | `true` | 关闭时全部退回文本引用，不粘贴 |
| `IMAGE_PASTE_MAX` | `4` | 单轮最多粘贴几张（0 = 全部降级为文本） |
| `IMAGE_DOWNLOAD_TIMEOUT_MS` | `10000` | 远程图片下载超时（毫秒） |

### 3.5 Driver 协议（`gemini_web/chat_io.py`）

```python
async def send_chat(
    self,
    prompt: str,
    on_delta=None,
    seeded_prompt: str | None = None,
    key: str | None = None,
    images: list | None = None,
) -> tuple[str, list[dict]]: ...
```

各层签名同步加 `images` 参数后，测试里的 `FakeDriver.send_chat` 也要同步补上，否则会 `TypeError: got an unexpected keyword argument 'images'`。

### 3.6 声明多模态能力（`/v1/models`）

**这一步不可省略。** 若只做 3.1~3.5 而不声明能力，OpenAI 兼容客户端（pi 等）会在**请求构建阶段就把图片静默剥离**——图片根本到不了桥接服务，用户只看到「Current model does not support images」。

在 `gemini_web/models.py` 中显式声明输入模态：

```python
INPUT_MODALITIES = ["text", "image"]
OUTPUT_MODALITIES = ["text"]
MODALITY = "text+image->text"

class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "gemini-web-bridge"
    input_modalities: list[str] = Field(default_factory=lambda: ["text", "image"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    architecture: dict[str, Any] = Field(
        default_factory=lambda: {"modality": "text+image->text"}
    )
```

---

## 4. 测试与验证

### 4.1 测试策略

1. **单元测试**：数据抽取、data URI 解码、去重逻辑（`only_delta=True`）、配置开关降级。
2. **事件派发测试**：使用 Fake Page 验证 `evaluate` 调用传参和脚本组合。
3. **路由/模型声明测试**：断言 `/v1/models` 中各模型的 `input_modalities` 与 `architecture.modality` 包含图片能力。

### 4.2 运行测试

```bash
pytest
```
