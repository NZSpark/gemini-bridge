# 站点专属：GeminiBridge 家族化之前的 SKILL.md 正文（逐字留档）

> 本文件是 **site-specific**：`family_sync.py` 的 `sync` 永不覆盖它，
> 所以这是该站点知识的权威副本（共享正文接管后仍在此处可查）。
> 来源：GeminiBridge 并入 skill 家族（共享源 `web-to-openai-api@1.2.3#6ac0a079`）之前的正文，
> 于 2026-10-13 原样保留，仅加了本段说明。

---

# Web 页面 → OpenAI API 包装 Skill（GeminiBridge 实战版）

把**任意一个给定 Web 页面**桥接成一个 **OpenAI 兼容的本地 API**，让任何支持 OpenAI 协议的客户端
（OpenAI SDK、Codex CLI、Pi、Cline、LangChain…）直接使用。核心手段是 **Playwright 驱动真实
Chromium + 复用登录态 + DOM 观测**，不依赖对方的官方 API / Key。

本 skill 由 **GeminiBridge**（把 `gemini.google.com` 网页版包装成 OpenAI 兼容 API：9 天、50 次提交、
14,348 行 Python、419 个不联网用例（另有 18 个显式开关的联网对等 E2E）+ 载体保真 / 长度探测脚本）
的完整落地过程蒸馏而来；
同族项目还有 ChatGPTBridge / ClaudeBridge / DeepseekBridge / GrokBridge。
**按时间顺序的完整实施复盘**见 [references/case-study-geminibridge.md](references/case-study-geminibridge.md)
——先读它，再动手。GeminiBridge 的排错轮次比任何文档都值钱：它证明了「写不进输入框」有至少五种不同的
真因，每一次都曾把判断带向错误方向。

四个阶段是**强制流程**，每个阶段都有独立的 reference 文件与退出条件：

| 阶段 | 目标 | 必读 | 自动化工件 |
| --- | --- | --- | --- |
| ① 设计 | 侦察页面、定接口契约、定会话模型、定错误语义、写验收标准 | [references/design.md](references/design.md) | `scripts/recon.py` |
| ② 编码 | 按参考架构实现，配置即文档，先假 page/driver 可测 | [references/implementation.md](references/implementation.md) | `assets/config.template.env` |
| ③ 排错 | 按「症状 → 根因」表定位，先看日志时间签名，再看选择器 | [references/troubleshooting.md](references/troubleshooting.md) | `/debug/dom` 类诊断端点 |
| ④ 验收 | 静态 + 契约 + 真机对等 + 安全四层，留下证据 | [references/acceptance.md](references/acceptance.md) | `scripts/smoke_test.py` |

---

## 何时使用 / 不适用

**使用**：用户要「把 chat.xxx.com / 某网页版模型 / 只有网页界面的服务接入成 OpenAI 接口」；
要给 AI 编程 Agent 配一个「不用官方 Key」的模型后端；或给同族 Bridge 项目排错、加固、移植能力。

**不适用**：目标站点已有稳定的官方 API / Key（直接用）；目标是抓数据而不是对话（那是 scraper）；
绕过付费墙、验证码、安全挑战、风控或多人共享账号——**不做**，见文末红线。

---

## 快速开始（新项目或克隆同族项目时最快）

```bash
# 0) 依赖
python3 -m pip install -e '.[dev]' 2>/dev/null || python3 -m pip install playwright fastapi uvicorn pydantic
python3 -m playwright install chromium

# 1) 侦察目标页面：候选选择器 + 流式形态 + 网络接口线索
python3 skills/web-to-openai-api/scripts/recon.py \
  --url https://chat.example.com/ --user-data ./user_data \
  --network-log --out ./recon-report.md

# 2) 按 ② 编码（references/implementation.md 的骨架 + assets/config.template.env）

# 3) 服务跑起来后做契约验收（用用户会用的那个接口）
python3 skills/web-to-openai-api/scripts/smoke_test.py --base-url http://127.0.0.1:8001 \
  --session demo-1 --session-header X-Gemini-Session
```

---

## ① 设计（先写清楚，再写代码）

1. **侦察**：跑 `scripts/recon.py`（默认有头、可手动登录）；回答六个问题——
   要不要登录、输入框是什么元素、回复是**原地流式更新**还是**追加新节点**、「生成中」的
   可观测信号、会话 URL 形态与到顶提示原文、代码块容器。再决定路线：
   **A) 直接复用站点自身 XHR/JSON 接口**（`--network-log` 能看到则优先，最稳）；
   **B) 浏览器 DOM 自动化**（默认路线）；**C) 混合**（DOM 登录 + 接口回放）。
2. **定契约**：必须实现 `GET /v1/models`、`POST /v1/chat/completions`（流式 + 非流式）、
   `GET /healthz`；错误统一 `{"error":{"message","type","code"}}`；客户端是 Codex CLI 时加
   `POST /v1/responses`。未知字段一律 `extra="allow"`，**绝不 422**。
3. **定错误语义**（提前定死，直接决定客户端行为）：
   超时 504 / 到顶 400 `context_length_exceeded` / 浏览器未就绪 503 / 找不到输入框 502 /
   同桶忙 503 `upstream_busy` / 空输入 400。**「上游明确拒绝」与「上游无响应」必须可区分**。
4. **定会话模型**：默认「轮转 + 播种」——复用一条网页会话、只发增量；一旦轮转/新开，
   **必须同时实现播种历史**（两者成对出现，否则退化成「一条孤立消息」的静默失败）。
   多任务隔离用「显式 header → `user` → User-Agent」优先级分桶。**不要恢复会话 URL**——
   网页版的会话 URL 与「当前对话」绑定关系不稳定，回填易落到空白页；新开对话 + 历史重放更鲁棒。
5. **定验收标准**：把 [references/acceptance.md](references/acceptance.md) 的清单抄成项目专属版。
6. **列风险**：登录态是敏感数据；只绑 `127.0.0.1`；`user_data/` 不进 git；
   默认串行（真并发会同时驱动多个网页会话，有风控风险）。

**产出**：`doc/design.md`（选择器表 + 契约表 + 会话决策链 + 风险 + 验收标准 + 不做的事）。
**退出条件**：能回答「这条网页会话活多久、由谁轮转、失败如何分类」；选择器全部来自实测而非猜测。

---

## ② 编码（模块化 + 可测 + 配置即文档）

按 [references/implementation.md](references/implementation.md) 的骨架实现，硬性约定：

- 入口文件是**薄封装**；实现拆进 `<site>_web/` 包
  （`config / models / prompting / toolcalls / driver / chat_io / page_pool / session_store /
  completion / streaming / responses / server`，可按规模合并）。
- **所有可调参数（含 DOM 选择器）集中配置**，运行期用 `config.<NAME>` 取属性（便于测试 patch）；
  默认值与 `assets/config.template.env`、README 三方一致，由漂移测试守护。
- 数据模型宽松接收；`content` 可能是字符串或内容分片数组；`temperature` / `max_tokens` 这类
  网页版没有的旋钮**接受但忽略**（不报错）。
- 分阶段交付（每阶段可独立验收）：
  **A 浏览器原型**（单条短 prompt 能提交并取回**确定的新回复**）→
  **B Chat Completions**（models / 非流式 / SSE / healthz）→
  **C 多会话与可靠性**（分桶 / 锁 / 页面池 / 会话轮转 + 播种 / 到顶）→
  **D 工具调用**（注入 + 载体解析 + 回灌）→
  **E Responses API + 文档**（先验证过再默认开启）。
- 有行为变化的提交**必须配回归测试**；结束判定、写入、提交、回显判断等启发式逻辑优先写成
  **纯函数 + 可注入替身**，用假 page/driver 穷举，不启动浏览器（GeminiBridge 419 个用例全部不联网）。

**退出条件**：`/v1/models`、非流式、流式都能跑通；单测全绿且不联网。

---

## ③ 排错（先看日志时间签名，再看选择器）

拿到故障按固定顺序二分（完整表见 [references/troubleshooting.md](references/troubleshooting.md)）：

| 观察 | 结论 |
| --- | --- |
| 某桶**几乎立刻**失败（0.x 秒），其它桶正常 | 页面句柄已死（标签被关 / 渲染进程崩溃）→ 判活 + 专用异常 + 重建 |
| 失败耗时 ≈ 轮数 × 选择器数 × 超时 | 页面活着但真的没有输入框（改版 / 未登录 / 弹层 / 候选退化） |
| **回复已生成完**却 500 | 生成后的收尾代码炸了（落盘、编码、序列化、签名） |
| 多种失败都表现为「等到超时」 | 结束判定失灵或消息没发出去 → 内容判定 + 快速失败 + 提交验证 |
| 轮询一直 `nodes=0` | 回复选择器失效（网页改版）→ DOM 诊断端点 + 探测复位 |
| 日志里「没有的东西」 | 缺失的失败日志本身就是证据（例：原语报错会打 DEBUG 却没打 → 原语没抛错，是静默空操作） |
| 每 14 秒一轮 200 OK、prompt 长度恒定 | 旧内容 / prompt 回显被当成本轮回复 → 立刻查「新正文」判据是否失守 |

纪律：**先用证据定位，再改代码**；每次修复补回归测试 + **区分力实验**（把修复换回旧代码，
新用例必须变红）；把「AI / 直觉给出的修复建议」先核实字段与 API 真实存在；
**不把可自愈故障说成「请检查登录」**。修复落地后回写 `doc/update.md` 式台账
（现象 / 证据 / 根因 / 修法 / 验证 / 未验证项）。
**退出条件**：所有已知失败都能映射到错误码，单测全绿，每个已修故障都有对应回归用例。

---

## ④ 验收（构建通过 ≠ 功能可用）

必须用**用户会用的那个接口**验证，四层缺一不可（清单见 [references/acceptance.md](references/acceptance.md)）：

1. **静态与单测**：`pytest -q`（E2E 默认 skip）；依赖防漂移（requirements ↔ pyproject ↔ 配置模板）。
2. **契约冒烟**：`scripts/smoke_test.py`——models / 非流式 / 流式 `[DONE]` + 首块 role + 末块
   finish_reason / 未知字段不 422 / 空输入 4xx / 错误体形状 / 无工具请求的 token 预算护栏
   （防工具脚手架回归）/ （可选）工具调用与 `/v1/responses`。
3. **真机对等（E2E）**：直连基线（Playwright 直接操作网页）vs bridge 同 prompt 对等执行；
   上游/环境失败转 **SKIP**，bridge 不达标记 **FAIL**；独立 profile 副本 + 独立会话桶；
   `GEMINI_E2E=1` 式显式开关，默认不跑；每例一次真实生成，改哪个用例只跑哪个，全量套件只在发版前跑。
4. **安全**：仅回环监听、调试端点默认关闭且**不回显对话正文**、`user_data/`（Cookie）与 `.env`
   不进版本控制、模型可控的本地写文件工具必须沙箱（根内相对路径 + 默认 dry-run）、可选 Bearer 门。

**证据留痕**：命令、耗时、响应片段、`/healthz` 快照，写进 `doc/acceptance.md`。
未通过 / 未验证的项**如实报告为限制**，绝不写成通过。**退出条件**：四层都过；
未过的项有名字、有原因、有下一步。

---

## 硬规则（全部来自 GeminiBridge 真机踩坑，违反会重演）

1. **写入输入框是复合动作**：`fill()` 内部是「建立选区（`selectNodeContents` + `addRange`）
   → `Input.insertText`」；裸 `keyboard.insert_text` / `execCommand('insertText')` **只在当前
   选区处插入**，输入框已聚焦时没有选区 → 两者退化为**静默空操作**（不报错、0 字符）。
   分块写入前必须**显式放置光标**（focus + selectNodeContents + 折叠到末尾）。
2. **超长 prompt 必须分块写入**：一次性 `fill(整段)` 会把几万字符排成网页主线程上的长任务，
   Playwright 连「元素是否可编辑」都探测不到（`waiting for element to be visible, enabled and editable`），
   页面本身也假死。分块（默认 4000 字符）+ 每块之间让出主线程；每块前**重读输入框文本**只补缺失段
   （幂等、可续写、重挂载不重复）。
3. **读回是「有损」的**：富文本编辑器把换行渲染成独立块级节点，读回 `textContent` **不含换行**，
   markdown 标记会变格式。比较必须**归一化**（空白序列折成空格 + 前 N 字符 + 长度），
   否则逐字节前缀判据对多行 prompt 永远为假 → 触发「清空重写」自我毁灭循环（写进去又被自己清掉）。
4. **校验只能发现失败，不能否决成功**：`fill()` 成功返回就**信任它**（读回差异只记 INFO）；
   只有 `fill()` **报错**时才读回救援（真机见过报超时但文本已落地）。用有损读回否决一次成功的
   `fill`，等于把已经写好的 prompt 判死，再去分块、再清空重写，反而把它弄坏。
5. **提交必须验证**：合成 `KeyboardEvent` 是 `isTrusted=false`，受控编辑器常直接忽略
   （正文含换行时更会被当软换行）；「发出去就认为成功」会卡到超时。提交阶梯 =
   **真实键盘 Enter → 原生 `click()` 发送按钮 → 合成 Enter**，每级之后验证
   （**输入框已清空**或页面进入「生成中」）；读不到输入框内容时返回「无法判断」，不据此报错也不空等。
6. **结束判定是多信号与门**：内容稳定 / 长度稳定双阈值 + 「生成中」信号 + 未显现 token 检查
   （停止按钮消失时若节点还有 `.pending`/`.animating`，要继续等，否则读到半截）；读文本先
   **克隆节点去动画类**再取全文（`innerText` 取不到未显现 token，会让 JSON 只剩半截）。
   节点数不是内容——原地流式更新时数量不变。
7. **旧内容 / 回显绝不作为回复**：发送前基线与轮询**同一口径**（最后一个**有正文**的节点，
   末尾空容器不能把基线带偏）；收尾必须要求「新正文」且非 prompt 回显（指纹去空白比较 +
   开头 120 字符 + 长度比容忍有损渲染）。**宁可超时失败（随后轮转 + 播种），也不返回旧内容**——
   否则客户端会把上一轮的指令原样重放，形成「提示词反复叠加」的死循环，且永不失败。
8. **页面句柄要判活**：入池前检查 `page.is_closed()`（假 page 无此 API 时按未关闭处理）；
   死句柄要**摘除 + 重建**并置「待播种」（对话丢页面不丢）；导航失败不允许把半死句柄留在池里。
   报错必须指向真因：「页面已关闭」不得说成「未登录 / 找不到输入框」。
9. **工具调用用「标记行 + 围栏」混合载体**：
   `TOOL_CALL:` 纯文本标记行**只负责可识别**（它会被 markdown 段落渲染吃掉转义），
   紧随其后的 ` ```tool_call ` 围栏**只负责逐字节保真**（Gemini 上围栏与 info string 都不进
   `innerText`，DOM 里只剩 `Code snippet`，单用围栏解析 0 条）。解析优先级：围栏 →
   标记行 + 平衡 JSON → 裸标签 → JSON/DSML 兜底；JSON 引号 / 控制字符 / 缺尾括号要有修复启发式；
   正文里的普通 JSON 代码块**不得**当调用执行。载体必须真机 A/B 探测（运输层回声、**不带 tools**，
   避免桥自己的指令污染实验），旧样本固化为单测。
10. **会话轮转与播种成对**：到顶（轮次 / 估算 token / 到顶提示正则）→ 开新对话 + 把历史作为
    首条消息重放；**任务目标用任务快照单独保住**（防止被字符预算从尾部截断丢任务）；
    状态落盘用「同目录临时文件 + `os.replace`」原子替换，临界区**不得有 await**（有则补锁）；
    读取失败留 warning，不许静默清零。
11. **配置即文档，防漂移**：选择器与全部旋钮集中 `.env`，代码从 config 读；默认值与
    `.env.example`、README **双向**守护（新增键忘写模板 / 模板出现死键都失败）。
12. **内置工具按需注入**：客户端未声明工具时不得注入内置工具（曾每请求白付 ~970 tokens 脚手架，
    还会产生客户端没声明的 `tool_calls`）；留显式逃生口开关。无工具请求的 token 预算要有回归护栏。
13. **长度控制要分三层**：单条工具结果上限（截短 + 标注「已截断 N 字符」，**不整块丢弃**）→
    拼装后成品预算（优先压缩最长的工具结果，保留开头并标注）→ 发送侧兜底截断（必须记 WARNING，
    不再静默丢弃）。构建 ≠ 发送：日志必须标注本轮实际发出的是**播种还是增量**（每轮会构建两份 prompt，
    把构建期日志误读成发送行为会浪费一整天）。
14. **桥内命令本地应答**：`/bridge ...`（status / models / session reset / settings）在 HTTP 边缘
    本地处理，浏览器没起来、句柄失效时仍可用；只认「整条消息就是这一行」，只作用当前桶，
    历史里的旧命令不重放，回执写清影响范围。
15. **验收纪律**（三条元规则，每条都来自真实误判）：
    **「未复现」不等于「已排除」**（代码层确定的缺陷即使真机没触发也要修）；
    **先检查测量工具本身**（读回不含换行曾让三轮排查找错方向）；
    **区分力实验是唯一证据**（把修复换回旧代码，新用例必须逐字变红，日志要对上真机原签名）。

---

## 安全红线

- 只监听 `127.0.0.1`：转发的是你自己的登录会话，暴露到网络等于把账号交出去。
- `user_data/`（Cookie）与 `output/` 不进版本控制；`.gitignore` 先写好；`user_data/` 不要同步到
  iCloud / Dropbox 等（会带走登录态）。
- 不做验证码绕过、不规避风控、不做多账号池；遵守目标站点 ToS，仅供个人学习研究。
- 调试端点默认关闭且**不回显对话正文 / 提示词 / Cookie**；日志不打印完整 prompt。
- 模型可控的本地写文件工具必须有沙箱（只允许根内路径，`../` 与软链接逃逸拒绝）且默认 dry-run。
- 可选 Bearer 门（`BRIDGE_TOKEN` 式）默认关闭以保持兼容；开启后只保护生成端点，探活 / 模型发现保持开放。

---

## 交付物清单

```text
doc/design.md                 # ① 侦察结论 + 契约 + 会话决策 + 风险 + 验收标准
doc/tasks.md                  # 任务分解：状态 / 优先级 / 验收标准 / WONTFIX 理由
doc/update.md                 # ③ 故障复盘台账：现象 / 证据 / 根因 / 修复 / 验证 / 未验证
doc/e2e_test_design.md        # ④ 真机测试：判定矩阵 / 开关 / 前置条件 / 陷阱
doc/acceptance.md             # ④ 验收记录（命令 + 证据 + 未通过项）
<site>_web/                   # ② 实现包（config/models/prompting/toolcalls/driver/chat_io/…）
<site>_api_server.py          # 入口薄封装
.env.example  .env            # 配置契约（与 README、config 三方一致）
tests/                        # 假 page/driver 回归测试（默认不联网）
tests/e2e/                    # 直连基线 + bridge 对等 E2E + 探测脚本（显式开关才跑）
skills/web-to-openai-api/…    # 本 skill 的落地副本（scripts 供持续使用）
```

每完成一步更新对应文档；**文档与代码不一致时以代码为准并立刻改文档**。

---

## 执行顺序速记

```text
0 读案例 → references/case-study-geminibridge.md（知道别人踩过什么）
1 侦察   → recon.py 只读探测；拿命中表 + 流式形态 + 网络接口线索
2 设计   → design.md + 验收标准（先于代码）
3 编码   → A 原型 → B 协议 → C 会话/可靠性 → D 工具 → E Responses/文档
4 排错   → 现象 → 错误码 → 不含正文的现场 → 回归测试 + 区分力实验 → 回写 update.md
5 验收   → 单测 → smoke_test.py → 真机对等 E2E → 安全;证据进 acceptance.md
```

同族项目移植时优先读 [references/case-study-geminibridge.md](references/case-study-geminibridge.md)
的「迁移核对表」，再按本文件四阶段执行。
