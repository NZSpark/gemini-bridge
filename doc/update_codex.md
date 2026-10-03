# GeminiBridge 代码分析：改进建议（Codex 版）

> 分析日期：2026-10-04
> 范围：`gemini_api_server.py`、`gemini_web/*`（16 模块）、`tests/*`、`doc/*`、`.env`、`README.md`
> 复现环境：仓库自带 `.venv`（`python3` 系统解释器缺 `playwright`，必须用 `.venv/bin/python`）

---

## 0. 实测基线

| 项 | 实测结果 |
| --- | --- |
| 测试 | `.venv/bin/python -m unittest discover -s tests -t .` → **123 用例通过**（7 文件） |
| 系统 Python 跑测试 | **5 个 ImportError**：`No module named 'playwright'`（详见 2.1） |
| 缺失交付物 | `.env.example`、`client_test.py`、`INSTALL.md`、`cmdlog.md` 均不存在 |
| `output/` | 27 个文件，无清理策略 |
| 代码规模 | `gemini_web/` 约 3900 行；最大单文件 `toolcalls.py` 605 行、`responses.py` 556 行 |

---

## 1. 总体评价

分层清晰：入口 / 配置 / 模型 / prompting / toolcalls / driver（拆 completion + page_pool + session_store + chat_io 四个 mixin）/ streaming / responses / tasks / server。

明显优于常见「网页套壳」的设计：

- **新开对话 + 历史播种**取代易碎的会话 URL 恢复，对 DOM 改版鲁棒。
- **任务快照 `tasks.py`** 在轮转播种前注入任务目标，避开尾部截断丢目标。
- **`_complete_text` 去动画**：克隆节点剥离 `animating/pending` 再读 `innerText`，规避逐 token 显现导致的半截 JSON。
- **双阈值结束判定**（内容稳定 / 长度稳定）+ 「生成中」信号探测。
- **错误分型**：`context_length_exceeded` / `upstream_busy` / `timeout` / `upstream_error`。
- **选择器全部外置** `config.py`，改版只改配置。

问题集中在三类：**Responses 协议一致性**、**默认副作用与资源无界**、**测试与文档缺口**。

---

## 2. 高优先级

### 2.1 系统 Python 直接跑测试即 5 个 ImportError（新人第一脚就踩坑）

- 现象：`python -m unittest discover -s tests -t .` → `ModuleNotFoundError: No module named 'playwright'`，5 个测试模块整体加载失败（`test_config` / `test_end_detection` / `test_parsing` 等）。用 `.venv/bin/python` 才 123 全过。
- 根因：`gemini_web/__init__.py:16` 顶层 `from .driver import ...`，而 `driver.py:21` 顶层 `import playwright`。**只要 import 包（哪怕只想读 `config`）就会拉起 Playwright**，纯逻辑测试因此被迫依赖浏览器库。
- 建议（任选，推荐 a）：
  - a. `driver.py` 把 `from playwright.async_api import async_playwright` 挪进 `init()` 内（惰性 import），`__init__.py` 不再因缺 playwright 而崩；
  - b. 至少在 `README` 顶部用醒目方式写明「必须用 `.venv/bin/python`」，并在 `tests/` 加 `conftest`/`sitecustomize` 级别的缺失依赖提示。
- 验收：干净系统 Python 下 `import gemini_web.config` 成功；测试报错信息从 `ImportError` 变为可读提示。

### 2.2 `responses.py` 流式分支 id / `output_index` 四处不一致（直接影响 Codex 工具调用）

已核对行号：

| 位置 | 问题 |
| --- | --- |
| responses.py:479 vs :505 | `output_item.added` 生成一个 `call_id`，`final_output`（进 `response.completed`）在 :505 **又生成一个**，两者对不上 |
| responses.py:493 / :498 | `function_call_arguments.delta/done` 的 `item_id` 塞的是 `call_id`；规范应为 `fc_...` 的 **item id** |
| responses.py:423 vs :481 | 进函数先无条件发 `message` item 的 `output_item.added(index=0)`；工具分支 `enumerate` 又从 0 起 → **两个 item 同为 index 0** |
| responses.py:543-546 | `output_item.done` 只发一次、固定 `output_index: 0`、只带 `final_output[0]` → N 个工具调用只有 1 个 done，message item 永远没有 done |

- 影响：Codex 按 `call_id` / `item_id` 关联响应，多工具调用时错乱或丢失。
- 建议：进入流式函数先定形态（文本 / 工具），工具分支用 `RESPONSES_TOOL_BUFFER` 预判；循环里保存 `(item_id, call_id)` 对，`added`/`delta`/`done`/`final_output` 全程复用；`output_item.done` 按 item 逐个发，索引与 `added` 对齐。
- 验收：新增 `tests/test_responses.py`，断言同一 item 全事件 id 一致、done 数 = added 数、索引无冲突。

### 2.3 `parse_tool_calls` 的「标记后无对象」会重复消费同一次调用

`toolcalls.py` 首选形态：对每个 `TOOL_CALL:` 标记取 `segment = text[match.end():]`（**标记之后的全文**），只消费第一个平衡对象后 `break`。

- 契约本身合理（一行一调用），但**未写进 docstring / 注释 / 测试**。
- 真实边角：若某 `TOOL_CALL:` 行后**没跟对象**（模型写了标记又改主意），该标记会消费**下一个标记的对象**；轮到下一个标记时又消费同一对象 → **同一次调用出现两次**。`_consume(allow_bare_object=True)` 不去重。
- 建议：a) docstring 固化「一行一调用，每标记只取其后第一个平衡对象」；b) `segment` 截到下一个 `_TOOL_CALL_LINE_RE` 匹配处，或对已消费对象做位置去重；c) 补测试：多行多调用、同行两对象（第二个丢弃）、标记后无对象+后续合法调用（不得重复）。

### 2.4 `save_files` 默认 `True`，非标准字段默认产生落盘副作用

- 证据：`models.py` 的 `save_files: Optional[bool] = True`，`responses.py:50` 同样默认 True；`server.py:373` `if request.save_files:` 才落盘。`saved_files` 非 OpenAI 标准字段。
- 影响：任何不感知该字段的客户端（Pi / LangChain / …）每次请求都写 `output/`，已积 27 文件且无清理。
- 建议：默认改由 `.env` 的 `SAVE_FILES`（默认 `false`）控制，请求字段仅显式传入时覆盖；新增 `OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS` 启动清理。

### 2.5 会话相关 dict 无界增长（长跑泄漏）

- 证据：`driver.py:54` `_last_prompts`、`:61` `_sessions`、`:66` `_locks` 只增不减；`_pages` / `_page_last_used` 已有空闲回收与 LRU。
- 注意：**不要**在 `_close_bucket_page` 里清 `_sessions`——该函数契约是「只关页面、状态保留」（`page_pool.py:83`），清掉会破坏轮转记账。
- 正确姿势：状态已落盘（`session_store._state()` 未命中从磁盘恢复），内存 dict 只是缓存 → 对 `_sessions` / `_last_prompts` / `_locks` 做**有界 LRU 逐出**（未持锁 + 最旧）。
- 验收：`tests/test_sessions.py` 断言桶数超阈值后旧键被逐出、磁盘状态仍在。

### 2.6 `/session/reset` 无鉴权；`/debug/dom` 暴露信息

- `/session/reset` 可被任意本地进程调用；`/debug/dom` 暴露 `sha1` 等页面信息。默认只监听 `127.0.0.1` 降低了风险，但同机多用户仍可触发。
- 建议：加可选 `RESET_TOKEN`（未设置则维持现状），`/debug/dom` 收窄返回或同样加 token。

---

## 3. 中优先级

### 3.1 `design.md` 默认值表与 `config.py` 漂移；`.env.example` 缺失

- `README` 配置表与 `config.py` 逐项一致（无问题）；漂移只在 `design.md`。
  注意：`config.py` 的默认值会被 `.env` 覆盖，运行时以 `.env` 为准
  （实测 `.env`：`STABLE_POLLS=5`、`LEN_STABLE_POLLS=8`、`MAX_SESSION_BUCKETS=3`、`BUCKET_LOCK_TIMEOUT_S=15`）。
  防漂移测试因此对照 `.env` 而非 `config.py` 字面量。
- 缺 `.env.example`，新用户无法一键起步。
- 建议：生成 `.env.example`（从 `config.py` 全量导出），同步 `design.md`，并加一个「防漂移」测试读 `config.py` 默认值比对文档。

### 3.2 `prompting._delta_piece` 在节点被整体替换时客户端内容错乱

- 网页版生成中可能重排/替换回复节点，`current` 不再以 `streamed` 为前缀。当前退回「公共前缀之后的部分」→ 旧尾巴不撤回、新内容追加。
- 建议：检测到非前缀替换时**停发增量**，等结束一次性给全量文本（或发「重置」语义），避免客户端拼接出重复内容。

### 3.3 `chat_io._send_chat_locked` 职责过多，结束判定难测

- 发送、轮询、`_has_pending_tokens`、到顶探测、stall 判定、代码块提取全在一个方法内，行为耦合、单测只能整体驱动。
- 建议：抽 `ReplyWatcher`（输入 page + 选择器，输出 `(text, blocks, reason)`），把结束判定独立成可注入时钟的纯逻辑对象。

### 3.4 `streaming.py` 工具模式 keep-alive 硬编码 10s

- 工具模式需先缓冲整段回复才吐字，期间客户端只能看到注释保活。超时硬编码，无法按客户端调。
- 建议：改 `CHAT_KEEPALIVE_S`（复用 `RESPONSES_KEEPALIVE_S` 风格），0 = 关闭。

### 3.5 测试缺口

- 无路由级测试；缺 `test_sessions.py` / `test_tasks.py` / `test_responses.py`；`prompting` 主路径、DSML 分支无独立覆盖。
- 建议：优先 `test_responses.py`（护住 2.2）与 `test_sessions.py`（护住 2.5），再加 `test_routes_*.py`。

### 3.6 缺交付物

- `.env.example`、`client_test.py`、`INSTALL.md`、`cmdlog.md` 均缺（`design.md` 曾要求）。
- 建议：`client_test.py` 用 `openai` SDK 打本地服务，覆盖 chat 与 responses。

---

## 4. 低优先级 / 代码质量

- **统一日志**：目前散落 `print`，无级别、无时间戳；改 `logging` 便于 DEBUG 开关与重定向。
- **`ModelCard.context_window`**：`SUPPORTED_MODELS` 已带，`/v1/models` 是否透出需核对；Pi 依赖它做裁剪。
- **`_sessions` 与 `_active_buckets` 命名相近**，易混；建议注释区分「状态缓存」与「运行中占用」。
- **`tasks.py` 冗余截断**：`record()` 已按 `TASK_KEEP_MESSAGES` + `_wrap_recent_item` 截断，`resume_block()` 再截一次——无害且快照有界，仅做可读性清理。
- **`.env` 行内注释**：当前解析器不支持 `KEY=value # 注释`，`.env` 里若有会污染值；建议要么支持行内 `#`，要么在文档里明确禁止。
- **`save_extracted_files` 扩展名猜测**：无语言时按 `import ` / `def ` 猜 `py`，对 Java/JS 误判；默认落 `txt` 更稳。

---

## 5. 安全与运维

- **`user_data/` 含登录 cookie**：`.gitignore` 已忽略，`README` 有「不提交」提示，但缺「勿备份/勿同步（iCloud/Dropbox 会带走登录态）」。
- **profile 单实例**：已给 `pkill` 提示；可在 `init()` 先探测 `user_data/SingletonLock`，给出更早更明确的失败。
- **`output/` 无清理**：并入 2.4 的保留策略。
- **`GEMINI_DEBUG` 日志**：轮询行不含正文，`_last_prompts` 不打印；建议在 `README` 明确承诺「DEBUG 日志不含消息正文」。

---

## 6. 建议落地顺序

| 顺序 | 项 | 理由 |
| --- | --- | --- |
| 1 | 2.1 惰性 import playwright + README 说明 | 新人第一步，零风险 |
| 2 | `.env.example` + `design.md` 同步 + 防漂移测试 | 交付缺口 |
| 3 | 2.2 `responses.py` 一致性 + `test_responses.py` | 直接影响 Codex 工具调用 |
| 4 | 2.3 `parse_tool_calls` 契约 + 重复消费修复 + 测试 | 工具调用正确性 |
| 5 | 2.4 `save_files` 默认 + `output/` 保留策略 | 消除默认副作用 |
| 6 | 2.5 会话 dict 有界化 | 长跑泄漏，局部可测 |
| 7 | 3.5 `test_sessions.py` / `test_routes_*.py` | 为后续重构护航 |
| 8 | 2.6 端点加固 | 安全，向后兼容 |
| 9 | 3.2 / 3.4 流式体验与可配置 | 体验项 |
| 10 | 3.3 抽 `ReplyWatcher` | 等测试就位后再动刀 |
| 11 | §4 质量项 + 3.6 联调件 | 择机批量 |

---

## 7. 结论

架构方向正确（播种取代 URL 恢复、任务快照、去动画取文），主要风险已从「能不能用」转为「长跑与协议细节」：**Responses 流式 id/索引** 与 **会话 dict 无界** 是两处最值得先修的实质缺陷；**惰性 import playwright** 与 **`.env.example`** 是最低成本的体验提升。建议按 §6 顺序推进，每步先补失败用例再修。

---

## 8. 落地进度（2026-10-04 本轮实施）

已完成并验证（`.venv/bin/python -m unittest discover -s tests -t .` → **153 用例通过，13 skip**）：

| 项 | 状态 | 关键改动 |
| --- | --- | --- |
| §2.1 惰性 import playwright | 完成 | `driver.py` 顶层不再 import；`init()` 内按需 import 并给出可读错误。系统 Python 现可 `import gemini_web` |
| §2.2 Responses id / output_index | 完成 | `responses.py` 工具分支：item id / call_id 全程复用、每个 function_call 单独发 `output_item.done`、message item 延迟到确定文本形态才发；新增 `tests/test_responses.py`（8 用例） |
| §2.3 parse_tool_calls 契约 | 完成 | `toolcalls.py` segment 截到下一个标记前，消除重复消费；docstring 写明「一行一调用」；新增 3 用例 |
| §2.4 save_files 默认副作用 | 完成 | `save_files` 默认改 `None`，回落 `config.SAVE_FILES`（默认 false）；新增 `OUTPUT_MAX_FILES` / `OUTPUT_MAX_AGE_DAYS` 与 `_prune_output_dir` |
| §2.5 会话 dict 无界 | 完成 | `session_store.py` 新增 `_evict_session_cache`，`MAX_SESSION_STATE_CACHE`（默认 64）LRU 逐出 `_sessions` / `_last_prompts`；默认桶与忙桶不逐出；新增 `tests/test_sessions.py`（4 用例） |
| §2.6 端点加固 | 完成 | `RESET_TOKEN` 门控 `/session/reset`（`X-Reset-Token` 头） |
| §3.1 配置/文档漂移 | 完成 | 新增 `.env.example`；修正 `design.md` 过期默认值；新增 `tests/test_doc_sync.py` 防漂移（对照 `.env`） |
| §3.2 `_delta_piece` 替换语义 | 完成 | 检测到非前缀替换时停发增量（不再追加），保留已发内容；更新 `tests/test_parsing.py` |
| §3.4 chat keep-alive | 完成 | 新增 `CHAT_KEEPALIVE_S`，替换 `streaming.py` 硬编码 10s |

**勘误（本轮实测）**：§3.1 原表述暗示可按 `config.py` 默认值对齐文档，但 `.env` 会覆盖
`config.py`，运行时以 `.env` 为准（实测 `STABLE_POLLS=5`、`LEN_STABLE_POLLS=8`、
`MAX_SESSION_BUCKETS=3`、`BUCKET_LOCK_TIMEOUT_S=15`）。防漂移测试因此对照 `.env`。

**未做（需真实环境或较大重构）**：
- §3.3 抽 `ReplyWatcher`：重构面大，建议在更多路由级测试就位后再动。
- §3.5 路由级测试（`test_routes_*.py`）：本轮补了 responses / sessions / doc_sync，路由级仍缺。
- §3.6 `client_test.py` / `INSTALL.md` / `cmdlog.md`：需真实登录环境。
- §4 低优先级质量项（统一 logging、`ModelCard.context_window` 透出等）。

**注意**：系统 `python` 未装 Playwright，跑测试请用 `.venv/bin/python`（本轮已通过惰性 import
让纯逻辑模块不再依赖该库，但 `unittest discover` 仍会加载 driver 相关测试）。
