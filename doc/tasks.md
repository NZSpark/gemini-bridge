# GeminiBridge 任务分解（v4）

依据 doc/update_pi.md 与当前项目实际进展重新梳理拆分。状态标记：[ ] 未开始、[~] 进行中、[x] 完成（实现 + 验收测试齐备）。

核对时间：2026-10-04；基线：pytest 全绿（193 passed, 19 skipped）。

阶段 0：基础与配置

[x] T0.1 仓库骨架：gemini_web/ 包、tests/、output/、__init__.py、requirements.txt。

[x] T0.2 config.py：全键 GEMINI_*，|| 回退链解析，布尔/数值容错。

[x] T0.3 配置防漂移与对齐：补齐 .env.example，与 gemini_web/config.py 全部参数对齐；新增配置漂移自动化测试。

阶段 1：核心逻辑与转换

[x] T1.1 models.py：OpenAI 兼容 Pydantic 模型，extra="allow"。

[x] T1.2 prompting.py：messages → 输入框文本、token 估算、含测试 tests/test_prompting.py。

[x] T1.3 toolcalls.py：工具注入 + TOOL_CALL 解析。

[x] T1.4 streaming.py：SSE 编码，首 chunk 带 role，末 chunk 带 finish_reason。

[x] T1.5 markdown_io.py：Markdown 读写、围栏扫描与代码块结构化提取。

阶段 2：驱动与 DOM 交互

[x] T2.1 driver.py：Playwright 浏览器驱动、READY_SELECTOR、NEW_CHAT_SELECTOR。

[x] T2.2 chat_io.py 发送：DOM 事件派发（_dispatch_enter / _click_send_button / _submit_prompt），不依赖窗口焦点。

[x] T2.3 代码块提取保真：_strip_code_noise() 保留首尾空白，仅剥语言标签行与 Copy/Download 行。

阶段 3：服务接口与流式响应

[x] T3.1 /v1/models、/v1/chat/completions：支持流式与非流式。

[x] T3.2 /v1/responses：Wire API 映射；支持流式与非流式 Responses API 及 item_id / call_id 一致性。

[x] T3.3 路由与监控：/healthz、/session/reset、/debug/dom 端点。

阶段 4：Pi / Coding Agent 增强 (依据 doc/update_pi.md)

[x] T4.1 Responses API ID/索引强一致性：流式事件输出中严格保持 item_id、call_id 和分片 index 递增一致。

[x] T4.2 Tool Call 结尾语法强校验：收尾阶段校验 JSON 完整性，修正截断或回退普通文本。

[x] T4.3 会话缓存有界 LRU 逐出：SessionStore 的 _sessions 与 _locks 引入上限逐出机制，防止内存增长。

[x] T4.4 output/ 自动清理：服务启动及后台周期任务中，执行 OUTPUT_MAX_FILES 与 OUTPUT_MAX_AGE_DAYS 清理。

[x] T4.5 任务快照 Agent 元素过滤：tasks.py 提取快照时过滤 Pi 重复的 System/Skill 模板，确保用户真实 goal 处于截断保护首位。

[x] T4.6 会话上限自动无缝轮转：达到 SESSION_MAX_TURNS 或 SESSION_MAX_TOKENS 时自动初始化新会话并播种任务快照。

阶段 5：文档与规范

[x] T5.1 doc/design.md 同步：校对并修正设计文档中的默认配置参数说明。

[x] T5.2 契约样例：增加 /v1/responses 的请求/响应示例集成测试。

推荐执行顺序

Phase 1: 执行 T4.1 & T4.2（提升 Responses API 与 Tool Call 稳定性）

Phase 2: 执行 T4.3 & T4.4（内存与磁盘资源防膨胀清理）

Phase 3: 执行 T4.5 & T4.6（Pi 任务快照与长会话无缝轮转）

Phase 4: 执行 T0.3 & T5.1（配置防漂移测试与文档同步）
