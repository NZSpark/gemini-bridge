# GeminiBridge 任务分解（v5）

依据 `doc/update.md`（2026-10-06 全项目复审）重新梳理。旧版 v4 的阶段 0–5 已作为「历史已完成基线」压缩保留在附录 A，本文新增任务全部对应 update.md 中的 P0 / P1 / P2 条目。

状态标记：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成（实现 + 验收测试齐备）。

核对时间：2026-10-06（实施轮 + 随行轮，阶段 6–10 已全部落地）；基线：`.venv/bin/python -m pytest -q` → **279 passed, 19 skipped, 31 subtests passed**（随行轮新增 41 个不联网用例，238 → 279）。

**联网实测状态（见 `doc/update.md` 第 10 节，真实 Gemini 登录）**：chat（流式/非流式）、tools（流式/非流式）、Responses（流式/非流式）、长文本结束判定、多轮增量、session 分桶**均已实测通过**。两点关于 P0 的结论要分开看：

- **P0-1（流式丢尾）**：代码层确认存在（节点整体替换时停发增量，而收尾只在「从未发过增量」时补全）；真实流量下未复现（缺触发条件），已按代码层修复并补回归用例（T6.1 / T9.1）。**“未复现”不等于“已排除”**——修复是为了从机制上消除它。
- **P0-2（状态落盘）**：「跨桶读-改-写竞态」**已被实测证伪**（`_save_session_state` 无 `await`，单事件循环下不可打断；8 桶 × 30 轮强制交错零丢失）；真正的问题是 `write_text` 非原子，已改为临时文件 + `os.replace`（T6.2）。

> 说明：update.md 指出「真正的代码缺陷只有两处（P0）」，因此 T6.x 为最高优先级；T7.x 为一致性修复（低成本、高确定性）；T8.x 为工程化；T9.x 为测试补全；T10.x 为文档同步。
> 每个任务都给出**对应条目**与**验收标准**，验收未达成不得标记 `[x]`。

---

## 阶段 6：稳定性与正确性（P0，最高优先级）

- [x] **T6.1 修复流式回复静默丢尾**（对应 update.md P0-1）—— **已实施**
  - 现状：`prompting._delta_piece` 在「回复节点被整体替换」时停发增量（取舍正确），但收尾补全仅在「从未发过任何增量」时触发（`streaming.py:146`、`responses.py:587`）；中途替换会使客户端只拿到被截断的前半段，且无报错。
  - 方案：分离「是否发过增量」与「客户端当前持有文本」；收尾时若 `reply_content` 与已发内容不一致，前缀扩展则补发差额，非前缀（真实替换）则用清空/覆盖语义纠正——chat 流式发 `content:""` + 全量重放，Responses 流式以携带全文的 `response.output_text.done` 为准。
  - 验收：新增回归用例，模拟「第 2 次 on_delta 后 current 变为非前缀」，断言最终 SSE 拼接结果（及 `output_text.done.text`）等于完整 `reply_content` 且无重复片段；chat 与 responses 两条路径各一例。
  - 验收测试：`tests/test_streaming.py`、`tests/test_responses.py` 新增用例。

- [x] **T6.2 状态落盘正确性**（对应 update.md P0-2，**已修正原判断**）—— **已实施**
  - 勘误：原任务卡声称「跨桶读-改-写有竞态」——**实测证伪**。`_save_session_state` 内无任何 `await`，单事件循环下临界区不可被打断；8 桶 × 30 轮强制交错实验零丢失。因此**不需要**额外加锁。
  - 真正的问题：`write_text` 先截断再写，中途被 kill 会留下残缺 JSON；而 `_read_state_file` 对解析失败一律返回 `{}`，导致状态（轮数 / token 预算 / 到顶标记）**静默清零**。
  - 已实施：写入改「同目录临时文件 + `os.replace`」；失败改 `logging.warning`；docstring 写明“临界区必须无 await”不变量。
  - 验收：`tests/test_sessions.py::StateFileTests`（并发不丢 / 无残留 tmp / 截断文件不致命）。

---

## 阶段 7：一致性与可运维性（P1）

- [x] **T7.1 `WEBSITE` 死配置处理**（对应 update.md P1-1）—— **已实施**（走推荐路径：真正生效）
  - 现状：`.env` 与 `design.md` §4 都有 `WEBSITE`，`gemini_web/` 内零引用；入口 URL 硬编码为 `errors.HOME_URL`。
  - 方案（推荐）：让 `config.WEBSITE` 生效，`HOME_URL` 改为从 config 读取并保留同名默认值；否则从 `.env` / `.env.example` / `design.md` 删除。
  - 验收：若走「生效」路径，修改 `.env` 的 `WEBSITE` 能改变实际打开的入口 URL；新增/更新配置漂移测试覆盖该键。

- [x] **T7.2 `output/` 清理落到启动与后台**（对应 update.md P1-2）—— **已实施**
  - 现状：`_prune_output_dir()` 仅在 `chat_io.py:565`（`save_extracted_files` 内）调用；`server.lifespan` 启动不清理、无后台任务，与 v4 T4.4 宣称不符。
  - 方案：`lifespan` 启动时调用一次；（可选）加固定间隔（如 1h）的 `asyncio` 后台任务；`_prune_output_dir` 的 `except` 补 DEBUG 日志。
  - 验收：无任何落盘行为的长跑进程在启动后即按 `OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS` 回收旧文件；有对应单测（见 T9.6）。

- [x] **T7.3 防漂移测试 + `design.md` §3/§4 重写**（对应 update.md P1-3）—— **已实施（范围调整）**
  - 实施时把“逐键在 design.md 比对默认值”改为更强且更可维护的方案：**`config.py` ↔ `.env.example` 双向校验**（新增键忘写模板、模板出现死键都会失败），且写页默认值的单一事实来源定为 `.env.example`（design.md §4 已注明）。该测试上线即抓到遗漏的 `CAP_NOTICE_PATTERNS`。
  - 现状：`tests/test_doc_sync.py` 的 `DOC_KEYS` 仅守 4 个键；`design.md` §4 的 `SESSION_MAX_TURNS`(80→60)、`SESSION_MAX_TOKENS`(240000→1000000) 已漂移，§3 文件清单列出 `test_ending.py` / `test_routes_responses.py` / `client_test.py` / `INSTALL.md` / `cmdlog.md` 等不存在项，且遗漏大量真实模块。
  - 方案：把 `DOC_KEYS` 扩为「从 `config.py` 自动提取全部 `env_*` 键，逐一在 `design.md` 中比对 `.env`」；同步重写 `design.md` §3/§4 反映现状。
  - 验收：`config.py` 新增任一配置键而 `design.md` 未记录时，测试失败；重写后的 `design.md` 文件清单与实际文件一一对应。

- [x] **T7.4 补全 `README.md` 配置表并统一措辞**（对应 update.md P1-4）—— **已实施**
  - 现状：README 缺 `STALL_POLLS`、`CAP_CHECK_EVERY`、`CAP_NOTICE_PATTERNS`、`READY_TIMEOUT_MS`、`SEED_SYSTEM_MAX_CHARS`、`SESSION_KEY_MAX_LEN`、`GEMINI_NEW_SESSION`、`SEND_BUTTON_SELECTORS`、`EDIT_MARKDOWN_LOCAL` / `EDIT_MARKDOWN_BACKUP_DIR`、`TASK_RECENT_ITEM_MAX_CHARS` 等；`GEMINI_RETRIES` 描述为「重试次数」而实现是「最大尝试次数」。
  - 方案：以 `.env.example` 为准补全配置表；统一「重试次数 vs 尝试次数」措辞。
  - 验收：`.env.example` 的每个键都能在 README 配置表中找到；可加断言测试防回归。

- [x] **T7.5 复核并修正「已完成」状态**（对应 update.md P1-2 / §7）—— **已实施**
  - 现状：`doc/tasks.md` v4 T4.4、`doc/update_codex.md` §8 的措辞与实现不符（见 T7.2）。
  - 验收：T7.2 完成后，相关文档状态与实现一致；若选择不加后台任务，则同步修正文档措辞而非保留「已完成」的失实描述。

- [x] **T7.6 收敛 edit_markdown 的自动注入开销**（对应 update.md P1-5，**联网实测新增**）—— **已实施并量化**
  - 现状：`EDIT_MARKDOWN_LOCAL=true` 时，即使客户端未声明任何工具，服务端也会追加 `EDIT_MARKDOWN_TOOL`，连带注入 `format_tool_call_emphasis`(240) + `format_tools_instruction`(481) + `edit_markdown_spec`(153)，每个新桶请求恒定多付 **~970 tokens**（已由线上 `usage` 与本地复算双向确认）。副作用：模型可能发出客户端从未声明的 `edit_markdown` 调用（`finish_reason=tool_calls`）；`usage` 不再反映客户端自己的输入。
  - 方案（推荐 a）：a. 仅在客户端已声明工具时才注入内置工具；b. 新增 `EDIT_MARKDOWN_ALWAYS_REGISTER` 开关（默认 false）保留现有行为；c. 至少把 `format_tool_call_emphasis` 限制为「本轮确实有工具且是 seed 轮」。
  - 已实施：新增 `toolcalls.should_register_edit_markdown()`（默认只在客户端已声明工具时注入），`EDIT_MARKDOWN_ALWAYS_REGISTER=true` 作为逃生口。
  - 验收（已实测）：同一请求从 **970 → 93 tokens，省下 877**；`test_toolcalls.py` 新增 5 用例覆盖矩阵（无工具 / 有工具 / 客户端已自备 / 本地执行关闭 / 逃生口）。

---

## 阶段 8：工程质量与可维护性（P2）

- [x] **T8.1 结构化日志**（P2-1）—— **已实施**：新增 `gemini_web/logging_setup.py`（包级 handler、幂等、`propagate=False`），41 处 `print` 全部改为 `logging`（含 `traceback.print_exc()` → `exc_info=True`），级别由 `GEMINI_DEBUG` 决定（true → DEBUG，否则 INFO）。
  - 验收：`tests/test_logging.py`（级别映射 / 幂等 / 输出格式 / **全包无裸 `print` 的结构性守护**）。
- [x] **T8.2 异常可观测**（P2-2）—— **已实施**：`session_store` / `tasks` / `chat_io.prune_output_dir` 的关键副作用失败改为 `logging.warning`（不再 `except: pass`）。
- [x] **T8.3 清理死代码**（P2-3）—— **已实施**：删除 `responses.py` 无调用的 `_resolve_session()`（`markdown_io.generate_edit` 属预留，保留）。
- [x] **T8.4 依赖固定**（P2-4）—— **已实施**：新增 `pyproject.toml`（`requires-python >=3.10`，运行时依赖全部带区间）+ dev extras（`pytest` / `httpx2` / `openai`）；`requirements.txt` 同步为带区间的运行时列表。
  - 验收：`tests/test_config_drift.py` 新增依赖防漂移（requirements ↔ pyproject 同名同集、关键依赖必须有版本约束、`openai` 只能在 dev）。
- [x] **T8.5 最小 CI**（P2-5）—— **已实施**：`.github/workflows/ci.yml`（push/PR，Python 3.10 + 3.13 矩阵，装依赖后 `python -m pytest -q`）。
  - 不装浏览器、不设 `GEMINI_E2E`，因此 E2E 全部 skip：CI 只跑不联网套件（全部用假 page / driver / TestClient）。
- [x] **T8.6 测试框架统一**（P2-6）—— **已实施（走“明确并存”路线）**：标准入口统一为 `.venv/bin/python -m pytest -q`（`pyproject.toml` 设 `testpaths = ["tests"]`），`design.md` §7 写明 unittest / pytest 并存与为何不再把 `unittest discover` 当标准入口（收集不到 pytest 风格文件的 parametrize 用例）。
- [x] **T8.7 `context_window` 一致化**（P2-7）—— **已实施（走“透出 + 统一数值”路线）**：`SUPPORTED_MODELS` 删掉硬编码的 `65536`，`ModelCard.context_window` 由 `/v1/models` 按 `SESSION_MAX_TOKENS`（会话轮转预算）填充；README 的 Pi 示例注明该值应以端点返回为准。
  - 验收：`tests/test_routes_chat.py` 断言端点透出且随 `SESSION_MAX_TOKENS` 变化；`tests/test_models.py` 反向断言模型表里不再有硬编码值。
- [x] **T8.8 `estimate_tokens` 注释**（P2-8）—— **已实施**：docstring 明确「量级估算 / order-of-magnitude，不是精确值」，并列出它同时充当计价单位的三个地方（`usage` / `SESSION_MAX_TOKENS` 轮转预算 / `/v1/models` 的 `context_window`），要求三处同源。
- [x] **T8.9 `edit_markdown` 路径约束**（P2-9）—— **已实施**：新增 `config.EDIT_MARKDOWN_ROOT`（默认项目根，留空回落）与 `toolcalls._resolve_edit_path()`；路径先 `resolve()` 再校验必须落在根内，`../` 逃逸、根外绝对路径、软链接跳出一律拒绝（读写 / 备份 / 落盘用同一份解析结果）。
  - 附带修复：目标是目录或权限不足时返回结构化 `读取失败：...`，不再把 `IsADirectoryError` 抛给上层。
  - 验收：`tests/test_toolcalls.py::EditMarkdownPathGuardTests`（6 例：根内允许、`..` 与根外绝对路径拒绝、`write=true` 同关卡、根内写入真落盘 + 备份、目录目标不抛错）；README 安全节已说明。
- [x] **T8.10 可选 `BRIDGE_TOKEN`**（P2-10）—— **已实施**：`config.BRIDGE_TOKEN`（默认空 = 不校验）+ FastAPI 依赖 `_require_bridge_token`，用 `dependencies=[Depends(...)]` 只挂在两个生成端点上；`/healthz`、`/v1/models` 保持开放。
  - 验收：`tests/test_auth.py::BridgeTokenTests`（默认关闭可直接访问、缺头 / 错 token 401、正确 token 200、探活与模型发现不被挡）。

---

## 阶段 9：测试补全

- [x] **T9.1 流式节点替换回归**（对应 T6.1）—— 已实施：chat 4 例 + Responses 4 例。
- [x] **T9.2 多桶并发落盘**（对应 T6.2）—— 已实施：8 桶 × 20 轮，每桶 `turns` 完整。
- [x] **T9.3 新增 `tests/test_routes_responses.py`**（对应 update.md §6）—— **已实施**（7 例）：`ENABLE_RESPONSES_API=false` → 404、空 `input` → 400（不是 500）、`input` 数组形态、浏览器未就绪 → 503、非流式结构（`object` / `status` / `output[].content[].text` / `usage`）、流式命名事件序列。
- [x] **T9.4 `tasks.py` 表驱动单测**（对应 update.md §6）—— **已实施**：新增 `tests/test_tasks.py`（表驱动 12 + 11 例，覆盖 `environment_context` / `skills_instructions` / `permissions instructions` / `collaboration_mode` / `env` 等注入块与标题生成 / 交接 JSON / 字数约束类元提示），并覆盖 `_goal_from_messages` 选取与快照往返（goal 只写一次、跨命名空间隔离、recent 截断、开关关闭不落盘）。
  - 有意锁定的边界：「标签 + 同行正文」（如 `<environment_context>请修复…`）**不**视为包装块，避免整条真实请求被丢掉。
- [x] **T9.5 鉴权分支测试**（对应 update.md §6）—— **已实施**：新增 `tests/test_auth.py`（10 例）——`RESET_TOKEN` 缺失 / 错误头 403、正确头 200 且真的作用到指定桶、未设置时不校验；`/debug/dom` 在 `GEMINI_DEBUG=false` 时 404、调试开启但浏览器未就绪时 503；`BRIDGE_TOKEN` 的两个端点 401/200 分支（T8.10）。
- [x] **T9.6 `_prune_output_dir` 保留策略单测**（对应 T7.2）—— 已实施：`tests/test_output_prune.py`（保留策略 / 启动清理 / 周期开关 / 缺失目录不致命）。
- [x] **T9.7 E2E 脚手架定向回归测试**（本轮新增，对应 update.md 第 11 节）
  - 交付：`tests/test_e2e_harness.py`（9 用例，不联网、不起浏览器），守护 `_resolve_port`（拒绝 `0`/越界/非数字/非整数浮点/bool）、`BridgeServer.ensure_started`（端口非法快速失败、`503+init_error` 快速失败并清理、外部服务复用不重复拉起）、`DirectGeminiClient.start`（必须导航到 Gemini 入口、落点非 Gemini 或就绪失败时必须关闭浏览器）。
  - 验证：`pytest tests/test_e2e_harness.py -q` → 9 passed；全量 `pytest -q` → 206 passed, 19 skipped。
  - 说明：本轮还修掉了「失败不关浏览器 → 桌面残留 about:blank 空白窗口」与「失败不杀自己拉起的 uvicorn → 孤儿进程」两处真实缺陷。

---

## 阶段 10：文档同步

- [x] **T10.1 `design.md` 同步**：§3 文件清单、§4 默认值/配置优先级、§7 测试框架说明 —— 已实施。
- [x] **T10.2 `README.md` 同步**：配置表补全 + `SEND_BUTTON_SELECTORS` + `GEMINI_RETRIES` 措辞 —— 已实施。
- [x] **T10.3 复核历史分析文档状态** —— **已实施**：在 `doc/update_codex.md`（新增 §8.1）与 `doc/update_pi.md`（新增 §4）追加「后续进展」对照表，逐条给出「已完成 / 仍未做」及对应任务卡；纠正两处过期快照（测试入口与用例数、`_prune_output_dir` 只在落盘时触发）。历史结论保留原文，不改写历史。
- [x] **T10.4 Markdown 编辑设计稿归档**：原 `doc/update.md` 的设计稿已迁移至 `doc/markdown_io_design.md`，并修正 `gemini_web/markdown_io.py` 与 `doc/e2e_test_design.md` 的引用。

---

## 推荐执行顺序

| 批次 | 任务 | 理由 |
| --- | --- | --- |
| Phase 1 | T6.1 | ✅ 已完成（唯一会静默损坏客户端数据的缺陷） |
| Phase 2 | T6.2 | ✅ 已完成（原“竞态”判断已实测证伪，改为修非原子写盘） |
| Phase 3 | T7.1 / T7.2 / T7.6 | ✅ 已完成；T7.6 实测每请求省 877 tokens |
| Phase 4 | T7.3 / T7.4 / T7.5 | ✅ 已完成（防漂移改为 config ↔ .env.example 双向校验） |
| Phase 5 | T8.1 / T8.4 / T8.5 / T8.6 / T8.7 / T8.8 | ✅ 已完成（日志改造 / 依赖固定 / CI / 框架统一 / context_window / 注释） |
| Phase 6 | T8.9 / T8.10 | ✅ 已完成（安全加固：edit_markdown 路径约束、可选 BRIDGE_TOKEN） |
| 随行 | T9.3 / T9.4 / T9.5 / T10.3 | ✅ 已完成（responses 路由级测试 / tasks 表驱动单测 / 鉴权分支测试 / 历史文档复核） |

> 阶段 6–10 至此全部完成。后续若要继续推进，建议方向见 `doc/update.md` 的“仍未做”与 §8.1 / §4
> 对照表中明确标注的「仍未做」两项（`ReplyWatcher` 重构、真实环境专属交付物）。

---

## 附录 A：历史已完成基线（v4，保留）

> 阶段 0–5 的原始任务卡措辞见 `doc/tasks.md` 的 v4 版本记录；下方为完成状态，`T4.4` 已按 T7.2 修正为「未完全达成」。

- 阶段 0 基础与配置：T0.1 仓库骨架 `[x]`、T0.2 `config.py` `[x]`、T0.3 配置防漂移与 `.env.example` 对齐 `[x]`（覆盖范围待 T7.3 扩展）。
- 阶段 1 核心逻辑：T1.1 `models.py` `[x]`、T1.2 `prompting.py` `[x]`、T1.3 `toolcalls.py` `[x]`、T1.4 `streaming.py` SSE `[x]`、T1.5 `markdown_io.py` `[x]`。
- 阶段 2 驱动与 DOM：T2.1 `driver.py` `[x]`、T2.2 DOM 事件派发发送 `[x]`、T2.3 代码块提取保真 `[x]`。
- 阶段 3 服务接口：T3.1 `/v1/models` + `/v1/chat/completions` `[x]`、T3.2 `/v1/responses` `[x]`、T3.3 路由与监控 `[x]`。
- 阶段 4 Pi / Coding Agent 增强：T4.1 Responses ID/索引一致性 `[x]`、T4.2 Tool Call 收尾校验 `[x]`、T4.3 会话缓存有界 LRU `[x]`、**T4.4 `output/` 自动清理 `[~]`（仅落盘时触发，未落到启动/后台，见 T7.2）**、T4.5 任务快照过滤 `[x]`、T4.6 会话上限自动轮转 `[x]`。
- 阶段 5 文档与规范：T5.1 `design.md` 同步 `[x]`（同步结果已随 T7.3 再次漂移，待重做）、T5.2 `/v1/responses` 契约样例 `[x]`。
