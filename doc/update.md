# GeminiBridge 全项目分析与改进建议

> 分析日期：2026-10-06
> 范围：`gemini_api_server.py`、`gemini_web/*`（16 模块）、`tests/*`、`doc/*`、`.env` / `.env.example`、`README.md`、`requirements.txt`
> 方法：通读全部源码 + 实跑测试 + **真实 Gemini 登录下的联网实测** + 文档/配置/任务清单交叉核对
> 基线：`.venv/bin/python -m pytest -q` → **206 passed, 19 skipped**（含本轮新增 9 个 E2E 脚手架用例）；解释器 Python 3.14.5
> 联网实测：见第 10 节（真实调用 gemini.google.com，覆盖 chat / tools / responses / 长文本 / 多轮）

本文是**面向当前代码现状**的独立复审。此前的两份视角分析（`doc/update_codex.md`、`doc/update_pi.md`）已覆盖大部分历史问题并留有落地记录，本文不重复其已修复项，重点给出**仍未解决、且本轮实测可复现**的问题与建议。

---

## 1. 结论摘要

项目方向正确、分层清晰，已从「能不能用」阶段进入「长跑稳定性 + 协议细节 + 文档一致性」阶段。当前**真正的代码缺陷只有两处**（P0），其余是配置/文档/工程化的一致性缺口：

| 编号 | 问题 | 级别 | 影响面 |
| --- | --- | --- | --- |
| P0-1 | 流式回复在「回复节点被整体替换」时**静默丢尾** | 高 | chat 流式、Responses 流式（Pi / Codex 都会命中） |
| P0-2 | ~~会话状态文件跨桶读-改-写竞态~~ **该判断已实测证伪**（见 §3 修正）；真正的问题是非原子写盘 → 崩溃时状态清零 | 中 | 进程被 kill / 崩溃的瞬间 |
| P1-1 | `.env` 的 `WEBSITE` 是**死配置**（代码从不读取） | 中 | 误导排障，改它不生效 |
| P1-2 | `output/` 清理**只在落盘时触发**，`tasks.md` T4.4 宣称的「启动 + 后台周期清理」未实现 | 中 | 长跑磁盘增长 |
| P1-3 | `design.md` 默认值与文件清单**漂移**，防漂移测试只守住 4 个键 | 中 | 文档可信度、新人上手 |
| P1-4 | `README.md` 配置表**缺项**（多个实际生效键未列出） | 中 | 文档可信度 |
| P1-5 | `EDIT_MARKDOWN_LOCAL=true` 时**自动注入** edit_markdown，每个请求多付 ~970 tokens 脚手架 | 中 | 所有客户端的上下文预算与 `usage` 可信度（联网实测确认） |
| P2 | 日志、异常吞没、死代码、依赖未固定、无 CI、测试/文档清单缺口等 | 低 | 可维护性 |

---

## 2. 项目概览与亮点

**定位**：用 Playwright 驱动持久化 Chromium，把 gemini.google.com 包装成 OpenAI 兼容本地 API（`/v1/chat/completions`、`/v1/responses`），供 Pi、Codex CLI、`agy` 等只认 OpenAI 协议的客户端使用。

**分层**：入口薄封装 `gemini_api_server.py` → `gemini_web/` 包。`driver` 由 5 个 mixin 组装（`session_store` / `page_pool` / `completion` / `chat_io` + 自身生命周期），配置集中在 `config.py` 且全部运行期按属性读取（便于测试 patch）。

**值得保留的设计**（应作为后续改动的硬约束）：

- **新开对话 + 历史播种**取代易碎的会话 URL 恢复；用 `tasks.py` 任务快照保住任务目标，避免被 `SEED_MAX_CHARS` 尾部截断丢任务。
- **`_complete_text` 去动画**：克隆回复节点、剥离 `animating/pending` 再读 `innerText`，规避逐 token 显现导致的半截 JSON。
- **双阈值结束判定**（内容稳定 / 长度稳定）+「停止按钮」信号 + `_has_pending_tokens` 二次确认。
- **错误分型**：`context_length_exceeded` / `upstream_busy` / `timeout` / `upstream_error`，客户端可区分。
- **发送不依赖窗口焦点**：页面内派发 Enter / click（`chat_io._submit_prompt`），有头模式不抢前台。
- **选择器全外置**，网页改版只改配置。
- **工具解析韧性**：`TOOL_CALL` 纯文本首选 + 围栏/DSML/裸 `tool_uses` 兜底 + JSON 引号/控制字符修复 + salvage。

---

## 3. 实质性问题（P0）

### P0-1 流式回复在「回复节点被整体替换」时静默丢尾

**证据链**

1. 增量计算刻意在「节点被替换」时停发：`gemini_web/prompting.py` 的 `_delta_piece()`，当 `current` 不以 `streamed` 为前缀时返回 `(None, streamed)`——保留已发内容、不再追加（这个取舍本身是对的）。
2. 但流式收尾只在「从未发过任何增量」时才补全：
   - `gemini_web/streaming.py:116` 任一增量下发即 `streamed = True`；`:146` `if not streamed and reply_content:` 才补发全文。
   - `gemini_web/responses.py:512` 累计 `streamed`；`:587` `if not streamed and full:` 才补发全文。
3. 于是当生成中途发生一次节点整体替换（长会话下 Gemini 会回收/替换消息节点，这一点 `chat_io._send_chat_locked` 的注释已明确承认），客户端拿到的是**被截断的前半段**，而真正完整的 `reply_content` 从不补发。用户体感是「流式偶发少一截」，且**无任何报错**。

**影响**：Pi 走 chat 流式、Codex 走 Responses 流式，都会命中；与工具结果拼接时还可能让客户端基于不完整文本决策。

**建议**：把「是否发过增量」与「客户端当前持有的文本」分开记账。收尾时若 `reply_content` 与已发内容不一致，按前缀关系补发差额；若已无法构成前缀（发生了替换），用 OpenAI 允许的**清空语义**纠正——chat 流式可发一个 `content: ""` + 全量重放的纠错块（或直接退回「非增量一次性给全量」），Responses 流式则发一个覆盖性的 `response.output_text.done`（该事件本就携带全文 `text`）并以它为准。关键是**保证最终 `done`/`completed` 里的文本一定与 `reply_content` 一致**，不可依赖客户端「只看 delta」。

**验收**：构造「第 2 次 on_delta 后 current 变为非前缀」的假页面，断言最终 SSE 拼接结果（或 `output_text.done.text`）等于完整 `reply_content`，且无重复片段。

### P0-2（已修正）状态文件并发：**原判断是误报**，真正的问题是非原子写盘

> **勘误（2026-10-06 实测）**：本节初版声称「跨桶读-改-写有竞态、并发会丢更新」——**这是错的**。
> `_save_session_state()` 内部**没有任何 `await`**，是一段纯同步代码；在单事件循环下，
> 没有 await 的临界区不可被打断，因此不同会话桶并发调用时读-改-写天然互斥。
> 实验（8 桶 × 30 轮，每步插入 `await asyncio.sleep(0)` 强制交错）结果：**每桶 `turns` 都是 30，零丢失**。
> 之前把「按桶加锁」误读成了「临界区跨 await」，特此更正。

**真正存在的问题**：写入用 `config.SESSION_FILE.write_text(...)`——**先截断再写**。
若进程在写入中途被 kill / 崩溃，文件会残缺；而 `_read_state_file` 对解析失败一律返回 `{}`
（原实现还是 `except Exception: pass`），于是**状态（轮数 / token 预算 / 到顶标记）静默清零**，
直接后果是会话轮转阈值失真。

**已实施**：

- 写入改为「同目录临时文件 + `os.replace`」原子替换（`session_store.py`）。
- 失败不再静默：`except OSError` 改为 `logger.warning`；`_read_state_file` 对读失败 / 非 JSON / 解析失败也各留一条 warning。
- 在 `_save_session_state` 的 docstring 里写明**不变量**：「本方法必须保持纯同步无 await；
  一旦引入 await，就必须补一把跨桶 asyncio.Lock」。

**验收（已通过）**：`tests/test_sessions.py::StateFileTests`

- `test_concurrent_buckets_do_not_lose_updates`：8 桶 × 20 轮并发，每桶 `turns` 完整（锁住“无 await”不变量，将来有人加 await 会立刻变红）。
- `test_write_uses_atomic_replace_and_leaves_no_tmp`：不残留临时文件。
- `test_corrupt_state_file_is_ignored_not_fatal`：截断 JSON 不致命。

---

## 4. 一致性与可运维性问题（P1）

### P1-1 `WEBSITE` 是死配置

- `.env` 与 `design.md` §4 都列了 `WEBSITE`，但 `grep WEBSITE gemini_web/` **零命中**；入口 URL 实际硬编码为 `gemini_web/errors.py` 的 `HOME_URL = "https://gemini.google.com/app"`。
- 改 `.env` 的 `WEBSITE` 不会有任何效果，是明确的排障陷阱。
- **建议**：二选一——要么让 `config.WEBSITE` 生效（`HOME_URL` 改从 config 读取，保留同名默认值），要么从 `.env`、`.env.example`、`design.md` 中删除。鉴于「入口 URL 可配置」确有价值，推荐前者。

### P1-2 `output/` 清理未按宣称执行

- `doc/tasks.md` 的 T4.4 标记为已完成，描述为「服务启动及后台周期任务中执行清理」；但 `_prune_output_dir()` 仅在 `gemini_web/chat_io.py:565`（`save_extracted_files` 内）被调用，**启动与后台均未调用**（`server.lifespan` 无相关代码）。
- 后果：`SAVE_FILES=true` 时，只有「又发生一次落盘」才会顺带清理一次；纯读取/无落盘的长跑进程不会回收旧文件。
- **建议**：在 `lifespan` 启动时调用一次，并（可选）加一个 `asyncio` 后台任务按固定间隔（如 1 小时）清理；同时把 `_prune_output_dir` 里的 `except Exception: continue/pass` 补上 DEBUG 日志。若不想加后台任务，至少修正 `tasks.md` 的措辞，使文档与实现一致。

### P1-3 `design.md` 漂移，防漂移测试覆盖不足

- 防漂移测试 `tests/test_doc_sync.py` 的 `DOC_KEYS` 只校验 4 个键（`STABLE_POLLS` / `LEN_STABLE_POLLS` / `MAX_SESSION_BUCKETS` / `BUCKET_LOCK_TIMEOUT_S`），其余默认值**无守护**，已实际漂移：
  - `design.md` §4 写 `SESSION_MAX_TURNS` 默认 `80`、`SESSION_MAX_TOKENS` 默认 `240000`；本机 `.env` 实为 `60` / `1000000`。
  - `design.md` §4 的 `WEBSITE` 键根本不存在（见 P1-1）。
- `design.md` §3 文件清单已严重过期：列出的 `test_ending.py`、`test_routes_responses.py`、`client_test.py`、`INSTALL.md`、`cmdlog.md` **均不存在**；同时遗漏了真实存在的 `chat_io.py`、`completion.py`、`page_pool.py`、`session_store.py`、`tasks.py`、`markdown_io.py`、`errors.py`、`streaming.py` 等模块与 `test_markdown_io.py` / `test_responses.py` / `test_seed_prompt.py` / `test_routes_chat.py` 等测试。
- **建议**：把 `DOC_KEYS` 扩成「从 `config.py` 自动提取全部 `env_*` 键，逐一在 `design.md` 中查找默认值并比对 `.env`」，新增键时漏写文档即测试失败；同步重写 `design.md` §3/§4 使其反映现状。

### P1-4 `README.md` 配置表缺项

- `README.md` 的配置表未列出实际生效的多个键（`grep` 零命中）：`STALL_POLLS`、`CAP_CHECK_EVERY`、`CAP_NOTICE_PATTERNS`、`READY_TIMEOUT_MS`、`SEED_SYSTEM_MAX_CHARS`、`SESSION_KEY_MAX_LEN`、`GEMINI_NEW_SESSION`、`SEND_BUTTON_SELECTORS`、`EDIT_MARKDOWN_LOCAL` / `EDIT_MARKDOWN_BACKUP_DIR`、`TASK_RECENT_ITEM_MAX_CHARS` 等，而 `.env.example` 里都有。
- 另外语义漂移一处：`README` 把 `GEMINI_RETRIES` 描述为「上游超时重试次数」，而 `config.py` 注释与 `chat_io.send_chat` 的用法是**最大尝试次数**（`max_attempts = max(1, GEMINI_RETRIES)`），同一数值含义不同。
- **建议**：以 `.env.example` 为准补全 `README` 配置表，并统一「重试次数 vs 尝试次数」的措辞。

### P1-5 `EDIT_MARKDOWN_LOCAL=true` 让每个请求多付 ~970 tokens 脚手架（联网实测确认）

- **实测**：一条极短的 `只回复 K7Q9`，服务端 `usage.prompt_tokens = 970`；而本地用同样的 `build_prompt` 复算只有 ~93。差值不是估算误差，是**自动注入的工具脚手架**。
- **机制**：`server.chat_completions` / `responses._maybe_register_edit_markdown` 在 `EDIT_MARKDOWN_LOCAL=true` 时把 `EDIT_MARKDOWN_TOOL` 追加进 `request.tools` —— 即客户端**没要**任何工具，也会触发 `format_tools_instruction`、`format_tool_call_emphasis`、`edit_markdown_spec` 三段注入。
- **量化**（本地复算与线上 `usage` 完全对得上，`estimate_tokens` 合计 = 970）：

  | 组成 | tokens |
  | --- | --- |
  | `format_tool_call_emphasis`（仅 seed 轮） | 240 |
  | `format_tools_instruction([edit_markdown])` | 481 |
  | `edit_markdown_spec` | 153 |
  | 上下文重建头 + 任务块 + 用户消息 | ~96 |

- **影响**：① 每个新桶请求恒定多付 ~970 tokens，直接挤占 Gemini 网页会话预算（与 `SESSION_MAX_TOKENS` 轮转阈值同单位）；② 模型可能在本不需要编辑文件时发出 `edit_markdown` 工具调用，而客户端（Pi / Codex）从未声明过这个工具，会收到意料之外的 `finish_reason=tool_calls`；③ `usage` 对客户端不再反映“我的输入”。
- **建议**：把内置工具改为**仅在客户端已声明工具时**注入（或新增 `EDIT_MARKDOWN_ALWAYS_REGISTER` 开关，默认 false）；至少把 `format_tool_call_emphasis` 的插入限制为「本轮确实有工具且是 seed 轮」。实测中没有出现误调用，但成本是确定的。

---

## 5. 工程质量与可维护性（P2）

1. **无结构化日志**：全仓大量 `print`（含 `[debug]`/`[恢复]`/`[轮转]` 前缀），无级别、无时间戳、无法重定向。建议改用 `logging`，让 `GEMINI_DEBUG` 控制级别，`/healthz` 之外也给运维一个统一观测面。
2. **异常静默吞没**：`session_store._save_session_state`（`:188`）、`tasks.record`、`_prune_output_dir` 等关键副作用失败时 `except Exception: pass`。这类失败会直接导致「状态/快照悄悄丢」，应至少 `logging.warning` 一次。
3. **死代码**：`gemini_web/responses.py:280` 的 `_resolve_session()` 无任何调用点；`markdown_io.generate_edit` 有设计但未接入任何 driver（可接受，属预留）。
4. **依赖未固定**：`requirements.txt` 只有 5 个裸包名（`fastapi`/`uvicorn`/`playwright`/`pydantic`/`openai`），无版本约束、无 lock、无 `pyproject.toml`；在 Python 3.14 这类较新解释器上，上游破坏性变更会直接打穿环境。建议加 `pyproject.toml`（声明 `requires-python >=3.10` 与版本区间）并锁定关键依赖；`openai` 实际只在 `tests/e2e` 可选路径用到，可考虑移入 `dev` extras。
5. **无 CI**：仓库无 `.github/workflows`，也没有 `pyproject`/`pytest.ini` 等测试配置。测试全靠本地手动跑，回归无护栏。建议加一条最小 CI：安装依赖 + `pytest -q` + `GEMINI_E2E` 未设置时自动 skip 的 E2E 保持跳过。注意惰性 import Playwright 后，纯逻辑测试可在无浏览器环境运行，CI 成本很低。
6. **测试框架混用**：`design.md` §7 承诺「全部使用标准库 unittest」，实际 `tests/test_config_drift.py` 等已改用 pytest 风格（裸 `assert`），其余仍是 `unittest`。建议统一到 pytest（已装），或在文档里明确二者并存。
7. **`context_window` 不一致且未透出**：`gemini_web/models.py:104-107` 的 `SUPPORTED_MODELS` 标 `context_window: 65536`，`README.md:82` 的 Pi 示例写 `"contextWindow": 1000000`，而 `/v1/models` 返回的 `ModelCard`（`server.list_models`）**根本不含该字段**。若目标是让客户端据此裁剪上下文，应把字段透出到 `/v1/models` 并取一致数值；否则删掉以免误导。
8. **`estimate_tokens` 的 CJK 判定**（`prompting.py`）把 `\u3000`–`\u30ff` 整体按 1 char/token 计，含大量标点/符号，估算偏乐观。仅影响 `usage` 展示，优先级低，但可在注释里点明这是「量级估算」而非精确值。
9. **`edit_markdown` 本地执行的路径未约束**（`toolcalls.execute_edit_markdown`）：`path` 可为任意路径，`write=true` 时会在工作区之外落盘（默认 dry-run 降低了风险，但 `.env` 当前 `EDIT_MARKDOWN_LOCAL=true` 意味着该能力已开启）。建议限制到工作区根（或加「仅允许相对路径 + `..` 拒绝」的白名单），并在 README 安全节补充说明。
10. **`/v1/chat/completions` 无鉴权**：默认只监听 `127.0.0.1`，但同机任意进程都能借登录态发起请求。`RESET_TOKEN` 只保护了 `/session/reset`。可选加一个 `BRIDGE_TOKEN`（Bearer）开关，默认关闭以保持兼容。

---

## 6. 测试缺口

当前 197 passed / 19 skipped 覆盖面不错（配置、prompting、toolcalls、结束判定、sessions、responses、streaming、routes、markdown、seed），但仍缺：

- **P0-1 的回归用例**：流式中途节点替换 → 最终文本完整性。
- **P0-2 的并发用例**：`PARALLEL_BUCKETS=true` 下多桶并发落盘状态不丢。
- **`test_routes_responses.py`**：`design.md` 声称存在，实际没有；`/v1/responses` 只有契约级用例，缺路由级（鉴权、404 开关、非法 input）覆盖。
- **`tasks.py` 的独立单测**：现有 `test_seed_prompt.py` 部分覆盖，但 `_is_environment_wrapper` / `_is_meta_prompt` 的多分支（各类 harness 注入块）值得表驱动化。
- **`/debug/dom`、`/session/reset` 的鉴权分支**：`RESET_TOKEN` 设置后 403 的路径无测试。
- **`_prune_output_dir` 的保留策略**：`OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS` 无单测。

建议优先补前两条（直接对应 P0），其余按需。

---

## 7. 文档缺口

- `design.md` §3/§4 需按 P1-3 重写；§7 的「全部 unittest」需按 P2-6 修正。
- `README.md` 配置表需按 P1-4 补全，并补 `SEND_BUTTON_SELECTORS`（README 故障排查里提到过发送按钮思路，但配置表没有该键）。
- `doc/update_codex.md` §8 与 `doc/tasks.md` 的「已完成」状态需要与实现复核：至少 T4.4（output 清理）与实现不符（见 P1-2）。
- 本次已将原 `doc/update.md` 中的「Markdown 读写修改设计稿」迁移至 `doc/markdown_io_design.md`（该能力已实现，设计稿归档），并同步修正 `gemini_web/markdown_io.py` 与 `doc/e2e_test_design.md` 中的引用。

---

## 8. 建议落地顺序

| 顺序 | 项 | 理由 |
| --- | --- | --- |
| 1 | P0-1 流式丢尾修复 + 回归测试 | 唯一会静默损坏客户端数据的缺陷，优先 |
| 2 | P0-2 状态文件读写串行化 + 并发测试 | 本机已开 `PARALLEL_BUCKETS`，实证风险 |
| 3 | P1-1 `WEBSITE` 生效或删除 | 低成本，消除排障陷阱 |
| 4 | P1-2 output 清理落到启动/后台 | 兑现任务清单，防磁盘增长 |
| 5 | P1-3 全键防漂移测试 + 重写 `design.md` §3/§4 | 一次修好，长期防回归 |
| 6 | P1-4 补全 `README` 配置表 | 文档可信度 |
| 7 | P2 工程化批处理（logging、异常可观测、死代码、依赖固定、CI） | 提升可维护性 |
| 8 | 安全项（edit_markdown 路径约束、可选 BRIDGE_TOKEN） | 纵深防御 |

---

## 9. 复现命令（本轮实测）

```bash
# 测试基线
.venv/bin/python -m pytest -q          # -> 197 passed, 19 skipped

# P1-1：WEBSITE 死配置
grep -rn "WEBSITE" gemini_web/         # -> 无命中（仅 .env / design.md 有）

# P1-2：output 清理调用点
grep -rn "_prune_output_dir" gemini_web/   # -> 仅 chat_io.py 定义 + save_extracted_files 内调用

# P1-3：防漂移覆盖范围
grep -n "DOC_KEYS" -A 6 tests/test_doc_sync.py

# P2-3：死代码
grep -rn "_resolve_session" gemini_web/    # -> 仅定义，无调用

# P2-7：context_window 不一致
grep -rn "context_window\|contextWindow" gemini_web/ README.md

# P1-5：量化自动注入的工具脚手架（应等于线上 usage.prompt_tokens）
PYTHONPATH=. .venv/bin/python /tmp/buffy_overhead2.py   # -> seeded+tools tokens: 970

# 本轮新增的 E2E 脚手架定向测试（不联网、不起浏览器）
.venv/bin/python -m pytest tests/test_e2e_harness.py -q    # -> 9 passed
```

---

## 10. 联网实测记录（2026-10-06，真实 Gemini 登录）

### 10.1 环境与方法

- **不再新起实例**：复用当时已在运行的桥接服务（`127.0.0.1:8001`，`browser_ready=true`，`HEADLESS=false`），避免抢占 `user_data` profile。
- **会话隔离**：每个用例用独立 `X-Gemini-Session` 桶（`buffy-net-*`），不污染默认桶。
- 工具：标准库 `urllib` 发请求 + 手写 SSE 解析；usage 用 `estimate_tokens` 复算交叉验证。

### 10.2 结果

| 用例 | 结果 | 耗时 | 关键证据 |
| --- | --- | --- | --- |
| 非流式 chat（哨兵 `K7Q9`） | ✅ | 18.5s | `finish_reason=stop`，回复就是 `K7Q9`；无注入块泄漏 |
| 流式 chat（哨兵 `Q3X8`） | ✅ | 17.6s（首字节 ~0s） | 首块 `delta.role=assistant`；末块 `finish_reason=stop`；`[DONE]`；拼接文本 `Q3X8`；`include_usage` 生效 |
| 工具调用（非流式，`get_weather`） | ✅ | 17.6s | `finish_reason=tool_calls`；`{"city": "北京"}` 为合法 JSON |
| 工具调用（流式，`get_weather`） | ✅ | — | `finish_reason=tool_calls`；分片 arguments 拼接后合法 JSON |
| Responses 非流式 | ✅ | — | `resp_*` id、`status=completed`、`output_text=M4N2`、`usage.input/output_tokens` 齐全 |
| Responses 流式 | ✅ | — | 8 个事件序列完整（created→output_item.added→content_part.added→text.delta→text.done→content_part.done→output_item.done→completed）；`sequence_number` 严格递增；`output_text.done.text=Z9P1` |
| 长文本 + 结束判定（~530 字，尾行 `END7`） | ✅ | 20.6s | `END7` 落在末尾 100 字内，**未被截断**；`completion_tokens=501` |
| 同桶多轮连续性（暗号 `Zebra-42`） | ✅ | turn1 / 6.6s | turn1 `OK`；turn2 正确回忆 `Zebra-42`，且走的是增量 prompt（耗时明显短于首轮） |
| `usage` 随输入增长 | ✅ | — | 输入 15 / 407 / 1607 字符 → `prompt_tokens` 978 / 1272 / 1899（线性可解释；固定基线即 P1-5） |

### 10.3 结论

- **主干链路真实可用**：chat（流式/非流式）、tools（流式/非流式）、Responses（流式/非流式）、长文本结束判定、多轮增量、session 分桶全部通过，未发现功能性失败。
- **P0-1（流式丢尾）本次未复现**：它需要「生成中途回复节点被整体替换」这一偶发条件。实测全程文本完整，但这不构成它不存在——`_delta_piece` 停发增量 + 收尾不补全的逻辑在代码层是确定的（`streaming.py:146` / `responses.py:587`），仍应按 T6.1 修并补回归用例。**“未复现”不等于“已排除”。**
- **P0-2（状态落盘竞态）本次未触发**：测试是串行的，没有两桶并发落盘。它需要 `PARALLEL_BUCKETS=true` 下的并发，仍按 T6.2 修。
- **新增确认**：`/v1/models` 返回的 `ModelCard` 确实**不含** `context_window`（P2-7 实测坐实）。
- **新增发现（P1-5）**：`EDIT_MARKDOWN_LOCAL=true` 的自动注入使每个请求多付 ~970 tokens，已由 `usage` 与本地复算双向确认。

---

## 11. 本轮修复的测试脚手架缺陷

E2E 套件（`tests/e2e/`）此前在后台上跑不起来，且会在桌面上留下空白浏览器窗口。根因与修复如下。

### 11.1 根因链（实际发生的失败）

1. 运行环境里存在 `PORT=0`，而**真实环境变量优先于 `.env`**，于是 `config.PORT = 0`。
2. `test_parity` 用 `config.PORT` 拼 `BASE_URL = http://127.0.0.1:0` → 探活必然失败。
3. `BridgeServer.ensure_started()` 据此认为“服务没起”，用 `uvicorn --port 0` 拉起**第二个实例**（实际绑到随机端口 65388）。
4. 该实例的浏览器初始化失败（同一个 `user_data` profile 已被运行中的服务占用）→ `/healthz` 永远 503。
5. 旧代码只在 `200` 时才返回，于是**白等满 90s** 才报错；由于 `setUpModule` 抛错，`tearDownModule` 不执行，那个 uvicorn 进程**泄漏**（实测确认 PID 8019 一直存活，需手工 `kill`）。
6. 同理，`DirectGeminiClient.start()` 中途失败时**从不关闭浏览器**，`_ensure_direct()` 又把失败的 client 直接丢弃 → headed 模式下桌面上留下一个停在 `about:blank` 的空白窗口，且没有任何人负责关闭它。

### 11.2 修复内容

| 文件 | 修复 |
| --- | --- |
| `tests/e2e/direct.py` | `start()` 失败时先 `self.close()` 再抛错；导航后强校验落点（必须是 `https://gemini.google.com`，否则报错并附 `url`/`title`），把“空白页/登录页”从静默跳过变成可读失败 |
| `tests/e2e/bridge.py` | 端口非法（含 `0`）**立即报错**，不再 `--port 0`；自己拉起的实例若返回 `503 + init_error` 立即报错并 `stop()`；启动超时也 `stop()`，绝不留下孤儿 uvicorn |
| `tests/e2e/test_parity.py` | 新增 `_resolve_port()` 端口护栏（拒绝 `0`/越界/非数字/非整数浮点/bool，回退 `8001` 并打印提示）；`_ensure_direct()` 失败时兜底 `client.close()` |
| `tests/test_e2e_harness.py` | **新增 9 个不联网用例**守护以上行为（含 `int(3.5)==3` 这类截断陷阱） |

### 11.3 修复后验证

- 正向（真实联网、真实浏览器）：`DirectGeminiClient.start()` → `url=https://gemini.google.com/app`，`title=Google Gemini`，输入框就绪 = True。
- 负向（真实浏览器）：就绪选择器不存在 → 抛 `RuntimeError`，`context`/`playwright` 均已释放，**无残留 Chromium 进程**（`ps` 核实为空）。
- 端口护栏：环境 `PORT=0` 时 `PORT` 正确回退 `8001`；`BridgeServer("http://127.0.0.1:0")` 在 **0.01s** 内报错且不拉起任何进程。
- 定向测试：`pytest tests/test_e2e_harness.py -q` → **9 passed**；全量 `pytest -q` → **206 passed, 19 skipped**。
- 全量 E2E（`test_parity`）**未重跑**（按用户要求：已跑过的无需重跑；本轮只跑改过代码对应的用例）。

---

## 12. 本轮代码改动（已实施并验证）

按 `doc/tasks.md` 的推荐顺序落地了 P0 / P1 与两个低成本 P2：

| 任务 | 改动 | 验证 |
| --- | --- | --- |
| T6.1 | `streaming.py` / `responses.py` 收尾对账：按前缀关系补齐差额；无法追加时补发全文并告警。修复「节点被整体替换后客户端永久少一截且无报错」 | `test_streaming.py` / `test_responses.py` 新增 8 用例（含“不得重复”与“工具模式缓冲后补发”） |
| T6.2 | `session_store.py` 写入改「临时文件 + `os.replace`」原子替换；失败改 `logging.warning`；注明“临界区必须无 await”不变量 | `test_sessions.py::StateFileTests`（并发不丢 / 无残留 tmp / 损坏文件不致命） |
| T7.1 | `config.WEBSITE` 真正生效，`errors.HOME_URL` 取自它（不再是硬编码死配置） | `test_config.py::WebsiteConfigTests`（reload 验证接线） |
| T7.2 | `prune_output_dir()` 公开化（保留旧名别名），启动时清一次 + `OUTPUT_PRUNE_INTERVAL_S` 后台周期任务；失败不再静默 | `test_output_prune.py`（保留策略 5 例 + lifespan 启动清理 + 间隔 0 关闭） |
| T7.6 | 新增 `should_register_edit_markdown()`：客户端未声明工具时不再注入内置工具；提供 `EDIT_MARKDOWN_ALWAYS_REGISTER` 逃生口 | `test_toolcalls.py` 新增 5 用例；本地复算：无工具请求由 **970 → 93 tokens，省下 877** |
| T8.2 | `session_store` / `tasks` / `chat_io` 的关键副作用失败改为 `logging.warning`，不再 `except: pass` | 全量回归 |
| T8.3 | 删除死代码 `responses._resolve_session()` | 全量回归 |
| T7.3 | `test_config_drift.py` 改为**双向**校验（config.py ↔ .env.example）：新增键忘写模板 / 模板出现死键都会失败 | 该测试立即抓到遗漏的 `CAP_NOTICE_PATTERNS` |
| T7.4 | 补全 `README` 配置表（WEBSITE / STALL_POLLS / CAP_* / READY_TIMEOUT_MS / SEED_SYSTEM_MAX_CHARS / SESSION_KEY_MAX_LEN / GEMINI_NEW_SESSION / OUTPUT_PRUNE_INTERVAL_S / edit_markdown 三项 / TASK_RECENT_ITEM_MAX_CHARS / SEND_BUTTON_SELECTORS），并纠正 `GEMINI_RETRIES` 的“重试次数 vs 最大尝试次数”语义 | 人工核对 + 防漂移测试 |
| T7.5 | 重写 `design.md` §3 文件清单（删除 5 个不存在的文件）、修正 §4 过期默认值、补充配置优先级陷阱”；§7 改为“unittest + pytest 混合” | 人工核对 |
| T9.7 | （前一轮）`tests/test_e2e_harness.py` | 9 passed |

**测试基线**：`pytest -q` → **238 passed, 19 skipped**（本轮新增 20 个用例，全部不联网）。

**本轮未做**（仍留在 `doc/tasks.md`）：T8.1 全量日志改造、T8.4 依赖固定、T8.5 CI、
T8.6 测试框架统一、T8.7 `context_window` 一致化、T8.8 `estimate_tokens` 注释、
T8.9 `edit_markdown` 路径约束、T8.10 可选 `BRIDGE_TOKEN`、T9.3 `/v1/responses` 路由级测试、
T9.4 `tasks.py` 表驱动单测、T9.5 鉴权分支测试、T10.x 剩余文档同步。

> 另注：上述改动均在**新代码**上验证；当时运行的存量服务（PID 33060）仍跑着旧代码，
> 未重启以避免抢占 `user_data` profile。T7.6 的收益是用本地复算量化验证的，
> 下次重启服务后可用线上 `usage.prompt_tokens` 复核（预期无工具请求从 ~970 降到 ~93）。

---

## 13. 随行轮：P2 与测试缺口收尾（已实施并验证）

接上一节，把 §4（P2）与 §6（测试缺口）的剩余项全部落地。每一项都附了守护测试，验收标准见 `doc/tasks.md`。

| 任务 | 改动 | 验证 |
| --- | --- | --- |
| T8.1 | 新增 `logging_setup.py`；41 处 `print` → `logging`（含 `traceback.print_exc()` → `exc_info=True`）；级别由 `GEMINI_DEBUG` 决定；`server.py` 导入时安装包级 handler（幂等、`propagate=False`） | `tests/test_logging.py`（级别 / 幂等 / 格式 / **全包无裸 `print`**） |
| T8.4 | 新增 `pyproject.toml`（`requires-python >=3.10`、运行时依赖带区间、dev extras = pytest/httpx2/openai）；`requirements.txt` 同步带区间、`openai` 移出运行时 | `test_config_drift.py` 依赖防漂移（名字集合一致、必须有版本约束、`openai` 仅 dev） |
| T8.5 | 新增 `.github/workflows/ci.yml`（push/PR，3.10 + 3.13，装依赖后 `pytest -q`） | 不设 `GEMINI_E2E` → E2E 保持 skip |
| T8.6 | 标准入口统一为 `python -m pytest -q`（`pyproject` 设 `testpaths`）；`design.md` §7 写明混合框架与为何弃用 `unittest discover` 作入口 | 人工核对 + 全量回归 |
| T8.7 | `ModelCard.context_window` 透出（= `SESSION_MAX_TOKENS`）；`SUPPORTED_MODELS` 删掉硬编码 `65536`；README 示例注明以端点为准 | `test_routes_chat.py`（透出 + 随配置变化）、`test_models.py`（不再硬编码） |
| T8.8 | `estimate_tokens` docstring 明确「量级估算」并列出三处同源用途（`usage` / 轮转预算 / `context_window`） | 人工核对 |
| T8.9 | 新增 `EDIT_MARKDOWN_ROOT` + `_resolve_edit_path()`：`resolve()` 后校验根内归属；附带修掉目录目标抛 `IsADirectoryError` 的问题 | `test_toolcalls.py::EditMarkdownPathGuardTests`（6 例） |
| T8.10 | 新增可选 `BRIDGE_TOKEN`（Bearer），用 FastAPI 依赖只挂两个生成端点；`/healthz`、`/v1/models` 保持开放 | `test_auth.py::BridgeTokenTests`（4 例） |
| T9.3 | 新增 `tests/test_routes_responses.py`（7 例）：开关 404、空 input 400、数组 input、未就绪 503、非流式结构、流式事件序列 | 新增文件全绿 |
| T9.4 | 新增 `tests/test_tasks.py`（表驱动 23 例 + 快照往返）：环境包装块 / 元提示矩阵、goal 选取、跨命名空间隔离、recent 截断 | 新增文件全绿 |
| T9.5 | 新增 `tests/test_auth.py`（10 例）：`RESET_TOKEN` 403/200、`/debug/dom` 404/503、`BRIDGE_TOKEN` 401/200 | 新增文件全绿 |
| T10.3 | `update_codex.md` 新增 §8.1、`update_pi.md` 新增 §4「后续进展」对照表；纠正过期快照（测试入口 / 用例数、`_prune_output_dir` 只在落盘触发） | 人工核对 |

**测试基线**：`pytest -q` → **279 passed, 19 skipped, 31 subtests passed**（随行轮 238 → 279，新增用例全部不联网）。

**仍未做**（已知且有意保留，均已写明原因）：

- `ReplyWatcher` 重构（update_codex.md §3.3）：重构面大，建议在更多路由级测试就位后再动。
- 真实环境专属交付物（`client_test.py` / `INSTALL.md` / `cmdlog.md`，update_codex.md §3.6）。
- 线上复核 T7.6 的真实 token 收益：需重启服务并用线上 `usage.prompt_tokens` 观察（预期无工具请求 ~970 → ~93）。

> 本轮为纯本地改动 + 不联网测试，未触碰浏览器 profile；`.env` 只插入了两个新键
> （`BRIDGE_TOKEN=`、`EDIT_MARKDOWN_ROOT=`，均为空 = 保持现有行为），键数 61 = 61。
