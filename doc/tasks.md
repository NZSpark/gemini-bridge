# GeminiBridge 任务分解

依据 `doc/design.md` 拆分。任务按依赖分层，同一层内可并行；每个任务给出产出与验收标准。
移植来源为 `/Users/onetreehill/Github/DeepseekBridge/`，除标注「重写」外均可复用其结构并改 `GEMINI_*` 命名。

状态标记：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成。

---

## 阶段 0：骨架与配置（无上游依赖）

- [x] **T0.1 仓库骨架**：建 `gemini_web/` 包、`tests/`、`output/`，补 `__init__.py`。
  - 产出：目录结构与设计文档第 3 节一致；`requirements.txt` 含 `fastapi`、`uvicorn`、`playwright`、`pydantic`。
  - 验收：`python -c "import gemini_web"` 不报错。
- [x] **T0.2 `config.py`（改造）**：复用 DeepSeek 的极简 `.env` 解析器，改全部键为 `GEMINI_*`，删 `SESSION_FILE` / `SESSION_URL_RE`，加 `NEW_CHAT_SELECTOR`。
  - 产出：`config.<NAME>` 运行时取属性，便于测试 `patch.object`。
  - 验收：`test_config.py` 覆盖默认值、`.env` 覆盖、`||` 回退链解析、布尔 / 数值容错。
- [~] **T0.3 `.env` 校正 + `.env.example`**：把残留 DeepSeek 项（`.ds-markdown` 选择器、DeepSeek 到顶文案、`deepseek` 前缀调试键）替换为 Gemini 网页版实际值。
  - 产出：`.env` 生效配置、`.env.example` 带注释模板、`.gitignore` 忽略 `user_data/ output/ .venv/ __pycache__/`。
  - 验收：`.env.example` 与 `config.py` 默认值一一对应，无遗留 `DEEPSEEK_` / `ds-` 字样。

## 阶段 1：纯逻辑模块（可并行，均可用假对象测试）

- [x] **T1.1 `models.py`（移植）**：OpenAI 兼容 Pydantic 模型，`ChatCompletionRequest` 用 `extra="allow"` 宽松校验，未知字段绝不 422。
  - 验收：`test_models` 覆盖 chat / chunk / tool_call / responses / error 结构。
- [x] **T1.2 `prompting.py`（移植）**：messages → 输入框文本，合并 system/developer 前缀、拼接历史、附当前输入；`estimate_tokens` 估算。
  - 验收：`test_prompting.py` 覆盖多角色、多轮、无 system、空历史。
- [x] **T1.3 `toolcalls.py`（移植）**：工具 JSON Schema 注入提示词 + 输出解析成 `tool_calls`；解析失败按普通文本处理。
  - 验收：`test_toolcalls.py` 覆盖单 / 多调用、参数非法、无工具、注入模板稳定。
- [x] **T1.4 `streaming.py`（移植）**：chat completions 的 SSE 编码，首 chunk 带 `role`，末 chunk 带 `finish_reason`，以 `data: [DONE]` 收尾。
  - 验收：SSE 行可被标准 OpenAI 解析器逐块消费。
- [x] **T1.5 `tasks.py`（移植，可选）**：会话桶任务快照，轮转播种时优先注入任务目标，防被 `SEED_MAX_CHARS` 截断。
  - 验收：`test_tasks.py` 覆盖写入 / 读取 / 截断 / 关闭开关。

## 阶段 2：会话与驱动（本项目核心，需重写）

- [x] **T2.1 `driver.py` 骨架（重写）**：`launch_persistent_context(USER_DATA_DIR)` 打开 `WEBSITE`；等待 `READY_SELECTOR`；点击 `NEW_CHAT_SELECTOR` 开干净对话。
  - 验收：假 page 下能按选择器回退链命中输入框并发送文本。
- [x] **T2.2 回复抓取与结束判定（重写）**：按 `RESPONSE_SELECTORS` 抓最后一条回复，`POLL_INTERVAL_S` 轮询，双阈值 `STABLE_POLLS` / `LEN_STABLE_POLLS` 判结束。
  - 验收：`test_ending.py` 覆盖正常收尾、长停顿不误判、超时兜底。
- [x] **T2.3 会话生命周期（重写）**：每桶首次与轮转均新开对话并播种历史（受 `SEED_MAX_CHARS`），不做 URL 恢复；到顶用 `CAP_NOTICE_PATTERNS` 与 `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS` 双判。
  - 验收：`test_sessions.py` 覆盖创建 / 播种 / 到顶 / 轮转 / 重试退避 / 回收。
- [x] **T2.4 分桶与锁（移植逻辑）**：键优先 `X-Gemini-Session`，其次 `user`，再按 UA；`MAX_SESSION_BUCKETS` LRU 回收，`BUCKET_LOCK_TIMEOUT_S>0` 超时 503 `upstream_busy`。
  - 验收：`test_sessions.py` 覆盖分桶优先级、上限回收、同桶排队与超时、并行开关。

## 阶段 3：API 层

- [x] **T3.1 `responses.py`（移植）**：Responses 请求映射为内部 chat 调用，再转命名 SSE 事件；`RESPONSES_KEEPALIVE_S>0` 发注释保活；工具调用映射 function call 事件。
  - 验收：`test_responses.py` 覆盖文本流、工具流、保活、错误结构。
- [x] **T3.2 `server.py`（移植 + 接线）**：FastAPI app 与路由 `/v1/models`、`/v1/chat/completions`、`/v1/responses`（受 `ENABLE_RESPONSES_API`）、`/healthz`、`/debug/dom`（受 `GEMINI_DEBUG`，不回显正文）。
  - 验收：`test_routes_chat.py` / `test_routes_responses.py` 用假 driver 覆盖非流式、流式、工具、错误码、`/healthz` cluster 字段。
- [x] **T3.3 `gemini_api_server.py` 入口**：薄封装，重导出公开名字 + 启动 uvicorn，读 `HOST` / `PORT`。
  - 验收：`uvicorn gemini_api_server:app` 可启动，`/healthz` 返回 200。
- [x] **T3.4 代码落盘**：回复含代码块时按语言写 `OUTPUT_DIR`；无代码块存 `.md`。
  - 验收：假 DOM 下提取多语言代码块并正确命名文件。

## 阶段 4：联调与文档

- [ ] **T4.1 `client_test.py`**：用 `openai` SDK 打本地服务的示例，覆盖 chat 与（可选）responses。
  - 验收：服务启动后脚本跑通并打印回复。
- [ ] **T4.2 首次真实登录**：`HEADLESS=false` 手动登录 Gemini，登录态落 `user_data/`；确认真实一轮对话可抓取。
  - 验收：真实请求返回非空文本，结束判定在 `GEMINI_TIMEOUT` 内收敛。
- [~] **T4.3 选择器校正**：按真实 DOM 调整 `RESPONSE_SELECTORS` / `INPUT_SELECTORS` / `NEW_CHAT_SELECTOR`，仅改 `.env`。
  - 验收：`/debug/dom` 能定位输入框与回复节点。
- [~] **T4.4 文档**：`README.md`、`INSTALL.md`、`cmdlog.md`；`design.md` 与实现保持同步。
  - 验收：新用户按 `INSTALL.md` 可从零跑通。
- [ ] **T4.5 agy 接入验证**：确认 `agy` CLI 可直连本服务（OpenAI 兼容端点）。
  - 验收：agy 指向 `http://127.0.0.1:8001/v1` 能完成一轮对话。

---

## 依赖关系

```
T0.1 -> T0.2 -> T0.3
T0.2 -> T1.*（阶段 1 全部可并行）
T0.2 -> T2.1 -> T2.2 -> T2.3 -> T2.4
T1.* + T2.* -> T3.*
T3.* -> T4.*
```

## 关键风险

- **Gemini 网页版 DOM 不稳定**：选择器集中在 `.env`，改版只改配置；T4.3 是必做校正步。
- **不恢复旧会话的代价**：每轮播种重放历史，token 消耗高于 URL 恢复；用 `tasks.py` 快照保住任务目标。
- **风控**：`PARALLEL_BUCKETS` 默认关，同时只驱动一个网页会话。

---

## 核对记录（2026-10-03）

对照仓库实际文件逐项核对，标记依据如下：

- **T0.1 [x]**：`gemini_web/`（10 模块）、`tests/`、`output/` 均存在，`gemini_web/__init__.py` 齐全；`requirements.txt` 含 fastapi / uvicorn / playwright / pydantic。
- **T0.2 [x]**：`config.py` 全部键已改 `GEMINI_*`，新增 `NEW_CHAT_SELECTOR`；`SESSION_FILE` 仍保留（`./user_data/.gemini_state`），`SESSION_URL_RE` 已删除。
- **T0.3 [~]**：`.env` 已校正为 Gemini 值（`WEBSITE=https://gemini.google.com/app`、`PORT=8001`、Gemini 选择器与到顶文案）；仅剩注释里一处 `DeepseekBridge` 字样。**缺 `.env.example`**（验收要求存在，未做）。
- **T1.1–T1.5 [x]**：`models.py` / `prompting.py` / `toolcalls.py` / `streaming.py` / `tasks.py` 均存在且为完整实现（106/169/212/170/154 行）。
- **T2.1 [x]**：`driver.py` 用 `launch_persistent_context` 打开 `HOME_URL=https://gemini.google.com/app`，有 `_wait_ready` / `_open_new_chat`（按 `NEW_CHAT_SELECTOR` 回退链）。
- **T2.2 [x]**：`send_chat` / `_page_is_generating` / 双阈值 `STABLE_POLLS` / `LEN_STABLE_POLLS` 均在 `driver.py` 内。
- **T2.3 [x]**：`_start_new_session`、`_remember_session`、`_session_over_budget`、`_page_shows_context_limit`、`_recover_session` 均已实现。
- **T2.4 [x]**：`_session_lock` 支持 `BUCKET_LOCK_TIMEOUT_S` 超时抛 `GeminiBusyError`；`_evict_lru_page` / `_recycle_idle_pages` 回收；`X-Gemini-Session` 优先级在 `server._session_key`。
- **T3.1 [x]**：`responses.py` 556 行，`handle_responses` 已实现。
- **T3.2 [x]**：`server.py` 路由齐全：`/healthz`、`/session/reset`、`/`、`/v1/models`、`/v1/responses`、`/debug/dom`、`/v1/chat/completions`；`ENABLE_RESPONSES_API` 与 `GEMINI_DEBUG` 门控均生效；`/healthz` 返回 `cluster`。
- **T3.3 [x]**：`gemini_api_server.py` 存在，读 `HOST` / `PORT`（`.env` 为 8001）。
- **T3.4 [x]**：`driver.save_extracted_files` 按语言落盘，无代码块存 `.md`。
- **T4.1 [ ]**：**`client_test.py` 不存在**。
- **T4.2 [ ]**：未做真实登录/抓取验证（`user_data/` 下仅有 `.gemini_tasks`，无会话状态文件）。
- **T4.3 [~]**：`.env` 中 Gemini 选择器已按推测值填入，但**未经真实 DOM 校正**，`/debug/dom` 未实跑。
- **T4.4 [~]**：`README.md` 已是 Gemini 文案；**`INSTALL.md` / `cmdlog.md` 缺失**；`design.md` 与 `tasks.md` 均在。
- **T4.5 [ ]**：未验证 agy 接入。

### 缺口汇总

1. **测试全缺**：`tests/` 为空目录，T0.2 / T1.* / T2.* / T3.* 的验收所要求的 `test_config.py`、`test_models.py`、`test_prompting.py`、`test_toolcalls.py`、`test_ending.py`、`test_sessions.py`、`test_responses.py`、`test_routes_*.py` 均未编写。上述 [x] 仅表示**实现代码存在**，不代表验收测试通过。
2. **缺文件**：`.env.example`、`client_test.py`、`INSTALL.md`、`cmdlog.md`。
3. **未验证**：真实登录抓取（T4.2）、真实 DOM 选择器（T4.3）、agy 接入（T4.5）。
