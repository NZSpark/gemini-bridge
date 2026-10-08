# GeminiBridge 任务分解 (参考 chatgpt-bridge 架构改进方案)

> **依据**：`doc/update_pi.md`（2026-10-07 分析与对比改进建议）  
> **状态标记**：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成（实现 + 验收测试齐备）  
> **自动化测试基线**：`.venv/bin/python -m pytest -q`  

---

## 阶段 1：流式传输对账与收尾文本补全 (P0)

- [x] **T1.1 流式节点替换/回退时的文本强制对账与差额补全**
  - **说明**：借鉴 `chatgpt-bridge` 的流式收尾防护逻辑。当 Gemini Web 在生成中途发生节点替换时，记录客户端已收到的文本 prefix，在生成结束时强制与最终 `reply_content` 进行对账；若有未送达差额，补发增量或按照 OpenAI SSE 纠错机制补全，防止流式文本静默丢尾。
  - **验收**：`tests/test_streaming.py` 单元测试及补全逻辑校验通过。

---

## 阶段 2：页面池管理与 Context 韧性增强 (P1)

- [x] **T2.1 页面健康度检测与空闲期优雅刷新 (Page Rot/Refresh)**
  - **说明**：参考 `chatgpt-bridge` 建立 Context/Page 的生命周期与健康监控。对闲置页面增加轻量探活，当请求次数达到上限或内存过高时在空闲期自动刷新 Context，规避 Playwright 长时间运行卡死。
  - **验收**：页面池生命周期与探活刷新逻辑实现齐备。

- [x] **T2.2 并发隔离与锁超时死锁保护**
  - **说明**：优化多桶并发情况下的页面分配与锁管理，对页面借用与释放增加超时保护和日志追踪，防止单一异常请求阻塞整个 Bridge 服务。
  - **验收**：通过并发与锁超时保护验证。

---

## 阶段 3：工具调用与 Token 预算优化 (P1)

- [x] **T3.1 内置工具按需注入 (On-Demand Tool Injection)**
  - **说明**：优化 `EDIT_MARKDOWN_LOCAL` 的注册逻辑。仅在客户端请求中显式包含 `tools` 参数时才注入 `edit_markdown` 及工具强调 Prompt，避免纯文本交互请求无谓多消耗 ~900+ Tokens。
  - **验收**：`should_register_edit_markdown` 单元测试通过，纯文本请求不再触发工具脚手架注入。

---

## 阶段 4：可观测性与异常诊断 (P2)

- [x] **T4.1 全仓结构化日志重构 (Structured Logging)**
  - **说明**：全面清理代码中的 `print` 输出，替换为 Python 标准 `logging` 模块；日志格式统一包含时间戳、日志级别及 `session_id`/`bucket` 追踪标识，并通过 `GEMINI_DEBUG` 控制输出级别。
  - **验收**：`gemini_web/logging_setup.py` 统一接管日志面，测试集中无散落 print。

- [x] **T4.2 强化 DOM 诊断快照 Dumps**
  - **说明**：完善 `_composer_diag` 诊断机制，在定位输入框或发送按钮失败/超时时，自动保存包含 DOM 属性 JSON 及界面截图至 `/debug` 目录。
  - **验收**：输入框定位与写入异常诊断 dump 功能齐备。

---

## 阶段 5：工程规范与测试守护 (P2)

- [x] **T5.1 依赖版本固定与 GitHub Actions CI 流水线**
  - **说明**：配置 `pyproject.toml` 固定依赖版本范围，并集成自动化测试与质量守护。
  - **验收**：`pyproject.toml` 配置与测试流构建完成。

- [x] **T5.2 全量配置防漂移双向守护测试**
  - **说明**：扩充 `tests/test_doc_sync.py`，自动提取 `config.py` 中的全部配置键，检查 `.env.example` 与 `README.md` 的说明同步，缺项即报警。
  - **验收**：`pytest tests/test_doc_sync.py` 测试通过，双向守护生效。