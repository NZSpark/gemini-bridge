# GeminiBridge 任务规划与状态记录（Codex 版）

> 关联文档：`doc/update_codex.md` / `doc/update.md` / `doc/tasks.md`  
> 更新日期：2026-10-08

本文档基于 `doc/update_codex.md` 的分析与建议，将项目改进项拆分为具体的任务模块，并记录当前最新完成状态与后续计划。

---

## 1. 任务概览与状态汇总

| 任务 ID | 任务名称 | 优先级 | 关联模块/文件 | 当前状态 |
| --- | --- | --- | --- | --- |
| **T1** | Playwright 惰性导入 | P0 (高) | `gemini_web/__init__.py`, `driver.py` | ✅ 已完成 |
| **T2** | Responses 流式事件 ID/索引对齐 | P0 (高) | `gemini_web/responses.py`, `tests/test_responses.py` | ✅ 已完成 |
| **T3** | Tool Call 解析与边缘情况加固 | P0 (高) | `gemini_web/toolcalls.py`, `tests/test_toolcalls.py` | ✅ 已完成 |
| **T4** | `output/` 目录清理与副作用控制 | P0 (高) | `gemini_web/config.py`, `gemini_web/server.py` | ✅ 已完成 |
| **T5** | 会话内存字典 LRU 逐出机制 | P0 (高) | `gemini_web/session_store.py`, `tests/test_sessions.py` | ✅ 已完成 |
| **T6** | 端点安全加固 (`RESET_TOKEN` & `BRIDGE_TOKEN`) | P0 (高) | `gemini_web/server.py`, `tests/test_auth.py` | ✅ 已完成 |
| **T7** | 配置与文档防漂移测试 | P1 (中) | `.env.example`, `doc/design.md`, `tests/test_doc_sync.py` | ✅ 已完成 |
| **T8** | `_delta_piece` 增量替换逻辑优化 | P1 (中) | `gemini_web/markdown_io.py`, `tests/test_parsing.py` | ✅ 已完成 |
| **T9** | SSE Chat 心跳流拉取配置化 | P1 (中) | `gemini_web/streaming.py`, `gemini_web/config.py` | ✅ 已完成 |
| **T10** | 路由级与鉴权 API 测试覆盖 | P1 (中) | `tests/test_routes_chat.py`, `tests/test_routes_responses.py` | ✅ 已完成 |
| **T11** | 全仓日志规范统一 (Logging Standard) | P2 (低) | `gemini_web/logging_setup.py`, 全仓 `print` | ✅ 已完成 |
| **T12** | ModelCard `context_window` 暴露 | P2 (低) | `gemini_web/models.py`, `/v1/models` | ✅ 已完成 |
| **T13** | E2E 测试隔离与系统环境兼容 | P1 (中) | `tests/e2e/`, `tests/test_e2e_harness.py` | ⏳ 进行中 (单测隔离已完成，E2E 需 Playwright) |
| **T14** | `ReplyWatcher` 模块重构抽离 | P2 (低) | `gemini_web/chat_io.py` | ⏸️ 暂缓 (需等待更多底层链路测试就位) |

---

## 2. 任务详细拆解与具体进展

### T1. 驱动库惰性导入 (P0)
- **目标**：解决系统 Python 环境未安装 Playwright 时 `import gemini_web` 即直接报 `ImportError` 的问题。
- **状态**：✅ 已完成
- **改动**：
  - 移除了 `gemini_web/driver.py` 顶层的 `from playwright.async_api import async_playwright`。
  - 改为在 `Driver.init()` 内部延迟加载，并在缺失依赖时抛出友好提示。

### T2. Responses 协议流式事件规范化 (P0)
- **目标**：修正 SSE `responses.py` 流式输出中 `call_id` 与 `item_id` 不一致、多工具调用时 `output_index` 重叠且缺少 `output_item.done` 事件的问题。
- **状态**：✅ 已完成
- **改动**：
  - 重构 `responses.py` 工具调用流式分支，实现全程复用 `(item_id, call_id)`。
  - 确保每个 `function_call` 正确单独触发 `output_item.done` 事件并精准匹配索引。
  - 新增 `tests/test_responses.py` 覆盖 8 个测试用例。

### T3. Tool Call 解析容错与多参数挽救 (P0)
- **目标**：修复 `parse_tool_calls` 中无对象标记重复消费后续调用的 Bug，并支持 Raw JSON 及多参数挽救（Multi-arg salvage）。
- **状态**：✅ 已完成
- **改动**：
  - `toolcalls.py` 将截取 segment 限制在下一个标记之前，消除重复消费漏洞。
  - 支持针对提取异常的兼容恢复逻辑。
  - 新增 `tests/test_toolcalls.py` 单元测试用例。

### T4. 文件落盘与 `output/` 自动清理 (P0)
- **目标**：限制默认文件落盘副作用，并提供 `output/` 目录生命周期轮转。
- **状态**：✅ 已完成
- **改动**：
  - `save_files` 默认调整为 `None`（回落至配置 `SAVE_FILES=False`）。
  - 新增 `prune_output_dir` 工具函数并接入 `server.py` 启动与后台定时任务，按数量/过期时间清理。

### T5. 会话内存状态 LRU 逐出控制 (P0)
- **目标**：防止多会话长时间运行导致 `_sessions` 和 `_last_prompts` 内存增长无界。
- **状态**：✅ 已完成
- **改动**：
  - 在 `session_store.py` 中增加 `_evict_session_cache` LRU 逐出逻辑，受 `MAX_SESSION_STATE_CACHE`（默认 64）控制。
  - 新增 `tests/test_sessions.py` 进行验证。

### T6. API 访问控制与端点加固 (P0)
- **目标**：防止敏感端点（如 `/session/reset` 及生成端点）被未经授权调用。
- **状态**：✅ 已完成
- **改动**：
  - 增加 `RESET_TOKEN`（请求头 `X-Reset-Token`）校验。
  - 增加可选 `BRIDGE_TOKEN`（Bearer Auth）保护 API 生成端点，并补全 `tests/test_auth.py`。

### T7. 配置与文档自动化同步测试 (P1)
- **目标**：防止 `.env` 配置项与代码默认值、设计文档脱节。
- **状态**：✅ 已完成
- **改动**：
  - 提供标准的 `.env.example` 模板。
  - 编写 `tests/test_doc_sync.py` 自动化检测配置漂移。

### T8. Markdown 增量替换与解析流优化 (P1)
- **目标**：处理前端 Markdown 渲染过程中非单纯追加模式（如局部重写）导致的文本重复追加问题。
- **状态**：✅ 已完成
- **改动**：
  - 优化 `markdown_io.py` 中的 `_delta_piece` 逻辑，识别非前缀替换并进行截断处理。

### T9. SSE 心跳机制配置化 (P1)
- **状态**：✅ 已完成
- **改动**：引入 `CHAT_KEEPALIVE_S` 参数，替代 `streaming.py` 中硬编码的 10 秒等待。

### T10. 路由层接口测试与模型卡片信息暴露 (P1 & P2)
- **状态**：✅ 已完成
- **改动**：
  - 增加 `tests/test_routes_chat.py` 与 `tests/test_routes_responses.py`。
  - `/v1/models` 端点透出 `context_window` 字段，数值与 `SESSION_MAX_TOKENS` 对齐。

### T13. E2E 测试环境隔离与执行说明 (P1)
- **目标**：防止直接运行 `pytest` 时因缺少 Playwright 导致整体 unittest/pytest 跑板报错。
- **现状**：
  - 单元测试（333 个测试用例）在纯 Python 环境下全部顺利通过。
  - E2E 测试包含 `tests/e2e/test_parity.py` 和 `tests/test_e2e_harness.py`，依赖 `playwright` 库及浏览器内核。
- **后续计划**：
  - 在 pytest 命令行配置或 CI 配置中显式排除 `tests/e2e`（使用 `pytest --ignore=tests/e2e` 或通过环境变量 `GEMINI_E2E=1` 触发）。

---

## 3. 测试验证基线

- **单元测试（不含 E2E）**：
  ```bash
  pytest --ignore=tests/e2e --ignore=tests/test_e2e_harness.py
  ```
  *结果*：**333 passed** (1.45s)
- **完整测试（含 Playwright E2E 环境）**：
  ```bash
  .venv/bin/python -m pytest
  ```
  *结果*：**279 passed, 19 skipped**
