# GeminiBridge 任务分解（v5）

依据 `doc/update.md`（2026-10-06 全项目复审）重新梳理。旧版 v4 的阶段 0–5 已作为「历史已完成基线」压缩保留在附录 A，本文新增任务全部对应 update.md 中的 P0 / P1 / P2 条目。

状态标记：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成（实现 + 验收测试齐备）。

核对时间：2026-10-07（实施轮 + 随行轮 + 载体改造轮，阶段 6–11 已全部落地）；基线：`.venv/bin/python -m pytest -q` → **335 passed, 18 skipped, 31 subtests passed**（随行轮 238 → 279；E2E 重组轮 284；载体改造轮 +19 → 303；fill 重试轮 +5 → 308；长度控制核查轮 +10 → 318；长度控制修复轮 +7 → 325；分块写入轮 +4 → 329；ChatGPTBridge 方案合并轮 +4 → 333；光标与整段 fill 回退轮 +2 → 335）。

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

- [x] **T9.8 E2E 用例集设计优化 + 定向联网验证**（本轮新增）
  - **优化**：删除 **A2**（与 A1 同型、无独立信息量）；A5（语言）/B3（schema）/B5（落盘）/A6（注入块泄漏）等断言并入 A1 **同一次请求**；C1/C2 共用直连基线（`tool_call_baseline` 进程内缓存——单跑 C2 也有基线，一起跑不重复付一次上游调用）。
  - **新增护栏**：A1 的 **P1-5 token 预算断言**（`usage.prompt_tokens < 500`；`EDIT_MARKDOWN_ALWAYS_REGISTER=true` 时自动让步）——此前 P1-5 的收益（970 → 93 tokens）只有本地复算、无回归护栏；B2 的 `context_window == SESSION_MAX_TOKENS` 契约断言（T8.7 的对外承诺）。
  - **设计依据**：删除/合并/新增的理由与「保留用例的不可删理由」写入 `doc/e2e_test_design.md` §3.0；「只跑改动过的用例」的定向运行口径写入 §4.1。
  - **联网验证（2026-10-06）**：复用 8001 上正在运行的服务（不另起实例、不抢 profile），`GEMINI_E2E=1` 跑**本次改动过的 4 个用例** → **A1 / B2 / C1 / C2 全部 ok，4 tests in 115s**（a1 direct=29.2s bridge=17.5s；c1 direct=27.5s bridge=17.8s；c2 复用缓存的直连基线，只付一次 bridge=17.8s）。
  - **同时覆盖上一 commit 的脚手架改动**（bc51489）：端口护栏实测生效（日志：“端口 0 非法（环境变量 PORT 会覆盖 .env），回退到 8001”）、服务复用（不拉起第二个实例）、直连落点校验。
  - **未联网验证（按定向口径）**：A4 / B1 / B4 / B6 / D1–D3 / B8 / `test_markdown_io_e2e` 本轮**未**联网验证（其代码未改动），状态保持「未验证」，不得当成通过；全量套件（15–20 分钟）留待发版前或大改后跑。

---

## 阶段 10：文档同步

- [x] **T10.1 `design.md` 同步**：§3 文件清单、§4 默认值/配置优先级、§7 测试框架说明 —— 已实施。
- [x] **T10.2 `README.md` 同步**：配置表补全 + `SEND_BUTTON_SELECTORS` + `GEMINI_RETRIES` 措辞 —— 已实施。
- [x] **T10.3 复核历史分析文档状态** —— **已实施**：在 `doc/update_codex.md`（新增 §8.1）与 `doc/update_pi.md`（新增 §4）追加「后续进展」对照表，逐条给出「已完成 / 仍未做」及对应任务卡；纠正两处过期快照（测试入口与用例数、`_prune_output_dir` 只在落盘时触发）。历史结论保留原文，不改写历史。
- [x] **T10.4 Markdown 编辑设计稿归档**：原 `doc/update.md` 的设计稿已迁移至 `doc/markdown_io_design.md`，并修正 `gemini_web/markdown_io.py` 与 `doc/e2e_test_design.md` 的引用。

---

## 阶段 11：工具调用载体改造（2026-10-07，本轮新增）

- [x] **T11.1 载体从「纯文本 `TOOL_CALL:{}` 行」改为「标记行 + ```tool_call 代码围栏」**（参照姊妹项目 ChatGPTBridge 的 `doc/code_block_fence.md`）—— **已实施**
  - 问题：旧载体把载荷全塞进一行纯文本，网页版按 markdown **段落**渲染，取回 DOM 时 `\"` 被消费 → 严格 `json.loads` 失败，只能靠修复启发式“猜”。
  - **本条改动前的真机证伪（重要）**：照搬“只用 ```tool_call 围栏”在本桥**不可用**——Gemini 把代码块渲染成 code-snippet 组件，围栏与 info string 都**不进** `innerText`（DOM 里只剩 UI 标题 `Code snippet`），真机实测解析 **0 条**。
  - 采用方案：**混合载体**——`TOOL_CALL:` 标记行只负责“可识别”（无载荷），```tool_call 围栏只负责“逐字节保真”（承载载荷），两者互补。
  - 实现：注入侧 4 处（`format_tools_instruction` / `format_tool_call_emphasis` / `edit_markdown_spec` / `markdown_io._build_edit_prompt`）；解析侧把围栏提到分支 0、标记行分支降为分支 1（标注为 Gemini 渲染后的主路径，**无新增宽容分支**）；新增诊断 warning（有标记却 0 条调用时不再静默）；`_TOOL_CALL_FENCE_RE` 收紧 info string 行匹配。
  - 兼容：历史一体化载体 `TOOL_CALL: {json}`、裸 `tool_call` 标签、DSML / ```json 兜底**全部保留**。
  - 验收（本地）：`pytest -q` → **303 passed, 18 skipped, 31 subtests passed**（284 → 303，新增 19 个用例）；新增 `FencedCarrierTests`（含 3 个真机 DOM 固化样本）与 2 个 Markdown 编辑链载体用例；**区分力实验**：换回旧 `toolcalls.py` → `test_toolcalls.py` + `test_markdown_io.py` **7 failed / 117 passed**（恢复后 124 passed）。
  - 验收（联网）：新增 `tests/e2e/probe_carrier_fidelity.py`（4 臂传输层回声，**不带 tools** 以避开桥注入污染）→ line 臂 `\"`=0、严格 JSON 失败；fence 臂严格 JSON OK 但**解析 0 条**；hybrid 臂严格 JSON OK + 命令逐字节一致 + 解析 1 条。定向回归 `TestCToolParity` → **2 tests OK（74.3s；措辞微调后复跑 72.1s 同样 OK）**；A1 / B2 同批通过（另见本轮首次运行的 SKIP 说明：属上游波动，已用措辞 A/B 排除回归）。
  - 交付物：`doc/code_block_fence.md`（机制、真假附件对照、兼容矩阵、残留与后续方向）+ 固化样本用例 + 探测脚本。
  - 未联网验证（按定向口径）：A4 / B1 / B4 / B6 / D1–D3 / B8 / `test_markdown_io_e2e` 本轮未跑（代码未改动）。

- [x] **T11.2 修复「写入输入框超时」导致整轮请求失败**（2026-10-07，真实故障）—— **已实施**
  - 现象（用户侧真实报错）：`ElementHandle.fill: Timeout 30000ms exceeded`，连续 4 次（04:35:06 / 04:35:54 / 04:36:39 / 04:37:27），每次白等 30s；客户端那段窗口期的工具结果请求全丢。
  - 诊断：Playwright `fill` 默认超时 30s，而旧实现「只定位一次句柄 + 一次 fill、失败就整轮报错」；网页版重挂载 composer 后旧句柄会一直卡在“等它可见/可编辑”上。已排除的假设：**不是** prompt 过长——本次失败 prompt 仅 21.9KB，而 `PROMPT_MAX_CHARS=100000` 根本没有触发截断。
  - 方案（参照姊妹项目 ChatGPTBridge 的 `FILL_TIMEOUT_MS` / `FILL_RETRIES`）：新增 `FILL_TIMEOUT_MS`（默认 10000）与 `FILL_RETRIES`（默认 3）；`_fill_prompt` **每次尝试都重新定位输入框**（拿到重挂载后的新节点），失败按 `RETRY_BACKOFF_S` 退避再试；仍失败则报可行动的错误（不再只丢 Playwright 原文），并附**输入框状态诊断**（tag / contenteditable / aria-disabled / isConnected / display / 尺寸 / activeElement）。
  - 顺带夹带：参照同一份 `prompting.py` 的 `_seed_messages`，给 `SEED_SYSTEM_MAX_CHARS=0`（“不限制”）补上“单条上限与剩余总预算取小”——否则一条 10 万字系统提示会原样进 prompt。
  - 验收：`pytest -q` → **308 passed, 18 skipped, 31 subtests**；新增 `FillRetryTests`（4 例：重定位后成功 / 每次都用配置的超时 / 连续失败后报错且只试 FILL_RETRIES 次 / 失败日志含诊断）与 `test_giant_system_message_is_clamped_even_when_per_message_limit_is_off`；**区分力实验**：换回改动前的 `chat_io.py` → 4 failed。

- [x] **T11.3 核查「prompt 长度控制没生效」：把长度链改成可观测（2026-10-07，用户报告）** —— **已实施**
  - 报告：prompt 长度控制没生效，太长的 prompt 让 Gemini 失去响应；要求检查为什么改动无效。
  - **先证伪**：控制没有被绕过（全仓唯一 `fill` 点在 `chat_io._send_chat_locked`，`_clamp_prompt` 必定执行；四个旋钮都真的被读到）。失效的是**计量口径 + 可观测性**：单条预算/原始文本预算管不到拼装后的成品、多条工具结果无总量预算、`role="user"` 无上限、而唯一的兜底（`PROMPT_MAX_CHARS` 头尾截断）**一行日志都不留**。
  - **真机实测（新增 `tests/e2e/probe_prompt_limit.py`）**：20K / 60K / 100K 字符单条 prompt、5×20KB 工具结果（100,080 字符，被兜底截到 100,030）、同一会话连发 3 轮 ×60KB —— 全部 200 OK 且答对（17.6–24.9s）。即**在桥能发出的范围内没能复现「长 prompt → 失去响应」**；`PROMPT_MAX_CHARS` 是主动预算，不是 composer 物理上限（旧 docstring 的“输入框字符上限”说法无依据，已改）。
  - 实现（纯可观测性，不改裁剪行为）：`_clamp_prompt` 截断记 WARNING；每次发送记 `[发送] … prompt=N 字符`；两条超时错误带 prompt 长度；`build_prompt` 拼装即超预算时记 WARNING（点名历史/工具结果/工具说明各占多少）；播种截断记 INFO。
  - 验收：`pytest -q` → **318 passed, 18 skipped, 31 subtests**（新增 10 例：`PromptBudgetObservationTests` 4 / `SeedBudgetAccountingTests` 2 / `PromptLengthGuardTests` 3 / 发送长度 1）。真机结果落 `output/prompt_limit_probe.txt`。
  - 已由 T11.4 落地：预算改为「只留开头 + 标注截短」（不整块丢掉），`TOOL_RESULT_MAX_CHARS` 20000 → 50000。仍未动：`SESSION_MAX_TOKENS=1000000` 是按模型容量而非网页版响应性测出来的。详见 `doc/update.md` §16 / §17。
  - 真机冒烟：重启服务后一发短 prompt → HTTP 200 / 17.8s / `bridge fill ok`（快乐路径未触发重试，符合预期）。
  - **未能复现验证**：原故障依赖“composer 正好处于失效句柄状态”，本轮无稳定复现手法；重试路径由假 page 单测覆盖，真机上只验证了不影响正常发送。

- [x] **T11.4 修复「文字进了输入框但消息没被提交」+ 成品预算真正落地**（2026-10-07，用户报告）—— **已实施**
  - 现象（用户侧观察）：prompt 已经在网页输入框里，但**发送按钮没有被点击**（或点了无响应），客户端收不到任何回应。
  - 真因：`chat_io._dispatch_enter` 的 JS 只要把 `KeyboardEvent` 派发出去就 `return true`，而旧 `_submit_prompt` 据此**直接 return**——`_click_send_button` 的兜底路径**从未被执行过**；网页没接住合成按键时就是「文字在、消息没发、页面不产生回复」，客户端干等到超时。
  - 修法：`_submit_prompt` 阶梯改为 **Enter → 发送按钮 → Enter**，每次尝试后用 `_prompt_submitted` 验证（**输入框已清空**或页面进入「生成中」；读不到则 `None` = 无法判断、不据此报错，也不空等）；`_click_send_button` 先原生 `click()`（真实鼠标事件）再退化为 DOM click；三次都失败抛可行动 `RuntimeError`（含输入框剩余字符数与按钮状态）；新增配置 `SUBMIT_VERIFY_MS`（默认 3000）。
  - 预算落地（用户定策略：超长结果**不是整块丢掉**，而是只留前边一段 + 在 prompt 里说明已截短）：`TOOL_RESULT_MAX_CHARS` **20000 → 50000**；新增 `prompting._fit_segments_to_budget`——成品超过 `PROMPT_MAX_CHARS` 时，每轮挑**当前最长**的工具结果砍半（同长时先压最旧的），直到进预算或到 `MIN_TOOL_RESULT_KEEP_CHARS=2000` 下限，每段都带「结果太长已被截短」标注。
  - 实测（离线，50K/100K）：1 条 50KB → 50,032 字符；2 条 50KB（100,066）→ 75,069；3 条 50KB（150,100）→ 75,109；6 条 50KB（300,202）→ 87,720；1 条 80KB → 50,035；每段开头都保住且都带截断标注。
  - 验收：`pytest -q` → **325 passed, 18 skipped, 31 subtests**（新增 `SubmitVerificationTests` 4 例 + 预算落地用例）；**区分力实验**：换回旧 `chat_io.py` → `SubmitVerificationTests` + `PromptLengthGuardTests` **4 failed**，恢复后 18 passed。
  - **未真机验证**：曾以隔离 profile 副本 + 独立端口 8011 起第二实例做端到端验证，被用户中止（已清理，未触碰 8001 实例）。提交阶梯与压缩的真实网页行为目前只有假 page 单测 + 离线实测覆盖；运行中的实例需**重启**才会带上本轮修复与新日志。

- [x] **T11.5 修复「超长工具结果把网页输入框卡死」（分块写入）**（2026-10-07，用户真机报告）
  - 现象：客户端 `find` 的超长结果导致网页输入框卡死，桥报「写入输入框连续失败 3 次（单次超时 10000ms）：命中的元素始终不处于「可见 / 可编辑」状态」。
  - 原因：一次性 `fill(整段)` 把几万字符排成网页主线程上的长任务（React 重渲染 + 富文本编辑器同步），期间 Playwright 探测不到元素状态 → 超时；页面本身也卡住。T11.2 的重试只是把同一件事重试三遍。
  - 修法：`chat_io._insert_prompt_in_chunks` **分块写入**——新增 `FILL_CHUNK_CHARS`（默认 4000）；每块前重读输入框文本只补缺失段（幂等、可续写、不重复）；首选 `execCommand('insertText')`，无效退回 CDP `keyboard.insert_text`；每次插入用 `asyncio.wait_for` 受 `FILL_TIMEOUT_MS` 约束；句柄失效则重新定位续写；“已写入字符数不前进”连续 `FILL_RETRIES` 次即报错（带进度）；读不到输入框文本的页面退回整段 `fill`。
  - 验收：`pytest -q` → **329 passed, 18 skipped, 31 subtests**（新增 `ChunkedInsertTests` 4 例）；**区分力实验**：换回分块之前的 `chat_io.py` → 3 failed（报的正是用户看到的 `waiting for element to be visible, enabled and editable`），恢复后 4 passed。
  - **未验证**：真实网页端未跑（需重启服务；本轮不再起第二实例）。`FILL_CHUNK_CHARS=4000` 的手感待真机确认，偶发卡顿可先调到 2000。

- [x] **T11.6 对比姊妹项目 ChatGPTBridge 的输入框方案并合并更优实现**（2026-10-07）
  - 对比结论：**他们的插入/聚焦/清空/提交原语更强**（`keyboard.insert_text` 走 CDP 真实编辑管线、真实 click 聚焦、循环校验清空草稿、**真实键盘 Enter 优先**——合成事件 `isTrusted=false` 常被受控编辑器忽略）；**本项目的分块与双重校验更强**（他们不分块，超长文本仍会卡；也无提交后验证）。
  - 合并：`_focus_composer`（真实 click 优先）/ `_insert_chunk`（键盘优先，无进展时交换原语）/ `_clear_composer`（多手法 + 循环校验 + 残留 warning）/ `_keyboard_enter`（真实键盘 Enter）/ `_submit_prompt` 阶梯改为「真实键盘 Enter → 发送按钮 → 合成 Enter」，每级后都验证。未采纳 `_shrink_seed_if_repeated_cap`（属“会话到顶死循环”，与本问题无关）。
  - 验收：`pytest -q` → **333 passed, 18 skipped, 31 subtests**（新增 4 例守护）；**区分力实验**：换回合并前 `chat_io.py` → 4 failed，恢复后 26 passed。
  - **未验证**：真机（需重启服务）。详见 `doc/update.md` §19。

- [x] **T11.7 修复「分块插入零字符」：插入前必须显式放置光标 + 零进展退回整段 `fill()`**（2026-10-07）
  - **真机现象**（用户贴出日志）：prompt 仅 9633 字符，三次尝试全部报 `分块写入无进展：已写入 0/9633 字符`；输入框诊断显示 `active: DIV(self)`、可见、可编辑、`connected`。日志里**没有**任何 `keyboard.insert_text 失败` / `execCommand 插入失败` 的 DEBUG 行（而 DEBUG 是开着的）——说明原语没报错，却一个字也没写。
  - **根因（已从 Playwright 源码证实）**：`page.keyboard.insert_text()` 发的是 CDP `Input.insertText`，`document.execCommand('insertText')` 同理，**都只在「当前选区」处插入**。输入框往往已经是 `document.activeElement`，此时 `el.focus()` 是空操作，页面里没有任何落在编辑器内的选区 → 两种插入退化成**静默空操作**（不抛错、0 字符）。对照 Playwright 自己的 `fill()`：它对 contenteditable 先 `selectText(element)`（`focus` + `range.selectNodeContents` + `addRange` 建立选区），**再**调 `keyboard.insertText`——这正是它一直可用的原因。
  - 修法：新增 `_SET_CARET_JS` / `_set_caret`（focus + `selectNodeContents` + **折叠到末尾**，与 `fill()` 同源但只追加不覆盖），`_insert_chunk` 每次插入前调用；插入原语返回**真正生效的原语名**，零进展时记 warning（含 `caret_in_composer` 诊断）并抛 `ChunkedInsertUnavailable`，由 `_fill_prompt` **退回整段 `fill()`**（本页面上被证实的可用原语），而不是直接失败。
  - `_COMPOSER_DIAG_JS` 增记 `caret_in_composer` / `child_nodes`：下次真机失败可直接看出“选区是否在编辑器内”。
  - 验收：`pytest -q` → **335 passed, 18 skipped, 31 subtests**（新增 2 例守护：`test_caret_is_placed_before_writing` / `test_silent_noop_inserts_fall_back_to_whole_fill`）。
  - **区分力实验**：去掉 `_set_caret` 调用 → `test_caret_is_placed_before_writing` failed；去掉整段 fill 回退 → `test_silent_noop_inserts_fall_back_to_whole_fill` failed；两处恢复后 28 passed。
  - **未验证**：真机（需重启服务）。详见 `doc/update.md` §20。

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
| Phase 7 | T11.1 / T11.2 / T11.3 / T11.4 | ✅ 已完成（工具调用载体改造：标记行 + 代码围栏；fill 超时/重试修复；长度链可观测性；提交链修复 + 成品预算落地） |

> 阶段 6–11 至此全部完成。后续若要继续推进，建议方向见 `doc/update.md` 的“仍未做”与 §8.1 / §4
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
