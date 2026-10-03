# GeminiBridge E2E 对等测试设计

> 目的：用 **Playwright 直连 https://gemini.google.com/app 的结果** 作为"上游能力基线"，
> 与 **通过 GeminiBridge（本地 OpenAI 兼容 API）得到的结果** 逐用例比较，
> 判定 GeminiBridge 的代码功能是否正确。
> 实现：`tests/e2e/`（`direct.py` 直连客户端 / `bridge.py` 桥客户端 / `test_parity.py` 用例）。

---

## 1. 判定原则

### 1.1 为什么不做逐字对比

Gemini 是非确定性生成模型：同一 prompt 两次直连的回复也不会相同。因此**不比较两侧文本是否相等**，改用三类断言：

| 断言类型 | 说明 | 例子 |
| --- | --- | --- |
| **期望答案断言** | 独立于两侧实现的 ground truth（哨兵词 / 事实 / 尾部标记） | 回复含 `K7Q9`、含 `巴黎`、末尾含 `END7` |
| **协议结构断言** | OpenAI / Responses 规范的静态结构 | `id` 前缀、`finish_reason`、SSE 事件序列 |
| **量化区间断言** | 软性区间，容忍模型波动 | 长度比 ∈ [0.4, 2.5]、耗时上限 |

### 1.2 基线原则（区分"代码缺陷"与"环境问题"）

直连侧跑通 = 上游（登录态、网络、模型行为）正常。据此判定：

| 直连基线 | Bridge | 结论 |
| --- | --- | --- |
| 达标 | 达标 | **PASS** |
| 达标 | 不达标 | **FAIL —— bridge 代码缺陷**（附两侧原文） |
| 不达标（重试后仍不） | 任意 | **SKIP —— 环境/上游问题**（登录失效、模型拒绝配合、风控），不算代码缺陷 |
| 不适用（纯协议用例） | 不达标 | **FAIL** |

每侧允许**重试 1 次**（模型可能不听"只回复 X"这类严格指令）；重试用**新会话桶 / 新对话**，避免脏上下文。

### 1.3 独立性（避免循环论证）

- **共享的是配置不是逻辑**：两侧共用 `.env` 的选择器与入口 URL——选择器错则两侧同错，但用例的"期望答案断言"（K7Q9/391/巴黎/END7）不依赖选择器，能独立暴露"抓错节点"。
- **结束判定算法刻意不同**：bridge 用"停止按钮 + 双阈值稳定"（`_send_chat_locked`）；直连用独立的保守算法——"文本连续 N 轮不变 **且** 节点内无 `.pending/.animating` 未显现 token"，不引用 bridge 任何代码路径。
- **不复用 bridge 的发送/轮询/提取实现**，只复用 `format_tools_instruction`/`build_prompt` 注入工具指令（C 组需要**同源指令**才能对比）。

---

## 2. 总体架构

```
                     ┌────────────────────────────┐
                     │  tests/e2e/test_parity.py  │  （串行执行，GEMINI_E2E=1 才运行）
                     └─────────┬──────────────────┘
                ┌──────────────┴───────────────┐
                ▼                              ▼
   ┌─────────────────────────┐   ┌──────────────────────────────┐
   │ DirectGeminiClient      │   │ BridgeClient (HTTP)          │
   │ Playwright 直连网页版    │   │ POST /v1/chat/completions 等 │
   │ 独立轮询/结束判定         │   │ BridgeServer 自动拉起 uvicorn│
   └───────────┬─────────────┘   └──────────────┬───────────────┘
               ▼                                ▼
   user_data_e2e/（profile 副本）     user_data/（原 profile）
   Chromium #1                       Chromium #2（bridge driver 持有）
               └──────────┬─────────────────────┘
                          ▼
              https://gemini.google.com/app
```

- **profile 隔离**：两个 Chromium 不能共享同一 profile（`SingletonLock`）。测试启动时把 `user_data/` 复制为 `user_data_e2e/`（删掉 `Singleton*` 锁文件），直连用副本，bridge 用原目录。
- **服务生命周期**：`/healthz` 通则复用已在跑的服务；不通则以子进程拉起 `uvicorn gemini_api_server:app`（日志落临时文件），`tearDownModule` 只清理自己拉起的进程。
- **上下文隔离**：bridge 每个用例用独立 `X-Gemini-Session` 桶（`e2e-<case>-r<n>`）；直连每个用例先 `new_chat()`。两侧互不污染，用例可乱序重跑。
- **风控**：全程串行，单轮间隔自然存在；一轮完整 E2E 约 18–22 次上游请求，预计 **15–25 分钟**。

---

## 3. 测试用例清单

### 组 A：内容对等（直连 vs Bridge，双侧同断言）

| ID | Prompt（要点） | 断言 | 覆盖的 bridge 风险 |
| --- | --- | --- | --- |
| A1 | `只回复 K7Q9` | 双侧含 `K7Q9`；bridge 回复无注入块泄漏（`[上下文重建]`/`[工具调用说明]`/`[任务状态]`/`TOOL_CALL`）；CJK 占比 ≥0.2；**同一次请求附带 B3 schema、B5 落盘断言** | prompt 拼装污染、回复节点抓取错误、注入块泄漏进回复 |
| A2 | `法国首都？只回答城市名` | 双侧含 `巴黎`/`Paris` | 抓到用户回显/错误节点、历史播种串台 |
| A4 | `写约 600 字短文，最后一行单独输出 END7` | 双侧 ≥300 字且 **`END7` 位于末尾 100 字内**；长度比 ∈ [0.4, 2.5] | **结束判定过早收尾 → 截断**（update.md 双阈值/`_complete_text` 风险） |

> A5（语言一致性）并入 A1；A3（算术 391）与 A2 等价，列入可选扩展。

### 组 B：协议正确性（仅 Bridge，静态规范）

| ID | 用例 | 断言 | 覆盖风险 |
| --- | --- | --- | --- |
| B1 | `GET /healthz` | 200、`status=ok`、含 `cluster`/`session_keys`/`init_error` 字段 | 探活、可观测性 |
| B2 | `GET /v1/models` | `object=list`、含 `gemini-chat` | 模型发现（Pi） |
| B3 | （并入 A1 的非流式请求） | `id` 以 `chatcmpl-` 开头、`object=chat.completion`、`choices[0].message.role=assistant`、`finish_reason` 合法、`usage` 存在、`model` 回显、`saved_files` 为 list（非空时文件确实存在于磁盘） | 响应结构、落盘副作用一致性 |
| B4 | 流式 `stream=true` + 哨兵 prompt | 首 chunk `delta.role=assistant`；全程 `id/created/model` 唯一；末 chunk `finish_reason=stop`；`data: [DONE]` 收尾；拼接文本含哨兵；记录首字节耗时与 `: keep-alive` 次数（软观测） | SSE 编码、**`_delta_piece` 节点替换丢/重字** |
| B6 | `POST /v1/responses`（stream） | 事件序列含 `response.output_text.done`、`response.completed`；`sequence_number` 严格递增；completed 的 output 文本非空 | Responses 兼容层（Codex 路径） |
| B8 | （可选，`E2E_FULL=1`）openai SDK 直连 | `openai.OpenAI(base_url=...)` 一次非流式调用成功 | T4.1 客户端联调雏形 |

### 组 C：工具调用对等

| ID | 用例 | 断言 | 覆盖风险 |
| --- | --- | --- | --- |
| C1 | 带 `tools=[get_weather]` 的强制调用 prompt | **直连**：用 `prompting.build_prompt(..., tools=)` 构造**同源指令**，原始回复须含 `TOOL_CALL` 与 `get_weather`（否则 SKIP=模型不配合）；**bridge**：`message.tool_calls[0].function.name == get_weather` 且 `arguments` 可 `json.loads` | 工具指令注入、**DOM 提取/markdown 转义破坏 JSON**、`parse_tool_calls` 主路径、护栏误杀 |
| C2 | 同 C1 但 `stream=true` | chunk 序列 `finish_reason=tool_calls`；`delta.tool_calls` 拼出的 name/arguments 完整可解析 | 流式工具缓冲（`RESPONSES_TOOL_BUFFER`）与分片编码 |

### 组 D：会话与上下文（Bridge 为主）

| ID | 用例 | 断言 | 覆盖风险 |
| --- | --- | --- | --- |
| D1 | 同桶 2 轮：记暗号 `Zebra-42` → 询问 | **双侧**含 `Zebra-42`（直连=原生多轮基线） | 增量 prompt 拼装、会话延续 |
| D2 | 桶 A 记暗号 `Tiger-77` → 桶 B 询问（无历史） | 桶 B 不含 `Tiger-77` 且含 `NONE` | **分桶隔离**（`X-Gemini-Session`） |
| D3 | 桶记暗号 `River-13` → `POST /session/reset` → 再询问 | 重置后仍含 `River-13` | **轮转 + 新开对话 + 历史播种 + 任务快照**全链路（`tasks.resume_block`） |

### 组 E：时效（软观测，自动记录）

- 用例辅助函数自动记录直连/桥两侧单轮耗时，`tearDownModule` 打印汇总表；
- 硬断言仅一条：bridge 单轮耗时 < `GEMINI_TIMEOUT`（否则早已超时报错）；
- `bridge/direct` 耗时比 > 3 时打印 WARNING（不判失败——两条链路独立，波动大）。

---

## 4. 运行方式

```bash
# 前置：
#  1) user_data/ 已有有效 Gemini 登录（先跑过 T4.2：HEADLESS=false 手动登录）
#  2) 本机可访问 gemini.google.com；无其他 Chromium 占用 profile

# 完整套件（约 15–25 分钟）
GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity -v

# 只跑某一组 / 某个用例（快速回归）
GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity.TestAContentParity -v
GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity.TestBProtocol.test_b4_stream_sequence -v

# 可选开关
E2E_HEADED=1     # 直连浏览器可见（调试用）
E2E_PORT=8001    # bridge 端口（默认取 .env 的 PORT）
E2E_FULL=1       # 额外启用 B8（openai SDK 联调）
```

- 未设置 `GEMINI_E2E=1` 时全部 **skip**，常规 `unittest discover`（123 用例）不受影响、不发起任何网络请求。
- bridge 子进程日志：临时目录 `gemini_e2e_uvicorn.log`（失败时查看）。

---

## 5. 误报与已知局限

1. **模型不配合**：严格指令（"只回复 X"）偶被无视 → 每侧重试 1 次；仍不达标按基线原则 SKIP，并打印原文供人工复核。
2. **选择器同源**：两侧共用 `.env` 选择器。若选择器整体失效，A 组靠期望答案断言判定为环境问题（双侧不达标 → SKIP），**不会**误判为 bridge 缺陷——但这意味着选择器失效需要 T4.3（`/debug/dom` 实跑）单独兜底。
3. **长文长度比**：Gemini 波动可使比值越界 [0.4, 2.5]，失败时先看 `END7` 是否在双侧尾部（截断 vs 波动可区分），必要时重跑。
4. **profile 副本时效**：`user_data_e2e/` 每次运行从原目录重新复制；若登录态恰好在两轮之间过期，表现为双侧不达标 SKIP。
5. **风控**：调用量大可能触发 Google 验证码。控制在每轮 ≤22 次请求、串行执行；出现人机验证时按环境问题 SKIP，人工处理后重跑。
6. **不覆盖**：真实 DOM 改版（T4.3）、多 Agent 并发压测、`PARALLEL_BUCKETS=true` 路径（建议后续补一组 D4）。

---

## 6. 与项目任务/分析文档的对应

| 本文档 | 任务（doc/tasks.md） | 分析条目（doc/update.md） |
| --- | --- | --- |
| 组 A/B 基础部分 | T4.1（client_test 联调雏形）、T4.2 前置校验 | §2.1 id 一致性 → B6 事件断言 |
| B4 流式 | T3.1 验收补充 | §3.2 `_delta_piece` 替换语义 |
| C 组 | T1.3 真实输出验证、T5.2 契约实测 | §2.2 解析契约、DOM 提取风险 |
| D3 | T2.3 播种链路验收 | §1 任务快照设计 |
| 组 E | — | §2.1"能连上但工具调用不稳"的耗时观测 |
