# GeminiBridge

把 Gemini 网页版包装成 **OpenAI 兼容 API** 的本地桥接服务。用 Playwright 驱动一个持久化的浏览器会话，把 `/v1/chat/completions`（以及可选 `/v1/responses`）请求转发到 Gemini 网页界面，再把回复转回标准 OpenAI 结构。

面向 OpenAI SDK、Pi、Codex CLI 等只认 OpenAI 端点的客户端与应用。

## 项目数据统计

- **开发周期**：2026-10-03 至 2026-10-08
- **Git 提交数**：47 次 commits
- **代码总行数**：12,989 行 Python 代码
  - **核心业务逻辑 (`gemini_web/` + `gemini_api_server.py`)**：6,368 行
  - **自动化测试套件 (`tests/`)**：6,621 行

## 特性

- **OpenAI 兼容端点**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`。
- **流式与非流式**：SSE 逐块输出，首块带 `role`，末块带 `finish_reason`，`data: [DONE]` 收尾。
- **模拟 function calling**：把 OpenAI `tools` 注入提示词，要求模型用「`TOOL_CALL:` 标记行 + ```tool_call 代码围栏」载体回话（围栏保逐字节、标记行保可识别，见 `doc/code_block_fence.md`），解析成 `tool_calls`；兼容历史纯文本 `TOOL_CALL: {...}` 行与 DSML 兜底；支持单引号 shell 命令引导与控制字符 / 双引号容错修复；解析失败按普通文本返回。
- **会话分桶**：按 `X-Gemini-Session` → `user` → User-Agent 分优先级隔离会话，LRU 回收，可选同桶排队锁。
- **不丢任务**：会话轮转时按任务快照 + 历史播种，任务目标不被字符预算截断。
- **登录态持久化**：浏览器 profile 落在 `user_data/`，登录一次即可复用。
- **纯本地**：默认只监听 `127.0.0.1`。
- **无头防失焦**：有头模式下窗口失焦会被系统降级为后台标签，Gemini 懒渲染卸载输入框导致发送失败；推荐 `HEADLESS=1`。

## 环境要求

- Python 3.10+
- Playwright + Chromium

```bash
# 运行时依赖
pip install -r requirements.txt
playwright install chromium

# 若要跑测试 / 用 openai SDK 联调（dev extras 含 pytest / httpx2 / openai）
pip install -e ".[dev]"
```

## 快速开始

```bash
# 1. 首次登录：有头模式手动登录 Gemini，登录态存入 user_data/
HEADLESS=0 python gemini_api_server.py

# 2. 之后可无头运行
python gemini_api_server.py
```

服务默认监听 `http://127.0.0.1:8001`。冒烟测试：

```bash
curl http://127.0.0.1:8001/healthz
curl http://127.0.0.1:8001/v1/models
```

### 客户端接入

任何 OpenAI 兼容客户端指向 `http://127.0.0.1:8001/v1` 即可（`api_key` 随便填）。

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8001/v1",
    api_key="unused",                                     # 未设 BRIDGE_TOKEN 时随便填
    default_headers={"X-Gemini-Session": "python-sdk"},   # 可选：固定会话桶，见下
)
resp = client.chat.completions.create(
    model="gemini-chat",                                  # /v1/models 返回的 id
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

- `model` 用 `/v1/models` 返回的 `gemini-chat` / `gemini-reasoner`。
- **会话桶**：不传 `X-Gemini-Session` 时，桥按 User-Agent 自动分桶——SDK 的
  `OpenAI/Python x.y.z` 会落到 `ua:openai` 桶。每个桶持有**独立的网页页面与会话**；
  固定一个桶名（如 `python-sdk`，或用请求字段 `user="python-sdk"`）可让同一客户端
  稳定复用同一条网页会话，也便于 `/healthz` 按桶排查。
- 想让所有客户端共用默认会话（不按 UA 自动隔离）：`.env` 设 `SESSION_SCOPING_BY_UA=false`。
- 流式/非流式都支持；`temperature`、`max_tokens` 等字段会被忽略（网页版接口没有这些旋钮）。

### Pi 接入

Pi 通过 `~/.pi/agent/models.json` 做模型发现，走 OpenAI **Chat Completions**（`/v1/chat/completions`）。

在 Pi 的 `models.json` 里加一个指向本服务的模型条目（`baseUrl` 指向 `/v1`，`apiKey` 随便填）：

```json
{
  "providers": {
    "gemini-web": {
      "baseUrl": "http://127.0.0.1:8001/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false
      },
      "models": [
        {
          "id": "gemini-chat",
          "name": "Gemini Pro (Web)",
          "input": ["text"],
          "contextWindow": 1000000,
          "maxTokens": 65535
        }
      ]
    }
  }
}
```

启动 Pi 并指定模型：
```bash
pi --provider gemini-web --model gemini-chat
```

- 模型名用 `/v1/models` 返回的 `gemini-chat` 或 `gemini-reasoner`。
- Pi 会自动请求 `GET /v1/models` 做模型发现；该端点现在会返回 `context_window`
  （= `SESSION_MAX_TOKENS`，会话轮转预算），上面示例里的 `1000000` 就是它的值。
  改了 `SESSION_MAX_TOKENS` 请以端点返回为准，不要手写另一个数（旧版本这里曾与代码里的 `65536` 互相矛盾）。
- 想固定会话桶可加请求头 `X-Gemini-Session: <name>`（见「配置」的 `SESSION_KEY_HEADER`）。

### Codex CLI 接入

Codex CLI 只走 **Responses API**（`POST /v1/responses`），不再用 chat 端点；该路由由 `ENABLE_RESPONSES_API` 控制（默认开启）。

在 `~/.codex/config.toml` 里加一个自定义 provider：

```toml
[model_providers.gemini_bridge]
name = "Gemini Bridge"
base_url = "http://127.0.0.1:8001/v1"
wire_api = "responses"

[profiles.gemini]
model_provider = "gemini_bridge"
model = "gemini-chat"
```

然后用该 profile 启动：

```bash
codex --profile gemini
```

- `wire_api = "responses"` 必填，否则 Codex 会去打 `/v1/chat/completions`。
- 工具调用（function calling）会被桥接层解析为 Responses 的 function_call 事件。
- 关闭 `ENABLE_RESPONSES_API` 后该端点返回 404，但 Pi 的 chat 路径不受影响。

### Bridge 聊天命令（`/bridge ...`）

把一条**整行**的消息发给任意生成端点，桥就会本地应答这条命令，**不发给 Gemini 网页版**。
页面失效、登录过期、浏览器还没起来时，它是唯一还能用的排障入口。

```bash
curl -s -X POST http://127.0.0.1:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-chat","messages":[{"role":"user","content":"/bridge status"}]}'
```

| 命令 | 说明 | 影响范围 |
| --- | --- | --- |
| `/bridge` / `/bridge help [session\|settings]` | 列出已注册命令与只读 / 改状态标记 | 只读 |
| `/bridge status` | 桥接、浏览器与当前桶页面的状态摘要 | 只读 |
| `/bridge models` | 本桥对外公布的模型名（与 `GET /v1/models` 同源） | 只读 |
| `/bridge session [status]` | 当前桶的会话状态（播种 / 重置 / 轮数 / 预算 / 上次错误） | 只读 |
| `/bridge session reset` | 当前桶下一轮**新开网页会话**并把历史播种进去（同 `POST /session/reset`） | 改当前桶 |
| `/bridge session reseed` | 当前桶下一轮把完整历史**再播种一遍**（不换会话，会出现重复内容） | 改当前桶 |
| `/bridge settings save-files [on\|off\|default\|status]` | 代码块落盘偏好，**按桶持久化**，不改全局 `SAVE_FILES` | 改当前桶 |

- 只识别「整条消息就是这一行」：历史消息里的旧命令不重放，正文 / 多行 / 代码块里的命令
  字样照旧当普通提问发给模型；`/bridge` 之外的 `/` 开头文本一律不拦截。
- 命令只作用于当前会话桶（`X-Gemini-Session` → `user` → User-Agent），**没有**「指定别的桶」
  的参数；Coding Agent 多开时互不干扰。
- 落盘生效顺序：请求里的 `save_files` 字段 > 本桶偏好 > `SAVE_FILES` 配置；目前只有
  `/v1/chat/completions` 的非流式回复会落盘。
- `chat` 与 `responses` 两条路径行为一致（含流式）；命令的 `prompt_tokens` / `input_tokens` 计 0。

命令的完整设计与取舍见 [doc/bridge_command.md](doc/bridge_command.md)。

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 可用模型列表 |
| POST | `/v1/chat/completions` | Chat Completions，支持 `stream` |
| POST | `/v1/responses` | Responses API（受 `ENABLE_RESPONSES_API` 控制） |
| GET | `/healthz` | 健康检查与 cluster 状态 |
| POST | `/session/reset` | 重置指定会话桶 |
| GET | `/debug/dom` | DOM 调试（受 `GEMINI_DEBUG` 控制，不回显正文） |
| — | `/bridge ...`（聊天消息） | 桥本地命令，见上文「Bridge 聊天命令」 |
| GET | `/` | 服务信息 |

## 配置

所有可调参数集中在项目根目录的 `.env`（已 gitignore）。已存在的真实环境变量优先于 `.env`。

**服务**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8001` | 监听地址 |
| `WEBSITE` | `https://gemini.google.com/app` | 入口 URL（每桶新开对话的落点），改这里会真的生效 |
| `HEADLESS` | `false` | 无头模式，推荐正式运行时设为 `1`；首次登录需 `false` |
| `GEMINI_DEBUG` | `false` | 打印轮询状态、开放 `/debug/dom` |

> **真实环境变量优先于 `.env`**。若环境里已存在同名变量（例如 `PORT=0`），它以环境为准，
> 见「故障排查」最后一条。

**结束判定与超时**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GEMINI_TIMEOUT` | `180` | 单轮总超时（秒） |
| `POLL_INTERVAL_S` | `1.5` | 轮询间隔 |
| `STABLE_POLLS` | `2` | 内容不变连续次数判定结束 |
| `LEN_STABLE_POLLS` | `4` | 仅长度不变时的保守阈值 |
| `STALL_POLLS` | `20` | 连续多少次既无正文也无「生成中」信号即提前失败 |
| `CAP_CHECK_EVERY` | `4` | 每多少轮检查一次「会话到顶」提示 |
| `CAP_NOTICE_PATTERNS` | 见模板 | 「会话到顶」提示语正则（`||` 分隔） |
| `GEMINI_RETRIES` | `2` | 上游**最大尝试次数**（不是“额外重试次数”：2 表示最多发 2 次） |
| `RETRY_BACKOFF_S` | `1.0` | 退避基数（×n） |
| `READY_TIMEOUT_MS` | `15000` | 新建/恢复页面后等输入框就绪的超时（毫秒） |

**会话生命周期**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SESSION_KEY_HEADER` | `X-Gemini-Session` | 分桶键请求头 |
| `SESSION_SCOPING` | `true` | 是否启用分桶 |
| `SESSION_SCOPING_BY_UA` | `true` | 无键时按 UA 分桶 |
| `MAX_SESSION_BUCKETS` | `8` | 会话桶上限（LRU） |
| `MAX_SESSION_STATE_CACHE` | `64` | 内存会话状态缓存上限（LRU 逐出，0 不限） |
| `BUCKET_IDLE_TTL_S` | `900` | 空闲回收 |
| `PARALLEL_BUCKETS` | `false` | 各桶并行页面 |
| `BUCKET_LOCK_TIMEOUT_S` | `0` | 同桶排队超时，>0 超时返回 503 `upstream_busy` |
| `GEMINI_NEW_SESSION` | `false` | 启动时忽略已保存状态直接开新会话 |
| `SESSION_KEY_MAX_LEN` | `64` | 分桶键长度上限 |
| `FILL_CHUNK_CHARS` | `4000` | 写入输入框时每块插入的字符数（分块写入，避免超长结果把网页输入框卡死） |
| `SEED_MAX_CHARS` | `12000` | 轮转播种字符预算 |
| `SEED_SYSTEM_MAX_CHARS` | `2000` | 播种时单条 system 消息的字符上限（harness 每轮注入的系统提示会被截断） |
| `TOOL_RESULT_MAX_CHARS` | `50000` | **单条** tool 结果注入 prompt 的最大字符数：超出只保留开头一段并标注“已截断 N 字符”（0 不限）。多条合计另有成品预算：超过 `PROMPT_MAX_CHARS` 时按同样的“留开头 + 标注”策略继续压缩 |
| `PROMPT_MAX_CHARS` | `100000` | 单次 fill() 入参上限（我们设的预算，不是输入框物理上限：实测 20K/60K/100K 都能正常收发，见 `tests/e2e/probe_prompt_limit.py`）；超出时头尾各半、中间截断并记 WARNING（0 不限） |
| `FILL_TIMEOUT_MS` | `10000` | 单次 `fill()` 超时（毫秒）；失败会重新定位输入框并重试 |
| `FILL_RETRIES` | `3` | `fill` 重试次数（每次重新定位，规避重挂载导致的失效句柄） |
| `SESSION_MAX_TURNS` | `60` | 轮数到顶阈值（0 禁用） |
| `SESSION_MAX_TOKENS` | `60000` | 估算 token 到顶阈值（0 禁用） |

> 上表列出的是 `config.py` 的**内置默认值**。实际运行时以 `.env` 为准（真实环境变量优先）；
> 例如仓库自带 `.env` 覆盖为 `STABLE_POLLS=5`、`MAX_SESSION_BUCKETS=3`。
> 想从零起步可直接复制 `.env.example`。

**代码落盘 / 调试端点**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SAVE_FILES` | `false` | 是否把回复代码块落盘（请求字段仅在显式传入时覆盖） |
| `OUTPUT_MAX_FILES` | `0` | `output/` 保留文件数上限（0 不限） |
| `OUTPUT_MAX_AGE_DAYS` | `0` | `output/` 最长保留天数（0 不限） |
| `OUTPUT_PRUNE_INTERVAL_S` | `3600` | 后台周期清理间隔（秒）；启动时一定清一次，0 = 只保留启动清理 |
| `RESET_TOKEN` | 空 | 设置后 `/session/reset` 需带 `X-Reset-Token` 头 |
| `BRIDGE_TOKEN` | 空 | 设置后 `/v1/chat/completions` 与 `/v1/responses` 需带 `Authorization: Bearer <同值>`，否则 401（`/healthz`、`/v1/models` 不受影响） |
| `CHAT_KEEPALIVE_S` | `10.0` | chat 流式 keep-alive 间隔（0 关闭） |

**内置工具：edit_markdown**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EDIT_MARKDOWN_LOCAL` | `false` | 允许桥接层本地执行 `edit_markdown`（默认只注册 schema，由客户端执行） |
| `EDIT_MARKDOWN_ALWAYS_REGISTER` | `false` | 客户端未声明任何工具时是否仍注入 `edit_markdown`。开启会给每个请求多付约 970 tokens 脚手架 |
| `EDIT_MARKDOWN_BACKUP_DIR` | `output/backups` | 落盘前的备份目录 |
| `EDIT_MARKDOWN_ROOT` | 项目根目录 | 允许读写的工作区根；`../` 逃逸、指向根外的绝对路径、软链接跳出都会被拒绝 |

**Responses API**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENABLE_RESPONSES_API` | `true` | 关闭后 `/v1/responses` 返回 404 |
| `RESPONSES_KEEPALIVE_S` | `10.0` | 保活注释间隔（0 关闭） |
| `RESPONSES_TOOL_BUFFER` | `true` | 工具模式先缓冲整段再解析 |

**任务快照**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TASK_SNAPSHOT_ENABLED` | `true` | 轮转时注入任务目标，防止被截断 |
| `TASK_FILE_DIR` | `./user_data/.gemini_tasks` | 快照目录 |
| `TASK_NAMESPACE` | 派生自包名 | 多桥共用目录时的命名空间 |
| `TASK_GOAL_MAX_CHARS` | `2000` | 任务目标保留上限 |
| `TASK_KEEP_MESSAGES` | `8` | 滚动保留的最近消息数 |
| `TASK_RECENT_ITEM_MAX_CHARS` | `500` | 快照里单条 recent 文本的上限（防 harness 系统块灌满 prompt） |

**DOM 选择器**（网页版改版时改这里）

`RESPONSE_SELECTORS`、`INPUT_SELECTORS`、`SEND_BUTTON_SELECTORS`、`READY_SELECTOR`、`NEW_CHAT_SELECTOR`、`CODE_BLOCK_SELECTOR`、`CODE_TAG_SELECTOR`、`CAP_NOTICE_PATTERNS`。

## 项目结构

```
gemini_api_server.py      入口：重导出公开名字 + 启动 uvicorn
gemini_web/
  config.py               配置加载与全部可调参数
  models.py               OpenAI 兼容 Pydantic 模型
  prompting.py            messages -> 输入框文本、token 估算
  toolcalls.py            工具注入与解析
  markdown_io.py          Markdown 提取与工具调用解析器
  driver.py               Playwright 浏览器驱动、会话生命周期
  chat_io.py              DOM 交互、输入框等待、页面内事件提交与代码块提取
  session_store.py        会话状态落盘与持久化管理
  page_pool.py            Playwright Page 实例池管理
  completion.py           Chat Completions 逻辑处理与重试驱动
  bridge_commands.py      /bridge 聊天命令的解析、执行、帮助与状态摘要
  streaming.py            SSE 流式编码
  responses.py            Responses API 映射
  tasks.py                会话桶任务快照
  logging_setup.py        统一日志配置（GEMINI_DEBUG 控制级别）
  server.py               FastAPI 应用与路由
pyproject.toml            依赖声明（运行时 + dev extras）与 pytest 配置
.github/workflows/ci.yml  CI：安装依赖 + 跑不联网的回归套件
doc/design.md             设计文档
doc/tasks.md              任务分解与验收标准
output/                   回复与代码块落盘（gitignore）
user_data/                浏览器 profile 与状态（gitignore）
```

## 设计要点

- **不恢复旧会话**：每轮播种重放历史，代价是 token 消耗更高，换来对网页版改版的鲁棒性；用 `tasks.py` 快照保住任务目标。
- **结束判定偏保守**：双阈值（内容 / 长度）避免把生成中途的长停顿误判成结束；
  且「停止按钮消失」时若回复节点仍有未显现 token（`.pending`/`.animating`），会继续等待，
  避免读到被截断的半截回复（读取文本走 `_complete_text`，克隆节点去动画后取全文）。
- **到顶可区分**：用 `CAP_NOTICE_PATTERNS` 与轮次/token 双阈值判定对话长度上限，不伪装成超时。
- **选择器外置**：全部集中在 `.env`，网页版改版只改配置、不改代码。
- **命令本地应答**：`/bridge` 命令由桥自己处理，浏览器不可用时仍可查询状态、重置会话
  （见 [doc/bridge_command.md](doc/bridge_command.md)）。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 一直判不到结束 | 设 `GEMINI_DEBUG=1` 看轮询日志；确认 `RESPONSE_SELECTORS` 命中 |
| 工具调用参数被截断（如 JSON 只剩半截） | Gemini 逐 token 显现动画会让 `inner_text()` 取不到未显现的 token；代码已改为 `_complete_text`（去动画类后取全文）并在 `.pending` 清空前不收尾。若仍出现，检查页面是否新增了别的动画类名 |
| 登录态失效 | `HEADLESS=0` 手动重登，profile 存在 `user_data/`；登录完成后设 `HEADLESS=1` 长跑 |
| profile 被占用 | 同一时间只允许一个实例，先 `pkill` 旧进程 |
| Codex 连接断开 | 调大客户端 `stream_idle_timeout_ms`，或调小 `RESPONSES_KEEPALIVE_S` |
| 抓不到回复节点 | `/debug/dom` 定位，改 `.env` 里的选择器 |
| 某个会话桶一直 502「无法找到对话输入框」，其它桶正常 | 该桶的标签被手工关闭或崩溃（句柄已失效）。桥会在下一次请求时**自动重建该桶的页面并重新播种**；若报的是默认桶，或版本还不含该修复，重启服务即可（登录态在 `user_data/`，重启不丢）。排查先看 `curl -s http://127.0.0.1:8001/healthz` 的 `session_keys` / `open_pages`，并用 `X-Gemini-Session: default` 发一次请求对比 |
| 有头模式下报「无法找到对话输入框」/ 输入焦点丢失 | 有头 Chromium 是真实窗口，切到其它 App 会失焦，甚至被系统挂起或降级为后台标签。后台标签的 `requestAnimationFrame` 被节流，Gemini 懒渲染会卸载或延迟挂载输入框，`chat_io.py` 的 `wait_for_selector(INPUT_SELECTORS, timeout=3000)` 三个候选全部超时，于是抛「无法找到对话输入框」；窗口不在前台时 `fill()` + `press("Enter")` 的按键也会落到别的窗口。对策：在 `.env` 设 `HEADLESS=1`（解析器认 `1`/`true`/`yes`/`on`），无头不参与窗口焦点竞争，首次登录仍用有头，之后切无头；仍用有头时，发送前先 `page.bring_to_front()` 并 `focus()` 输入框，给定位加整体重试（如 3 次 × 2s，期间 `bring_to_front()`），并可加 `--disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows` 缓解后台节流 |

## 安全

- 默认仅监听 `127.0.0.1`，不要暴露到公网。
- `.env`、`user_data/`（含登录 cookie）、`output/` 均不提交。
- `user_data/` **不要备份 / 同步**（iCloud、Dropbox 等会带走登录态）；DEBUG 日志不含消息正文。
- `edit_markdown` 本地执行时只能改工作区根（`EDIT_MARKDOWN_ROOT`，默认项目根）内的文件：
  `../` 逃逸、根外绝对路径、经软链接跳出的路径一律拒绝；真正的落盘还要显式 `write=true`，
  并先备份到 `EDIT_MARKDOWN_BACKUP_DIR`。
- 需要给生成端点加一层本地门槛时设 `BRIDGE_TOKEN`（默认关闭，向后兼容）；
  它只保护 `/v1/chat/completions` 与 `/v1/responses`，探活与模型发现保持开放。

## 状态

配置、模型、prompting、toolcalls、driver、streaming、responses、server、bridge_commands 均已实现；
doc/tasks.md 的阶段 6–10 已全部落地（含日志改造、依赖固定、CI、鉴权开关与测试补全），
且已根据 doc/update_pi.md 完成最新分解与对账；
真实联网对等测试见 doc/e2e_test_design.md（GEMINI_E2E=1 才跑）。

