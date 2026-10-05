# Gemini Web-to-API Bridge 设计文档

把 **Gemini 网页版（gemini.google.com）** 包装成 **OpenAI 兼容的本地 API 服务**，让支持 OpenAI 协议的客户端（Pi Coding Agent、OpenAI Codex CLI、OpenAI SDK、LangChain、Cline 等）直接使用 Gemini 网页版能力，包括流式输出与 function calling（工具调用）模拟。

本文件只描述方案，不含实现代码。全部配置键统一采用 `GEMINI_*` 命名，以仓库根目录 `.env` 为准。

---

## 1. 目标与非目标

### 目标
- 暴露 OpenAI 兼容端点：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`。
- 支持 `messages`、`tools`、`stream` 等标准字段，错误也以 OpenAI 兼容 `error` 结构返回。
- 流式响应（SSE），逐字吐出内容，兼容 OpenAI 流式解析器。
- 通过「提示词注入 + 结构化解析」模拟 OpenAI `tool_calls` 语义。
- 复用本地登录态（Playwright 持久化上下文），无需官方 API Key。
- 可运维：健康检查、无头模式、DOM 诊断端点、会话生命周期管理。

### 非目标
- 不实现 Gemini 原生协议（generateContent 等）。
- 不做多用户鉴权；服务只监听 `127.0.0.1`。
- 不追求真实 token 计量（网页版无 usage，仅估算）。

---

## 2. 总体架构

```
客户端 (Pi / Codex CLI / OpenAI SDK)
        |  OpenAI 协议 (HTTP + SSE)
        v
+-----------------------------------------+
| FastAPI 服务 (gemini_web/server.py)     |
|  /v1/models  /v1/chat/completions       |
|  /v1/responses  /healthz  /debug/dom    |
|                                         |
|  会话桶管理 (buckets + locks)           |
|  prompting  messages -> 文本            |
|  toolcalls  工具注入 + 解析             |
|  streaming  SSE 编码 (chat)             |
|  responses  Responses API 兼容层        |
+-----------------------------------------+
        |  Playwright
        v
+-----------------------------------------+
| Chromium (persistent context)           |
| user_data/ 保存登录态                    |
| 打开 gemini.google.com/app              |
+-----------------------------------------+
```

数据流（一次 chat 请求）：
1. 路由层校验请求体（宽松，未知字段忽略），按 `X-Gemini-Session` / `user` / User-Agent 选择会话桶。
2. `prompting` 把 messages 数组渲染为输入框文本（含 system/developer 前缀、历史拼接、工具说明）。
3. `driver` 在桶对应页面输入并发送，轮询 DOM 抓取回复，按结束判定返回文本。
4. `toolcalls` 解析文本中的结构化调用块，得到 `tool_calls`。
5. `streaming` 或普通 JSON 返回给客户端；`responses` 负责 Responses API 命名事件。
6. 回复含代码块时从 DOM 提取并落盘到 `output/`。

---

## 3. 文件清单

```
GeminiBridge/
├── gemini_api_server.py      # 入口薄封装：重导出历史公开名字 + 启动 uvicorn
├── gemini_web/
│   ├── __init__.py
│   ├── config.py             # 解析 .env，导出全部可调参数（运行期按属性读取）
│   ├── errors.py             # 异常类型 + DEFAULT_SESSION_KEY / HOME_URL（来自 config.WEBSITE）
│   ├── models.py             # OpenAI 兼容 Pydantic 模型（chat / responses / 错误）
│   ├── prompting.py          # messages 数组 -> 输入框文本（含工具注入模板、播种）
│   ├── toolcalls.py          # 工具描述注入 + 输出解析成 tool_calls + 内置工具
│   ├── markdown_io.py        # Markdown 围栏安全的按行读写（纯逻辑，内置工具用）
│   ├── tasks.py              # 任务快照（轮转后不丢任务目标）
│   ├── driver.py             # Driver 组装 + 浏览器生命周期（5 个 mixin）
│   ├── session_store.py      # 会话状态与落盘（SessionStoreMixin）
│   ├── page_pool.py          # 页面池 / 空闲回收 / LRU / 按桶加锁（PagePoolMixin）
│   ├── completion.py         # 新建对话 / 轮转 / 到顶判定（CompletionMixin）
│   ├── chat_io.py            # 发送 / 轮询 / 提取 / 落盘清理（ChatIOMixin）
│   ├── streaming.py          # chat completions 的 SSE 编码
│   ├── responses.py          # Responses API 兼容层（命名 SSE 事件）
│   └── server.py             # FastAPI app、路由、会话桶、健康检查
├── tests/                    # unittest + pytest 混合，全部用假 page / driver
│   ├── test_config.py        test_config_drift.py   test_doc_sync.py
│   ├── test_models.py        test_prompting.py      test_toolcalls.py
│   ├── test_parsing.py       test_seed_prompt.py    test_markdown_io.py
│   ├── test_end_detection.py test_sessions.py       test_responses.py
│   ├── test_streaming.py     test_routes_chat.py    test_output_prune.py
│   ├── test_e2e_harness.py   # E2E 脚手架的定向回归（不联网）
│   └── e2e/                  # 真实联网对等测试（GEMINI_E2E=1 才跑）
├── requirements.txt
├── .env                      # 已存在，实际生效配置（gitignore）
├── .env.example              # 全量配置模板（防漂移测试双向守护）
├── .gitignore
├── README.md
└── doc/
    ├── design.md             # 本文件
    ├── tasks.md              # 任务分解与状态跟踪
    ├── update.md             # 全项目复审 + 联网实测记录 + 建议
    ├── update_codex.md       # 既有视角分析（Codex）
    ├── update_pi.md          # 既有视角分析（Pi）
    ├── markdown_io_design.md # markdown_io 设计稿（已实现，归档）
    └── e2e_test_design.md    # E2E 对等测试设计
```

---

## 4. 配置项（统一 GEMINI 命名）

全部来自根目录 `.env`，由 `gemini_web/config.py` 读取并提供默认值。

> **默认值的单一事实来源是 `.env.example`**（与 `config.py` 双向对齐，由
> `tests/test_config_drift.py` 守护）。下表只列出最常调的项，不再逐键重复，
> 避免再次出现“改了一处、另一处漂移”的情况。

### 服务监听
- `HOST`：默认 `127.0.0.1`，不要绑定 `0.0.0.0`（转发的是登录会话）。
- `PORT`：默认 `8001`。
- `WEBSITE`：默认 `https://gemini.google.com/app`。入口 URL，由 `errors.HOME_URL` 读取，
  改 `.env` 会真的生效（此前是硬编码死配置）。

> 注意：**真实环境变量优先于 `.env`**。例如环境里存在 `PORT=0` 时，实际生效的端口是 `0`
> （`uvicorn --port 0` 会绑定随机端口），这曾让 E2E 测试误起第二个实例。

### 上游等待 / 重试
- `GEMINI_TIMEOUT`：单轮生成总超时（秒），默认 `180`，须小于客户端 HTTP 超时。
- `GEMINI_RETRIES`：上游超时后最大尝试次数，默认 `2`。
- `RETRY_BACKOFF_S`：重试退避基数，第 n 次等待 `RETRY_BACKOFF_S * n`，默认 `1.0`。

### 结束判定轮询
- `POLL_INTERVAL_S`：轮询间隔（秒），默认 `1.5`。
- `STABLE_POLLS`：文本完全相同的连续次数，默认 `5`。
- `LEN_STABLE_POLLS`：仅长度不再增长的连续次数（更保守），默认 `8`。
- `STALL_POLLS`：连续多少次既无正文也无「生成中」信号即提前失败，默认 `20`。
- `CAP_CHECK_EVERY`：每多少轮检查一次「会话到顶」提示，默认 `4`。

### 运行模式 / 调试
- `HEADLESS`：无头模式，首次登录需 `false`，默认 `false`。
- `GEMINI_DEBUG`：开启后每轮轮询打印状态，并启用 `/debug/dom`（不回显正文），默认 `false`。

### 路径
- `USER_DATA_DIR`：Chromium 持久化用户目录，默认 `./user_data`，勿提交。
- `NEW_CHAT_SELECTOR`：新建对话按钮选择器，轮转 / 每桶首次进入时点击，默认按 Gemini 网页版实际调整。
- `OUTPUT_DIR`：代码 / 回复落盘目录，默认 `./output`，客户端可用 `output_dir` 覆盖。

> 不再落盘会话地址。原因：Gemini 网页版会话 URL 形态与「当前对话」绑定关系不稳定，
> 回填易落到空白页或错误会话。改为**每桶始终新开对话 + 历史播种**（见第 6 节）。

### DOM 选择器（网页版改版时在此调整）
- `RESPONSE_SELECTORS`：Gemini 回复节点候选，支持 `||` 回退链，默认覆盖 `message-content` 等常见容器（改版后按实际调整）。
- `INPUT_SELECTORS`：支持 `||` 回退链，首个命中即用；Gemini 输入区为 `rich-textarea` / `[contenteditable]`，需覆盖。
- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定可输入。
- `CODE_BLOCK_SELECTOR` / `CODE_TAG_SELECTOR`：默认 `pre` / `code`。
- `NEW_CHAT_SELECTOR`：新建对话入口，轮转与首轮前点击，确保从干净会话开始。

### 会话分桶
- `SESSION_SCOPING_BY_UA`：请求头与 `user` 都缺失时按 User-Agent 分桶，默认 `true`。
- `MAX_SESSION_BUCKETS`：最大会话桶数，默认 `3`，应 >= 同时访问的 Agent 数。
- `PARALLEL_BUCKETS`：是否真正并行驱动多会话，默认 `true`。
- `BUCKET_LOCK_TIMEOUT_S`：同一桶排队上限（秒），默认 `15`，超时返回 503 `upstream_busy`；`0` = 一直等。
- `SESSION_MAX_TURNS`：单会话最大轮次，默认 `60`。
- `SESSION_MAX_TOKENS`：单会话最大估算 token，默认 `60000`（本机 `.env` 覆盖为 `1000000`）。
- `MAX_SESSION_STATE_CACHE`：内存会话状态缓存上限，默认 `64`（LRU，`0` 不限）。
- `BUCKET_IDLE_TTL_S`：空闲页面回收秒数，默认 `900`（`0` 关闭）。

### Responses API（Codex CLI）
- `ENABLE_RESPONSES_API`：是否启用 `/v1/responses`，默认 `true`；关闭返回 404，不影响 chat。
- `RESPONSES_KEEPALIVE_S`：流式 keep-alive 注释间隔（秒），默认 `10.0`，`0` = 关闭。
- `RESPONSES_TOOL_BUFFER`：工具模式是否先缓冲整段回复再解析 `tool_calls`，默认 `true`。

---

## 5. 模块设计

### 5.1 `config.py`
- 加载 `.env`，导出强类型配置对象；未设置的键使用上文默认值。
- 选择器类配置支持 `||` 分隔的回退链，解析为列表。
- 路径类配置解析为绝对路径（相对 `.env` 所在目录）。

### 5.2 `models.py`
- `ChatCompletionRequest`：`model`、`messages`、`tools`、`stream`、`user` 等；`extra="allow"` 宽松校验，未知字段（`temperature`、`reasoning_effort`、内容分片数组等）一律接受，绝不返回 422。
- 响应模型：`ChatCompletion`、`ChatCompletionChunk`、`ToolCall`、`Usage`。
- `ResponsesRequest` / `ResponsesResponse`：Responses API 结构。
- `ErrorResponse`：OpenAI 兼容 `error` 对象。

### 5.3 `prompting.py`
- system/developer 消息合并为前缀指令块。
- 多轮对话按顺序拼接历史，标注角色，最后附当前用户输入。
- 有 `tools` 时追加工具说明段，要求模型在需要调用时按约定格式输出。
- 输出纯文本，可直接送入输入框。

### 5.4 `toolcalls.py`
- 把 OpenAI `tools`（JSON Schema）注入提示词，给出明确输出契约。
- 解析模型输出中的结构化调用块 -> `tool_calls`（含 `id`、`name`、`arguments`）。
- 无法解析时不报错，按普通文本回复处理。
- `RESPONSES_TOOL_BUFFER=true` 时先缓冲整段再解析。

### 5.5 `driver.py`
- `launch_persistent_context(USER_DATA_DIR)`，打开 `WEBSITE`；登录态持久化。
- 首次请求 / 轮转时点击 `NEW_CHAT_SELECTOR` 开一个干净对话，不尝试恢复旧会话。
- `READY_SELECTOR` 判定可输入后，向 `INPUT_SELECTORS` 首个命中元素输入文本并发送。
- 按 `RESPONSE_SELECTORS` 抓取最后一条回复；每 `POLL_INTERVAL_S` 轮询一次。
- 结束判定：文本不变连续 `STABLE_POLLS` 次，或长度不变连续 `LEN_STABLE_POLLS` 次。
- 超时用 `GEMINI_TIMEOUT`，重试 `GEMINI_RETRIES` 次，退避 `RETRY_BACKOFF_S * n`。
- profile 被占用、等待超时等情况给出可操作提示。

### 5.6 `streaming.py`
- 把增量文本编码为 chat completions 的 SSE（`data: {...}` + `data: [DONE]`）。
- 首个 chunk 带 `role`，后续为 `content` 增量；结束 chunk 带 `finish_reason`。

### 5.7 `responses.py`
- 把 Responses 请求映射为内部 chat 调用，再把结果转成命名 SSE 事件：`response.created`、`response.output_text.delta`、`response.completed` 等。
- `RESPONSES_KEEPALIVE_S>0` 时按间隔发注释保活，防 Codex 连接断开。
- 工具调用映射为 Responses 的 function call 事件。
- 错误映射为 Responses 兼容错误结构。

### 5.8 `server.py`
- 路由：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`（受 `ENABLE_RESPONSES_API` 控制）、`GET /healthz`、`GET /debug/dom`（受 `GEMINI_DEBUG` 控制，不回显正文）。
- 会话桶：键优先 `X-Gemini-Session`，其次 `user`，最后（`SESSION_SCOPING_BY_UA=true` 时）User-Agent。
- `PARALLEL_BUCKETS=true` 时各桶独立页面并行；`MAX_SESSION_BUCKETS` 限制总数。
- `BUCKET_LOCK_TIMEOUT_S>0` 时同桶排队超时返回 503 `upstream_busy`。
- 达到 `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS` 时轮转到新会话并播种已有上下文。
- `/healthz` 返回状态与 cluster 字段：`parallel`、`max_buckets`、`busy`、`open_pages`。

### 5.9 代码落盘
- 回复含代码块时，从 DOM 的 `CODE_BLOCK_SELECTOR` / `CODE_TAG_SELECTOR` 提取，按语言保存为 `.py` / `.js` / `.json` 等到 `OUTPUT_DIR`。
- 无代码块时保存完整回复为 `.md`。

---

## 6. 会话生命周期

- **创建**：某桶首次请求时打开页面，等待 `READY_SELECTOR`。
- **播种**：轮转到新会话时，把已有对话上下文作为首条消息送入，保持连续性。
- **到顶**：轮次或估算 token 超过阈值视为「对话长度上限」，不伪装成超时。
- **轮转**：到顶后开新会话并播种，替换桶内页面。
- **重试阶梯**：单轮超时按 `GEMINI_RETRIES` 重试，退避 `RETRY_BACKOFF_S * n`。
- **回收**：空闲 / 超限页面关闭，`open_pages` 反映当前数量。
- **锁**：同桶并发请求按 `BUCKET_LOCK_TIMEOUT_S` 排队，超时 503。

---

## 7. 测试策略

- 以标准库 `unittest` 为主，部分文件（如 `test_config_drift.py`、`test_output_prune.py`）为 pytest 风格；两者都由 `pytest -q` 统一收集。
- 用假 page / 假 driver 驱动，不启动浏览器、不需额外依赖；`tests/e2e/` 是真实联网对等测试，`GEMINI_E2E=1` 才运行，否则全部 skip。
- 覆盖：配置解析与默认值、prompting 拼接、toolcalls 注入与解析、结束判定、会话生命周期（播种 / 到顶 / 轮转 / 重试 / 分桶 / 回收 / 锁）、chat 路由、Responses 路由。
- 运行：`.venv/bin/python -m unittest discover -s tests -t . -v`。

---

## 8. 风险与缓解

- **网页版改版**：选择器集中在 `.env`，改版时只改配置。
- **登录态失效**：`HEADLESS=false` 手动重登，登录态存 `user_data/`。
- **profile 被占用**：同一时间只允许一个实例，重复启动给出提示并建议 `pkill`。
- **结束判定误判**：双阈值（`STABLE_POLLS` / `LEN_STABLE_POLLS`），偏保守。
- **Codex 连接断开**：`RESPONSES_KEEPALIVE_S` 保活，客户端侧调大 `stream_idle_timeout_ms`。
- **安全**：只监听 `127.0.0.1`，不提交 `user_data/`。
