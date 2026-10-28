# ④ 验收阶段：四层清单 + 证据

**首要原则：构建通过 ≠ 功能可用。** 必须用**用户会用的那个接口**（OpenAI SDK / Codex CLI / curl）
打真实端点，并留下证据。没有证据的「已完成」不算完成；未通过的项**如实报告为限制**。

自动化入口：

```bash
# 契约冒烟（对运行中的桥；退出码非 0 = 有硬失败）
python3 skills/web-to-openai-api/scripts/smoke_test.py --base-url http://127.0.0.1:8002 \
  --session demo-1 --session-header X-ChatGPT-Session

# 四阶段门禁（退出码即结论：0 全过 / 1 有 FAIL / 2 跑不起来；--strict 时 SKIP 也算失败）
python3 skills/web-to-openai-api/scripts/gate.py                      # 离线：① ② ③ + ④ 的文档与安全
python3 skills/web-to-openai-api/scripts/gate.py --base-url http://127.0.0.1:8002 --token-budget 500

# 回归模板自检（能跑 / 能把修复换回旧代码后变红 / 覆盖事故档案全部反模式）
python3 skills/web-to-openai-api/scripts/check_templates.py
```

四层缺一不可：**① 静态与单测 → ② 契约冒烟 → ③ 真机对等（E2E）→ ④ 安全**。

---

## 1. 静态与单测（每次改动都跑）

```bash
.venv/bin/ruff check .            # 静态检查（豁免必须写原因，不得静默忽略）
.venv/bin/mypy chatgpt_web        # 类型检查（只允许基线内既有错误，不得新增）
.venv/bin/python -m pytest -q     # 单测：不联网、不起浏览器；E2E 默认 skip
```

| # | 检查 | 期望 |
| --- | --- | --- |
| S1 | 单测计数 | 只增不削；`skipped` 全部是需人工环境的 E2E |
| S2 | 配置漂移 | `.env.example` 覆盖 config 全部键；README 默认值与 config 字面量一致 |
| S3 | 模块拆分 | 旧 import 路径保留 + facade；API 行为不变 |
| S4 | 源码卫生 | 无 `asyncio.get_event_loop`、无新增裸 `except: pass`、无业务 `print` |

---

## 2. 契约冒烟（自动化，`scripts/smoke_test.py`）

> 本节的 A1–A11 / R1–R2 是**验收检查项编号**；[../SKILL.md](../SKILL.md) 事故档案里的 A1–A16 是
> **反模式编号**，两套编号不要混。

| # | 检查 | 期望 |
| --- | --- | --- |
| A1 | `GET /healthz` | 200；含 `browser_ready` / `init_error` / 会话桶快照 |
| A2 | `GET /v1/models` | 200；`object=list`，`data` 非空且含预期 model id |
| A3 | 非流式 `POST /v1/chat/completions` | 200；`choices[0].message.content` 非空；`finish_reason`；有 usage（估算要标注） |
| A4 | 流式 `stream=true` | 收到 `data:` 增量 chunk 且以 `data: [DONE]` 结束；首字节延迟合理 |
| A5 | 未知字段 | 带 `temperature/top_p/reasoning_effort/自定义字段` 仍 200，**不返回 422** |
| A6 | 空输入 | `messages` 为空 / 缺字段 → 400（不是超时、不是 500） |
| A7 | 错误体形状 | 任何错误都是 `{"error":{"message","type","code"}}`，不是裸字符串 |
| A8 | 状态码映射 | 到顶 400 `context_length_exceeded`；未就绪 503；找不到输入框 502；超时 504；同桶忙 503 `upstream_busy` |
| A9 | `stream_options.include_usage` | usage 块落在 `finish_reason` 之后、`[DONE]` 之前，且文档注明来源（估算） |
| A10 | token 预算护栏 | 无工具请求的 `usage.total_tokens` ≤ `--token-budget`（超了通常=内置工具脚手架泄漏进了普通请求） |
| A11 | `/readiness`（如实现） | 能区分四种不可用：浏览器未起 / 未登录 / 遇到挑战 / 输入区缺失，各有 `error_code` |
| R1 | `/v1/responses` 非流式（可选） | `output_text` / `output[]` 里有正文，`status` 合理 |
| R2 | `/v1/responses` 流式（可选） | 命名事件 ≥2 种、`sequence_number` 严格递增、`response.completed` 或 `[DONE]` 收尾 |

---

## 3. 能力验收

| # | 检查 | 期望 |
| --- | --- | --- |
| B1 | 工具调用（围栏） | `tools` 注入后模型回 ` ```tool_call ` → 解析出 `tool_calls`，`finish_reason=tool_calls` |
| B2 | 工具调用（渲染退化形态） | DOM 把围栏渲染掉后只剩标签行 + JSON，仍能解析；`json` 围栏不得被当调用 |
| B3 | 工具回填 | 下一轮带 `role:"tool"` 结果能拼回 prompt；空输出有显式说明；重复签名被点名 |
| B4 | 代码块落盘 | 回复含代码块 → 有落盘路径且内容正确；无代码块 → 存 `.md`；可关闭 |
| B5 | 长 prompt | 超 `PROMPT_MAX_CHARS` 时截断并告警，请求仍成功 |
| B6 | 聊天内命令 | `/link`、`/bridge ...` 本地应答；浏览器未就绪时仍可用；只作用于当前桶 |

---

## 4. 会话验收

| # | 检查 | 期望 |
| --- | --- | --- |
| C1 | 分桶隔离 | 两个不同 `X-<Site>-Session` 各自独立上下文（同一任务上下文一致） |
| C2 | 自动分桶 | 无显式标识时按 `user` / 工作目录 / UA 分开 |
| C3 | 轮转 + 播种 | 超预算后下一轮轮转；新会话能续上任务上下文（任务目标不被截断） |
| C4 | 到顶 | 撑满会话 → 400 `context_length_exceeded`（**不是** 504） |
| C5 | 逃生口 | `/session/reset` 后下一轮确实开新会话且播种 |
| C6 | 桶回收 | 制造 > `MAX_SESSION_BUCKETS` 个桶不报永久失败；空闲页被回收、状态保留 |
| C7 | 并发语义 | 默认串行；同桶并发在 `BUCKET_LOCK_TIMEOUT_S` 内返回 503 `upstream_busy` |
| C8 | 绑定（可选） | `/link <URL>` 后每轮都回到该会话；临时 ID 被拒；落点漂移有告警与指标 |
| C9 | 断连取消 | 客户端中途断开：发送/生成被取消、页面归还、桶锁释放；**服务不卡死**（不得 `await` 被取消的 watcher） |
| C10 | 页面重建后会话 | 重建后要么续用同一条会话（`turns` 继续累加），要么新开 + 播种能续上任务（按 design.md 的取舍结论验收） |
| C11 | 不复现也要修 | 代码层能确定的缺陷，即使真机未触发也写回归用例（「未复现」不等于「已排除」） |

---

## 5. 真机对等 E2E（显式开关，独立记录）

### 5.1 判定矩阵（核心）

**直连（人直接操作网页）是「上游能力基线」**：同一 prompt、同一断言分别在直连与 bridge 执行。

| 直连侧 | bridge 侧 | 判定 |
| --- | --- | --- |
| 达标 | 达标 | **PASS** |
| 达标 | 不达标 | **FAIL**（bridge 缺陷） |
| 不达标 / 无法执行 | — | **SKIP**（环境 / 上游问题，不算 bridge 缺陷） |

实现要点：

- 直连侧**不复用**被测算法的发送/轮询/结束判定（避免用被测代码验证被测代码）；
- 上游/环境类失败（HTTP 502/503/504、`error.type ∈ {timeout, upstream_error,
  context_length_exceeded}`）一律转 SKIP；
- 每次尝试使用**独立会话桶**（如 `e2e-{case}-r{n}`），避免复用上一次失败的上下文；
- 「模型不按约定调用工具」属**软失败**：计入汇总、可重试一次，**不得用 skip 掩盖**。

### 5.2 用例矩阵（可照搬）

| 组 | 内容 |
| --- | --- |
| A 内容对等 | 哨兵、事实、长文尾哨兵（含长度比与中文占比） |
| B 协议 | `/healthz`、`/v1/models`、SSE 序列（首块 role / `[DONE]`）、Responses 命名事件、官方 SDK 冒烟 |
| C 工具调用 | 非流式 / 流式 function calling |
| D 会话 | 多轮记忆、跨桶（观察项）、reset 后播种保上下文 |
| E Markdown/文件工具 | render → 模型 edit → apply → write（dry-run） |
| F 提交链路 | 多行（含代码块）提交、残留草稿清理后提交 |
| 探针 | 选择器校准（不依赖 bridge，网页改版后复位 `.env`） |

### 5.3 Gating 与前置条件

| 变量 | 作用 |
| --- | --- |
| `CHATGPT_E2E=1` | 才运行 E2E；否则全部 skip |
| `E2E_HEADED=1` | **必须**：无头会被挑战页拦截，结果无效 |
| `E2E_PORT` | 显式指定端口，免疫宿主 shell 自带的 `PORT`（历史事故） |
| `E2E_FULL=1` | 启用官方 SDK 端到端冒烟 |
| `CHATGPT_DEBUG=1` | 服务侧选择器自检与轮询日志 |

跑前：停掉占用 `user_data` 的服务（`pkill -f <entrypoint>`）、清残留 `Singleton*`；
跑后：`pgrep` 确认无残留 uvicorn / Chromium；耗时汇总与 WARNING（bridge/direct > 3）打印留存。

### 5.4 真机验收命令（示例）

```bash
# 全量 E2E（真实访问，约 8–25 分钟；先停 bridge）
CHATGPT_E2E=1 E2E_HEADED=1 E2E_FULL=1 E2E_PORT=8002 \
  .venv/bin/python -m pytest tests/e2e -v -s

# 单条（例：工具调用对等）
CHATGPT_E2E=1 E2E_HEADED=1 E2E_PORT=8002 \
  .venv/bin/python -m pytest tests/e2e/test_parity.py -k c1 -v -s

# DOM / 选择器校准（不依赖 bridge）
CHATGPT_E2E=1 E2E_HEADED=1 .venv/bin/python -m pytest tests/e2e/test_dom_probe.py -v -s
```

---

## 6. 安全验收

| # | 检查 | 期望 |
| --- | --- | --- |
| E1 | 监听地址 | 仅 `127.0.0.1`（`lsof -iTCP -sTCP:LISTEN -P \| grep <port>`） |
| E2 | 敏感目录 | `git status` 与 `.gitignore` 确认 `user_data/`、`.env`、`output/` 未被跟踪 |
| E3 | 调试端点 | 未开 DEBUG 时 404；开启时响应**不含对话正文 / 提示词 / Cookie** |
| E4 | 日志 | 不打印完整 prompt / Cookie / Token |
| E5 | 本地工具沙箱 | `../../etc/passwd`、绝对路径被拒；默认 dry-run；写前备份 |
| E6 | 绑定校验 | 非白名单主机、非会话路径、临时 ID 均被拒 |
| E7 | 跨源变更 | 带 `Origin` 的跨站写请求被拒（`isSameOriginRequest` 式校验）；个性化响应带 `private, no-store` |
| E8 | 数据隔离 | 两个账号 / 两个桶的数据不串（跨桶上下文隔离、落盘目录分隔；不隔离的项要写成**已知限制**） |
| E9 | 云盘 | `user_data/` 没有被 iCloud / Dropbox 之类的目录同步（等于把 Cookie 复制出去） |

---

## 7. 证据模板（`doc/acceptance.md`）

```markdown
# 验收记录（<日期> / <commit>）

## 环境
- 服务：<PORT / HEADLESS / 关键 .env>   浏览器：<Chromium 版本>
- 客户端：OpenAI SDK <版本> / Codex CLI <版本> / curl

## 结果
| 项 | 命令 | 结果 | 证据（耗时 / 片段 / healthz） |
| --- | --- | --- | --- |
| S1 | pytest -q | ✅ | 657 passed / 17 skipped / 0 failed |
| A2 | smoke_test A2 | ✅ | data=[chatgpt-chat, chatgpt-reasoner] |
| A4 | smoke_test A4 | ✅ | 42 chunks + [DONE]，first=0.02s |
| C3 | 轮转 + 播种 | ✅ | turns 12→1 后仍记得任务目标 |
| D2 | 沙箱 page.close() 后复测 | ✅ | 新页面 + 同一会话 URL，turns 12→13 |
| ... | | | |

## 未通过 / 未能验证
- <项>：<原因>（缺账号 / 环境不支持 / 已知限制）——**如实记录，不写成通过**

## 已知限制
- <如：生成参数不生效；带 tools 时流式需先缓冲；usage 为估算；无头被拦截>
```

---

## 8. 交付定义（DoD）

**离线（每次改动都要绿）**

- [ ] `ruff check .` 全绿；`pytest -q` 全绿（E2E 默认 skip）；`mypy` 无**新增**错误。
- [ ] `gate.py` 四阶段全过（含 `--base-url` 时的契约冒烟）；SKIP 的项逐条写明原因，不当成通过。
- [ ] `check_templates.py` 退出码 0：事故档案每条反模式都有**能跑、能红**的用例（不是空断言）。
- [ ] `.env.example` ↔ config ↔ README 三方一致，且**无死键**。
- [ ] 启发式逻辑（结束判定 / 写入 / 提交 / 恢复）用假时钟 + 假 page/driver 可穷举，不启浏览器。
- [ ] 断连路径：非流式与 Responses 都检测 `is_disconnected`，断开即取消并释放桶锁与页面。
- [ ] 会话分桶优先级 / 键清洗 / 桶锁 / LRU / 闲置回收都有单测。
- [ ] 指标与状态注册表**逐测试注入**（不用进程单例）。

**真实（显式开启，独立记录）**

- [ ] readiness 四态可分；单轮短提示词返回**确定的新回复**（不误识别历史内容）。
- [ ] 多轮播种后模型能基于前文回答；轮转后任务目标不被截断。
- [ ] 流式拼接文本与非流式结构一致（Markdown / 长文本 / 换行 / 代码块不截断不重复）。
- [ ] 两桶并发不串线；同桶连续请求顺序确定；超时 / 页面丢失 / 未登录 / 额度用尽 / 到顶
      返回**正确且可区分**的错误分类。
- [ ] 工具调用能转成合法 `tool_calls`；工具结果轮次不污染其他桶。
- [ ] 安全：默认回环；诊断/指标无正文；绑定 URL 强制白名单 + 会话路径；跨源变更被拒。

**诚实性与取证**

- [ ] 四层验收都跑过；S1–S4 / A1–A11 / R1–R2（适用项）/ B1–B6（适用项）/ C1–C11（适用项）/
      E1–E9 每一项都有结论。
- [ ] 每条修复都有**有区分力**的回归测试（把修复关掉必须变红）；测试计数不低于改动前。
- [ ] `doc/design.md`、`doc/update.md`、`doc/e2e_test_design.md`、`doc/acceptance.md` 与代码一致。
- [ ] `/v1/models` 只返回真实配置的模型名；usage/流式如实声明；未验证项在 README 里列为限制。
- [ ] 三条元规则已执行：未复现≠已排除 / 先检查测量工具本身 / 区分力实验是唯一证据。
- [ ] 这条修改过的共享文件（`SKILL.md` 正文 / `references/*.md` / `scripts/*.py` / `tests/*.py`）
      已同步更新 `SKILL.md` 的 `family-source` 钉，`family_sync.py check` 无 STALE-PIN。
