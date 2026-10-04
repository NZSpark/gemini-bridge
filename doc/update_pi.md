# 项目分析与改进建议 (pi 集成与整体架构视角)

## 1. 项目概览与现状分析

GeminiBridge 是将 Gemini 网页版通过 Playwright 包装为 OpenAI 兼容接口（含 /v1/chat/completions 与 /v1/responses）的本地桥接服务。项目针对工具调用（function calling）模拟、流式 SSE 传输、会话隔离与任务快照续接等进行了专门设计。

当前状态：
- 核心模块完整，全套单元测试运行通过（193 passed, 19 skipped）。
- 已新增 gemini_web/markdown_io.py 模块，为代码块与 Markdown 读写提供结构化安全解析能力。
- 与 Pi Coding Agent (openai-completions API) 与 Codex CLI (responses Wire API) 实现无缝集成。

以下针对配合 Pi / Codex CLI 等 Coding Agent 长时间运行的稳定性与拓展性，提出系统性的改进建议。

---

## 2. 关键改进建议

### 2.1 工具调用与响应稳定度 (High Priority)
1. Responses API 事件 ID 与索引一致性：
   - 在 Responses API (/v1/responses) 流式输出中，确保 item_id、call_id 以及分片索引递增在整个响应周期中保持完全一致，避免 Pi/Codex 在并发或长文本生成时出现状态混乱。
2. Tool Call JSON 语法去容错与收尾校验：
   - Gemini 逐 token 动画显显过程中，inner_text() 可能偶发截断。虽然 _complete_text 已剔除 .pending/.animating 状态，但建议在流式收尾（End-of-Response）时增加一层 JSON 语法结构强校验，遇到括号不匹配时补充闭合或回退为普通文本，防止客户端 JSON 解析报错。

### 2.2 会话管理与内存容量控制 (Medium Priority)
1. 内存状态有界化 (LRU Eviction)：
   - SessionStore 中的会话状态缓存（如 _sessions、_locks）需确保严格受 MAX_SESSION_STATE_CACHE 约束，自动清理长期未使用的空闲会话与锁对象，防止长跑进程出现内存泄露。
2. output/ 目录落盘文件定期清理：
   - SAVE_FILES=true 时，生成的代码块会落盘到 output/。需在服务启动与后台定期任务中执行 OUTPUT_MAX_FILES 与 OUTPUT_MAX_AGE_DAYS 清扫逻辑，避免磁盘文件过度堆积。

### 2.3 任务快照与 Agent Prompt 优化 (Medium Priority)
1. Agent System Prompt 智能过滤与任务目标保护：
   - Pi Agent 会注入大量的环境元信息（如 current working dir、skills、permissions、docs 指引）。在 tasks.py 任务快照捕获与轮转播种时，需精确过滤掉重复的工具指引与系统头，确保 goal（第一条真正的用户指令）在 SEED_MAX_CHARS 预算截断中享有最高优先级，绝不被截断。
2. 会话上限到顶自动无缝轮转：
   - 当对话达到 SESSION_MAX_TURNS 或 SESSION_MAX_TOKENS 时，触发新会话重置并通过 tasks.py 自动播种历史任务快照，实现 Agent 无感续接。

### 2.4 工程质量与配置同步 (Low Priority)
1. 配置防漂移机制 (.env.example)：
   - 维护完整的 .env.example，与 gemini_web/config.py 中的全部配置项对齐，并通过 tests/test_doc_sync.py（或独立 test_config_drift.py）自动校验配置项一致性。
2. 选择器外置与回退链完善：
   - 将 Gemini 网页版最新变化的 DOM 选择器（输入框、发送按钮、停止按钮、动画类名）保持在 .env 外置配置中，进一步降低 Gemini 网页版改版对核心代码的冲击。

---

## 3. 实施优先级与路线图

| 任务/阶段 | 说明 | 优先级 | 对应任务卡 |
| --- | --- | --- | --- |
| Phase 1 | 修复 Responses 流式 ID/索引一致性 & 校验 Tool Call 结尾语法 | P0 | T3.2, T4.3 |
| Phase 2 | 完善 tasks.py 快照注入中的 Agent 元信息过滤与 Goal 优先保护 | P1 | T1.5, T6.2 |
| Phase 3 | 会话缓存有界 LRU 逐出与 output/ 文件自动清理 | P1 | T2.1, T7.3 |
| Phase 4 | 补全 .env.example 与配置防漂移测试 | P2 | T0.3, T6.5 |
