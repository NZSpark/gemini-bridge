# 项目分析与改进建议 (Pi / Codex CLI 集成与演进视角)

> **文档版本**：2026-10-07（基于 T11.8/T11.9 输入框容忍与信任机制完全落地后）  
> **自动化测试基线**：`.venv/bin/python -m pytest -q` → **340 passed, 18 skipped, 31 subtests passed**  
> **适用视角**：针对 Pi / Codex 等 Agent Harness 运行多轮长文本与大 Payload 场景的系统级分析

---

## 1. 项目概览与当前工程基线

GeminiBridge 将 Gemini 网页版（gemini.google.com）通过 Playwright 无头/有头浏览器包装为 OpenAI 兼容的 API 服务（同时提供 `/v1/chat/completions` 与 `/v1/responses` 接口）。项目重点解决了 Web 交互中的 DOM 差异、分块写入、流式输出补全、会话分桶隔离以及 Agent 任务快照重置续接。

### 1.1 最新重大突破（T11.8 / T11.9）
- **输入框写入双重优化**：针对超大 Prompt（如 36K 字符的 `read doc/update.md` 场景），确定了以 `fill()` 为第一优先级的输入策略，同时重构了读回校验机制 `_normalize_for_compare` 与 `_prompt_present`。
- **容忍与信任机制**：
  1. **容忍富文本编辑器空白规范化**：富文本编辑器将换行渲染为块级节点导致 `textContent` 丢换行，新机制可正确判定已写入文本，彻底消除「写入 -> 误判 False -> 清空 -> 循环归零」的自我毁灭循环。
  2. **信任成功的 `fill()`**：将校验严格限制在错误路径上，不再因编辑器 Markdown 渲染带来的微小长度差异（如 9967 字符读回 9834）而否决已成功的 `fill()`，确保长 Prompt 落地百分之百可靠。

---

## 2. 深度分析与后续演进建议

### 2.1 Agent 交互与大 Payload 极限处理 (P0 / High Priority)
1. **流式增量与工具调用语法缓冲对账**：
   - 在高并发或极快速响应下，Gemini 动画显显节点可能产生中间非 JSON 状态。当前已包含 `_complete_text` 过滤，建议在 Pi Agent 的 tool call 解析层保持对 HTML 实体解码与 Markdown 代码块闭合的强鲁棒容错。
2. **会话爆上限时的平滑续接与上下文剪裁**：
   - 当对话达到 `SESSION_MAX_TURNS` 或 `SESSION_MAX_TOKENS` 时，系统会自动触发会话轮转。应持续优化 `tasks.py` 对 Agent System Prompt（如 Pi 注入的各种 skills、permissions、cwd）的精准过滤，保证最初的 Task Goal 始终在 `SEED_MAX_CHARS` 截断预算中拥有最高优先级。

### 2.2 网页 DOM 变异防御与稳定性增强 (P1 / Medium Priority)
1. **选择器外置与多级回退**：
   - 目前 `SEND_BUTTON_SELECTORS` 与输入框选择器已支持从环境变量外置读取，后续可继续完善 DOM 变化时的自动感知与诊断日志输出（如 `_composer_diag`），提升有头/无头模式切换时的自愈能力。
2. **`output/` 与日志文件的定时清理**：
   - 随着 Agent 密集读取和产生代码块，`output/` 下的落盘文件及临时 Uvicorn 日志增长迅速。确保后台 `OUTPUT_PRUNE_INTERVAL_S` 任务在长时间常驻时持续稳定运行。

### 2.3 测试套件与工程规范 (P2 / Quality Guard)
1. **测试用例隔离与离线守护**：
   - 保持常规单元测试（340 passed）不发起任何真实网络请求；E2E 链路继续通过 `GEMINI_E2E=1` 环境变量隔离控制。
2. **配置双向漂移防护**：
   - 持续运行 `tests/test_doc_sync.py`，保持 `gemini_web/config.py` 与 `.env.example` / `README.md` 中的参数说明时刻同步。

---

## 3. 已落地改进对照表 (T1.0 ~ T11.9)

| 模块 / 阶段 | 核心改动 | 状态 | 测试验证 |
| --- | --- | --- | --- |
| **流式与响应 (T6.1)** | 修复流式回复静默丢尾，分离增量发送与持有状态 | ✅ 完成 | `tests/test_streaming.py` |
| **状态落盘 (T6.2)** | 状态写入采用临时文件 + `os.replace` 原子替换 | ✅ 完成 | `tests/test_sessions.py` |
| **输入写入 (T11.8)** | 容忍富文本编辑器空白规范化，消除误清空循环 | ✅ 完成 | `tests/test_end_detection.py` |
| **信任机制 (T11.9)** | 信任成功 `fill()`，有损读回仅记录 INFO，不否决成功 | ✅ 完成 | `tests/test_end_detection.py` |
| **配置与文档 (T10.x)** | 配置防漂移测试、E2E 测试设计与 Tasks 卡同步 | ✅ 完成 | `tests/test_doc_sync.py` |
