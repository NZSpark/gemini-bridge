# ② 编码阶段：模块骨架与代码模式

目标：把 ① 的设计落成**可测、可配、可自愈**的实现。本文件给出与站点无关的骨架与模式；
站点相关的只有选择器与 URL 正则（都进配置）。

---

## 1. 目录骨架（ChatGPTBridge 最终形态，可照搬）

```text
<site>_api_server.py            # 入口薄封装：重导出公开名字 + uvicorn 启动
<site>_web/
  __init__.py
  server.py                     # FastAPI 应用与路由（只留 HTTP 职责）
  models.py                     # OpenAI 兼容 Pydantic 模型（宽松接收）
  errors.py                     # 领域异常 + 错误常量（避免循环依赖）
  prompting.py                  # messages → 输入框文本；增量/播种；工具注入
  protocol_adapters.py          # Chat / Responses 共用的协议转换
  api/                          # chat_adapter / responses_adapter / streaming_adapter
  browser/                      # driver / dom_adapter / selectors / diagnostics
  completion/                   # lifecycle / generator / extractor
  session/                      # schema / store / migration / lock
  tools/                        # parser / validator / policy / executor / ledger
  config/                       # server / browser / tools / limits / debug（兼容 facade）
  page_pool.py                  # 页面池：判活 / 重建 / 回收 / 按桶锁
  session_store.py              # 兼容 facade（旧 import 路径保留）
  linking.py                    # /link 绑定逻辑（站点适配点集中在这里）
  bridge_commands.py            # /bridge 命令统一入口（本地应答）
  end_detection.py              # 结束判定状态机（纯函数，可穷举测试）
  streaming.py                  # Chat Completions SSE（facade）
  responses.py                  # Responses API（facade）
  tasks.py / task_state.py      # 任务快照与状态
  metrics.py / logging_setup.py # 指标与日志上下文
tests/                          # 假 page / 假 driver（stdlib unittest，不启浏览器）
tests/e2e/                      # 直连基线 + bridge 对等真机测试（显式开关）
doc/                            # design / tasks / update / e2e_test_design / acceptance
.env.example  pyproject.toml  README.md
```

**拆分纪律**（这是本项目两次大型重构没出事故的原因）：新增模块 → 迁移一个调用点 → 测试 →
删旧内部实现；**保留旧 import 路径并加 facade**；每次拆分单独提交、不改变 API 行为；
最后一步才做「server 只留 HTTP / browser 不懂协议 / tools 不懂网页」。

---

## 2. 各模块要点与模式

### 2.1 `config/` — 配置即文档

```python
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

def _load_env_file(path: Path) -> None:
    """极简 .env 解析：已存在的真实环境变量优先；支持 KEY=value、# 注释、引号。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_env_file(ENV_FILE)

def env_str(key, default): return os.environ.get(key, default)
def env_int(key, default):
    try: return int(os.environ.get(key, str(default)))
    except ValueError: return default
def env_bool(key, default=False):
    raw = os.environ.get(key)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}

# 多候选选择器统一用 "||" 分隔，站点改版只改这里 / .env，不改代码
INPUT_SELECTORS = [s.strip() for s in env_str(
    "INPUT_SELECTORS",
    'form div.ProseMirror[contenteditable="true"]||[contenteditable="true"][role="textbox"]'
).split("||") if s.strip()]
```

规则：其它模块一律 `from . import config` 后写 `config.NAME`（**不要** `from .config import NAME`）——
这样测试里 `patch.object(config, "TIMEOUT", 5)` 才生效。每个参数都写「为什么存在」的注释；
`.env.example` 与 README 三方一致，用**漂移测试**锁死（见 §3.3）。

### 2.2 `errors.py` — 让上层能区分失败类型

```python
class SiteTimeoutError(RuntimeError): ...          # 等待回复超时（可恢复、可重试）
class SiteContextLimitError(RuntimeError): ...     # 会话到顶（必须换会话）
class SitePageLostError(RuntimeError): ...         # 标签已失效（必须重建页面）
class SiteComposerError(RuntimeError): ...         # 页面活着但输入区不吃输入（重建后可恢复）
class SiteBusyError(RuntimeError): ...             # 本地排队保护（不触发重试）
DEFAULT_SESSION_KEY = "default"
HOME_URL = "https://chat.example.com/"
```

> `PageLostError` / `ComposerError` 必须独立成类且与「选择器失效」分开：
> 标签失效时 `wait_for_selector` 会**立刻**抛错，被吞成「选择器都没命中」就会
> 「每个请求瞬间 502 且提示请登录」。Composer 退化已实测：**重载页面能修好**（干净加载只有
> 1 个编辑器），所以计入可恢复阶梯，但共用有限的 `PAGE_REBUILD_MAX` 额度。

### 2.3 `models.py` — 宽松的 OpenAI 兼容

```python
class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Optional[Any] = None      # 可能是 str，也可能是 [{"type":"text","text":...}]
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")   # 未知字段绝不 422
    model: str = "chat"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None
    # temperature/top_p/max_tokens/stop：接收但不生效（网页版无法控制生成参数），仅作兼容占位
```

### 2.4 `prompting.py` — 只发增量 + 播种 + 工具注入

- 网页会话本身是持续的：**只发送「最后一条 assistant 消息之后」的新增消息**，不要每轮重发全史。
- 语义上维护 `(delta_prompt, seeded_prompt)`：`seeded_prompt` 在轮转/重建后重放，
  受 `SEED_MAX_CHARS` 预算限制（超出保留最近消息）；**任务目标单独快照**，轮转播种时优先注入，
  防止被字符预算截断。
- 工具说明块**紧贴用户任务之后**（播种路径曾被顶到第 3 位 → C1/C2 失败）。
- **提示词只描述链路，不描述模型自身能力**（§2.15 拒答事故的最终修法）：

```text
You are a language model: the only thing you produce is text, and you cannot run anything yourself.
This conversation is read by a small local client running on the user's own computer - when you write
a tool call, that client executes it there for real and returns the real output as your next message.
No tool has to be installed or mounted on your side; that is expected and normal. Writing a tool call
is not fabrication: you are writing the command and the client runs it.
```

禁止出现「你没有 X」「你的工具已挂载」这类关于模型能力的事实陈述（会被当成事实题核对 → 拒答）；
不要在注入文本里使用 `agent` 一词（回归测试直接断言）。
- **空工具输出必须显式解释**：空输出 = 命令成功且没有输出，继续下一步；重复同一命令是死循环，
  按「工具名 + 规范化参数」签名统计历史，≥2 次时点名要求换命令或收尾。
- **桥不自行追发 prompt**：纠偏（nudge）只在本轮**一次工具都没调用过**时生效；
  历史里出现 tool 结果 / `tool_calls` 后，纯文本回复就是最终答案。

### 2.5 `tools/` — 模拟 function calling（三步）

1. **注入**：把 `tools` 转成指令，要求模型把调用放进 **```tool_call 代码围栏**（info string
   必须是 `tool_call`，`json`/空都不执行）；示例本身用围栏形态。
2. **解析**：兼容三种形态——
   - 带围栏的 ` ```tool_call {...} ``` `（原样带回时）；
   - **围栏被网页渲染掉后**：DOM 里只剩 `tool_call` 单独一行 + JSON → 「裸标签 + 平衡括号扫描」；
   - shell 误写 ` ```bash ` 时的唯一候选恢复（只当该围栏能唯一对应一个 shell 类工具时才恢复）。
   用平衡括号扫描处理字符串与转义；按合法工具名过滤误报；**不**接受 ` ```json ` 或 DOM 的
   `json` 标签行（正文里展示的 JSON 会被误执行）。
3. **回填**：下一轮的 `role:"tool"` 结果拼回 prompt 再喂网页；`tool_call_id` 全程保留。
4. **留诊断**：出现标记/围栏却解析不出任何调用时打 warning（旧版完全静默，
   线上表现为「客户端什么都没执行就结束」）。

**按需注入 + 长度三层**（同族 Gemini 实测，只做前两条就能防住两个真实事故）：

- **客户端未声明 `tools` 就不得注入内置工具**：曾每请求白付 ~970 tokens 脚手架，还会产生
  客户端根本没声明的 `tool_calls`；留一个显式逃生口开关，并用**token 预算回归护栏**守护
  （无工具请求的 `usage` 不得超预算，`smoke_test.py --token-budget` 就是它的验收载体）。
- **长度控制分三层**：单条工具结果上限（截短 + 标注「已截断 N 字符」，**不整块丢弃**）→
  拼装后成品预算（优先压缩最长的工具结果，保留开头并标注）→ 发送侧兜底截断（必须记 WARNING，
  不再静默丢弃）。
- **构建 ≠ 发送**：每轮会构建两份 prompt（播种 / 增量），日志必须标注本轮实际发出的是哪一份；
  把构建期日志误读成发送行为会浪费一整天。

### 2.6 `page_pool.py` — 页面池（自愈的核心）

```python
class PagePoolMixin:
    @staticmethod
    def _page_is_alive(page) -> bool:
        # 快路径，不能作为唯一判据：崩溃的渲染进程仍可能报 is_closed() == False
        return page is not None and getattr(page, "is_closed", lambda: False)() is False

    async def _ensure_page(self, key):
        bucket = key or DEFAULT_SESSION_KEY
        page = self._pages.get(bucket)
        if page is not None and not self._page_is_alive(page):
            self._pages.pop(bucket, None)          # 从池里移除尸体
            page = None
        if page is None:
            page = await self._create_bucket_page(bucket)   # 回到 state.url，而非首页
            self._pages[bucket] = page
        return page
```

- 定位输入框：`TimeoutError` 才算「没命中」；页面已关闭/崩溃 → `PageLostError`
  （页面活着但选择器全不中 → `None` + `last_input_probe` 现场）。
- **默认桶也要能重建**（它死了没有退路，整桥会永久 502）。
- 回收/淘汰：`_recycle_idle_pages()`（空闲 TTL）+ `_evict_lru_page()`（超上限）；
  **只关页面、状态保留**。「有请求在飞」用**可重入计数**（`_active_counts`），不能用锁。
- 按桶锁：默认全部退回一把全局锁（串行）；`PARALLEL_BUCKETS=true` 才各持一把。
- 重建后**回到状态里的会话 URL** 并校验落点（被重定向回首页 = 该会话不可用）。

### 2.7 `session/` — 按桶状态

- `SessionState`：`url / turns / est_tokens / cap_hit / pending_rotation / pending_reseed /
  has_history / linked_url / 偏好`。
- 状态文件原子写（临时文件 + `Path.replace`），损坏容错 + warning（不静默丢全部桶）。
- `needs_seed()`：`cap_hit` 或轮转后或落点反查失败时为真。
- `_session_over_budget()`：超 `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS` → 下一轮轮转 + 播种。

> 常见错误：把 `has_history / turns` 这类**任务级**状态写在 driver 单例上 → 多任务互相覆盖。
> 用「按桶存放 + 属性代理」保住默认桶的旧用法。

两条同族血泪补丁：

- **播种标记必须同时绑到内存里的页面对象**（`_seeded_page`）：只信磁盘 `has_history` 时，
  进程重启 / 页面被换后磁盘仍为真而网页上下文已丢 → **串线**（A1）。页面对不上就重新完整播种。
- **状态落盘用「同目录临时文件 + `os.replace`」原子替换**，且**临界区不得有 `await`**
  （有则补锁）；读取失败留 warning，**不静默清零**（否则丢的是整个账号的会话绑定）。

### 2.8 `browser_input.py` + `chat_io.py` — 写入 / 提交 / 结束判定

**写入三防线**（`_fill_prompt`）：

```text
1) 写之前读回：输入框里已经是这条 prompt → 跳过（重试时最常见的浪费与风险）
2) 整段 fill()：之后读回——「成功」或「报错但文本已落地」都算成功
3) fill 确实没落地 → 分块插入：每块前重读输入框、只补缺的那段（可断点续写）
   → 超长 prompt 先按 PROMPT_MAX_CHARS 截断（保头尾）并打印 [截断] 警告
```

**写入的四个同族补丁**（Gemini 用 419 个用例试出来的；违反会重现「写不进去/被自己清掉」）：

1. **`fill()` 是复合动作**：建立选区 + `Input.insertText`。裸 `keyboard.insert_text` /
   `execCommand('insertText')` **只在当前选区**插入，没有选区就是**静默空操作**（不报错、0 字符）。
   分块写入前**必须显式放置光标**（focus + selectNodeContents + 折叠到末尾）。
2. **超长 prompt 分块**（默认 4000 字符）：一次性写入会把几万字符排成网页主线程上的长任务，
   页面假死、连「元素是否可编辑」都探测不到。每块之间让出主线程，每块前**重读输入框只补缺失段**
   （幂等、可续写、重挂载不重复）。
3. **读回是有损的，只能发现失败、不能否决成功**：富文本读回丢换行、markdown 变格式。比较必须
   **归一化**（空白折成单空格 + 前 N 字符 + 长度），且 `fill()` 成功返回就**信任它**（读回差异只记
   INFO）；只有 `fill()` **报错**时才读回救援。用有损读回否决成功的写入 → 触发「清空重写」
   自我毁灭循环。
4. **提交阶梯 + 逐级验证**：合成 `KeyboardEvent` 的 `isTrusted=false`，受控编辑器常直接忽略。
   阶梯 = 真实键盘 `Enter` → 原生 `click()` 发送按钮 → 合成 `Enter`；每级之后验「输入框已清空
   或页面进入生成中」；读不到输入框内容时返回「无法判断」，**不据此报错也不空等**。
   （本项目另加一条：React 重挂载会让提交误报 → 改读**当前**输入框 + 核对消息是否已出现在对话区；
   提交后**绝不无条件重发**，会造出重复对话，A2。）

**composer 候选处理**（§2.18 真机事故后）：枚举 `INPUT_SELECTORS` 的**全部**候选
（同一元素去重），可见的排前面但**不过滤**（真 composer 可能被判 not visible）；
逐个试填并**校验读回非空**才算成功；每次重试重新枚举（React 重挂载会换句柄）。
「页面活着、JS 也响应，但 composer 不接受输入」→ `ComposerError`（可恢复，重建后重发一次）。

**提交验证**：不要假设「按了 Enter 就发出去了」——提交后等待「已提交」证据
（输入框清空 / 页面进入生成中），超时抛可操作的错误。React 重挂载会导致提交误报：
改为读取**当前**输入框，并用「消息是否已出现在对话区」判定。

**结束判定循环**（核心，绝不看节点数量）：

```python
before_text = 末节点文本（发送前）
loop:
    nodes = query_selector_all(RESPONSE_SELECTORS)
    if not nodes: 连续 N 轮 → 快速失败（打一次选择器命中诊断），别干等超时
    current = nodes[-1].inner_text()
    reply_seen = (current.strip() != before_text) or (len(nodes) > before_count) or saw_generating
    generating = 页面是否生成中
    # 主判定：生成中信号出现过又消失；兜底：内容/长度稳定 + 静默窗口；卡死：无进展看门狗
```

- 「生成中」检测：扫描 `button / [role=button]`，匹配**独立的** stop 词
  （`(^|[-_])stop([-_]|$)` + aria-label/title/data-testid），且**可见**；
  防止 `stopAtEnd-<hash>` 这类装饰类名先命中。
- 静默窗口（`RESUME_QUIET_POLLS`）：模型常「先给正文、停顿、再补 TOOL_CALL」；
  停止信号消失后仍要求内容连续 N 轮不变才收尾，避免截断。
- 到顶检测：把回复节点文本从页面文本剔除后再匹配 `CAP_NOTICE_PATTERNS`；命中抛
  `ContextLimitError`(400)，不要死等到超时。
- 状态机抽成**纯函数**（`end_detection.evaluate_poll`：输入 `generating / normalized /
  last / pending` → 输出 `continue | finish | extend | fail`），用假时钟穷举测试。

**同族补齐的三条**（否则会周期性读到半截）：

- **多信号与门**：停止控件消失 + `aria-busy` 为假 + 状态文案转过去时 + 内容静默窗口；
  丢掉任一信号都会把分段输出当成结束。
- **读文本先克隆节点并去掉动画类**（`[class*=animate]` / `[class*=pending]` / `[class*=streaming]`），
  再取 `innerText`：未显现的 token 取不到，会让流式 JSON 只剩半截。
- **阈值一律「秒」**：`IdleClock`（页面在生成或正文增长即归零）+ `SETTLE_S` / `STALL_S` /
  `SUBMIT_LOST_S`。用轮询次数当阈值时，调快轮询会**等比削弱**停滞保护（A15）。
- **新回复两把钥匙**（同族 Claude「只按条数认新回复」事故的修法）：① assistant 节点数 > 基线；② 结构指纹
  （本轮提问的文本指纹变化 + 其后出现 assistant 节点）。页面永远存在上一条 assistant 节点，
  只凭「有节点」会把上一轮当本轮；走结构信号时记 `ReplyRead.structural` 并写日志。

### 2.9 `streaming.py` — SSE

- `data: {...}\n\n` 逐块吐，最后 `data: [DONE]\n\n`；等待上游时发 `: keep-alive\n\n` 保活。
- 带 `tools` 时**必须先缓冲完整回复**才能判断是不是 `tool_calls`（期间只发 keep-alive），
  这是协议固有取舍，要在文档里写明。
- `stream_options.include_usage` → 末尾附 usage；usage 用**按桶记录的实发 prompt** 估算
  （并发时不会拿到别的请求的 prompt），并**明确标注为估算**。
- 上游异常编码成 `data: {"error": {...}}` + `[DONE]`，类型与非流式分支保持一致
  （超时=504/timeout）；**开始流式后不能再改写成普通 HTTP 错误**，只能结束流（流内错误 / 关闭连接）。
- **网页「重写回复」检测**：快照不再是前缀时停止继续发增量，最后以收尾文本为准，并 +1
  `stream_rewritten_total` 指标（同族 Claude）。不检测就会把半截旧文本留在客户端。
- `stream_options.include_usage` → usage 块位置是 **`finish_reason` 之后、`[DONE]` 之前**。
- **客户端断开**：检测 `is_disconnected` → 取消在途生成、归还页面、释放桶锁；取消时
  **只 `cancel()`，不 `await` 被取消的 watcher**（A16 曾因此把一个非流式路由拖死 >8 分钟）。

### 2.10 `server.py` — 路由与错误映射

```python
@app.get("/healthz")
async def healthz(): return {"browser_ready": ..., "init_error": ..., "session": {...}, "keys": [...]}

@app.get("/v1/models", response_model=ModelListResponse)
@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
@app.post("/v1/responses")            # 可选；ENABLE_RESPONSES_API=false 时返回 404
@app.post("/session/reset")           # 本地扩展
```

- `_session_key(request)` 实现分桶优先级：显式头 → `user` → 工作目录 → UA。
  只扫 `system/developer` 与**首条** `user`，避免把正文里粘贴的路径当工作目录。
- 异常 → 错误体映射集中一处，流式与非流式走**同一张表**。
- 空输入要在发送前拦住（否则会发给网页版、什么都生成不出来，客户端只能等到超时）。
- 浏览器实例用 FastAPI `lifespan` 初始化；`init_error` **不阻塞启动**（未登录时给 503 而不是崩）。
- 聊天内命令（`/link`、`/bridge`）在 HTTP 边缘识别并本地应答；「浏览器未就绪 → 503」检查
  对命令**放行**——那正是最需要它们的时候。
- **指标 / 状态注册表按请求或按测试注入**，不要用进程级单例：测试共用单例会让计数串味（A13）。
- 跨源变更类请求做 `isSameOriginRequest` 式校验（浏览器网页里发起的跨站写请求必须被拒）；
  会话分桶键只扫 `system/developer` 与**首条** `user`，**绝不按消息正文分桶**。

### 2.11 `linking.py` / `bridge_commands.py` — 聊天内命令（可选但强烈推荐）

- 命令由**桥本地应答**，不发给网页版；整条消息就是一行命令才认（混在正文/多行/历史里的不算）。
- 只读命令不导航网页、不触发生成；改状态命令**只作用于当前桶**，回执写清影响范围
  （如 reset「不是删除客户端历史」）。
- 绑定 URL 校验：站点白名单 + `/c/<会话段>`；拒绝客户端**临时 ID**
  （`local-chatgpt%3A<uuid>` / `WEB:<id>`，约 1 秒后失效）；`%XX` 转义与 `:` 视为同一条；
  导航后校验落点 + `session_link_miss_total` 指标。
- 偏好类设置（think 模式 / 落盘）按桶持久化，切换后**读回验证**，切不动如实报告失败。

---

## 3. 测试策略（不开浏览器）

用**假 page / 假 driver** 驱动所有逻辑，`stdlib unittest` 即可，零额外依赖：

```python
class FakePage:
    def __init__(self, nodes): self.nodes = nodes
    def is_closed(self): return False
    async def query_selector_all(self, sel): return self.nodes
    async def inner_text(self): return self.nodes[-1].text
    async def evaluate(self, js, *a): return False

class ClosedPage(FakePage):   # 标签被关闭
    def is_closed(self): return True
    async def wait_for_selector(self, *a, **k): raise RuntimeError("Target page has been closed")
class CrashedPage(FakePage):  # 渲染进程崩溃：is_closed 仍为 False，但什么都失败
    def is_closed(self): return False
    async def wait_for_selector(self, *a, **k): raise RuntimeError("Target page has been closed")
```

要覆盖的层：解析层、结束判定（含「节点数恒定但内容变化」）、输入写入（含多候选退化）、
会话生命周期（播种 / 到顶 / 轮转 / 重试阶梯）、会话桶（含 LRU / 空闲 TTL / 忙桶不被回收）、
页面失效与重建、路由与错误映射、流式输出、Responses 兼容层、聊天内命令。

> 两个必踩的测试陷阱：
> 1. 「类上未绑定调用」会掩盖缺少 `self` 的签名错误 → 公共方法补**按实例调用**用例。
> 2. 只测 happy path 会漏掉「页面已死」「读回有损」「composer 退化」这类真实分支 →
>    按 troubleshooting.md 的症状逐条造用例；修复的回归测试要能在「把修复关掉」时失败。

**不用从零造用例**：事故档案里 25 条反模式都有一份可粘贴的假 page/driver 模板，见
[references/regression-templates.md](regression-templates.md)（配 `scripts/regression_kit.py`）。
每个模板都带一个「旧行为开关」：`TEMPLATE_VARIANT=<bug_id>` 把修复换回旧代码，对应用例**必须变红**。
`scripts/check_templates.py` 会机械地验证这三件事（能跑 / 能红 / 覆盖全）——写入阶段的 DoD 之一。

**三条验收元规则**（同族 Gemini，每条都来自真实误判）：

1. **「未复现」不等于「已排除」**：代码层能确定的缺陷，即使真机这次没触发也要修。
2. **先检查测量工具本身**：读回不含换行这件事曾让三轮排查找错方向；工具口径变了，结论就变了。
3. **区分力实验是唯一证据**：把修复换回旧代码，新用例必须**逐字变红**，日志要对上真机原签名。

**必测的负面分支**（照抄这份清单能直接抓出真事故）：响应以 `</html>` 结束的非 JSON、403 挑战壳里
残留的输入框、`is_closed()==True` 的关闭页与 `is_closed()==False` 的崩溃页（两种死法走不同代码路径）、
被取消的 watcher、同一 prompt 的第二轮（旧内容不得当新回复）。

### 3.3 配置 / 文档漂移测试

```python
# 1) 扫 .env.example 的每个键，断言 config 包里有对应 env_*("KEY", ...) 调用
# 2) 扫 config 包每个 env_* key，断言它出现在 .env.example（新增配置忘补模板 → 失败）
# 3) README 变量表的「默认值」== config 源码里的字面量默认值
```

---

## 4. 客户端适配

- **OpenAI SDK**：`base_url=http://127.0.0.1:8002/v1`，`api_key="none"`，`timeout=240`（> 服务端超时）。
- **Codex CLI**：`wire_api = "responses"`，`base_url=.../v1`；网页版生成慢 →
  调大 `stream_idle_timeout_ms` / 重试次数。
- **Pi / Cline 等**：走 `openai-completions`，`compat.supportsDeveloperRole=false`。
- 提供 `client_test.py`（官方 SDK）作为「用户会用的那个接口」的验收载体。
- README 写清：可用模型名以 `/v1/models` 实际返回为准；不提供 API Key 鉴权时要声明
  「仅本机使用」，非回环暴露不受支持。

---

## 5. 编码阶段的定义完成（DoD）

- [ ] 所有可调参数与选择器集中在 `config/`，`.env.example` 与之一一对应（漂移测试通过）。
- [ ] `GET /v1/models`、非流式、流式均能跑通；错误体形状与状态码符合设计表。
- [ ] 新增公共方法有「按实例调用」测试；启发式逻辑（结束判定、输入写入、恢复）有回归测试。
- [ ] 假 page/driver 单测全绿，**不启动浏览器**；E2E 默认 skip。
- [ ] `ruff check .` 全过（豁免写明原因）；`mypy` 无**新增**错误；`pytest -q` 无新增失败。
- [ ] README 记录：快速开始、接口表、错误表、会话模型、已知限制、安全说明。
