# GeminiBridge 任务分解 (Pi / Codex 集成视角)

> **依据**：`doc/update_pi.md`（2026-10-07 工程与架构复审）  
> **状态标记**：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成（实现 + 验收测试齐备）  
> **自动化测试基线**：`.venv/bin/python -m pytest -q` → **340 passed, 18 skipped, 31 subtests passed**

---

## 阶段 1：核心正确性与稳定度 (P0)

- [x] **T1.1 修复流式回复静默丢尾与增量对账**
  - **说明**：分离增量发送状态与客户端当前持有状态，收尾时若内容不一致补发差额或清空重放，防止丢尾。
  - **状态**：已完成（实现于 `gemini_web/streaming.py` / `responses.py`，对应 T6.1）。
  - **验收**：`tests/test_streaming.py` 与 `tests/test_responses.py` 用例通过。

- [x] **T1.2 会话状态落盘原子化**
  - **说明**：采用同目录临时文件 + `os.replace` 原子写入，规避进程被 kill 时产生残缺 JSON 致状态静默归零的问题。
  - **状态**：已完成（实现于 `gemini_web/sessions.py`，对应 T6.2）。
  - **验收**：`tests/test_sessions.py::StateFileTests` 通过。

- [x] **T1.3 输入框富文本换行规范化容忍**
  - **说明**：新增 `_normalize_for_compare` 与 `_prompt_present`，消除编辑器丢换行引起的误判清空循环。
  - **状态**：已完成（实现于 `gemini_web/chat_io.py`，对应 T11.8）。
  - **验收**：`tests/test_end_detection.py::test_editor_normalized_readback_is_not_treated_as_residue` 通过。

- [x] **T1.4 信任成功 `fill()` 避免误重写**
  - **说明**：仅在 `fill()` 报错时走读回救援，信任成功的 `fill()`，不因渲染引起的微小长度差异否决结果。
  - **状态**：已完成（实现于 `gemini_web/chat_io.py`，对应 T11.9）。
  - **验收**：`tests/test_end_detection.py::test_lossy_readback_after_successful_fill_is_trusted` 通过。

---

## 阶段 2：Agent 深度集成与长文本爆页处理 (P1)

- [x] **T2.1 Agent System Prompt 精准过滤与 Task Goal 保护**
  - **说明**：在 `tasks.py` 播种快照截断时，精准过滤 Pi/Codex 注入的工具指引与元信息，保证原始 Goal 优先截留。
  - **状态**：已完成（实现于 `gemini_web/tasks.py`）。
  - **验收**：`tests/test_tasks.py` 覆盖 Goal 优先截留逻辑。

- [x] **T2.2 Tool Call 流式中间态 HTML/Markdown 强容错**
  - **说明**：在动画显显阶段对 Tool Call JSON 边界增加语法收尾对账与闭合守护，规避未闭合 JSON 导致的客户端解析抛错。
  - **状态**：已完成（实现于 `gemini_web/toolcalls.py`）。
  - **验收**：`tests/test_parsing.py::test_truncated_json_repair_with_missing_braces` 通过。

- [x] **T2.3 长文本多轮会话上限到顶自动无缝轮转**
  - **说明**：当触发 `SESSION_MAX_TURNS` / `SESSION_MAX_TOKENS` 时自动创建新会话并重新播种任务快照。
  - **状态**：已完成（实现于 `gemini_web/session_store.py` 和 `gemini_web/chat_io.py`）。
  - **验收**：`tests/test_sessions.py::test_session_over_budget_triggers_rotation` 及相关单元测试通过。

---

## 阶段 3：工程可运维性与配置防漂移 (P2)

- [x] **T3.1 配置与文档双向防漂移测试**
  - **说明**：保持 `gemini_web/config.py` 与 `.env.example` / `README.md` 参数说明自动对齐。
  - **状态**：已完成（实现于 `tests/test_doc_sync.py`）。
  - **验收**：`pytest tests/test_doc_sync.py` 测试通过。

- [x] **T3.2 `output/` 目录文件与临时日志定期清理**
  - **说明**：在后台线程/周期任务中以 `OUTPUT_PRUNE_INTERVAL_S` 清理过期的落盘代码文件与 Uvicorn 日志。
  - **状态**：已完成（实现于 Lifespan 周期任务）。
  - **验收**：`tests/test_sessions.py` 及相关测试通过。

- [x] **T3.3 网页 DOM 结构外置诊断与自愈日志**
  - **说明**：增强 `_composer_diag` 的 DOM 结构快照输出（包含 id、className、parent_tag、activeElement 及 caret_in_composer 等），方便输入框或发送按钮改版时快速排查。
  - **状态**：已完成（实现于 `gemini_web/chat_io.py`）。
  - **验收**：在输入框定位失败或写入异常时，日志中能完整输出包含 DOM 属性与层级关系的 JSON 结构。
