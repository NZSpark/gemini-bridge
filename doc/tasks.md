# GeminiBridge 任务分解（v2）

依据 `doc/update.md`（v2 修订版）重新拆分。v1 的阶段 0–4 保留原编号与历史状态，阶段 5–8 为本次分析新增；同一层内可并行，每个任务给出产出与验收。

状态标记：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成（实现 + 验收测试齐备，或经实测核对）。
核对时间：2026-10-04；基线：`python -m unittest discover -s tests -t .` → **123 用例全部通过**。

---

## 阶段 0：骨架与配置

- [x] **T0.1 仓库骨架**：`gemini_web/` 包、`tests/`、`output/`、`__init__.py`、`requirements.txt`。
  - 验收：`python -c "import gemini_web"` 不报错。✅ 实测
- [x] **T0.2 `config.py`**：全键 `GEMINI_*`，运行时 `config.<NAME>` 取属性，`||` 回退链解析。
  - 验收：`test_config.py` 覆盖默认值、`.env` 覆盖、回退链、布尔/数值容错。✅ 18 个用例在库
- [~] **T0.3 配置一致性：`.env.example` + `design.md` 默认值同步**：`.env` 已校正（33 键，无 `DEEPSEEK_`/`ds-` 残留）；缺 `.env.example`；`design.md` §4 有 8 项默认值过期（README 无问题，实测与 `config.py` 一致）。
  - 产出：`.env.example`（键集合与 `config.py` 严格一致，可脚本生成）、`design.md` §4 改为引用 `config.py` 不再手抄、`config.py` 文档串引用的文件真实存在。
  - 验收：`.env.example` 与 `config.py` 一一对应；防漂移测试落地（→ T6.5）；`design.md` 不再出现具体数字。
  - 关联：update.md §3.1。

## 阶段 1：纯逻辑模块

- [x] **T1.1 `models.py`**：OpenAI 兼容 Pydantic 模型，`extra="allow"` 宽松校验。
  - 验收：`test_models.py` 覆盖 chat / chunk / tool_call / responses / error 结构。✅ 18 个用例
- [~] **T1.2 `prompting.py`**：messages → 输入框文本，合并 system/developer、拼接历史、附当前输入；`estimate_tokens` 估算。
  - 现状：实现完成；**无独立 `test_prompting.py`**，多角色/多轮/无 system/空历史的主拼接路径仅被 `test_seed_prompt.py` 间接覆盖。
  - 验收：`test_prompting.py` 覆盖上述四类 + 播种截断（→ T6.3）。
- [x] **T1.3 `toolcalls.py`**：工具注入提示词 + 输出解析；失败按普通文本处理。
  - 验收：`test_toolcalls.py` 覆盖单/多调用、参数非法、无工具、注入模板稳定。✅ 40 个用例；DSML 分支缺口 → T6.4；契约固化 → T5.2。
- [x] **T1.4 `streaming.py`**：SSE 编码，首 chunk 带 `role`，末 chunk 带 `finish_reason`，`data: [DONE]` 收尾。
  - 验收：`test_streaming.py` 逐块消费 + 工具流。✅ 9 个用例
- [~] **T1.5 `tasks.py`**：会话桶任务快照，轮转播种优先注入任务目标。
  - 现状：实现完成；**`test_tasks.py` 缺失**。
  - 验收：`test_tasks.py` 覆盖写入/读取/截断/关闭开关/goal 自愈（→ T6.3）。

## 阶段 2：会话与驱动

- [~] **T2.1 `driver.py` 骨架**：`launch_persistent_context` 打开 `WEBSITE`，等 `READY_SELECTOR`，`NEW_CHAT_SELECTOR` 开干净对话。
  - 现状：实现完成（`_wait_ready` / `_open_new_chat` 在库）；"假 page 下选择器回退链命中并发送"无测试。
  - 验收：选择器回退与发送的假 page 测试（→ T6.6）。
- [x] **T2.2 回复抓取与结束判定**：`RESPONSE_SELECTORS` 抓取、`POLL_INTERVAL_S` 轮询、双阈值判结束。
  - 验收：结束判定测试覆盖正常收尾、长停顿不误判、超时兜底。✅ `test_end_detection.py` 6 用例（含生成中/停止按钮/节点替换/pending tokens）
- [~] **T2.3 会话生命周期**：每桶首次与轮转均新开对话并播种；到顶双判；`tasks.py` 快照防丢目标。
  - 现状：实现完成（`_start_new_session` / `_remember_session` / `_session_over_budget` 等）；**`test_sessions.py` 缺失**。
  - 验收：`test_sessions.py` 覆盖创建/播种/到顶/轮转/重试退避/回收（→ T6.1）。
- [~] **T2.4 分桶与锁**：`X-Gemini-Session` 优先分桶，LRU 回收，锁超时 503 `upstream_busy`。
  - 现状：实现完成；验收同样卡在 `test_sessions.py`（→ T6.1）。另注意 update.md §2.4：状态 dict 需有界化（→ T7.1）。

## 阶段 3：API 层

- [~] **T3.1 `responses.py`**：Responses 请求 → 内部 chat → 命名 SSE 事件；keep-alive；工具映射。
  - 现状：实现完成（556 行）；**`test_responses.py` 缺失**；且流式 id / `output_index` 存在 4 处不一致（update.md §2.1）。
  - 验收：`test_responses.py` 覆盖文本流、工具流、保活、错误结构 + **id 全程一致性**（→ T5.1）。
- [~] **T3.2 `server.py` 路由**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/healthz`、`/debug/dom`、`/session/reset`。
  - 现状：路由齐全，门控生效；**全仓库无路由级测试**。
  - 验收：`test_routes_chat.py` / `test_routes_responses.py` 用假 driver 覆盖非流式、流式、工具、错误码、`/healthz` cluster 字段（→ T6.2）。
- [~] **T3.3 `gemini_api_server.py` 入口**：薄封装，重导出公开名字，读 `HOST` / `PORT`。
  - 现状：实现存在；启动冒烟（`/healthz` 200）并入 T6.2 的 TestClient 冒烟。
- [~] **T3.4 代码落盘**：按语言写 `OUTPUT_DIR`，无代码块存 `.md`。
  - 现状：实现存在；无测试；且扩展名"靠内容猜"需改（update.md §4.7）。
  - 验收：假 DOM 提取多语言代码块正确命名 + 无语言标注默认 `txt` 不猜（→ T6.6）。

## 阶段 4：联调与文档（遗留）

- [ ] **T4.1 `client_test.py`**：用 `openai` SDK 打本地服务，覆盖 chat 与（可选）responses。
  - 验收：服务启动后脚本跑通并打印回复。
- [ ] **T4.2 首次真实登录**：`HEADLESS=false` 手动登录，登录态落 `user_data/`；真实一轮对话可抓取。
  - 验收：真实请求返回非空文本，结束判定在 `GEMINI_TIMEOUT` 内收敛。
- [~] **T4.3 选择器校正**：按真实 DOM 调整选择器，仅改 `.env`。
  - 现状：选择器为推测值，`/debug/dom` 未实跑。
  - 验收：`/debug/dom` 能定位输入框与回复节点。
- [~] **T4.4 文档**：`README.md` 已是 Gemini 文案；**`INSTALL.md` / `cmdlog.md` 缺失**；`design.md` §4 待同步（→ T0.3）；`design.md` §3 文件树漏列 `tasks.md`、将 `update.md` 标注为"进度规划"（实际是代码分析）需修正。
  - 验收：新用户按 `INSTALL.md` 可从零跑通；`design.md` 文件树与实际一致。
- [ ] **T4.5 agy 接入验证**：agy CLI 直连 `http://127.0.0.1:8001/v1` 完成一轮对话。
- [~] **T4.6 E2E 对等测试套件（直连 vs Bridge）**：Playwright 直连 `gemini.google.com/app` 与经 bridge 的结果按“基线原则”比较（直连达标而 bridge 不达标 = 代码缺陷；双侧不达标 = 环境问题 SKIP）。设计见 `doc/e2e_test_design.md`，实现见 `tests/e2e/`（A 内容对等 / B 协议 / C 工具调用 / D 会话，共 13 例）。
  - 现状：设计 + 实现完成；`GEMINI_E2E=1` 时执行、平时自动 skip（当前套件 123+13 全绿）；**尚未在已登录环境实跑**（依赖 T4.2 前置）。
  - 验收：登录环境下 `GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity -v` 四组全绿；A4 的尾哨兵无截断、C1 的 tool_calls 解析与直连基线一致、D3 重置后上下文保持。

---

## 阶段 5：协议正确性修复（P0，源自 update.md §2）

- [~] **T5.1 `responses.py` 流式 id / 索引一致性**（update.md §2.1）。
  - 现状：问题已定位 4 处——`final_output` 重生成 `call_id`（:505 vs :479）、`function_call_arguments.*` 用 `call_id` 当 `item_id`（:493/:498）、message item 与 function_call 同为 `output_index 0`（:423 vs :481）、`output_item.done` 只发一次且固定 index 0（:543）。
  - 产出：循环内保存 `(item_id, call_id)` 全程复用；工具分支逐 item 发 done、索引与 added 对齐；message item 不与工具 item 抢 index。
  - 验收：**先写 `test_responses.py` 失败用例**再修——同 item 全事件 id 一致、N 个工具调用 added/done 数量相等、`response.completed` 里 `call_id` 与事件一致。
- [ ] **T5.2 `parse_tool_calls` 契约固定 + 重复消费修复**（update.md §2.2）。
  - 产出：docstring 写明"一行一调用"；`segment` 截断到下一标记（或位置去重），修"标记后无对象 → 重复消费下一标记对象"的边角。
  - 验收：测试覆盖多行多调用、同行两对象（第二个按契约丢弃）、标记后无对象不产生重复调用。
- [ ] **T5.3 `save_files` 默认行为 + `output/` 保留策略**（update.md §2.3 / §5.3）。
  - 产出：默认值改由 `.env` 的 `SAVE_FILES` 控制（默认 `false`）；`OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS` 启动清理；`saved_files` 仅落盘时返回。
  - 验收：不传字段的请求不落盘；`output/` 文件数有上界；`test_models.py` 默认值断言同步更新。
- [ ] **T5.4 端点加固**（update.md §2.5）。
  - 产出：`/session/reset` 支持可选 `RESET_TOKEN`；`/debug/dom` 的 `sha1` 需 `GEMINI_DEBUG=1` + token（或只回长度）。
  - 验收：设 token 后无 token 请求 401/403；未设 token 时行为向后兼容；README 补"仅限本机"说明。

## 阶段 6：测试补全（P1，源自 update.md §3.5）

- [ ] **T6.1 `test_sessions.py`**：分桶优先级、`MAX_SESSION_BUCKETS` LRU 回收、同桶排队与 `BUCKET_LOCK_TIMEOUT_S` 超时、`PARALLEL_BUCKETS` 开关、创建/播种/到顶/轮转/重试退避/空闲回收。
  - 验收：T2.3 / T2.4 转 `[x]`。
- [ ] **T6.2 `test_routes_chat.py` / `test_routes_responses.py`**：TestClient + 假 driver，覆盖非流式、流式、工具、错误码（504/503/404 分型）、`/healthz` cluster 字段、`/session/reset`、`/debug/dom` 门控；含 T3.3 启动冒烟。
  - 验收：T3.2 / T3.3 转 `[x]`；路由级回归有保护。
- [ ] **T6.3 `test_tasks.py` + `test_prompting.py`**：任务快照写入/读取/截断/关闭/goal 自愈；prompting 主拼接路径（多角色/多轮/无 system/空历史）。
  - 验收：T1.5 / T1.2 转 `[x]`。
- [ ] **T6.4 DSML 解析用例**（update.md §4.2）：单 invoke、多 invoke、缺闭标签、全角竖线、泛化名映射各 1 例。
  - 验收：`grep DSML tests/` 非空，防御层行为被固定。
- [ ] **T6.5 `.env.example` 防漂移测试**（依赖 T0.3）：断言 `.env.example` 键集合 == `config.py` 中 `env_*` 引用的键集合。
  - 验收：任一侧新增/删除键而另一侧未同步时测试失败。
- [ ] **T6.6 选择器回退与落盘测试**：假 page 下选择器回退链命中并发送（T2.1 验收）；多语言代码块提取命名 + 无语言标注默认 `txt`（T3.4 验收，顺带删掉 `chat_io.py:431` 的内容猜测）。
  - 验收：T2.1 / T3.4 转 `[x]`。

## 阶段 7：健壮性（P1，源自 update.md §2.4 / §3.2 / §3.4）

- [ ] **T7.1 会话 dict 有界化**：`_sessions` 按上限 LRU 逐出（未持锁 + `updated_at` 最旧，落盘状态可安全恢复）；`_locks` 空闲未持有即删；`_last_prompts` 随 `_sessions` 同步逐出。**不要**在 `_close_bucket_page` 里清状态（与其"只关页面、状态保留"契约冲突）。
  - 验收：模拟 > `MAX_SESSION_BUCKETS`×4 个不同 session key 后三 dict 长度有上界；轮转/播种行为回归不变（配合 T6.1）。
- [ ] **T7.2 `_delta_piece` 节点替换语义**（update.md §3.2）：检测到非前缀替换后该轮停止发增量、缓冲到结束一次性发，保证客户端内容与最终回复一致；记录替换次数并告警日志。
  - 验收：更新 `test_parsing.py` 两个固化用例为新契约；新增"替换后客户端内容 == 最终回复"用例。
- [ ] **T7.3 `CHAT_KEEPALIVE_S` 可配**（update.md §3.4）：chat 路径 10s 硬编码改为配置（默认 10.0，`0` 关闭），与 `RESPONSES_KEEPALIVE_S` 形式一致，两处共用实现。
  - 验收：`test_streaming.py` 覆盖可配间隔与关闭；`.env.example` 同步新键（依赖 T0.3）。

## 阶段 8：工程质量（P2，源自 update.md §3.3 / §4）

- [ ] **T8.1 `chat_io` 抽 `ReplyWatcher`**：结束判定做成纯函数 `(prev_state, frame) -> (decision, next_state)`，`decision ∈ {wait, done, timeout, context_limit, stall}`。**前置：T6.1 / T6.2 就位**，否则重构无保护。
  - 验收：结束判定分支可纯函数单测；`test_end_detection.py` 全绿；`_send_chat_locked` 变薄。
- [ ] **T8.2 引入 `logging`**：替换 38 处 `print`，`GEMINI_DEBUG` 控制级别；过渡期统一 `[ERR]` / `[恢复]` / `[轮转]` 前缀。
  - 验收：可按级别过滤、带时间戳；README 说明日志不含消息正文（update.md §5.4）。
- [ ] **T8.3 driver 移入 `lifespan`**：`server.py:36` 模块级 `driver = GeminiWebDriver()` 改为 `app.state.driver`，测试注入假 driver。
  - 验收：T6.2 测试不再依赖 `patch` 模块全局；进程启停能优雅关闭浏览器上下文。
- [ ] **T8.4 `ModelCard.context_window`**：`/v1/models` 带上 `SUPPORTED_MODELS` 里已有的上下文长度。
  - 验收：`test_models.py` 断言 `/v1/models` 返回该字段。
- [ ] **T8.5 `.env` 解析器支持行内注释**：未被引号包裹的 ` #` 之后截断；`export KEY=` 可识别或至少告警；文档说明不支持多行值。
  - 验收：`test_config.py` 覆盖 `KEY=value # comment`、`export KEY=value`、引号内 `#` 不截断。
- [ ] **T8.6 `prompting.DEFAULT_SEED_MAX_CHARS` 删除**：统一 `config.SEED_MAX_CHARS`。
  - 验收：`grep DEFAULT_SEED_MAX_CHARS` 无残留；播种测试全绿。
- [ ] **T8.7 `tasks.py` 冗余截断清理（可选）**：`resume_block` 不再二次截断；`record` 可读性整理。低价值，择机。
  - 验收：`test_tasks.py`（T6.3）全绿。

---

## 依赖关系

```
T0.1 -> T0.2 -> T0.3 -> T6.5
T0.2 -> T1.*（阶段 1 可并行）
T0.2 -> T2.1 -> T2.2 -> T2.3 -> T2.4
T1.* + T2.* -> T3.*
T3.1 -> T5.1（先写失败用例再修）
T3.* -> T5.2 / T5.3 / T5.4（可并行）
T2.* -> T6.1；T3.* -> T6.2；T1.* -> T6.3 / T6.4；T0.3 -> T6.5
T6.1 + T6.2 -> T7.1（行为回归保护）-> T8.1
T4.* 依赖真实环境，可与 T5–T8 并行
```

## 落地顺序（与 update.md §7 对应）

1. T0.3 + T6.5（配置一致性，零风险）
2. T5.1 + T6.3 中的 `test_responses.py` 部分（Codex 工具调用可用性）
3. T5.2（工具调用正确性）
4. T5.3（默认副作用与磁盘）
5. T7.1（长跑泄漏）
6. T6.1 + T6.2（回归保护，后续重构的前提）
7. T5.4（安全）
8. T7.2 + T7.3（流式体验与可配置性）
9. T8.1（有测试保护后再动刀）
10. T8.2–T8.7（择机批量）
11. T4.1–T4.5（真实环境联调，可穿插）

## 关键风险

- **Gemini 网页版 DOM 不稳定**：选择器集中在 `.env`，改版只改配置；T4.3 是必做校正步。
- **不恢复旧会话的代价**：每轮播种重放历史，token 消耗高于 URL 恢复；用 `tasks.py` 快照保住任务目标。
- **风控**：`PARALLEL_BUCKETS` 默认关，同时只驱动一个网页会话。
- **Responses 协议兼容**：T5.1 修复可能影响已"碰巧能用"的客户端——用 `test_responses.py` 先固化当前事件序列期望，再修 id，防止行为漂移。
- **重构风险**：T8.1（ReplyWatcher）必须排在 T6.1 / T6.2 之后；无测试保护不动 `chat_io`。

---

## 核对记录（2026-10-04，替代 v1 的 2026-10-03 记录）

- 实测 `123` 用例全部通过（7 个测试文件：config / models / parsing / toolcalls / streaming / end_detection / seed_prompt）。
- v1 记录中"tests/ 为空目录"已过期：纯逻辑层测试已补齐，缺口收敛为 **会话、路由、任务快照、prompting 主路径、DSML、responses id** 六项（见阶段 6）。
- `.env`：33 键、无 DeepSeek 残留、争议键均已覆盖；`.env.example` 仍缺（T0.3）。
- `README.md` 配置表与 `config.py` 逐项一致（v1 update.md 误报 README 过期）；`design.md` §4 确认 8 项过期。
- `output/` 27 文件无清理（T5.3）；`print` 38 处（T8.2）；DSML 测试 0 覆盖（T6.4）。
- 缺失文件复核：`.env.example`、`client_test.py`、`INSTALL.md`、`cmdlog.md` 均不存在。
