# Markdown 文档读写修改支持（建议稿）

## 1. 背景与问题

当前把文件内容喂给 Gemini 修改、再取回时，返回文本与原文件经常无法逐字节匹配，`edit` 工具报 “Could not find the exact text ... including all whitespace and newlines”。已定位到两类原因：

- 项目侧改写：`gemini_web/chat_io.py` 旧的 `clean_code = re.sub(...).strip()` 无条件剥掉首尾空白与空行；已由 `_strip_code_noise()` 修掉（仅剥头部语言标签行与 Copy/Download 行，保留正文空白）。
- 模型侧规范化：Gemini 网页把 Markdown 连续空行压成一个、在代码围栏前后补空行、重新缩进，这些无法从项目侧完全消除。

Markdown 文档普遍含 ``` 围栏代码块（json / bash / text / python 等）。围栏既是**内容**又是**结构分隔符**，是定位与保真的最大难点：模型很容易把围栏内的空白“顺手规整”，或把围栏计数弄错，导致整篇结构错位。

## 2. 目标

- 提供一套“可定位、可校验、可回滚”的 Markdown 读写修改能力，不再依赖脆弱的纯文本片段匹配。
- 读写结果保持原文件字节级保真（换行、空行、行尾空白、缩进不变），除非显式要求格式化。
- **正确处理 ``` 围栏代码块**：识别开/闭围栏、语言标签、围栏长度与缩进，围栏内部内容不参与任何 Markdown 结构解析。
- 与现有桥接服务（`/v1/chat/completions`、`/v1/responses`）解耦：核心是一套纯函数库，模型只是“内容生成器”。

## 3. 非目标

- 不引入完整的 Markdown AST 重排（会破坏用户原有格式）。
- 不做任意文件类型的结构化编辑；范围限定为 Markdown 与纯文本。
- 不替代 `apply_patch`；两者可共存，本能力是给“整段重写/生成”用的。

## 4. 总体设计

```
读取(read) ──► 扫描围栏(fence map) ──► 定位锚点(anchor) ──► 模型生成(new)
     ──► 校验(verify) ──► 写回(write) ──► 回滚(rollback)
```

核心思路：**不按整段文本匹配，按“锚点 + 行号范围”定位**；锚点来自文件自身结构（标题、围栏、唯一行）。**围栏扫描先于任何结构解析**，得到一张“哪些行在代码块内”的行掩码，后续所有步骤都据此跳过代码块内部。

## 5. 关键机制

### 5.1 结构化读取

- 按行读入，保留原始换行风格（`\n` vs `\r\n`）与文件是否以换行结尾。
- 为每行建立轻量索引：行号、是否空行、缩进宽度、**是否位于围栏代码块内**、所属围栏的语言标签。
- 输出“带行号的视图”给模型，而不是裸文本；模型据此返回行号区间。

### 5.2 围栏代码块（``` 区块）扫描

这是本能力的核心。规则严格遵循 CommonMark，逐行状态机扫描：

- **开启围栏**：行首最多 3 个空格缩进后，出现 ≥3 个连续反引号（```）或波浪号（~~~）；反引号后跟“信息串”（语言标签，如 `json`、`bash`、`text`）。
- **闭合围栏**：同字符、长度 ≥ 开启围栏、行首最多 3 个空格缩进、其后只有空白。**长度必须 ≥ 开启长度**——所以内含 ``` 的块会用 ```` 四反引号包裹。
- **围栏内的 ``` 不闭合**：长度不足的围栏（如外层是 ````，内层是 ```）视为块内普通内容，不改变状态。
- **语言标签**：记录 `json` / `bash` / `text` / `python` 等，用于校验与后续按类型处理；无标签视为 `text`。
- 产出 `fence_ranges: [(open_line, close_line, lang), ...]` 与 `in_fence[line] -> bool` 掩码。

扫描必须一次性完成并缓存；任何编辑后重新扫描，避免缓存与实际内容漂移。

### 5.3 锚点定位（替代纯文本匹配）

支持三种定位方式，按优先级：

1. **行号区间**：`start`/`end`（1-based，闭区间）。模型直接引用读取视图里的行号。
2. **标题锚点**：`## 小节名` 到下一个同级/更高级标题之前，做整节替换。**扫描标题时必须跳过围栏内的 `#` 行**（否则代码块里的注释会被误判为标题）。
3. **围栏锚点**：按“第 N 个围栏块”或“语言标签为 json 的第一个块”定位，整体替换该块内容（含或不含围栏行可指定）。
4. **唯一片段锚点**：原文片段必须唯一命中，否则报错并列出行号候选，绝不“猜一个”。

任何定位失败都返回结构化错误（含候选行号），供上层重试或改锚点。

### 5.4 保真写回

- 只替换命中区间，区间外的行**原样拷贝**，不重新序列化整个文档。
- 保留原换行风格与结尾换行；不自动“修正”空行、缩进、行尾空格。
- 修改命中区间**内部**时，围栏行本身默认不重排；若新内容需含围栏，由调用方显式给出。
- 写回前生成统一 diff 供人工确认；写回采用“临时文件 + 原子重命名”，失败不破坏原文件。

### 5.5 围栏相关的校验

- **围栏配对**：扫描结果中不得出现未闭合的开启围栏。
- **语言标签保留**：替换整块时，除非显式要求，否则保留原 `lang`。
- **围栏长度安全**：若新块内容自身含 ```，自动升级外层围栏为更长（````）。
- **围栏计数不漂移**：修改前后 `fence_ranges` 数量与顺序应一致（整块新增/删除除外，需显式声明）。

### 5.6 校验与回滚

- 写回前校验：定位唯一、围栏配对、新内容可解析、未越界。
- 每次修改前把原文件快照写入 `output/backups/`（带时间戳），支持一键回滚。
- 提供 `dry-run`：只输出 diff 不落盘。

## 6. 建议接口（纯函数库，供 server / CLI 复用）

```python
read_md(path) -> MdDoc                       # 行索引 + 换行风格 + 是否尾换行 + fence_ranges
text_lines(doc) -> list[Line]                # 每行带 in_fence / lang / indent
locate(doc, anchor) -> (start, end)          # 行号 / 标题 / 围栏 / 唯一片段
apply_edit(doc, start, end, new_text) -> MdDoc
verify(doc) -> list[Issue]                   # 围栏配对、锚点唯一、语言标签等
write_md(doc, path, dry_run=False) -> str    # 返回统一 diff
generate_edit(doc, instruction, llm) -> (start, end, new_text)
```

`generate_edit` 把“带行号视图 + 指令”发给模型，要求其返回 `start/end/new_text` 三元组；解析走现有 `toolcalls.py` 的 `TOOL_CALL` 通道，无需新协议。发给模型的视图应显式标注围栏边界（例如 `L12 ```json` / `L20 ````），让模型知道哪些行不可当结构处理。

## 7. 对现有代码的影响

- `gemini_web/chat_io.py`：`_strip_code_noise()` 已修掉项目侧空白改写；本能力进一步避免“整段重写再匹配”。
- 新增 `gemini_web/markdown_io.py`（纯逻辑，无浏览器依赖），新增 `tests/test_markdown_io.py`。
- **已接入**：`gemini_web/toolcalls.py` 提供 `EDIT_MARKDOWN_TOOL` / `BUILTIN_TOOLS` / `edit_markdown_spec()` / `execute_edit_markdown()`；`prompting.build_prompt` 在工具列表含 `edit_markdown` 时追加使用说明；`server.py`（chat）与 `responses.py`（Responses）在 `EDIT_MARKDOWN_LOCAL=1` 时自动注册并本地执行，结果挂回对应 tool_call（含 diff / written / backup / error）。
- 配置：`config.EDIT_MARKDOWN_LOCAL`（默认 false，仅注册 schema，不本地执行）、`config.EDIT_MARKDOWN_BACKUP_DIR`（默认 `output/backups`）。
- README「故障排查」中“有头模式失焦”一条已过时（仍在讲 `bring_to_front()` / `focus()`），应改为“DOM 事件派发、不依赖焦点”，并补 `SEND_BUTTON_SELECTORS`。

## 8. 风险与取舍

- 模型可能返回错误行号：靠“写回前 diff 确认 + dry-run + 备份”兜底，不静默写入。
- 标题/片段在代码块内出现：靠 `in_fence` 掩码排除，绝不在围栏内做结构定位。
- 嵌套/加长围栏：靠“闭合长度 ≥ 开启长度”规则处理；无法判定的畸形围栏直接报错。
- 该方案不消除模型侧的 Markdown 规范化，只是让定位不再依赖被规范化的文本。

## 9. 测试要点（围栏相关）

- 基本：单个 ```json 块、多个不同语言块、无标签块。
- 加长围栏：外层 ```` 内嵌 ```。
- 缩进围栏：行首 1–3 空格缩进；4 空格应视为缩进代码而非围栏。
- 波浪号围栏：`~~~` 与 ``` 混用、互不闭合。
- 未闭合围栏：报错并给出起始行号。
- 围栏内出现 `# 标题` / `##` / `Copy` 行：结构扫描与噪声清理都必须跳过。
- 编辑后围栏计数不漂移；整块替换保留原语言标签与换行风格。

## 10. 建议落地顺序

1. ✅ `read_md` / 围栏扫描 / `locate` / `apply_edit` / `write_md` + 单元测试（无模型依赖）。
2. ✅ `verify`（围栏配对/长度安全）+ 备份 + dry-run。
3. ✅ `generate_edit` + `edit_markdown` 接入 chat / Responses 通道（`EDIT_MARKDOWN_LOCAL=1` 时本地执行，默认 dry-run）。
4. 补 README 与 `doc/tasks.md` 条目。
