# 项目分析与改进建议 (pi 集成视角)

## 1. 项目概览与现状分析

GeminiBridge 是将 Gemini 网页版通过 Playwright 包装为 OpenAI 兼容接口（含 /v1/chat/completions 与 /v1/responses）的本地桥接服务。项目架构清晰，针对工具调用（function calling）模拟、流式传输、会话隔离与任务快照续接等进行了专门设计。

目前单元测试覆盖率较高（140 项通过），核心功能已就绪。但从配合 pi (coding agent) 的实际使用体验来看，仍存在部分优化空间与稳定性隐患。

---

## 2. 关键改进建议

### 2.1 工具调用与响应稳定度 (High Priority)
1. Responses API 事件 ID 与索引一致性：
   - 在 Responses 流式输出中，item_id 与 call_id 存在不一致现象，可能导致客户端在解析工具调用分片时发生状态混乱或上下文错位。
2. Tool Call JSON 截断与去容错：
   - Gemini 网页版 DOM 在逐字渲染时可能存在未显现 token，导致 inner_text() 取到不完整文本。需要确保 _complete_text 去除动画样式，并在流收尾时校验 JSON 括号闭合。

### 2.2 会话管理与资源清理 (Medium Priority)
1. 内存状态有界化 (LRU Eviction)：
   - _sessions 与 _locks 等状态字典缺乏严格的内存容量上限，长时间运行或频繁更换 Session Key 可能导致无界增长。建议引入有界 LRU 逐出策略。
2. 落盘文件保留策略：
   - 默认开启或频繁保存的输出代码文件应默认受 OUTPUT_MAX_FILES 和 OUTPUT_MAX_AGE_DAYS 管辖，启动或定期执行自动清理，防止 output/ 膨胀。

### 2.3 任务快照与提示词注入优化 (Medium Priority)
1. System Prompt 截断与包装过滤：
   - pi 等 Agent 会自动注入大量的环境元信息（如 cwd、skills、permissions）。在 tasks.py 快照提取时，需进一步精确识别并过滤元信息，避免轮转播种时系统提示无限膨胀。
2. 播种与任务目标保护：
   - 确保 SEED_MAX_CHARS 截断时，goal （第一条真实 user 消息）绝对优先被保留，不被长上下文挤压。

### 2.4 可维护性与配置同步 (Low Priority)
1. 配置示例 .env.example 补全：
   - 创建并补全 .env.example，与 gemini_web/config.py 中的配置项保持严格一致，并增加配置防漂移单元测试。
2. 文档同步：
   - 更新 doc/design.md 中的配置默认值描述，避免手写配置数值与代码实现不同步。

---

## 3. 实施计划与优先级

| 任务/阶段 | 说明 | 优先级 |
| --- | --- | --- |
| Phase 1 | 修复 Responses 流式 ID/索引一致性 & 补全 .env.example | P0 |
| Phase 2 | 优化会话 LRU 逐出与资源自动清理 (output/) | P1 |
| Phase 3 | 加强工具调用解析去错与任务快照过滤逻辑 | P1 |
| Phase 4 | 补全配置同步测试与 doc/design.md 文档清理 | P2 |
