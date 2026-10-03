# GeminiBridge

把 Gemini 网页版包装成 **OpenAI 兼容 API** 的本地桥接服务。用 Playwright 驱动一个持久化的浏览器会话，把 `/v1/chat/completions`（以及可选 `/v1/responses`）请求转发到 Gemini 网页界面，再把回复转回标准 OpenAI 结构。

面向 Pi、Codex CLI、`agy` 等只认 OpenAI 端点的客户端。

## 特性

- **OpenAI 兼容端点**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`。
- **流式与非流式**：SSE 逐块输出，首块带 `role`，末块带 `finish_reason`，`data: [DONE]` 收尾。
- **模拟 function calling**：把 OpenAI `tools` 注入提示词，解析模型输出的调用块为 `tool_calls`；解析失败按普通文本返回。
- **会话分桶**：按 `X-Gemini-Session` → `user` → User-Agent 分优先级隔离会话，LRU 回收，可选同桶排队锁。
- **不丢任务**：会话轮转时按任务快照 + 历史播种，任务目标不被字符预算截断。
- **登录态持久化**：浏览器 profile 落在 `user_data/`，登录一次即可复用。
- **纯本地**：默认只监听 `127.0.0.1`。

## 环境要求

- Python 3.10+
- Playwright + Chromium

```bash
pip install -r requirements.txt
playwright install chromium
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

client = OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="unused")
resp = client.chat.completions.create(
    model="gemini",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### Pi 接入

Pi 通过 `~/.pi/agent/models.json` 做模型发现，走 OpenAI **Chat Completions**（`/v1/chat/completions`）。

在 Pi 的 `models.json` 里加一个指向本服务的模型条目（`baseUrl` 指向 `/v1`，`apiKey` 随便填）：

```json
{
  "models": [
    {
      "id": "gemini-chat",
      "name": "Gemini (本地桥接)",
      "provider": "openai",
      "baseUrl": "http://127.0.0.1:8001/v1",
      "apiKey": "unused",
      "model": "gemini-chat"
    }
  ]
}
```

- 模型名用 `/v1/models` 返回的 `gemini-chat` 或 `gemini-reasoner`。
- Pi 会自动请求 `GET /v1/models` 做模型发现，无需手填上下文长度。
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

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 可用模型列表 |
| POST | `/v1/chat/completions` | Chat Completions，支持 `stream` |
| POST | `/v1/responses` | Responses API（受 `ENABLE_RESPONSES_API` 控制） |
| GET | `/healthz` | 健康检查与 cluster 状态 |
| POST | `/session/reset` | 重置指定会话桶 |
| GET | `/debug/dom` | DOM 调试（受 `GEMINI_DEBUG` 控制，不回显正文） |
| GET | `/` | 服务信息 |

## 配置

所有可调参数集中在项目根目录的 `.env`（已 gitignore）。已存在的真实环境变量优先于 `.env`。

**服务**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8001` | 监听地址 |
| `HEADLESS` | `false` | 无头模式；首次登录需 `false` |
| `GEMINI_DEBUG` | `false` | 打印轮询状态、开放 `/debug/dom` |

**结束判定与超时**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GEMINI_TIMEOUT` | `180` | 单轮总超时（秒） |
| `POLL_INTERVAL_S` | `1.5` | 轮询间隔 |
| `STABLE_POLLS` | `2` | 内容不变连续次数判定结束 |
| `LEN_STABLE_POLLS` | `4` | 仅长度不变时的保守阈值 |
| `GEMINI_RETRIES` | `2` | 上游超时重试次数 |
| `RETRY_BACKOFF_S` | `1.0` | 退避基数（×n） |

**会话生命周期**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SESSION_KEY_HEADER` | `X-Gemini-Session` | 分桶键请求头 |
| `SESSION_SCOPING` | `true` | 是否启用分桶 |
| `SESSION_SCOPING_BY_UA` | `true` | 无键时按 UA 分桶 |
| `MAX_SESSION_BUCKETS` | `8` | 会话桶上限（LRU） |
| `BUCKET_IDLE_TTL_S` | `900` | 空闲回收 |
| `PARALLEL_BUCKETS` | `false` | 各桶并行页面 |
| `BUCKET_LOCK_TIMEOUT_S` | `0` | 同桶排队超时，>0 超时返回 503 `upstream_busy` |
| `SEED_MAX_CHARS` | `12000` | 轮转播种字符预算 |
| `SESSION_MAX_TURNS` | `60` | 轮数到顶阈值（0 禁用） |
| `SESSION_MAX_TOKENS` | `60000` | 估算 token 到顶阈值（0 禁用） |

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

**DOM 选择器**（网页版改版时改这里）

`RESPONSE_SELECTORS`、`INPUT_SELECTORS`、`READY_SELECTOR`、`NEW_CHAT_SELECTOR`、`CODE_BLOCK_SELECTOR`、`CODE_TAG_SELECTOR`、`CAP_NOTICE_PATTERNS`。

## 项目结构

```
gemini_api_server.py      入口：重导出公开名字 + 启动 uvicorn
gemini_web/
  config.py               配置加载与全部可调参数
  models.py               OpenAI 兼容 Pydantic 模型
  prompting.py            messages -> 输入框文本、token 估算
  toolcalls.py            工具注入与解析
  driver.py               Playwright 浏览器驱动、会话生命周期
  streaming.py            SSE 流式编码
  responses.py            Responses API 映射
  tasks.py                会话桶任务快照
  server.py               FastAPI 应用与路由
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

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 一直判不到结束 | 设 `GEMINI_DEBUG=1` 看轮询日志；确认 `RESPONSE_SELECTORS` 命中 |
| 工具调用参数被截断（如 JSON 只剩半截） | Gemini 逐 token 显现动画会让 `inner_text()` 取不到未显现的 token；代码已改为 `_complete_text`（去动画类后取全文）并在 `.pending` 清空前不收尾。若仍出现，检查页面是否新增了别的动画类名 |
| 登录态失效 | `HEADLESS=0` 手动重登，profile 存在 `user_data/` |
| profile 被占用 | 同一时间只允许一个实例，先 `pkill` 旧进程 |
| Codex 连接断开 | 调大客户端 `stream_idle_timeout_ms`，或调小 `RESPONSES_KEEPALIVE_S` |
| 抓不到回复节点 | `/debug/dom` 定位，改 `.env` 里的选择器 |

## 安全

- 默认仅监听 `127.0.0.1`，不要暴露到公网。
- `.env`、`user_data/`（含登录 cookie）、`output/` 均不提交。

## 状态

配置、模型、prompting、toolcalls、driver、streaming、responses、server 均已实现；测试套件与文档仍在补全（见 `doc/tasks.md`）。
