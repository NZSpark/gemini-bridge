# GeminiBridge 代码分析：现状、问题与改进建议（v2 修订版）

> 修订：2026-10-04（初稿同日；v2 对照源码逐条复核，勘误见第 6 节）
> 范围：`gemini_api_server.py`、`gemini_web/*`（14 模块 + `__init__`）、`tests/*`、`doc/*`、`.env`、`README.md`
> 证据约定：结论尽量标注 `文件:行号`（以修订当日 `main` 为准）；未标注行号的为跨文件/运行时结论，附实测命令。

---

## 0. 核对基线（实测）

| 项 | 实测结果 |
| --- | --- |
| 测试 | `python -m unittest discover -s tests -t .` → **123 个用例全部通过**；7 个测试文件 |
| 缺失文件 | `.env.example`、`client_test.py`、`INSTALL.md`、`cmdlog.md`（`design.md` §3 均要求存在） |
| `.env` | 33 个键；无 `DEEPSEEK_` / `ds-` 残留；`STABLE_POLLS` 等 8 个争议键均已显式覆盖 |
| `output/` | 27 个文件，无清理机制 |
| README 配置表 | 与 `config.py` 默认值**逐项一致**（README:141-159） |
| `design.md` §4 默认值表 | **8 项与 `config.py` 不一致**（见 3.1 表） |
| 测试缺口 | 无路由级测试；缺 `test_sessions.py` / `test_tasks.py` / `test_responses.py`；`prompting` 主路径无独立测试文件；DSML 分支 0 覆盖 |

---

## 1. 总体评价

项目结构清晰，分层合理：入口 / 配置 / 模型 / prompting / toolcalls / driver（拆为 completion / page_pool / session_store / chat_io 四个 mixin）/ streaming / responses / tasks / server 各司其职。

几个明显优于常见"网页套壳"方案的设计：

- **不恢复旧会话，改为"新开对话 + 历史播种"**：对网页版 DOM 改版鲁棒，不依赖易变的会话 URL。
- **任务快照 `tasks.py`**：轮转时先注入任务目标，避免 `SEED_MAX_CHARS` 从尾部截断把任务目标丢掉。
- **双阈值结束判定 + `_complete_text` 去动画**：规避 Gemini 逐 token 显现动画导致的半截 JSON；`test_end_detection.py` 6 个用例覆盖。
- **会话分桶 + 可选并发锁**：多 Agent 场景下上下文隔离，`BUCKET_LOCK_TIMEOUT_S` 避免无限排队。
- **选择器外置 `.env`**：网页版改版只改配置。
- **错误映射分型**：`context_length_exceeded` / `upstream_busy` / `timeout` / `upstream_error` 语义清晰，客户端可分辨。
- **JSON 尽力修复 + shell 护栏**：`_repair_json_quotes` 修无歧义损伤，`_shell_quotes_balanced` + `_call_args_sane`（toolcalls.py:403/:581）把引号不配对的命令在桥接层丢弃，避免静默发坏命令——初稿低估了这层防护（见 6.3）。

主要问题集中在三类：**Responses 协议一致性**（id / output_index 错乱）、**配置与文档漂移**（`design.md` 过期、`.env.example` 缺失）、**长跑健壮性**（dict 无界、`output/` 膨胀）。下面按高 / 中 / 低列出。

---

## 2. 高优先级问题

### 2.1 `responses.py` 流式分支 id / 索引四处不一致（影响 Codex 工具调用）

一处比初稿更严重：**不只是 `call_id` 重新生成，`output_index` 还会撞车**。

| 位置 | 问题 |
| --- | --- |
| responses.py:479 vs :505 | `added` 事件生成 `call_id`，`final_output`（进 `response.completed`）又重新生成，两者对不上 |
| responses.py:493/:498 | `function_call_arguments.delta/done` 的 `item_id` 塞的是 `call_id`，规范上应是 `fc_...` 的 item id |
| responses.py:423 vs :481 | 进入函数前**无条件**先发 `message` item 的 `output_item.added(index=0)`；即使最终走工具分支，函数调用 `enumerate` 也从 0 开始 → **两个 item 同为 index 0** |
| responses.py:543-546 | `output_item.done` 只发一次、固定 `output_index: 0`、且只带 `final_output[0]` → N 个工具调用只有 1 个拿到 done，且 message item 永远没有 done |

- 影响：Codex 按 `call_id` / `item_id` 关联请求响应，多工具调用场景关联失败或错乱；这是"能连上但工具调用时灵时不灵"的典型根源。
- 建议：进入流式函数时先判定/记录本轮是"文本"还是"工具"形态（工具分支要等 `run_chat` 结果，可用 `buffer_tools` 标志预判，非缓冲模式至少做到工具事件内部自洽）；循环内保存 `(item_id, call_id)` 对，`added` / `delta` / `done` / `final_output` 全程复用；`output_item.done` 按 item 逐个发、索引与 `added` 对齐。
- 验收：新增 `tests/test_responses.py`，断言同一 item 全事件 `id`/`call_id` 一致、多工具调用 done 数量 = added 数量、索引不冲突。

### 2.2 `parse_tool_calls` 的"一行一调用"契约未固定，且存在重复消费边角

`toolcalls.py:502-513`：外层 `for match in _TOOL_CALL_LINE_RE.finditer(text)` 逐个 `TOOL_CALL:` 行首标记扫描，内层 `for obj in objs: _consume(...); break` **每个标记只消费第一个平衡对象**。

初稿对此表述前后矛盾（先说"丢多对象"，后又承认外层已是 finditer）。定论与真正风险：

1. **同行多对象**：`TOOL_CALL: {..} {..}` 第二个对象丢弃——这就是"一行一调用"契约，是合理取舍（避免把后文无关 `{...}` 吞进来），但**契约没有写进 docstring / 注释 / 测试**。
2. **初稿没发现的边角**：`segment = text[match.end():]` 是"标记之后的**全文**"而非本行。若某个 `TOOL_CALL:` 行**没跟对象**（模型写了标记又改主意写了解释文字），该标记会消费**下一个标记的对象**，轮到下一个标记时又消费同一对象 → **同一调用重复出现两次**。`_consume` 对 `allow_bare_object=True` 不去重，重复会原样进入 `tool_calls`。
3. 兜底链 `if not calls:` 逐级降级（fence → DSML → 裸 `tool_uses` → marker），主路径解析成功但被护栏过滤后不会回退——影响极小，记录备查。

- 建议：
  a. docstring 写明契约："每个 `TOOL_CALL:` 标记只取其后第一个平衡对象；一行一调用；多调用必须多行"；
  b. 修边角：标记与下一标记之间若无 `{`，跳过该标记不消费（`segment` 截到下一个 `_TOOL_CALL_LINE_RE.match` 的位置即可），或对已消费对象做位置去重；
  c. 补测试固定：多行多调用、同行两对象（第二个丢弃）、标记后无对象+后续合法调用（不重复）。

### 2.3 `save_files` 默认 `True`，非标准字段默认产生落盘副作用

- 证据：`models.py:58`（`save_files: Optional[bool] = True`）、`responses.py:50` 同样默认 True；门在 `server.py:373`（`if request.save_files:`）。响应字段 `saved_files`（`models.py:87`）不是 OpenAI 标准字段。
- 影响：任何不感知该字段的 OpenAI 客户端（Pi、LangChain、…）每次请求都落盘，`output/` 已积 27 个文件且无清理；纯 API 用途属意外副作用。
- 建议：
  1. 默认改为 `.env` 的 `SAVE_FILES`（默认 `false`）；请求字段仅在显式传入时覆盖；
  2. `saved_files` 仅在落盘发生时返回，或改 `x_saved_files`；
  3. 增加 `OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS`，启动时清理（与 §5.3 合并落地）。

### 2.4 会话相关 dict 无界增长（长跑泄漏）

- 证据：`driver.py:54` `_last_prompts`、`:61` `_sessions`、`:66` `_locks` 只增不减；`_pages` / `_page_last_used` 有空闲回收与 LRU（`page_pool.py:82/:100/:115`，且 `_close_bucket_page` 会 pop 这两个）。
- 初稿建议"在 `_close_bucket_page` 里清理 `_sessions` / `_last_prompts`" **与现有设计冲突**：该函数的契约是"只关页面、状态保留"（page_pool.py:83 注释），空闲回收后靠状态决定重开与播种；把状态清掉会破坏轮转记账。
- 正确姿势（状态本就落盘，`session_store._state()` 未命中会从磁盘恢复，session_store.py:52-57 → 内存 dict 是**缓存**，逐出安全）：
  1. `_sessions`：按 `MAX_SESSION_BUCKETS` 或独立上限做 LRU，逐出 `updated_at` 最旧且**当前未持锁**的状态（下次访问自动从磁盘加载）；
  2. `_locks`：在锁**未被持有**且桶空闲超 `BUCKET_IDLE_TTL_S` 时删除（无害，`_lock_for` 会重建）；
  3. `_last_prompts`：随 `_sessions` 同步逐出（丢失仅影响 usage 估算回退，可接受）。
- 验收：模拟 N（> `MAX_SESSION_BUCKETS`×4）个不同 session key 后，三个 dict 长度有上界；轮转/播种行为回归不变。

### 2.5 `/session/reset` 无鉴权；`/debug/dom` 暴露 `sha1`

- 证据：`server.py:152` `/session/reset` 无任何 token，知道桶名（默认 `default`）即可触发"下轮新开会话"——未鉴权的状态修改端点；`server.py:215-259` `/debug/dom` 受 `GEMINI_DEBUG` 门控（默认 404），但返回节点 `class` / `text_length` / `sha1`，`sha1` 可做离线字典比对反推短回复。
- 缓解现状：默认只监听 `127.0.0.1`（config.py `HOST` 默认值），风险有界。
- 建议：
  1. `/session/reset` 支持可选 `RESET_TOKEN`（环境变量，设置后强制校验）；
  2. `/debug/dom` 的 `sha1` 仅在 `GEMINI_DEBUG=1` 且带 token 时返回，或降级为只返回长度；
  3. 文档写明：两端点仅限本机使用，勿做端口转发。

---

## 3. 中优先级问题

### 3.1 `design.md` 默认值表过期；`.env.example` 缺失（README **没有**问题）

初稿称"README / design.md 里列的默认值与 config.py 已对不上"——**实测 README 全对**（README:141-159 与 `config.py` 逐项一致），过期的只有 `design.md` §4：

| 项 | design.md §4 | config.py 实际 | `.env` 当前 |
| --- | --- | --- | --- |
| `STABLE_POLLS` | 5 | 2 | 已覆盖 |
| `LEN_STABLE_POLLS` | 8 | 4 | 已覆盖 |
| `MAX_SESSION_BUCKETS` | 3 | 8 | 已覆盖 |
| `PARALLEL_BUCKETS` | true | false | 已覆盖 |
| `BUCKET_LOCK_TIMEOUT_S` | 15 | 0 | 已覆盖 |
| `SESSION_MAX_TURNS` | 80 | 60 | 已覆盖 |
| `SESSION_MAX_TOKENS` | 240000 | 60000 | 已覆盖 |
| `RESPONSES_KEEPALIVE_S` | 10.0 | 10.0 | 一致 |

- 附带问题：`config.py:3` / `config.py:13` 两处引用 `.env.example`（"模板见 .env.example"），**文件并不存在**（T0.3 验收项，未做）。
- 影响：新用户/新 Agent 按 `design.md` 理解行为会出错；直接改 `.env` 起步的用户不受影响。
- 建议：
  1. 补 `.env.example`，键集合与 `config.py` 引用的键严格一致（可从 `config.py` 脚本生成）；
  2. `design.md` §4 不再手抄数字，改为"引用 `config.py` / `.env.example`"；
  3. 新增测试：断言 `.env.example` 键集合 == `config.py` 中 `env_*` 调用的键集合（防漂移）。

### 3.2 `prompting._delta_piece` 在节点被整体替换时，客户端内容错乱

- 证据：`prompting.py:52-70`。`current` 不再以 `streamed` 为前缀时，退回"公共前缀之后的部分"继续追加。
- 真实危害（初稿表述不准确，说"这部分已经发过"）：**旧尾巴不会被撤回，新内容直接追加**。现有测试把它固化成了预期行为：
  - `test_parsing.py:72`：`_delta_piece("abc", "xyz") == ("xyz", ...)` → 客户端最终持有 `abcxyz`，而真实回复是 `xyz`；
  - `test_parsing.py:68`：`_delta_piece("abc", "abd")` 补发 `d` → 客户端得到 `abcd`，真实回复是 `abd`。
- 影响：流式输出偶发"旧尾巴残留 / 重复片段"；非流式最终以 `reply_content` 为准，不受影响。
- 建议（OpenAI SSE 无"重置"语义，只能退化处理）：
  1. 记录本轮是否发生过"非前缀替换"（计数 + 日志告警）；
  2. 发生替换后切换策略：该轮**停止发增量**，缓冲到结束一次性发（等价于非流式），保证客户端内容正确——宁可晚一点也不要错；
  3. 更新上述两个测试为新契约，并加"替换后内容仍与最终回复一致"的用例。

### 3.3 `chat_io._send_chat_locked` 职责过多，结束判定难测

- 证据：`chat_io.py:187` 起，单函数内同时处理：输入框定位/发送、"新回复出现"多重判据（文本变化/节点数/`saw_generating`）、停止按钮 + 文本稳定双兜底、到顶探测（`CAP_CHECK_EVERY`）、卡死检测（`STALL_POLLS`）、总超时、`on_delta` 增量 diff、状态更新与轮转标记。
- 影响：分支靠大量 mock 集成测试覆盖（`test_end_detection.py` 已尽力），条件间耦合（`reply_seen` / `generating` / `saw_generating` / `stalled` 互相影响），改一处易伤另一处。
- 建议：抽 `ReplyWatcher`，把"判定一帧"做成纯函数 `(prev_state, frame) -> (decision, next_state)`，`decision ∈ {wait, done, timeout, context_limit, stall}`；网页交互只负责取帧。**优先级低于 2.x**：先补 6 节的回归测试再动刀。

### 3.4 `streaming.py` 工具模式 keep-alive 硬编码 10s

- 证据：`streaming.py:98` `await asyncio.wait_for(queue.get(), timeout=10.0)`，超时发 `: keep-alive` 注释。
- 问题：与 `RESPONSES_KEEPALIVE_S`（10.0，可配、可关）不一致，chat 路径不可配也关不掉。
- 建议：新增 `CHAT_KEEPALIVE_S`（默认 10.0，`0` 关闭——关闭时 `timeout=None`），chat / responses 两处共用一个实现。

### 3.5 测试缺口（对照 tasks.md 验收）

| 缺失测试 | 对应验收 | 现状 |
| --- | --- | --- |
| `test_sessions.py` | T2.3 / T2.4（分桶、轮转、到顶、LRU、锁超时） | 无 |
| `test_routes_chat.py` / `test_routes_responses.py` | T3.2 | 全仓库无任何路由级测试（无 `TestClient` 引用） |
| `test_tasks.py` | T1.5 | 无 |
| `test_responses.py` | T3.1；也是 2.1 修复的回归保障 | 无 |
| `test_prompting.py` | T1.2（多角色/多轮/无 system/空历史） | 仅 `test_seed_prompt.py` 覆盖播种与 meta 判定，主拼接路径无独立覆盖 |
| DSML 用例 | 4.2 所述防御层 | `grep DSML tests/` = 0 命中 |

- 建议：`test_responses.py` 作为 2.1 修复的失败用例随 T5.1 **先行**；其余缺口按 `test_sessions.py` → `test_routes_*.py`（行为最易回归）的顺序补齐，再动 3.3 的重构。

### 3.6 缺失交付物：`.env.example`、`client_test.py`、`INSTALL.md`、`cmdlog.md`

- `design.md` §3 文件清单全部要求；T0.3 / T4.1 / T4.4 验收项，均未做。
- 建议：`.env.example`（配合 3.1 的防漂移测试）与 `client_test.py`（`openai` SDK 打本地服务的最小闭环，联调必备）优先。

---

## 4. 低优先级 / 代码质量

1. **`prompting.DEFAULT_SEED_MAX_CHARS` 重复定义**（`prompting.py:74` = 12000，`config.SEED_MAX_CHARS` 默认 12000）：删除常量，统一 `config.SEED_MAX_CHARS`（`prompting.py:201` 调用处改为直接取 config）。
2. **DSML 兼容层无测试**：`_DSML_*` 系列 / `_resolve_dsml_name` / `_parse_dsml_invokes` 约 100 行防御，`tests/` 零覆盖。补：单 invoke、多 invoke、缺闭标签、全角竖线、泛化名映射各 1 例（并入 3.5 表格）。
3. **`.env` 解析器过简**（`config.py` `_load_env_file`）：`KEY=value # comment` 会把注释吃进值；`export KEY=value` 会静默失效（key 含空格，落回默认值）；不支持多行值。至少支持未引号包裹的行内 ` #` 截断，文档说明不支持多行。
4. **日志用 `print`**：`gemini_web/*.py` 实测 38 处，无级别、无时间戳、无法按模块过滤。引入 `logging`，`GEMINI_DEBUG` 控制级别；过渡期至少统一 `[ERR]` / `[恢复]` / `[轮转]` 前缀便于 grep。
5. **`server.py:36` 模块级 `driver = GeminiWebDriver()`**：测试只能 `patch`，无法多实例、无法在 `lifespan` 里优雅启停。改为 `app.state.driver`（`lifespan` 创建），测试用注入/`dependency_overrides`。
6. **`ModelCard` 丢掉 `context_window`**（`models.py:90-94` vs `SUPPORTED_MODELS:103-105`；`server.py:186-193` 只传 `id`）：Pi 做模型发现时缺上下文长度。`ModelCard` 加字段，`list_models` 带上。
7. **`save_extracted_files` 扩展名靠猜**（`chat_io.py:431`：`"py" if "import " in code or "def " in code else "txt"`）：无语言标注时误判率高（Java/JS 含 `import ` 也会中）。默认 `txt`，不猜。
8. **`tasks.py` 冗余截断**（初稿 3.2）：`record()` 写入时已按 `TASK_KEEP_MESSAGES` 截取（tasks.py:126）+ `_wrap_recent_item` 按 500 字符截断（:79），`resume_block()` 又 `recent[-TASK_KEEP_MESSAGES:]`（:195）二次截断——**无害且快照有界**（≈ goal 2000 + 8×500 字符）。仅做可读性清理，从"中优先级"降到这里。
9. **覆盖不均的既有事实**：7 个测试文件共 123 用例全部通过，纯逻辑模块（config/models/parsing/toolcalls/streaming/seed）覆盖良好；薄弱点即 3.5 表 + 会话/路由两块。

---

## 5. 安全与运维

1. **`user_data/` 含登录 cookie**：`.gitignore` 已忽略，README:226 有"不提交"提示，但未提示"勿备份 / 勿同步该目录"（iCloud/Dropbox 会带走登录态）。README 风险表补一行即可。
2. **profile 单实例**：已给出 `pkill` 提示，无锁文件检测。可在 `init()` 里先探测 `user_data/SingletonLock`，给出更早、更明确的失败信息。
3. **`output/` 无清理**：并入 2.3 的保留策略（`OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS`，启动时清理）。
4. **`GEMINI_DEBUG` 日志**：`[debug] session_key=...`、轮询状态行不含正文；`_last_prompts` 不打印。在 README 明确承诺"DEBUG 日志不含消息正文"，防止用户误开后外传。

---

## 6. 与初稿的差异（勘误清单）

| # | 初稿论断 | 复核结果 | 处理 |
| --- | --- | --- | --- |
| 1 | §3.1 "README / design.md 默认值都对不上" | **README 全对**（141-159 行逐项与 `config.py` 一致），只有 `design.md` 过期 | 改为仅指 `design.md` |
| 2 | §2.1 先说"多对象同行会丢需去 break"，后又承认外层已是 finditer | 结论混乱；且遗漏"标记后无对象 → 消费下一标记对象 → 重复调用"的真实边角 | 重写为 2.2，给明确契约 + 修复点 |
| 3 | §2.2 用 `{"cmd": "git commit -m "msg""}` 当反例 | 该形态实际能被状态机正确修复（双尾引号→`\"msg\"`）；真正风险是**单尾引号截断**（shell 类已被 `_call_args_sane` 拦下）与 `-m` 悬挂截断、非 shell 工具截断 | 修正示例与风险面，认可现有护栏 |
| 4 | §2.4 "在 `_close_bucket_page` 里清理 `_sessions`/`_last_prompts`" | 与"只关页面、状态保留"的设计冲突；状态已落盘，内存 dict 是缓存 | 改为有界 LRU 逐出（未持锁 + 最旧） |
| 5 | §3.2 "tasks.py 重复保存长文本" 列为中优先级 | 快照有界（≤ ~6KB/轮），二次截断无害 | 降为低优先级（4.8） |
| 6 | §3.3 "公共前缀之后的部分已经发过" | 不准确：是"旧尾巴不撤回 + 新内容追加"，`test_parsing.py:72` 把全量追加固化成了预期 | 改写机制描述，建议替换后停发增量 |
| 7 | §3.5 只列 3 处 id 不一致 | 漏了 message item 与 function_call 同为 `output_index 0`、`output_item.done` 只发一次 | 补全为 4 处（2.1 表） |
| 8 | §4.8 测试缺口清单 | 漏 `test_prompting.py`（T1.2）与 `test_responses.py`（T3.1） | 补入 3.5 表 |
| 9 | §5.1 "README 未强调 user_data 不要备份" | README:226 已有"不提交"提示，缺的是"勿备份/同步" | 措辞收敛 |
| 10 | 无基线实测 | 已补 §0（123 用例通过、`output/` 27 文件、`.env` 33 键无 DeepSeek 残留等） | 新增 §0 |

初稿其余论断（2.3 / 2.5 / 3.4 / 3.6 / 3.7 / 4.1 / 4.3 / 4.4 / 4.5 / 4.6 / 4.7、§1 优点清单）均复核成立，予以保留。

---

## 7. 建议的落地顺序

| 顺序 | 项 | 理由 |
| --- | --- | --- |
| 1 | 补 `.env.example` + `design.md` 默认值同步 + 防漂移测试（3.1） | 新用户第一步，T0.3 验收缺口，改动零风险 |
| 2 | `responses.py` 流式 id / output_index 一致性 + `test_responses.py`（2.1） | 直接影响 Codex 工具调用可用性；先写失败用例再修 |
| 3 | `parse_tool_calls` 契约文档化 + 重复消费修复 + 测试（2.2） | 工具调用正确性 |
| 4 | `save_files` 默认改由 `.env` 控制 + `output/` 保留策略（2.3 / 5.3） | 消除默认副作用与磁盘膨胀 |
| 5 | 会话 dict 有界化（2.4） | 长跑泄漏，改动局部、可测 |
| 6 | `test_sessions.py` / `test_routes_*.py`（3.5） | 行为最易回归的两块，为后续重构护航 |
| 7 | 端点加固：`RESET_TOKEN`、`/debug/dom` 收 sha1（2.5） | 安全，配置向后兼容 |
| 8 | `_delta_piece` 替换语义（3.2）、`CHAT_KEEPALIVE_S`（3.4） | 流式体验与可配置性 |
| 9 | `chat_io` 抽 `ReplyWatcher`（3.3） | 在第 6 步测试就位后再动刀 |
| 10 | 质量项：logging、`ModelCard.context_window`、`.env` 行内注释、driver 移入 lifespan、扩展名不猜（§4） | 择机批量处理 |
| 11 | 联调件：`client_test.py`、真实登录、选择器校正、`INSTALL.md` / `cmdlog.md`（3.6 / 原 T4） | 依赖真实环境，可与 1-10 并行 |

---

## 8. 结论

核心设计（不恢复会话 + 播种 + 任务快照 + 双阈值结束判定 + 分桶）扎实，123 个既有用例全部通过，纯逻辑层质量良好。真正需要优先处理的是：

1. **Responses 流式事件的 id / 索引一致性**（2.1）——当前最可能造成"客户端能连、工具调用却不稳"的问题；
2. **配置文档漂移与 `.env.example` 缺失**（3.1）——成本最低、对新用户收益最大；
3. **工具调用解析的契约固定与重复消费边角**（2.2）——正确性问题，测试成本小；
4. **默认落盘与长跑有界性**（2.3 / 2.4）——决定服务能否无人值守长期运行。

按第 7 节顺序推进，可在不改架构的前提下显著提升协议兼容性、可维护性与长跑稳定性。任务拆解见 `doc/tasks.md`（v2）。
