# ③ 排错阶段：症状 → 根因 → 判定 → 修复

本文件把 ChatGPTBridge 与同族项目的真实事故沉淀成可查询的表。拿到故障先**二分**，
不要先改选择器。

---

## 0. 排错顺序（照做，能省掉一半时间）

1. **看耗时**：瞬时失败 vs 等满超时，指向完全不同的根因（见 §1 判定表）。
2. **看范围**：只有某个会话桶 / 某类客户端挂，还是全挂？只有单桶 → 页面池问题；
   全挂 → 浏览器/登录/站点问题。
3. **看日志**：`DEBUG=1` 打开全量轮询日志（节点数 / 文本长度 / 稳定计数 / 生成中状态 /
   快速失败计数）。注意：节点数恒 0 且有「一次性诊断」时，先看诊断里的选择器命中数。
4. **看现场**：`/healthz`（`browser_ready` / `init_error` / `keys` / `session`）→
   `/_debug/selectors`（每条选择器命中数，需 DEBUG）→ `/diagnostics`。
5. **沙箱复现**：拷贝 `user_data`（先删 `Singleton*`）、换 `PORT`、状态/输出目录指到 `/tmp`，
   用**真浏览器**跑真 driver。**别拿线上实例做实验**（persistent context 同一 profile 不能并行）。
6. **改完补回归测试**，再回到 ④ 决定是否验收。**先证据、后修改**；核实建议里引用的字段/API
   真实存在（曾有 AI 建议引用虚构字段 `needs_reseed`，照改会引入新 bug）。

---

## 1. 症状 → 根因速查

| 症状 | 根因 | 判定（怎么确认） |
| --- | --- | --- |
| 某桶**几乎立刻**失败（0.x 秒），其它桶正常 | 页面池里留着**已死的页面**（标签被关 / 渲染进程崩溃） | 失败耗时 ≈ 0；日志搜 `Target page` / `closed` / `crash` |
| 同一错误却熬满选择器超时（如 3 候选 × 4s） | 页面活着但真的没有输入框（改版 / 非对话视图 / 未登录 / 弹层） | 失败耗时 ≈ 轮数 × 候选数 × 超时 |
| 轮询一直 `nodes=0`，直到超时 | **回复选择器失效**（网页改版换容器名） | 一次性诊断打印每条选择器命中数 + 页面文本长度 |
| 每个请求都拖到 180s + 延长额度才返回空 | `generating` 误报恒为 True（装饰类名含 stop 词） | 命中控件里找 `stopAtEnd-<hash>` 一类（可见但根本不是停止按钮） |
| 回复已生成完，客户端却收到 500 | 生成后的**收尾代码**炸了（落盘签名、编码、序列化） | 日志 `TypeError: ... takes N positional arguments but M were given` |
| **随机 502**：重试/别的桶建页时正常 | 建页与拿锁之间的窗口里页面被空闲回收/LRU 淘汰 | 并发时出现；单请求稳定复现不了 |
| 一直等到总超时、没有任何新回复 | 结束判定失灵，或消息压根没发出去 | 关闭 DEBUG 看有无 `[截断]` / 生成中状态从未出现 |
| 长会话突然「卡住」（其实是到顶） | 网页会话上下文达上限，页面弹提示并停止响应 | 页面文本含 `CAP_NOTICE_PATTERNS`；`/healthz` 的 `cap_hit` |
| 客户端报「输入框卡死」/ fill 超时 | 一次性写入超长 prompt 在网页主线程排成长任务 | 复现：超长 prompt + 整段 `fill` |
| fill 每次都读回 `''`，turns 不动 | **composer 候选退化**：同页多个编辑器，选到点不动的僵尸节点 | `/diagnostics` 里同页有 3 个 `ProseMirror` + 1 个 `textarea` |
| 客户端把回复当最终答案、任务静默结束 | 工具调用**载体被渲染改写**（纯文本行：引号/缩进/大括号） | 回复含 `TOOL_CALL` 标记但 `parse_tool_calls` 返回 0（诊断 warning） |
| 同一条命令被反复执行（死循环） | 空工具输出被模型读成「失败」 | 历史里同签名命令出现 ≥2 次 |
| 任务收不了尾 / 模型被迫再吐新指令 | 桥对纯文本收尾回复追发纠偏 prompt | 日志里同会话出现两条 prompt（第二条是 nudge） |
| 模型拒答「本会话没有挂载工具」 | 提示词用否定句 / 描述模型自身能力 | 注入文本含 `NO direct access` / `ALREADY MOUNTED` 等能力陈述 |
| 长会话丢上下文 / 模型瞎答 | 轮转后没播种，或拿「状态里有 url」当「页面真在会话里」 | 孤立消息无前因；`/healthz` 的 `has_history` |
| 两个任务互相污染 | 分桶键不够细：不同目录但同 UA 被并进同一桶 | `/healthz` 的 `session_keys` |
| 桶数到上限后新 key 永久失败 | 页面池只增不减 | 第 `MAX_SESSION_BUCKETS+1` 个桶开始持续 502 |
| `/link` 后每一轮都发进陌生新会话 | 绑定了**客户端临时 ID**（`local-chatgpt%3A…`，1 秒后失效） | `[绑定] … 导航后页面停在别处` + `session_link_miss_total` 增长 |
| 无头环境整体不可用 | 挑战页拦截（页面停在 `Just a moment...`，输入框/按钮不渲染） | 有头复测即好 → 用有头 / `xvfb-run` |
| E2E 首轮大量基础设施性失败 | 宿主环境变量 `PORT` 污染 + harness 失败不回收子进程 | 端口随机 / profile 被残留 Chromium 占用 |
| 写了但读回为空、`turns` 不动（非候选退化） | 写入退化成**静默空操作**：没有选区时的 `keyboard.insert_text` / `execCommand` 不报错、0 字符 | 手动 `fill` 同一目标是好的；日志里没有异常但有「读回空」（同族 Gemini） |
| 写入报超时，但文本其实已落地 | 用**有损读回**否决了成功的写入 → 清空重写形成自我毁灭循环 | 「写进去又被自己清掉」；多行 prompt 尤其明显（读回不含换行） |
| 发了消息但页面没动静，最后报超时 | 提交未生效：合成 `KeyboardEvent` 是 `isTrusted=false`，受控编辑器直接忽略 | 输入框仍留着本轮提示词（`SUBMIT_LOST_S`）；鼠标点按钮可用 |
| 客户端断开后整服务卡死 / 卡 >8 分钟 | 取消时 `await` 了被取消的 watcher（卡在不可取消的 `receive()`） | 断连后无新日志；只 `cancel()` 不 `await` 即恢复（A16） |
| 测试之间计数串味 / 断言随机失败 | 指标或状态注册表是**进程级单例** | 单独跑绿、一起跑红；修法：每测试注入独立 registry（A13） |
| 两条不同会话被当成同一条（或反之） | URL **前缀比较**：会话 ID 只差末位 | 解析 URL 后主机 + 路径精确比较（A12） |
| 进程重启后模型忘了上文、甚至串到别的任务 | 播种标记只信磁盘 `has_history`，内存页面已换 | `/healthz` 的 `has_history=true` 但会话是新的（A1） |
| 提示词写得进、Enter 似乎也有效，就是不出内容 | 额度用尽 / 升级弹窗盖住输入区 | 页面可见弹窗命中 `MODAL_CLOSE_TEXT_PATTERNS`；本轮真的没生成 → 归因 429（A6） |
| 正常对话里每个请求都 503「需要安全验证」 | 挑战/风控**误报**：会话正文里引用了那句话 | 只扫页面 chrome + 命中须可见 + 输入区不可用才算被挡（A4） |
| 客户端收完流又收到一遍不同文本 | 网页「重写回复」（快照不再是前缀），桥还在发增量 | `stream_rewritten_total` 增长；修法：停发增量、以收尾文本为准 |
| 探测脚本报「可用」，实际每个请求都失败 | 只看 DOM 命中，没看 `nav_status`（403 壳里也有输入框） | 成功判据必须含 `navigation_ok`（A5） |

---

## 2. 每个根因的修复模式

### 2.1 页面失效后仍被当「可用页面」（最贵的坑）

- **机制**：页面池只记录「这条页面是我们创建的」，不保证标签还活着。死页面留在池里，
  此后 `wait_for_selector` **立刻**抛错，而定位函数把它当成「选择器都没命中」→ 返回 `None` →
  `_ensure_page` 只看 `bucket in self._pages` → **永不重建** → 该桶永久 502。
- **修复七件事**：
  1. 加**判活** `page is not None and not page.is_closed()`（只是快路径，不能作唯一判据）。
  2. 区分异常：`TimeoutError` 才算「没命中」；页面已关/崩溃 → **专用 `PageLostError`**。
  3. 统一重建入口 `_ensure_page`：发现已登记页面死了 → 移出池 → 重建，**回到 `state.url`**。
  4. **默认桶也要能重建**。
  5. 重试阶梯接纳它：重建后**跳过**「恢复/换新会话」分支，直接重发（共用 `PAGE_REBUILD_MAX` 额度）。
  6. 轮询每轮开头判活：`TargetClosedError` 不是 `RuntimeError`，会冒到路由层变成**裸 500**。
  7. 报错带上现场（URL / `readyState` / 候选命中数 / 是否有 `[role=dialog]`），**不回显正文**；
     别把可自愈故障说成「请检查是否登录」。

### 2.2 落盘/收尾方法签名错误

- **机制**：`def save_extracted_files(raw, blocks, dir)`（漏了 `self`，也没 `@staticmethod`），
  实例调用会把实例占掉第一个位置参数 → `TypeError`。默认 `save_files=true` 时**每个非流式请求**
  都在最后一步 500。
- **修复**：加 `@staticmethod`（位置参数签名不变）；**补一条「按实例调用」用例**——
  测试都按「类上未绑定调用」写时抓不到这类错误。

### 2.3 请求进行中的桶被回收

- **机制**：发送流程在**拿桶锁之前**就确定页面，而回收/LRU 只跳过「锁被持有」的桶 →
  窗口内页面被关 → 随机 502。
- **修复**：「有请求在飞」用**可重入计数**，不是看锁：

```python
def _mark_bucket_active(self, bucket): self._active_counts[bucket] = self._active_counts.get(bucket, 0) + 1
def _unmark_bucket_active(self, bucket):
    left = self._active_counts.get(bucket, 1) - 1
    if left > 0: self._active_counts[bucket] = left
    else: self._active_counts.pop(bucket, None)
def _bucket_busy(self, bucket): return bucket in self._active_counts or self._lock_for(bucket).locked()
```

计数（不是集合）是必须的：锁与发送流程都会打标记，**内层先退出不能提前撤掉外层的保护**。

### 2.4 结束判定失灵 / 消息没发出去

- 以**内容变化**为准（不是节点数量）；观测到「生成中」再消失是最可靠的主判定；
  文本/长度稳定 + 静默窗口是兜底。停止词口径：**独立的 stop 词**
  `(^|[-_])stop([-_]|$)`（大小写不敏感），并纳入 `data-testid` / `aria-label`；
  `stopAtEnd-*` 这类装饰类名不得命中。
- 快速失败：连续 `NO_NODE_FAIL_POLLS` 轮无任何回复节点 → 立即报错 + 一次性选择器诊断，
  别等满超时。
- 看门狗：连续 `NO_PROGRESS_FAIL_POLLS` 轮「既无新回复也无生成中」→ 提前结束。
- 提交验证：等待输入框清空 / 进入生成中；React 重挂载时读**当前**输入框，
  并核对消息是否已出现在对话区。

### 2.5 到顶伪装成超时

- 周期性（`CAP_CHECK_EVERY`）扫描页面文本（**先剔除回复节点文本**）匹配到顶提示 →
  记 `cap_hit`、抛 `ContextLimitError`（400 `context_length_exceeded`），**不要**死等到超时。
- 重试阶梯最后一级 = 换新会话 + 播种。

### 2.6 长 prompt / 退化 composer 卡死

- `PROMPT_MAX_CHARS` 截断（保头尾 + `[截断]` 警告）。
- 写入用「整段 fill 优先 → 分块插入兜底」；分块时每块前重读输入框、只补缺的那段。
- **候选退化**：枚举全部候选、可见优先但**不过滤**、逐个试填并校验读回非空；
  读到非空才算成功；每次重试重新枚举。仍失败 → `ComposerError`（可恢复，重建页面后重发一次，
  额度与上游重试解耦）。

### 2.7 工具调用被静默丢弃 / 模型拒答

- 载体换 **```tool_call 代码围栏**（纯文本行会被 markdown 渲染改写：反斜杠被吃、
  缩进折叠、丢 `}`）；提示词的示例本身用围栏；解析保留旧格式兼容 + 解析失败诊断日志；
  真机**字节级 A/B** 验证保真（运输层回声对照，排除模型改写变量）。
- 拒答：只描述**链路**（你写、本地客户端执行、输出回到你这里），不描述模型自身能力；
  禁止「你没有 X」句式；回归测试断言注入文本不含 `agent` / 必修句式齐全。

### 2.8 会话串台 / 丢上下文 / 绑定失效

- 分桶优先级：显式头 → `user` → 工作目录 → UA；只扫 `system/developer` 与首条 `user`。
- 轮转必须**播种**；「新开会话」与「播种」成对出现；任务目标单独快照、优先注入。
- 「页面真在会话里」用**观察**（反查落点），不要用「状态里有 url」推导；
  绑定 `/link` 的 URL 校验要**拒绝客户端临时 ID**（`local-chatgpt%3A…` / `WEB:…`），
  发送前对齐 + 落点校验 + `session_link_miss_total` 指标。

### 2.9 桶页面只增不减 / E2E harness 污染

- `_recycle_idle_pages()`（空闲超 `BUCKET_IDLE_TTL_S`）+ `_evict_lru_page()`（超上限）；
  **只关页面、状态保留**；正在处理的桶不回收。
- E2E：跑前 `pkill` 占用 profile 的服务 + 清 `Singleton*`；显式端口；
  `setUpModule` 失败路径先杀自己拉起的进程（`addModuleCleanup` 兜底）；跑完 `pgrep` 确认无残留。

---

## 3. 通用排错纪律（惨痛教训）

1. **多种失败原因会收敛到同一个症状（超时）**。凡复用外部会话/进程处，都要把
   「上游明确拒绝」与「上游无响应」区分开。
2. **对接别人的 DOM 时，任何依赖「节点数量 / 句柄身份」的假设都不安全**，以内容为准。
3. **启发式逻辑必须配回归测试**；未验证的「看起来更宽容」的参数改动会放大既有故障。
4. **「重新推导状态」前先问：推导出来的值真的等于事实吗？** 能用观察验证的，不要用推导替代。
5. **审阅（包括 AI 给出的）修复建议时，先确认它引用的字段/API 真实存在。**
6. **给单例对象加字段前先问：它属于「进程」还是「任务」？** 任务级状态放进单例多任务必互相覆盖。
7. **测试替身要能假出两种「死法」**（`is_closed()==True` 的关闭页、`is_closed()==False`
   的崩溃页），它们走不同的代码路径。
8. **修复要有区分力**：回归测试必须在「把修复关掉」时失败（真机差异数据写进文档，
   如载体对照：纯文本行 `[4, 8]` 缩进 → `[]`，围栏逐字节一致）。
9. **「未复现」不等于「已排除」**：代码层能确定的缺陷，真机这次没触发也要修（同族 Gemini）。
10. **先检查测量工具本身**：读回口径、日志级别、代理/端口这些「观察手段」出问题，会让整个
    排查方向错得很自信——三类不同的「写不进去」真因曾被同一个错误读数带偏三轮（Gemini）。
11. **区分「上游拒绝」与「没发出去」**：额度弹窗、挑战页、未登录都能表现为「等到超时」；
    先查页面上的可见信号，再查结束判定。

---

## 4. 自查命令

先跑门禁拿到全局结论（`ruff` / `pytest` / 事故档案用例是否能红 / 复盘台账 / 共享源钉），
再回到下面这些单点 grep 定位：

```bash
python3 skills/web-to-openai-api/scripts/gate.py --phase debug
```

```bash
# 落盘签名：有 self / @staticmethod 吗？调用点实参个数对得上吗？
grep -rn "def save_extracted_files" -B2 --include=*.py . | grep -v .venv
grep -rn "save_extracted_files" --include=*.py . | grep -v .venv

# 有没有「页面存活」概念？（完全没有 = §2.1 必然存在）
grep -rn "is_closed\|page_lost\|PageLost" --include=*.py . | grep -v .venv

# 页面池是不是只看「登记过」？
grep -rn "in self._pages" --include=*.py . | grep -v .venv

# 定位输入框吞掉了哪些异常？（除了 TimeoutError 之外的都该抛出专用异常）
grep -rn "INPUT_SELECTORS" -B4 -A12 --include=*.py . | grep -v .venv

# 忙/回收判据：是否只看锁或只由锁维护的集合？
grep -rn "bucket_busy\|_active_buckets\|_active_counts" --include=*.py . | grep -v .venv

# stop 词口径是否收紧？（裸 /stop/i 会误报侧边栏装饰类名）
grep -rn "stop" --include=*.py chatgpt_web/ | grep -i "test\|js\|candidate" | head

# 注入文本是否出现能力陈述 / agent 字样（会被读成拒答理由）
grep -rn "NO direct access\|ALREADY MOUNTED\|agent" chatgpt_web/*.py

# 探测是否只看 DOM 命中（导航状态没看 = 403 壳会被报成「可用」）
grep -rn "nav_status\|navigation_ok" --include=*.py . | grep -v .venv

# 取消路径有没有 await 被取消的 watcher（会把路由拖死）
grep -rn "cancel()" -A3 --include=*.py . | grep -v .venv | grep -B1 "await"

# 指标/状态是否进程单例（测试会串味）
grep -rn "^metrics = \|^_REGISTRY\|_state = {}" --include=*.py . | grep -v .venv

# 会话 URL 比较方式（前缀比较会误判同一条）
grep -rn "startswith(" --include=*.py . | grep -i "url\|session" | grep -v .venv

# 播种标记是否只落在磁盘（应同时绑内存页面对象）
grep -rn "_seeded_page\|has_history" --include=*.py . | grep -v .venv
```

---

## 5. 回归测试模板（覆盖 §2 的每个根因）

下面这份编号清单是「要造哪些用例」的纲要；**可粘贴的代码版本**在
[regression-templates.md](regression-templates.md)（25 条，配 `scripts/regression_kit.py` 的假
page/driver，含 `TEMPLATE_VARIANT` 旧行为开关）。改完代码跑
`python3 skills/web-to-openai-api/scripts/check_templates.py` 就能知道：模板能跑吗、把修复换回去
真的会红吗、事故档案是否全覆盖。

```python
# 1) 死页面上定位函数抛专用异常（不再返回 None）
# 2) 页面活着但选择器全不中 → 仍返回 None（别把「改版」误判成「页面死了」）
# 3) _ensure_page 发现登记页面已死 → 重建，且回到同一条会话 URL、turns 保留
# 4) 默认桶页面已死 → 同样重建
# 5) 崩溃但 is_closed()==False → 恢复路径仍能无条件重建
# 6) 一直死 → 错误文案指向「标签已失效」，last_error 记下真实原因
# 7) 正在处理请求的桶：_recycle_idle_pages() 不回收它、_evict_lru_page() 跳过它
# 8) 退化 composer：第一个候选读回为空 → 换下一个；全不吃输入 → ComposerError
# 9) 载体：真机原文逐字节断言；json 围栏负向；解析失败必须有 warning
# 10) 空输出：空白/(no output) 被解释；重复签名被点名；非空结果逐字节不变
# 11) nudge：首轮无调用才追发；历史含 tool 结果时不再发第二条 prompt
# 12) 载体/选择器：把修复关掉时用例必须失败（区分力验证）
# 13) 取消路径：只 cancel() 不 await watcher；断连后桶锁与页面确实归还
# 14) 写入：无选区时的静默空操作被挡住；分块写入幂等（重跑不重复拼接）
# 15) 有损读回不否决成功的 fill（多行 prompt 用例）；只有 fill 报错才读回救援
# 16) 提交阶梯：真实 Enter 失败 → 原生 click；两级都失败 → 无法判断（不空等）
# 17) 额度弹窗：提交前关一次；本轮没生成 → usage_limit_reached(429)，不是 timeout
# 18) 挑战误报：正文里含挑战文案但输入区可用 → 不得 503
# 19) token 预算：无工具请求的 usage 不超过预算（内置脚手架泄漏的护栏）
# 20) 指标：每个测试注入独立 registry，计数不串味
```

> 一次真实事故通常对应 10+ 个用例：既钉住修复，也钉住「不要把相邻行为改坏」。
> 套件规模要**只增不削**：本项目单测基线 219 → 307 → 328 → 362 → 459 → 612 → 657；
> 收敛只允许发生在**联网 E2E**（单测覆盖不到的部分才保留真机用例）。

---

## 6. 同族项目贡献的症状（出处标注）

| 症状族 | 最初踩到的项目 | 本文件位置 |
| --- | --- | --- |
| 写入被渲染层吃掉、输入区看不见（写入/选区/分块/读回/提交） | GeminiBridge | §2.6、§1 新增行 |
| 会话 URL 恢复与绑定的坑（临时 ID、落点漂移） | ChatGPTBridge | §2.8 |
| 取消/生命周期（卡死、重复对话、埋藏的重发） | ClaudeBridge | §1 新增行、§3.9 |
| 接口路线判据（能否复用站点自身 XHR） | DeepseekBridge | references/design.md §1.2 |
| 额度用尽与风控归因（不用重试掩盖） | GrokBridge / ClaudeBridge | §1 新增行、§2.5 |
