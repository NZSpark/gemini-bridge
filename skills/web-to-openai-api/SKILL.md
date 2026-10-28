---
name: web-to-openai-api
description: 把任意给定 Web 页面（网页版 LLM、搜索框、表单式服务）包装成本地 OpenAI 兼容 API 的工程化 skill，覆盖项目设计、代码编写、排错、验收全流程，含可执行侦察 / 冒烟脚本与从 GeminiBridge 实战蒸馏的硬规则。触发词：「包装网页」「网页转 API」「网页变 API」「OpenAI 兼容」「web-to-api」「browser bridge」「Playwright 桥接」「/v1/chat/completions」「/v1/responses」「把某网站做成 API」「网页版转 API」「DOM 选择器适配」「SSE 流式桥接」「复用登录态」「bridge」「桥接」。只要用户提到「把某个网页 / 网页版服务接入成 OpenAI 接口 / 给 Coding Agent 用」，立即触发本 skill。
version: 1.2.3
family-source: web-to-openai-api@1.2.4#b1a19e65
metadata:
  openclaw:
    requires:
      bins:
        - python3
    emoji: 🌉
    setup:
      command: python3 -m pip install playwright fastapi uvicorn pydantic && python3 -m playwright install chromium
---
# Web 页面 → OpenAI API 包装 Skill

把**任意一个给定 Web 页面**（网页版大模型、搜索页、表单式服务）桥接成一个
**OpenAI 兼容的本地 API**，让任何支持 OpenAI 协议的客户端（OpenAI SDK、Codex CLI、
Pi、Cline、LangChain…）直接使用。核心手段是 **Playwright 驱动真实 Chromium + 复用登录态 +
DOM 观测**，不依赖对方的官方 API / Key。

canonical 由 **ChatGPTBridge**（把 `chatgpt.com` 网页版包装成 OpenAI 兼容 API：6 个包 /
59 个文件 / 11,911 行生产代码、657 个单测 + 17 条真机 E2E、65 次提交、7 天）的完整落地过程
蒸馏而来，并**对照同族实现补齐了五块**：六条不可违背的设计原则、每阶段**退出条件**、
错误词汇表（带可观察判据）、**事故档案（反模式检查项）**、诚实性要求。
同族项目：DeepseekBridge（16 模块 / 300+ 不联网用例）、ClaudeBridge（14 条反模式）、
GeminiBridge（419 个不联网用例 / 输入写入五类真因）、GrokBridge。
**本站点按时间顺序的完整复盘**见 `references/case-study-<站点>.md` —— 它是**站点专属**文件
（不参与同族共享，`sync` 永不覆盖）：canonical 的是 `case-study-chatgptbridge.md`，
各成员是各自的 `case-study-<站点>.md`。先读它，再动手。

> 血泪结论先写在最前面（违反必重演）：
> 1. **不能确认成功，就不报告成功**——提交、生成结束、工具解析、页面落点都要有可观察判据。
> 2. **节点的 `innerText` ≠ 内容**：先证明它等于什么；**旧内容 / prompt 回显绝不能作为本轮回复**。
> 3. **结束判定是多信号与门**（停止控件消失 + 生成标记为假 + 内容静默窗口），不是固定
>    `sleep`，也不是「节点数量」。
> 4. **每次真机故障 → 复盘文 + 回归测试 + 通用模式**，这是这类项目最值钱的资产。
> 5. **没跑过的验证项必须写「未验证」**，不得勾选、不得含糊成「应该没问题」。

四个阶段是**强制流程**，每个阶段都有独立的 reference 文件、可执行脚本与退出条件：

| 阶段 | 目标 | 必读 | 自动化工件 | 退出条件（不满足不进入下一阶段） |
| --- | --- | --- | --- | --- |
| ① 设计 | 侦察页面、定路线、定接口契约、定会话模型、定错误语义、写验收标准 | [references/design.md](references/design.md) | `scripts/recon.py` | 能回答「这条网页会话活多久、由谁轮转、失败如何分类」；选择器与「生成中」信号全部来自实测 |
| ② 编码 | 按参考骨架实现，配置即文档，先假 page/driver 可测 | [references/implementation.md](references/implementation.md) | `assets/config.template.env` | `/v1/models`、非流式、流式都能跑通；单测全绿且**不联网、不启浏览器** |
| ③ 排错 | 按「症状 → 根因」表定位，先看耗时签名与范围，再看选择器 | [references/troubleshooting.md](references/troubleshooting.md) | `/diagnostics` + 一次性 DOM 诊断 + `scripts/check_templates.py` | 所有已知失败都能映射到错误码；**事故档案每条反模式都有可跑、可红的回归用例** |
| ④ 验收 | 静态 + 契约 + 真机对等 + 安全四层，留下证据 | [references/acceptance.md](references/acceptance.md) | `scripts/smoke_test.py` + `scripts/gate.py` | 四层都过；未过的项有名字、有原因、有下一步 |

**门禁**：四个阶段的退出条件已经串成一条命令，**退出码即结论**（`0` 全过 / `1` 有失败 /
`2` 跑不起来）。别靠记清单，直接跑：

```bash
python3 skills/web-to-openai-api/scripts/gate.py            # 四阶段（默认离线，不碰真实账号）
python3 skills/web-to-openai-api/scripts/gate.py --phase debug
python3 skills/web-to-openai-api/scripts/gate.py --base-url http://127.0.0.1:8002 --token-budget 500
```

它逐条检查：① 设计文档六个议题 / 选择器候选串 / 错误码有文档 / 路线留档；② 配置三方一致 /
无死键 / 三个必需路由 / **单测不启浏览器**；③ `ruff` / `pytest` / 事故档案反模式都有**能变红**的
用例 / 复盘台账 / 同族共享源钉新鲜；④ 契约冒烟（给了 `--base-url` 才跑）/ 验收记录如实标注限制 /
安全三连（回环 + 敏感目录 + 调试默认关）。没给的项**如实 SKIP**（`--strict` 时 SKIP 也算失败）。

---

## 何时使用 / 不适用

**使用**：用户要「把 chat.xxx.com / 某网页版模型 / 只有网页界面的服务接入成 OpenAI 接口」；
要给 AI 编程 Agent 配一个「不用官方 Key」的模型后端；或给同族 Bridge 项目排错、加固、移植能力。

**不适用**：目标站点已有稳定的官方 API / Key（直接用，不要桥接）；目标是抓数据而不是对话
（那是 scraper）；绕过付费墙、验证码、安全挑战、风控或多人共享账号——**不做**，见文末安全红线。

---

## 六条不可违背的设计原则（先记住，再谈实现）

1. **协议与网页解耦**：OpenAI 的请求/响应模型、错误码与浏览器 DOM 互不依赖。DOM 层只知道
   「填字、提交、读回复」；协议层不知道 Playwright。这是后面所有重构与测试的前提。
2. **单一会话所有权**：同一会话桶（bucket）的页面状态由唯一任务顺序控制，同桶用**异步锁**串行；
   禁止两个请求同时往同一个输入框写。不同桶可并行（但要评估风控）。
3. **从完整消息历史恢复**：以 API 请求里的 `messages` 为事实来源。首轮「新对话 + 完整历史播种」，
   之后只发**增量**；播种标记必须**同时绑到内存里的页面对象**，不能只信磁盘
   `has_history`（进程重启 / 页面被换后磁盘仍为真 → 串线；同族 A1）。
4. **不能确认成功就不报告成功**：提交、生成结束、工具解析、页面导航都要有**可观察**判定条件；
   探测不到 ≠ 成功。宁可返回明确错误，也不返回空内容假装成功。
5. **网页变化可诊断**：选择器集中在配置 + 候选回退 + `SELECTOR_VERSION`；诊断端点只回布尔/计数/
   命中情况、不回正文；失败错误里带现场计数（「零节点一次性诊断」）。
6. **最小暴露**：登录 profile、提示词、回复正文、Cookie、DOM 默认不进日志/诊断/发布包；
   `.gitignore` 必须排除 `.env`、浏览器用户目录、会话状态、输出目录。

---

## 快速开始（新项目或克隆同族项目时最快）

```bash
# 0) 依赖
python3 -m pip install -e '.[dev]' 2>/dev/null || python3 -m pip install playwright fastapi uvicorn pydantic
python3 -m playwright install chromium

# 1) 侦察目标页面：候选选择器 + 流式形态 + 站点自身接口（路线 A 判据）
python3 skills/web-to-openai-api/scripts/recon.py \
  --url https://chat.example.com/ --user-data ./user_data \
  --network-log --wait 8 --out ./recon-report.md

# 2) 按 ② 编码（references/implementation.md 的骨架 + assets/config.template.env）

# 3) 服务跑起来后做契约验收（用户会用的那个接口）
python3 skills/web-to-openai-api/scripts/smoke_test.py --base-url http://127.0.0.1:8002 \
  --session demo-1 --session-header X-ChatGPT-Session --responses
```

---

## 路线决策（先决定怎么接，再写代码）

`recon.py --network-log` 会记录页面自己发出的 XHR/fetch 骨架（方法 / 主机 / 路径 / 状态码 /
content-type / 请求体字符数 / 响应 JSON 的**键名**，不含正文），并直接给出路线结论：

| 路线 | 何时选 | 代价 |
| --- | --- | --- |
| **A 直接复用站点自身接口** | 报告里出现 JSON POST 接口（如 `/backend-api/conversation`），且鉴权字段可稳定复现 | 最稳、最快；但接口可能签名/加密/频繁改版，需要 DevTools 对照一次真实请求 |
| **B 浏览器 DOM 自动化** | 没有可复放的 JSON 接口，或接口签名不可复现 | **默认路线**；慢但通用，本 skill 的排错/验收都在这一路 |
| **C 混合（DOM 登录 + 接口回放）** | 登录靠 DOM，生成走接口 | 复杂度最高，只在 A 可行但登录无法自动化时用 |

**注意**：recon 故意不采集任何请求/响应正文（只记长度与键名），走 A/C 时**必须**自己在
DevTools 里看一次真实结构与流式字段来源。
**单点否决项**：B 路线开跑前先验「无头会不会被挑战页拦截」——ChatGPT 实测 `HEADLESS=1` 停在
`Just a moment...`，必须**有头**，无显示服务器用 `xvfb-run` 包一层。

---

## ① 设计（先写清楚，再写代码）

1. **侦察（只读，不提交任何提示词）**：跑 `scripts/recon.py`（默认有头、可手动登录），回答六个问题——
   要不要登录、输入框是什么元素（**可能有多个**）、回复是**原地流式更新**还是**追加新节点**、
   「生成中」的可观测信号、会话 URL 形态与到顶提示原文、代码块容器。再按上面的路线表选 A/B/C。
   若全部命中为空 → 先怀疑「环境/登录态未就绪」，**不要**据此判定选择器失效。
   > 侦察期最常见的误报：HTTP 403 的挑战壳里**仍可能残留可命中的输入框**。所以「页面可用」的
   > 判据必须包含 `navigation_ok`（状态码 2xx/3xx），不能只凭 DOM 命中（同族 A5）。
2. **定契约**：必须实现 `GET /v1/models`、`POST /v1/chat/completions`（流式 + 非流式）、
   `GET /healthz`；错误统一 `{"error":{"message","type","code"}}`；客户端是 Codex CLI 时加
   `POST /v1/responses`。未知字段一律 `extra="allow"`，**绝不 422**。
3. **定参数三种状态**（照抄，别临时发明）：**支持**（真生效）/ **接受但忽略并告警**（如
   `temperature`、`max_tokens`——网页版没有这些旋钮）/ **明确拒绝**（400 `unsupported_parameter`）。
   **严格模式会把真实客户端全部挡在门外**：请求里一定有 `store` / `service_tier` 这类字段。
4. **定错误语义**（提前定死，直接决定客户端行为）：见下方错误词汇表。
   **「上游明确拒绝」与「上游无响应」必须可区分**——把额度用尽报成「解析失败」会让客户端无限重试。
5. **定会话模型**：默认「轮转 + 播种」——复用一条网页会话、只发增量；一旦轮转/新开，
   **必须同时实现播种历史**（两者成对出现，否则退化成「一条孤立消息」的静默失败）。
   多任务隔离用「显式 header → `user` → 工作目录 → User-Agent」优先级分桶。
   会话 URL 该不该恢复见下方**取舍矩阵**。
6. **列风险**：登录态是敏感数据；只绑 `127.0.0.1`；`user_data/` 不进 git（也**不要同步到
   iCloud / Dropbox**，等于把登录态带走）；默认串行（真并发会同时驱动多个网页会话，有风控风险）。
7. **先写验收标准**：把 ④ 的清单抄成本项目专属版，后面照着验。

**产出**：`doc/design.md`（选择器表 + 契约表 + 会话模型决策链 + 错误语义 + 风险 + 验收标准 + 不做的事）。
**退出条件**：能回答「这条网页会话活多久、由谁轮转、失败如何分类」；选择器与「生成中」信号
全部来自实测而非猜测；「页面可用」的判据含 `navigation_ok`。

---

## ② 编码（模块化 + 可测 + 配置即文档）

按 [references/implementation.md](references/implementation.md) 的骨架实现，硬性约定：

- 入口文件是**薄封装**；实现拆进 `<site>_web/` 包（本项目最终形态：`api / browser / completion /
  session / tools / config` 六个子包 + `page_pool` / `end_detection` / `linking` / `bridge_commands`）。
- **所有可调参数（含 DOM 选择器）集中配置**，运行期用 `config.<NAME>` 取属性（便于测试 patch）；
  默认值与 `assets/config.template.env`、README 三方一致，由漂移测试守护；**死键**（定义了却没人
  读）要么接线要么删（同族 A7）。
- 数据模型宽松接收；`content` 可能是字符串或内容分片数组。
- 分阶段交付（每阶段可独立验收）：
  **A 浏览器原型**（单条短 prompt 能提交并取回**确定的新回复**）→
  **B Chat Completions**（models / 非流式 / SSE / healthz）→
  **C 多会话与可靠性**（分桶 / 锁 / 页面池 / 会话恢复 / 到顶）→
  **D 工具调用**（注入 + 围栏解析 + 回填）→
  **E Responses API + 文档**（默认关闭，验证过再开）。
- 有行为变化的提交**必须配回归测试**；启发式逻辑（结束判定、输入写入、提交验证、恢复）优先写成
  **纯函数 + 可注入时钟**，用假 page/driver 穷举，不启动浏览器（本项目 657 个单测全部不联网）。
- 新增的公共方法：**要么带 `self`，要么 `@staticmethod`**，并至少配一条「按实例调用」的测试
  （这能防住一次真实事故，见事故档案 P0-B）。
- **浏览器依赖惰性 import**，且**不要因为浏览器没起来就让进程退出**：让 `/healthz` `/diagnostics`
  先起来、用 503 报告「尚不可用」，离线测试才能用替身驱动真实状态机。

**退出条件**：`/v1/models`、非流式、流式都能跑通；单测全绿且不联网、不启浏览器。

---

## ③ 排错（先看耗时签名与范围，再看选择器）

拿到故障先做二分，别急着改选择器：

| 观察 | 结论 |
| --- | --- |
| 某桶**几乎立刻**失败（0.x 秒），其它桶正常 | 页面句柄已死（标签被关 / 渲染进程崩溃）→ 判活 + 专用异常 + 重建 |
| 失败耗时 ≈ 轮数 × 选择器数 × 超时 | 页面活着但真的没有输入框（改版 / 非对话视图 / 未登录 / 遮挡弹窗） |
| **回复已生成完**却 500 | 生成后的**收尾代码**炸了（落盘、编码、序列化、签名） |
| 多种失败都表现为「等到超时」 | 结束判定失灵或消息没发出去 → 内容判定 + 快速失败轮数 + 提交验证 |
| 轮询一直 `nodes=0` | 回复选择器失效（网页改版）→ 一次性 DOM 诊断 + 探测复位 |
| **每轮 prompt 长度恒定、回复像是上一轮的** | 旧内容 / 回显被当成本轮正文 → 立刻查「新正文」判据 |
| 日志里「该出现的行没出现」 | **缺失的日志本身就是证据**（原语静默空操作，而不是抛错） |
| 卡在取消路径 > 8 分钟 | 取消时 `await` 了被取消的 watcher → 只 `cancel()`，**不 await**（同族 A16） |

**每条已修故障都要留下一条回归用例**：25 条反模式的可粘贴模板在
[references/regression-templates.md](references/regression-templates.md)（配
`scripts/regression_kit.py` 的假 page/driver，**不联网、不启浏览器**）；
`scripts/check_templates.py` 会把它们真跑一遍，并逐个把「修复换回旧代码」证明用例**必须变红**
（区分力实验的机械化版本，不是靠人回忆）。

完整表与修复模式见 [references/troubleshooting.md](references/troubleshooting.md)。排错顺序：
`DEBUG=1` 全量日志 → `/healthz` → `/diagnostics` / 选择器命中（**不回显正文**）→ 沙箱复现
（拷贝 profile、换 `PORT`、状态目录指到 `/tmp`）。
纪律：**先用证据定位再改代码**；每次修复补回归用例 + **区分力实验**（把修复换回旧代码，
新用例必须变红）；把「AI / 直觉给出的修复建议」先核实字段与 API 真实存在。
**退出条件**：所有已知失败都能映射到错误码；`ruff` / 单测全绿；每个已修故障都有对应回归用例。

---

## ④ 验收（构建通过 ≠ 功能可用）

必须用**用户会用的那个接口**验证（OpenAI SDK / Codex CLI / curl 打真实端点），四层缺一不可
（清单见 [references/acceptance.md](references/acceptance.md)）：

1. **静态与单测**：`ruff` / `mypy`（豁免写明原因）/ `pytest -q` 全绿；测试默认不联网、不启浏览器；
   配置三方一致且无死键。
2. **契约冒烟**（`scripts/smoke_test.py`，退出码即结论）：models / 非流式 / 流式 SSE（首块 `role`、
   增量、`finish_reason`、`[DONE]`）/ 未知字段不 422 / 空输入 4xx / 错误体形状 /
   **token 预算护栏**（无工具请求不该带工具脚手架）/ （可选）工具调用与 `/v1/responses` 命名事件序。
3. **真机对等（E2E）**：直连基线（人直接操作网页）vs bridge 同 prompt 对等执行；
   **上游/环境失败转 SKIP，bridge 不达标记 FAIL**；独立 profile 副本 + 独立会话桶；显式开关默认不跑；
   跑前停掉占用 `user_data` 的服务，跑后确认无残留进程。
4. **真机自愈（沙箱内）**：`await page.close()` 后下一轮自动重建**并续用同一条会话**，`turns` 继续累加。
5. **安全**：只监听 `127.0.0.1`、调试端点默认 404 且不含正文、`user_data/` 与 `.env` 未被提交、
   跨源变更被拒、**两个账号 / 两个桶的数据隔离**、沙箱内的本地工具（路径必须在根内、默认 dry-run）。

**证据留痕**：命令、耗时、响应片段、`/healthz` 快照，写进 `doc/acceptance.md`。
未通过 / 未验证的项**如实报告为限制**，绝不写成通过。
**退出条件**：四层都过；未过的项有名字、有原因、有下一步。

---

## 错误词汇表（提前定死；HTTP 由一处集中映射）

| 现象 | HTTP | `error.type` / `code` | 可观察判据 | 出处 |
| --- | --- | --- | --- | --- |
| 浏览器未就绪 / 未登录 | 503 | `unavailable` | `driver.page is None` 或就绪探测失败（`/healthz` 报 `degraded`） | ChatGPT / Claude |
| 请求本身不合法（空 messages / input） | 400 | `invalid_request_error` | 校验阶段直接拒，不进入浏览器 | ChatGPT |
| 命中上下文到顶提示 | 400 | `context_length_exceeded` | 页面文案匹配 `CAP_NOTICE_PATTERNS`（**扫描前剔除会话正文**） | ChatGPT / Claude |
| 同桶排队超时（本地保护） | 503 | `upstream_busy` | 等锁超过 `BUCKET_LOCK_TIMEOUT_S`；**不触发重试阶梯** | ChatGPT |
| 真的等不到回复 | 504 | `timeout` | 超过 `TIMEOUT`，且内容确实不再变化 | ChatGPT |
| 页面句柄失效 / 渲染进程崩溃 | 502 | `upstream_error` | 页面已关闭、`wait_for_selector` 立刻抛错 | ChatGPT / Deepseek |
| 页面活着但输入区不吃输入 | 502 | `composer_not_found` | 候选全枚举后读回仍为空 / 提交无法确认（可恢复：重建后重发一次） | ChatGPT / Gemini |
| 提交无法确认（提示词还在输入框） | 502 | `prompt_submission_failed` | 页面静止且输入区仍留着本轮提示词 | Claude |
| 命中安全挑战且无法继续 | 503 | `challenge_required` | 三条同时成立：页面 chrome 文案命中 + 命中可见 + **输入区不可用** | Claude |
| 上游明确拒绝（额度用尽 / 风控提示） | 429 | `usage_limit_reached` | 页面可见弹窗命中，且本轮确实没生成 | Claude / Grok |
| 工具调用解析失败 | 502 | `tool_call_parse_error` | 出现标记/围栏却解析出 0 条（诊断 warning） | ChatGPT / Gemini |
| 客户端断开取消 | 499（流内） | `cancelled` | 检测到 `is_disconnected` → 取消并**释放桶锁与页面** | Claude / Grok |
| 未捕获的内部 bug | 500 | `server_error` | ——（**不伪装成 503**） | Claude / Deepseek |

> 每个错误码都要有「谁能观察到它」的判据。没有可观察依据的错误码就是假承诺。

---

## 硬规则

前 12 条来自 ChatGPTBridge 真机事故；第 13–20 条由同族项目实测补齐（已标注出处）。

1. **结束判定以内容为准，不看节点数量**；结合「生成中」信号与静默窗口
   （网页停顿后可能继续补内容，勿截断），停止词口径要「独立 stop 词」——
   侧边栏 `stopAtEnd-<hash>` 曾让 `generating` 恒为 True。
   **阈值一律用「秒」，不要用「轮询次数」**（同族 Claude：调快轮询会等比削弱停滞保护）。
2. **工具调用用代码围栏载体（```tool_call），不用纯文本行**：网页 markdown 渲染会吃掉
   反斜杠、折叠缩进、丢末尾大括号，整条调用会被静默丢弃。载体要真机 A/B 验证
   （字节级回声对照），解析层保留旧格式兼容 + 诊断日志。
   （Gemini 补充：若围栏本身不进 `innerText`，用「标记行 + 围栏」混合载体——标记行负责可识别、
   围栏负责逐字节保真。）
3. **提示词只描述链路，不描述模型自身能力**：任何「你没有 X / 你的工具已挂载」都会被当成
   关于自身能力的事实题而拒答。标准句式：「你是语言模型；这条对话被用户电脑上的本地客户端读取；
   客户端会真实执行你写出的调用并返回输出」。工具空输出必须解释（已执行、无输出、继续），
   否则模型死循环重发同一条。
4. **空输出是成功形态**：工具空输出必须解释，并对重复调用点名（按「工具名 + 规范化参数」签名，
   ≥2 次时要求换命令或收尾）。
5. **任务进入执行阶段后桥不追发 prompt**：纠偏只在本轮「一次工具都没调用过」时生效；
   否则模型已收尾的纯文本会被推成新指令，任务永远收不了尾。
6. **选择器是配置不是代码**：候选用 `||` 串、语义属性优先、哈希类名兜底；改版时先跑
   DOM 探测复位，不凭猜测改。回复容器零节点要打**一次性诊断**（每条选择器命中数 + 页面文本长度）。
7. **composer 候选要全枚举、可见优先、逐个试填校验读回**：页面退化时同一页可能出现多个编辑器，
   「第一个 attached 就返回」会把 prompt 填进点不动的僵尸节点。页面活着但 composer 不吃输入
   要独立成可恢复异常（重建页面后重发，额度与上游重试解耦）。
8. **无头模式不一定可用**：ChatGPT 实测 `HEADLESS=1` 被 Cloudflare 挑战页拦截（页面停在
   `Just a moment...`）；用有头，无显示服务器用 `xvfb-run` 包一层。登录态放持久化 profile。
   复用已有 Chrome profile 时要**复制副本**（Chrome 136+ 拒绝在默认用户目录开远程调试），副本等价于凭据。
9. **会话轮转与播种成对**；绑定到指定网页会话的能力（`/link`）要校验落点（导航后被重定向
   回首页 = 这条地址不可用，记指标），并拒绝客户端**临时 ID**（如 `local-chatgpt%3A<uuid>`，
   约 1 秒后就被正式 ID 替换）。
10. **桥内命令在 HTTP 边缘本地应答**（`/bridge ...`、`/link`）：浏览器没起来、句柄已失效时
    它们必须还能用；只认「整条消息就是那一行命令」，只作用于当前桶，回执写清影响范围。
11. **输出参数用「描述实际效果」的方式**：`usage` 是合成/估算就写明；`/v1/models` 只返回
    真实配置的模型名；开关切不动就如实报告失败，绝不谎报。
12. **工程纪律**：不削弱断言、不用 skip 掩盖失败、不吞异常；配置漂移由测试守护；
    模块拆分保留旧 import 路径 + 加 facade；CI 跑 `ruff` + `mypy` + `pytest`。
13. **写入是复合动作**（Gemini）：`fill()` = 建立选区 + `Input.insertText`；裸
    `keyboard.insert_text` / `execCommand` 只在**当前选区**插入，没有选区就是**静默空操作**
    （不报错、0 字符）。分块写入前必须显式放置光标。
14. **读回是有损的，只能发现失败、不能否决成功**（Gemini）：富文本编辑器读回丢换行、markdown
    变格式。用有损读回否决一次成功的 `fill`，等于把写好的 prompt 判死，再去清空重写反而弄坏它。
15. **提交必须验证**（Gemini）：合成 `KeyboardEvent` 是 `isTrusted=false`，受控编辑器常直接忽略。
    阶梯 = **真实键盘 Enter → 原生 `click()` 发送按钮 → 合成 Enter**，每级之后验「输入框已清空
    或页面进入生成中」；读不到输入框内容时返回「无法判断」，不据此报错也不空等。
16. **结束判定是多信号与门**（Grok / Gemini）：停止控件消失 + `aria-busy` 为假 + 状态文案转过去时
    + 内容静默窗口；**丢掉任一信号都会读到半截**。读文本先**克隆节点并去掉动画类**，再取全文
    （`innerText` 取不到未显现 token，会让 JSON 只剩半截）。
17. **新回复两把钥匙**（Claude / Gemini）：① assistant 节点数 > 基线；② 结构指纹
    （本轮提问的文本指纹变化 + 其后出现 assistant 节点）。指纹口径能扛住 DOM 回收/虚拟化导致
    条数不升反降。**页面里永远存在上一条 assistant 节点**，只凭「有节点」会把上一轮当本轮。
18. **超长 prompt 分块写入**（Gemini）：一次性写入会把几万字符排成网页主线程上的长任务，
    页面假死、连「元素是否可编辑」都探测不到。分块（默认 4000 字符）+ 每块之间让出主线程 +
    每块前重读输入框只补缺失段（幂等、可续写）。
19. **内置工具按需注入，长度控制分三层**（Gemini）：客户端未声明工具就不得注入内置工具
    （曾每请求白付 ~970 tokens 脚手架，还产生客户端没声明的 `tool_calls`），无工具请求要有
    **token 预算回归护栏**；工具结果长度 = 单条上限（截短 + 标注「已截断 N 字符」）→ 拼装后成品
    预算 → 发送侧兜底截断（必须记 WARNING，不静默丢弃）。
20. **取消与状态的两条铁律**（Claude / Grok）：客户端断开时**不能 await 被取消的 watcher**
    （会卡在不可取消的 `receive()`，把非流式路由拖死）；断开即取消 + 归还页面 + 释放桶锁 + 配测试。
    指标 / 状态注册表**每测试独立注入**（进程单例会让测试互相串味）；会话 URL 比较用
    「主机 + 路径精确匹配」，不要前缀比较（会话 ID 只差末位会被误判同一条）。

---

## 事故档案（反模式检查项）

每条都真实发生过；接新站点时逐条核对。「出处」= 该反模式最初在哪个同族项目被踩到。
**每一条都有对应的回归用例模板**（[references/regression-templates.md](references/regression-templates.md)，
编号一一对应）：它是这份档案从「读过」变成「拦得住」的唯一方式——
`scripts/check_templates.py` 会验证模板能跑、能把修复换回旧代码后变红、且覆盖本表全部条目。

> 编号约定：`P0-*` / `A1–A16` 是**反模式编号**，与
> [references/acceptance.md](references/acceptance.md) 里的**验收检查项** A1–A11 / R1–R2 不是同一套东西
> （前者是「别怎么做」，后者是「要验什么」）。

| 代号 | 反模式 | 修法 | 出处 |
| --- | --- | --- | --- |
| P0-A | 回显当成回复：每轮 prompt 长度恒定、回复其实是上一轮内容 | 发送前基线与轮询**同一口径**（取最后一个**有正文**的节点）；收尾要求「新正文」且非回显（指纹去空白 + 长度比容忍有损渲染）；**宁可超时也不返回旧内容** | ChatGPT |
| P0-B | 落盘方法漏 `self`/`@staticmethod` → 每个非流式请求最后一步 500 | 改 `@staticmethod` + 补「按实例调用」用例（测试全按类上调用会掩盖它） | ChatGPT / Deepseek |
| P0-C | 建页与拿锁之间页面被别的桶回收 → **随机 502** | 「有请求在飞」改成**可重入计数**，不是看锁 | ChatGPT / Deepseek |
| P0-D | 标签被关/崩溃后页面仍留在池里 → 该桶**永久 502**，提示却写「请检查登录」 | 判活 + 专用 `PageLostError` + 无条件重建回原会话 + 默认桶也重建 + 重试阶梯接纳 | ChatGPT / Deepseek |
| P0-E | 用「状态里有 url」当「页面真在会话里」 | `goto` 后反查真实落点，按落点重算 `has_history` | ChatGPT / Deepseek |
| P0-F | 全页文本扫「到顶提示」→ 会话里引用过这句话就每个请求都误报到顶 | 扫描前**剔除会话正文**（含用户消息）；只在即将判定失败时扫 | ChatGPT / Claude |
| P0-G | 侧边栏装饰类名 `stopAtEnd-<hash>` 让 `generating` 恒为 True → 每次拖满超时+延长 | 停止词收紧为「独立 stop 词」`(^|[-_])stop([-_]|$)` + 必须可见 + 优先视口下半部 | ChatGPT |
| P0-H | 纯文本 `TOOL_CALL` 行被 markdown 渲染改写 → 调用静默丢弃 | 换代码围栏载体 + 真机字节级 A/B + 解析层兼容与诊断 | ChatGPT |
| P0-I | 提示词否定式措辞（「你没有 shell」）被读成能力事实 → 拒答 | 只描述链路；回归测试断言禁用 `agent` 等字样 | ChatGPT |
| A1 | 播种标记只信磁盘 `has_history` → 进程重启后串线 | 标记**同时绑内存页面对象**（`_seeded_page`），页面对不上就重新完整播种 | Claude |
| A2 | 提交后无条件重发 → 创建重复对话/回答 | 提交后只做「读页面现有回复」式恢复，无法安全恢复就明确失败 | Claude |
| A3 | 不可恢复错误（输入区缺失 / 登录失效）也进重试阶梯 | `_UNRECOVERABLE_*` 命中即冒泡；只对无副作用的阶段重试 | Claude / Deepseek |
| A4 | 挑战页误报：正文里出现 "security verification" 就让每个请求 503 | 只扫页面 chrome + 命中须可见 + `challenged = 命中 and 输入区不可用` | Claude |
| A5 | 探测凭 DOM 命中报成功（HTTP 403 的壳里仍有输入框） | 成功判据必须含 `navigation_ok`（200≤status<400） | Claude |
| A6 | 额度用尽弹窗被报成「解析失败 / 停滞」→ 客户端无限重试 | 提交前关一次 + 即将判定失败时再探一次（只点关闭/稍后类，**绝不点升级**）+ 归因 `usage_limit_reached`(429) | Claude / Grok |
| A7 | 配置「写了没用上」（死键）→ 虚假的保护感 | 漂移测试 + 死键 grep：要么接线，要么删 | Claude / Gemini |
| A8 | 用「整页文本减子树文本」字符串替换来剔除子树 | 同一段文本出现两次只剔掉一处 → 在 **body 克隆体上剪掉子树**再读文本 | Claude |
| A9 | 写入只做一次、读回不符就清空重写 | 有损读回**不能否决**成功的 `fill`；只有 `fill` 报错才读回救援 | Gemini |
| A10 | 照抄 AI 给出的修复建议（曾引用不存在的字段） | 先核实它引用的字段 / API 真实存在 | Deepseek |
| A11 | 只按「第一个 attached/可见」取元素 | 页面退化时同页多候选 → 枚举 + 排序 + 逐个校验读回 | ChatGPT |
| A12 | 用前缀比较 URL 判断会话是否同一条 | 解析 URL 后**主机 + 路径精确匹配** | Claude |
| A13 | 测试共用指标单例 / 状态注册表 | 每测试注入独立 registry，否则计数串味 | Claude |
| A14 | 验收只跑 happy path、未过的项含糊成「应该没问题」 | 未验证就写「未验证」；区分力实验证明修复真的生效 | Gemini |
| A15 | 用**轮询次数**当结束/停滞阈值 | 一律改成「秒」+ `IdleClock`：调快轮询会等比削弱停滞保护 | Claude |
| A16 | 取消时 `await` 被取消的 watcher → 路由被拖死 >8 分钟 | 只 `cancel()`，**不 await**；断开即取消 + 归还页面 + 释放桶锁 | Claude / Grok |

---

## 取舍矩阵（同族项目的分歧：没有唯一答案，但要显式选）

| 议题 | 路线 X | 路线 Y | 怎么选 |
| --- | --- | --- | --- |
| 页面重建后的会话 | **回到状态里的会话 URL**（ChatGPT / Deepseek） | **不恢复 URL，新开对话 + 重放历史**（Gemini：会话 URL 与「当前对话」绑定不稳定，回填易落空白页） | 用一次**落点校验实验**决定：`goto` 后反查 URL，若稳定停在该会话就用 X；否则用 Y（并保证播种可用） |
| 结束判定阈值 | 轮询次数（早期） | **秒 + `IdleClock`**（Claude / ChatGPT 最终态） | 一律用秒；次数阈值会在调快轮询时被等比削弱 |
| 工具调用载体 | 纯文本标记行 | 代码围栏（ChatGPT） / **标记行 + 围栏**（Gemini：围栏不进 `innerText`） | 用真机 A/B 回声对照决定；解析层同时兼容三种形态 |
| 无头 vs 有头 | `HEADLESS=1`（省事） | **有头 / `xvfb-run`** | 先验一次；被挑战页拦截就只能有头 |
| 页面外壳剔除 | 字符串替换（静默失败） | **克隆体剪子树** | 一律用克隆体剪子树 |
| 并发 | 各桶并行（快） | **默认全局串行**（稳） | 默认串行；并行是显式开关并接受风控风险 |

---

## 同族单一共享源（`family.json` + 漂移测试）

同族项目（ChatGPT / Deepseek / Claude / Gemini / Grok 五个桥）不该各维护一份会各说各话的 skill。
约定：**本 skill 目录是唯一共享源**，成员只保留站点专属部分。

| 类别 | 内容 | 规则 |
| --- | --- | --- |
| 共享 | `SKILL.md` **正文**、`references/*.md`（除案例）、`scripts/*.py`、`tests/*.py` | 只在共享源里改，成员由 `family_sync.py` 对齐 |
| 站点专属 | `SKILL.md` 的 frontmatter（`name`/`description`/触发词）、`references/case-study-*.md`、`assets/config.template.env`、`family.json` | 差异就该留在这里；`sync` **永不动**这些文件 |
| 版本钉 | 成员 `SKILL.md` 的 `family-source: <家族>@<版本>#<hash8>` | `hash8` 覆盖全部共享内容（`SKILL.md` 只算正文），**改了共享文件就要更新钉** |

```bash
python3 skills/web-to-openai-api/scripts/family_sync.py check        # 只读：谁漂了、钉是否过期
python3 skills/web-to-openai-api/scripts/family_sync.py diff DeepseekBridge
python3 skills/web-to-openai-api/scripts/family_sync.py sync DeepseekBridge          # dry-run 计划
python3 skills/web-to-openai-api/scripts/family_sync.py sync DeepseekBridge --apply  # 写入（自动先备份）
```

纪律：**漂移是报告，不是自动覆盖**——`sync` 默认 dry-run，写前把原文件备份到
`.skill-family-backup/<时间戳>/`，且绝不删除成员自己的文件；`check` 的结果进 ③ 阶段门禁
（R5：共享源的钉必须新鲜，否则等于「假共享源」）。skill 自带的
`skills/web-to-openai-api/tests/` 就是这套工具的回归测试（含 dry-run / 备份 / 不碰站点专属文件）。

---

## 发布 / 拷贝（整份自包含，拷到哪都能用）

这份 skill 是**靠拷贝交付**的（各桥项目的 `skills/` 目录本身不进版本库），所以「拷走还能不能用」
是它的核心契约之一。

**一起拷什么**：整个 skill 目录 —— `SKILL.md` + `references/` + `scripts/` + `tests/` +
`assets/` + `family.json`。**不要**带上 `__pycache__/`、`.pytest_cache/`、
`.skill-family-backup/`（缓存与历史备份不属于要发布的内容）。

**拷完先跑这三条自检**（前缀换成新位置的相对路径）：

```bash
python3 <skill>/scripts/check_templates.py     # 25 条模板真跑 + 逐条区分力 → 期望「结论：通过」
python3 -m pytest <skill>/tests -q             # 工具自身的回归测试（含可移植性）→ 期望全绿
python3 <skill>/scripts/gate.py --root <桥项目> --python <其 venv>/bin/python   # 四阶段门禁
```

**`family.json` 怎么处理**（拷贝后唯一需要手动决定的文件）：

| 情况 | 怎么设 | 会得到什么 |
| --- | --- | --- |
| 只要这份 skill 本身（最常见的「拷到别处用」） | `canonical_skill` 改成 `"."`，`members` 只保留自己 | 本副本**自成共享源**：`check` 拿自己的共享文件算指纹，门禁 R5 也能给出真实结论 |
| 还要跟同族仓库对漂移 | `canonical_skill` 指向真正共享源目录 | `check` / `diff` / `sync` 全可用；`members` 的路径以 `canonical_skill` 为基准 |

**硬规则**：找不到共享源时，工具必须**报环境错误并退出 2**，绝不输出「一致」——
用空指纹说「一致」等于把「没检查」伪装成「通过」，正是本 skill 反复强调的那条红线；
`gate.py` 的 R5 因此也不会给 PASS（两个方向都有回归用例钉住）。

**依赖**：`scripts/` 里除 `recon.py` 外只用 Python 标准库（3.9+ 即可跑）；
只有 `recon.py` 需要 Playwright + Chromium（它要真的开浏览器侦察）。

---

## 诚实性要求

- `/v1/models` **只返回真实配置的模型名**，不虚构型号或上下文窗口。
- `usage` 是合成/估算就写明；流式是「SSE 合成流」（不是上游 token 流）也要写明。
- 未验证的能力（如 `/v1/responses` 子集）默认关闭，验证通过再开。
- 开关切不动 / 参数不生效时**如实报告失败**，绝不谎报成功。
- 验收输出里未通过、未验证的项**必须原样保留为限制**。
- 三条验收元规则（Gemini）：**「未复现」不等于「已排除」**；**先检查测量工具本身**
  （读回不含换行曾让三轮排查找错方向）；**区分力实验是唯一证据**。

---

## 安全红线

- 只监听 `127.0.0.1`：转发的是你自己的登录会话，暴露到网络等于把账号交出去。
- `user_data/`（Cookie / Session）与 `output/` 不进版本控制；`.gitignore` 必须先写好（含 `.env`）；
  **不要**把 `user_data/` 同步到 iCloud / Dropbox 等云盘。
- 不做验证码绕过、不做挑战页规避、不做风控规避、不做多账号池；遵守目标站点 ToS，仅供个人学习研究。
- 调试端点默认关闭且**不回显对话正文 / 提示词 / Cookie**；日志里不打印完整 prompt。
- 本地写文件：**只要路径能由模型内容决定**（落盘代码块用回复里的标题、edit 工具等），就必须有沙箱——
  路径 resolve 后必须落在项目根内，越界/绝对路径一律拒绝，默认 dry-run，不提供「关闭沙箱」的开关。
  本项目采用**生成式命名**（`code_<ts>_<idx>_<uuid>.<ext>`）从而天然没有路径注入面，这也是最省事的做法。
- 可选 Bearer 门（`BRIDGE_TOKEN` 式）默认关闭以保持兼容；开启后只保护生成端点，探活与模型发现保持开放
  （本项目未做鉴权，**只绑回环地址是当前唯一防线**，迁移到新站点时建议补上）。

---

## 交付物清单

```text
doc/design.md                 # ① 侦察结论 + 路线选择 + 契约 + 会话决策 + 错误语义 + 风险 + 验收标准 + 不做的事
doc/tasks.md                  # 任务分解：模块映射 / 状态 / 优先级 / 验收标准 / 待真实验证项 / WONTFIX 理由
doc/update.md                 # ③ 故障复盘台账：现象 / 证据 / 根因 / 修复 / 验证 / 未验证
doc/e2e_test_design.md        # ④ 真机对等测试：判定矩阵 / 开关 / 前置条件 / 陷阱
doc/acceptance.md             # ④ 验收记录（命令 + 证据 + 未通过项 + 已知限制）
<site>_web/                   # ② 实现包（api/browser/completion/session/tools/config…）
<site>_api_server.py          # 入口薄封装
.env.example  .env            # 配置契约（与 README、config 三方一致）
tests/                        # 假 page/driver 回归测试（默认不联网、不启浏览器）
tests/e2e/                    # 直连基线 + bridge 对等 E2E（显式开关才跑，默认 skip）
client_test.py                # 用官方 openai SDK 的联调示例（「用户会用的那个接口」）
skills/web-to-openai-api/…    # 本 skill 的落地副本（scripts 供持续使用）
  ├── SKILL.md                 # 四阶段流程 + 硬规则 + 事故档案 + 取舍矩阵
  ├── family.json              # 同族单一共享源清单（共享 / 站点专属 / 成员 + 版本钉）
  ├── references/              # 设计 / 编码 / 排错 / 验收 + 站点案例 + 回归用例模板
  ├── scripts/                 # recon / smoke_test / gate / check_templates / regression_kit / family_sync
  └── tests/                   # 本 skill 工具链自己的回归测试（离线）
```

每完成一步更新对应文档；**文档与代码不一致时以代码为准并立刻改文档**。
每个真机故障都要写一份复盘文并补回归测试。

---

## 执行顺序速记

```text
0 读案例 → references/case-study-<站点>.md（本站在同族里的复盘；知道别人踩过什么，尤其是 P0-A..P0-I）
1 侦察   → recon.py（--network-log 只读探测）；命中表 + 流式形态 + 接口线索 → 定 A/B/C 路线
2 设计   → design.md + 验收标准（先于代码）；错误词汇表一次定死
3 编码   → A 原型 → B Chat Completions → C 多会话/可靠性 → D 工具调用 → E Responses/文档
4 排错   → 症状 → 错误码 → 不含正文的现场 → 回归测试 + 区分力实验 → 回写 update.md
5 验收   → 静态/单测 → smoke_test.py → 真机对等 E2E → 沙箱自愈 → 安全；证据进 acceptance.md
6 门禁   → gate.py（四阶段退出条件一条命令；退出码即结论，跳过的项如实标注）
```

同族项目移植时优先读**本站**的 `references/case-study-<站点>.md`（站点专属文件，同族目录里
还有别的站点案例可对照），第六节「迁移到新站点的核对表」与本文档的**事故档案**、
**取舍矩阵**，再按四阶段执行。
