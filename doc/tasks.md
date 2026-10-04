# GeminiBridge 任务分解（v3）

依据 `doc/update.md`（Markdown 读写修改支持建议稿，含围栏代码块处理）重新拆分。v1/v2 的阶段 0–8 保留原编号与历史状态，新增阶段 9 为 Markdown IO 能力。状态标记：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成（实现 + 验收测试齐备，或经实测核对）。

核对时间：2026-10-04；基线：`python -m unittest discover -s tests -t .` 全绿（含 `_strip_code_noise` 与 DOM 派发发送改动）。

---

## 阶段 0：骨架与配置

- [x] **T0.1 仓库骨架**：`gemini_web/` 包、`tests/`、`output/`、`__init__.py`、`requirements.txt`。
- [x] **T0.2 `config.py`**：全键 `GEMINI_*`，`||` 回退链解析，布尔/数值容错。
- [~] **T0.3 配置一致性**：`.env.example` + `design.md` 默认值同步；防漂移测试（→ T6.5）。

## 阶段 1：纯逻辑模块

- [x] **T1.1 `models.py`**：OpenAI 兼容 Pydantic 模型，`extra="allow"`。
- [~] **T1.2 `prompting.py`**：messages → 输入框文本；缺独立 `test_prompting.py`。
- [x] **T1.3 `toolcalls.py`**：工具注入 + `TOOL_CALL` 解析；DSML 分支缺口 → T6.4。
- [x] **T1.4 `streaming.py`**：SSE 编码，首 chunk 带 `role`，末 chunk 带 `finish_reason`。
- [~] **T1.5 `tasks.py`**：会话桶任务快照；缺 `test_tasks.py`。

## 阶段 2：会话与驱动

- [~] **T2.1 `driver.py` 骨架**：`launch_persistent_context`、`READY_SELECTOR`、`NEW_CHAT_SELECTOR`。
- [x] **T2.2 `chat_io.py` 发送**：DOM 事件派发（`_dispatch_enter` / `_click_send_button` / `_submit_prompt`），不依赖窗口焦点；移除 `bring_to_front` / `keyboard.press`。
- [x] **T2.3 代码块提取保真**：`_strip_code_noise()` 保留首尾空白，仅剥语言标签行与 Copy/Download 行。
- [~] **T2.4 输入框定位重试**：DOM 查询重试已就位；无独立假 page 测试。

## 阶段 3：HTTP 与响应

- [x] **T3.1 `/v1/models`、`/v1/chat/completions`**：含流式与非流式。
- [~] **T3.2 `/v1/responses`**：`ENABLE_RESPONSES_API` 控制；契约固化 → T5.2。
- [x] **T3.3 错误映射**：OpenAI 兼容 `error` 结构。
- [~] **T3.4 `/healthz`、`/session/reset`、`/debug/dom`**：已有；路由测试待补（→ T9.7 相关）。

## 阶段 4：工具调用与流式

- [x] **T4.1 模拟 function calling**：注入提示词 + 解析为 `tool_calls`。
- [x] **T4.2 SSE 逐块输出**：首块 `role`、末块 `finish_reason`、`[DONE]`。
- [~] **T4.3 工具流式**：已有；边界（截断 JSON 修复）待测。

## 阶段 5：文档与契约

- [ ] **T5.1 README 排查条目修订**：把“有头模式失焦”一条从 `bring_to_front()` / `focus()` 改为“DOM 事件派发、不依赖焦点”，并补 `SEND_BUTTON_SELECTORS`。
- [ ] **T5.2 Responses 契约固化**：为 `/v1/responses` 写请求/响应样例测试。
- [~] **T5.3 `doc/design.md` 去数字**：默认值改为引用 `config.py`，不再手抄。

## 阶段 6：测试补全（既有缺口）

- [ ] **T6.1 `test_prompting.py`**：多角色/多轮/无 system/空历史 + 播种截断。
- [ ] **T6.2 `test_tasks.py`**：写入/读取/截断/开关/goal 自愈。
- [ ] **T6.3 播种截断用例**：与 T6.1 合并。
- [ ] **T6.4 DSML 分支**：`toolcalls.py` 原生 DSML 标记回退解析用例。
- [ ] **T6.5 配置防漂移测试**：`.env.example` 与 `config.py` 键集一一对应。
- [ ] **T6.6 选择器回退链**：假 page 下 `INPUT_SELECTORS` 命中并发送。

## 阶段 7：运维与健壮性

- [ ] **T7.1 后台节流缓解**：评估 Chromium `--disable-background-timer-throttling` 等开关是否纳入。
- [ ] **T7.2 HEADLESS 默认策略**：文档化“首登有头、长跑无头”。
- [ ] **T7.3 会话到顶轮转**：补测“到顶 → 下次轮转新会话”。

## 阶段 8：历史遗留清理

- [ ] **T8.1 `.env.example` 补齐**：与 `config.py` 对齐。
- [ ] **T8.2 `design.md` §4 过期默认值**：8 项校正。

---

## 阶段 9：Markdown IO 能力（新增，源自 `doc/update.md`）

目标：提供“可定位、可校验、可回滚”的 Markdown 读写修改能力，正确处理 ``` 围栏代码块（json / bash / text / python 等）。新增 `gemini_web/markdown_io.py`（纯逻辑，无浏览器依赖）+ `tests/test_markdown_io.py`。

### 9.1 读取与围栏扫描

- [ ] **T9.1 `read_md(path) -> MdDoc`**：按行读入，保留 `\n`/`\r\n` 风格与是否尾换行。
  - 验收：`test_markdown_io.py` 覆盖 LF/CRLF、有/无尾换行、空文件。
- [ ] **T9.2 围栏扫描器**：逐行状态机，产出 `fence_ranges` 与 `in_fence[line]`。
  - 规则：开启 ≥3 反引号或 `~~~`，行首 ≤3 空格，信息串为语言标签；闭合需同字符且长度 ≥ 开启长度。
  - 验收：单/多块、无标签、加长围栏（```` 内嵌 ```）、缩进 1–3 空格、波浪号混用、未闭合报错。
- [ ] **T9.3 `text_lines(doc) -> list[Line]`**：每行带 `in_fence` / `lang` / `indent` / 是否空行。
  - 验收：围栏内 `# 标题`、`Copy` 行被正确标记为 fence 内。

### 9.2 定位

- [ ] **T9.4 `locate(doc, anchor)`**：支持行号区间 / 标题锚点 / 围栏锚点 / 唯一片段。
  - 标题与片段匹配必须跳过 `in_fence` 行；多命中报错并列候选行号。
  - 验收：四种锚点各一例；围栏内 `#` 不被当标题；非唯一片段报错。

### 9.3 编辑与写回

- [ ] **T9.5 `apply_edit(doc, start, end, new_text)`**：只替换命中区间，区间外原样拷贝。
  - 验收：区间外字节不变；换行风格与尾换行保持。
- [ ] **T9.6 `write_md(doc, path, dry_run=False)`**：临时文件 + 原子重命名；返回统一 diff；`dry_run` 不落盘。
  - 验收：写回后与预期字节一致；`dry_run` 不改文件。

### 9.4 校验与回滚

- [ ] **T9.7 `verify(doc) -> list[Issue]`**：围栏配对、锚点唯一、语言标签保留、围栏计数不漂移。
  - 验收：未闭合围栏、语言标签丢失、计数漂移各报 Issue。
- [ ] **T9.8 围栏长度安全**：新块内含 ``` 时自动升级外层围栏。
  - 验收：注入含 ``` 的内容后 `fence_ranges` 仍成对。
- [ ] **T9.9 备份与回滚**：写回前快照到 `output/backups/`（带时间戳），支持回滚。
  - 验收：回滚后文件字节与快照一致。

### 9.5 模型接入

- [ ] **T9.10 `generate_edit(doc, instruction, llm)`**：把“带行号 + 围栏边界标注的视图”发给模型，要求返回 `start/end/new_text`。
  - 视图需显式标注围栏边界（如 `L12 ```json` / `L20 ````）。
  - 解析复用 `toolcalls.py` 的 `TOOL_CALL` 通道，不新增协议。
  - 验收：假 LLM 返回三元组后正确落盘；返回越界/错行号时拒绝写入。

### 9.6 测试要点（围栏专项）

- [ ] **T9.11 围栏用例集**：基本块、多语言块、加长围栏、缩进围栏、波浪号混用、未闭合、围栏内 `#`/`Copy` 行、编辑后计数不漂移。

---

## 建议落地顺序

1. T9.1–T9.3（读取 + 围栏扫描 + 行视图），无模型依赖。
2. T9.4–T9.6（定位 + 编辑 + 写回）。
3. T9.7–T9.9（校验 + 围栏安全 + 备份回滚）。
4. T9.10（模型接入）端到端联调。
5. T5.1（README 修订）与 T9.11（围栏用例集）收尾。

## 状态

阶段 0–4 为既有实现（部分缺测试）；阶段 5–8 为文档/测试/运维缺口；阶段 9 为本次新增的 Markdown IO 能力，全部未开始。
