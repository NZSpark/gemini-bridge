# 技术报告：工具调用载体从「纯文本 `TOOL_CALL:{}` 行」改为「标记行 + ```tool_call 代码围栏」

> 报告日期：2026-10-07
> 项目：GeminiBridge（Playwright 驱动 Gemini 网页版，对外暴露 OpenAI 兼容 API）
> 参照：姊妹项目 ChatGPTBridge 的 `doc/code_block_fence.md`（同一问题在 ChatGPT 网页版上的复盘）
> 相关代码：`gemini_web/toolcalls.py`、`gemini_web/markdown_io.py`
> 回归测试：`tests/test_toolcalls.py::FencedCarrierTests`（真机样本固化为用例）
> 真机探测：`tests/e2e/probe_carrier_fidelity.py`（`GEMINI_E2E=1` 才跑，不纳入 pytest 收集）

---

## 1. 摘要

| 维度 | 结论 |
| --- | --- |
| **问题** | 旧载体把「调用 JSON」全部塞进一行纯文本 `TOOL_CALL: {...}`。这一行会被网页版当 markdown **段落**渲染，取回 DOM 时 JSON 里的 `\"` 被消费掉，文本不再是合法 JSON，只能交给修复启发式去**猜**。 |
| **姊妹项目的教训** | ChatGPT 侧的实测更狠：转义、缩进、末尾花括号都可能被改写，长命令**静默损坏**甚至整条调用消失。 |
| **天真方案（照搬）** | 只用 ```tool_call 围栏：Gemini 上**不可用**。真机实测：围栏与 info string 都**不进** `innerText`（DOM 里只剩 UI 标题 `Code snippet`），回复里连一个可识别标记都没有 → 解析 0 条。 |
| **采用方案** | **混合载体**：一行纯文本标记 `TOOL_CALL:` 只负责“可识别”（本身无载荷，弄坏也无所谓），紧随其后的 ```tool_call 围栏只负责“逐字节保真”（承载全部载荷，不需要携带标记）。 |
| **效果（真机，同一 92 字节 payload）** | 纯文本行：`\"` 计数 2→**0**，严格 `json.loads` **失败**；围栏内：`\"` 计数 **2**、4/8 空格缩进原样、严格 `json.loads` **OK**、命令**逐字节一致**。混合载体的真机 DOM 形态被解析器一次拿到（无需修复）。 |
| **代价** | 注入文案变长：工具指令 +267 字符（≈66 token）、格式强调块 +278（≈70 token）、`edit_markdown` 说明 +21（≈5 token）。**无工具请求仍为 0 增量**（不注入）。 |
| **残留** | 模型若只写围栏不写标记行 → 本次调用拿不到（提示词三重声明，解析侧无法补救）；`_extract_code_blocks` 仍会把围栏当普通代码块（`SAVE_FILES=true` 时可能落盘）。 |

---

## 2. 背景

### 2.1 桥如何“模拟” function calling

Gemini 网页版不提供 OpenAI 的 function calling。本桥用「提示词注入 + 结构化解析」三段式模拟：

1. **注入**：把客户端 `tools` 描述转成自然语言指令（`toolcalls.format_tools_instruction`）塞进 prompt；
2. **解析**：模型在回复文本里按约定写出调用，`toolcalls.parse_tool_calls` 还原成 OpenAI 的 `tool_calls`；
3. **回灌**：客户端执行工具后，`role="tool"` 的结果重新拼进 prompt（`prompting._render_message`）。

整条链路里“调用”不是结构化数据，而是**模型写的一段文本**。这段文本要活着穿过
`模型 → Gemini 网页 DOM → Playwright innerText → 桥` 四跳，中途经手的一环就是 **markdown 渲染**。

### 2.2 载体的不变量

| 不变量 | 含义 |
| --- | --- |
| **I1 逐字节保真** | JSON 里的 `\"`（值内引号）、`\n`（字面量换行）、命令正文的缩进空格必须原样到达解析层。 |
| **I2 结构完整** | 花括号/引号配对必须存活，否则连“是不是一次调用”都判断不了。 |
| **I3 唯一可识别** | 只有真调用会被执行；正文里展示的 JSON 片段不得被误判为调用。 |
| **I4 内容自足** | 载体本身（而非提示词措辞）就应挡住网页渲染的破坏。 |

旧载体（`TOOL_CALL: {...}` 全写一行）在 **I1** 上系统性失守：载荷与标记挤在同一段“会被渲染器解释的文本”里。

---

## 3. 真机证据（2026-10-07）

### 3.1 实验设置

| 项 | 值 |
| --- | --- |
| 页面 | 真实 Gemini 网页版，`HEADLESS=false`，真实登录 profile |
| 桥 | 运行中的服务（`127.0.0.1:8001`），请求**不带 `tools`** |
| 关键前提 | 不带 `tools` ⇒ 桥**不注入**任何格式指令，prompt 原样送入。这样“载体”是唯一变量，不会被桥自己的指令污染（姊妹项目 §6.3 复现过这种污染） |
| 任务 | **运输层回声**：让模型逐字节搬运给定 payload（它只搬运、不创作），任何差异只能归因于渲染层 |
| payload | 92 字节：`{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n        y = 2"}}`（`\"`×2、4/8 空格缩进） |
| 采集 | `GEMINI_E2E=1 .venv/bin/python -m tests.e2e.probe_carrier_fidelity` → `output/carrier_fidelity_probe.txt` |

### 3.2 结果（DOM 取回后的原文，逐项测量）

| 载体臂 | DOM 首行 | 字节 | `\"` 计数 | 4 空格段 | 最长空格段 | 严格 `json.loads` | 解析器交付 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `line`（`TOOL_CALL: {json}` 一行） | `TOOL_CALL: {` | 101 | **0** | 3 | 8 | **FAIL**：`Expecting ',' delimiter: line 1 column 48` | 1 条，但**必须靠修复启发式** |
| `fence`（只有 ```tool_call 围栏） | `Code snippet` | 106 | 2 | 3 | 8 | OK | **0 条** |
| `hybrid`（标记行 + 围栏） | `TOOL_CALL:` | 118 | **2** | 3 | 8 | **OK** | **1 条，无需修复** |
| `control`（```python 围栏） | `Python` | 19 | — | — | — | — | — |

真机原文（节选）：

````text
# line 臂：转义被吃掉，JSON 已不合法
TOOL_CALL: {"name":"bash","arguments":{"command":"printf "hi"; echo done\n    x = 1\n        y = 2"}}

# fence 臂：JSON 逐字节保真，但围栏与 info string 都不见了，只剩 UI 标题
Code snippet
{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n        y = 2"}}

# hybrid 臂：标记行在、载荷在围栏里逐字节保真
TOOL_CALL:

Code snippet
{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n        y = 2"}}

# control 臂：Gemini 用“语言名”当代码块标题；tool_call 不是已认语言 → 退化成 Code snippet
Python
print('hi')
````

### 3.3 机制

1. **段落 vs 代码块**：纯文本行属于普通流，CommonMark 会把「反斜杠 + ASCII 标点」当转义消费掉 → `\"` 变 `"` → **I1 失守**；代码块内部是字面文本（`<pre>` 语义），转义原样保留。
2. **围栏不进 `innerText`**：Gemini 把代码块渲染成 code-snippet 组件，从 DOM 取回的是它的**标题文字**：已认语言显示语言名（`Python`），未认语言显示 `Code snippet`。`tool_call` 不是已认语言 → 标记消失 → **I3 的“唯一可识别”在渲染后被破坏**，若只依赖围栏则**解析 0 条**。
3. 结论：**“可识别”与“保真”必须由两个部件分别承担**，并且可识别的那半必须写成不会被渲染改写的形态。

### 3.4 诚实标注

* 姊妹项目报告的三类损伤里，本次在 Gemini 上**只复现了“转义被消费”**；“连续空白被折叠”**没有复现**（line 臂的 4/8 空格缩进原样保留）；“末尾少一个 `}`”未出现。
* **短 payload 的 line 臂被修复启发式救回了**，且恢复出的命令恰好逐字节正确。这不构成“旧载体没问题”的结论：严格 `json.loads` 已经失败，说明该路径**本质上是猜**；正文裸引号在 JSON 里不可判别，payload 变长/引号变多时就会猜错（姊妹项目 §3.5 有“合法但错误”的实测记录）。
* 真机样本量小（每臂 1 次，2 轮独立运行结果一致），结论是**机制性**判断，不是统计学结论。

---

## 4. 方案：混合载体

### 4.1 形态

````text
TOOL_CALL:
```tool_call
{"name": "TOOL_NAME", "arguments": {"ARG_NAME": "ARG_VALUE"}}
```
````

* 标记行 `TOOL_CALL:`：**只负责可识别**。它是行首关键词 + 冒号，没有任何转义、缩进、长文本，渲染层无处可改。
* ```tool_call 围栏：**只负责保真**。载荷全在里面，不需要它携带任何标记，因此“围栏标签在 DOM 里丢失”不再是问题。
* info string 仍固定为 `tool_call`：在**围栏未被渲染**的形态下（客户端把回复原样贴回、别的 DOM 形态），它是 I3 的唯一凭据——**刻意不认** ```json（正文里正常展示的 JSON 代码块若恰好含 `name`/`arguments`，被当调用执行就是“模型可控文本触发本机命令”）。

### 4.2 为什么不选另外两条路

| 候选 | I1 保真 | 渲染后可识别 | 结论 |
| --- | --- | --- | --- |
| 纯文本 `TOOL_CALL: {json}` 一行（旧） | ❌ 转义被吃 | ✅ | 弃用（载荷与标记耦合在同一段易损文本里） |
| 只用 ```tool_call 围栏 | ✅ | ❌（变成 `Code snippet`） | **在 Gemini 上不可用**（真机 0 条） |
| **标记行 + 围栏（采用）** | ✅ | ✅ | 采用 |
| “要求模型不要写缩进/反斜杠” | ⚠️ 只是请求 | ✅ | 提示词里保留为**补充**规则，不当主防线（载体的 I4：约束不了渲染器） |

### 4.3 解析侧：优先级链（无需新分支）

`parse_tool_calls` 的分支顺序调整为：

| 顺序 | 分支 | 本次角色 |
| --- | --- | --- |
| 0 | ```tool_call 围栏 | 围栏**没被渲染掉**时的主路径 |
| 1 | `TOOL_CALL:` 标记行 + 其后第一个平衡 JSON 对象 | **Gemini 渲染后的主路径**（真机形态：标记行 + `Code snippet` + 保真 JSON；平衡扫描会跳过中间的非 JSON 文字）。历史一体化载体也走这条 |
| 2 | 裸 `tool_call` 标签 + JSON | 另一些 DOM/引用形态的兜底（既有实现） |
| 3 | DSML / ```json | 历史兜底（既有实现） |

标记行分支**不需要新代码**：它早就是旧载体的解析路径，本次只是把它从“历史兼容”提升为“当前载体的标记半”。

### 4.4 顺带补上的可观测性

旧代码在“回复里明明有调用标记、却一条都没解析出来”时**完全静默**（姊妹项目 §2.13 的故障就是这样被静默吞掉的）。
本次在 `parse_tool_calls` 收尾加了一条 warning（`_CARRIER_MARKER_RE` 命中但 `calls` 为空）：

```text
回复含工具调用标记/围栏，但未产出任何可用调用（截断、格式违规或工具名不合法）：<前 200 字符>
```

---

## 5. 改动清单

| 位置 | 改动 |
| --- | --- |
| `toolcalls.format_tools_instruction` | 主注入块改为「标记行 + 围栏」模板；规则重写（必须两部分齐全、JSON 必须在围栏内、单行纯文本调用不会被执行、逃逸与换行转义、原样粘贴） |
| `toolcalls.format_tool_call_emphasis` | 新会话播种时的格式强调块同步改；明确“标记行必须是 `TOOL_CALL:`、围栏标签必须是 `tool_call`” |
| `toolcalls.edit_markdown_spec` | 内置 `edit_markdown` 的示例改为「标记行 + 围栏」（示例本身是合法 JSON，可被解析器吃回去） |
| `markdown_io._build_edit_prompt` | Markdown 编辑链的注入同步（解析复用同一个 `parse_tool_calls`，不会各说各话） |
| `toolcalls.parse_tool_calls` | 围栏分支提到分支 0；标记行分支降为分支 1 并加注释说明它在 Gemini 渲染后是主路径；收尾加诊断 warning |
| `toolcalls._TOOL_CALL_FENCE_RE` | 收紧为 `[ \t]*` + `\r?\n`（更精确的 info string 行匹配） |
| 模块 docstring / 注释 | 写清“为什么不能只用围栏”（`Code snippet` 机制），并指向本文件 |

---

## 6. 回归测试与区分力

`tests/test_toolcalls.py`：

| 用例 | 锁定的行为 |
| --- | --- |
| `test_injection_asks_for_both_parts` | 工具指令与格式强调块必须**同时**给出 `TOOL_CALL:` 与 ```tool_call |
| `test_no_longer_asks_for_plain_text_line` | 不得残留「写成纯文本行 / 不要代码围栏」的旧措辞 |
| `test_instruction_example_roundtrips_through_parser` | 注入块里的示例是合法 JSON 围栏，且占位工具名不会泄漏成可用调用（**提示词与解析器同源**） |
| `test_captured_gemini_dom_hybrid_is_byte_exact` | **真机样本**：混合载体 DOM 形态 → 严格 `json.loads` OK + 命令逐字节一致 |
| `test_captured_gemini_dom_plain_line_is_not_valid_json` | **真机反例**：纯文本行载体 DOM 形态 → `\"` 计数 0、严格 `json.loads` 抛错（必须靠启发式） |
| `test_captured_gemini_dom_code_only_has_no_marker` | **真机反例**：只给围栏 → DOM 里没有 `tool_call` 标记 → 0 条（防止有人把注入格式“简化”回围栏单体） |
| `test_rendered_fence_preserves_escapes_and_indentation` | 三种可解析形态（未渲染围栏 / 裸标签 / 真机混合）都逐字节还原命令 |
| `test_rendered_json_label_is_not_taken_as_call` | 负向：`json` 标签行不得被执行（I3） |
| `test_warns_when_marker_but_no_call` / `test_no_warning_for_plain_answer` | 诊断 warning 的“该响就响、不该响不响” |
| `test_legacy_plain_text_carrier_still_parses` | 历史数据兼容：旧一体化载体仍能解析 |

`tests/test_markdown_io.py::GenerateEditTests`（Markdown 编辑链）：`test_parses_fenced_carrier`（新载体 + 注入块与解析器同源）、
`test_parses_rendered_hybrid_dom_form`（真机 DOM 形态）、`test_parses_tool_call`（旧一体化载体兼容）。

**区分力实验**（证明用例不是“必然通过”）：把 `gemini_web/toolcalls.py` 换回改动前的版本（`git show HEAD:...`），
`tests/test_toolcalls.py` + `tests/test_markdown_io.py` 立刻 **7 failed / 117 passed**（两部分齐全 1 条、注入文案 1 条、旧措辞消失 1 条、强调块 1 条、示例往返 1 条、`edit_markdown` 说明 1 条、诊断 warning 1 条）；
换回后 **124 passed**。

**真机回归**（本次改动过的用例，复用已在跑的服务）：

```bash
GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity.TestCToolParity -v
# 第一次：Ran 2 tests in 74.3s — OK（c1 direct=31.2s bridge=18.4s；c2 bridge=18.3s）
# 措辞微调后复跑：Ran 2 tests in 72.1s — OK（c1 direct=29.4s bridge=18.3s；c2 bridge=18.0s）
```

> 说明：`TestCToolParity` 第一次运行时**两侧都被 SKIP**（直连基线侧模型这次拒答“我无法直接调用 get_weather …”）。
> 这是该套件设计的 SKIP 路径（上游不配合 ≠ 桥缺陷），随后重跑即通过。为避免把“措辞回归”误判成“上游波动”，
> 另做了一次**措辞 A/B**（同一 prompt 内容，只换注入文案，经桥原样透传、不带 `tools`）：
> 旧文案与本文案**都**被模型照做（各解析出 1 条 `get_weather`），新文案的返回正是混合载体的真机 DOM 形态。
> 即：SKIP 属于上游随机波动，不是本文案的回归。

---

## 7. 残留与风险

1. **模型只写围栏、不写标记行** → 本次调用拿不到（Gemini 渲染后没有可识别标记）。解析侧无法补救，只能靠提示词三重声明（示例 / 规则 / 强调块）。
2. **纯文本调用行仍会被静默损坏**：若模型不听话写回 `TOOL_CALL: {...}` 一行，转义会再次被吃掉，只能靠修复启发式（见 §3.2 的 line 臂）。
3. **围栏会进入 `_extract_code_blocks` 的结果**：`SAVE_FILES=true` 时可能被当普通代码块落盘。后续可按 `info string` 过滤。
4. **提示词成本**：工具请求 +≈66 token（强调块 +≈70，仅播种轮）。无工具请求仍是 0 增量（不注入），P1-5 的预算护栏不受影响。
5. **提示词变更需重启桥才对客户端生效**：注入文案在进程启动时载入，运行中的服务不会自动拾取代码改动。
6. **历史回放不重放载体**：`_render_message` 对 assistant 的工具调用只渲染文本内容，模型无法从历史里“学会”新载体——载体必须靠注入指令反复声明。
7. **姊妹项目 §3.4 的“末尾少一个 `}`”** 未在 Gemini 上复现，本桥也未就此加兜底（保持不加：没有证据的修复会扩大误执行面）。

---

## 8. 复现步骤

### 8.1 载体保真探测（真实网页，约 1 分钟 / 4 臂）

```bash
# 需要：真实登录 profile + 已在跑的桥（默认 8001；用 E2E_PORT 指定别的端口）
GEMINI_E2E=1 .venv/bin/python -m tests.e2e.probe_carrier_fidelity
# 只跑部分臂：PROBE_ARMS=hybrid,fence ...
# 结果写入 output/carrier_fidelity_probe.txt（含每一臂的 DOM 原文）
```

### 8.2 本地复核（不需要网络，基于 §3.2 固化的真机样本）

```bash
.venv/bin/python -m pytest -q tests/test_toolcalls.py
```

### 8.3 真机对等回归（改动过的用例）

```bash
GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity.TestCToolParity -v
```

---

## 9. 结论与后续方向

**结论**：旧载体的毛病不是“解析不够强”，而是**把载荷放进了会被 markdown 解释的那一层**；
照搬姊妹项目的“纯围栏”方案在 Gemini 上又会丢掉唯一的识别标记（`Code snippet`）。
把「可识别」与「保真」拆成两个部件（标记行 + 代码围栏）后，两者都由**结构性**语义保证，而不是靠概率：

* 标记行是行首关键词 + 冒号，渲染层没有可改的地方 → 识别稳定；
* 围栏内部按字面保留 → 严格 `json.loads` 直接通过，连修复启发式都不需要。

解析侧只做了**优先级调整 + 一条诊断 warning**，没有新增宽容分支（新增宽容分支收益为零、误执行风险为正）。

**后续方向**：

1. **把载体固化为可声明的 carrier 契约**（`fence+marker` / `line` / `dsml`），注入措辞与解析优先级收敛到一处，便于按模型行为切换与灰度，而不是散落在四个格式化函数里；
2. **命令正文不经 JSON 字符串内嵌**（`edit_markdown` / 文件载体）——这是“模型自己重排命令”这类残留的唯一根治方向；
3. **按 `info string` 过滤 `_extract_code_blocks`**，避免 `tool_call` 围栏在 `SAVE_FILES=true` 时落盘；
4. **把 §3 的探测扩成带开关的用例集**（当前是手工脚本 + 固化样本；可加“`marker` 是否存活”“围栏是否被渲染”的断言），让载体保真从一次性实验变成可重复的门禁。
