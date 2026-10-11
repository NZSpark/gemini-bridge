# Bridge 聊天命令（`/bridge ...`）

> 本文是 GeminiBridge 的**实现说明**，不是规划稿。命令的解析与执行都在
> [`gemini_web/bridge_commands.py`](../gemini_web/bridge_commands.py)，两条协议路径
> （`/v1/chat/completions`、`/v1/responses`）共用同一个入口 `handle_command`。
>
> 设计依据是姊妹项目 ChatGPTBridge 的 `doc/bridge_command.md`（命令语义、作用域、
> 权限取舍、测试清单）。本项目按自己的会话模型裁剪了命令集，差异见 §4。

## 1. 为什么要有本地命令

Bridge 的普通对话要经过浏览器里的 Gemini 网页版。当页面失效、登录过期、浏览器尚未
初始化时，用户最需要知道「桥还行不行、请求会发到哪条会话」，但此时**恰恰发不出请求**。

`/bridge` 命令由桥接层直接应答、返回一条普通的 assistant 文本，不把这条消息交给网页版：

1. **精确匹配。** 只有「整条消息就是那一行命令」才执行；历史消息里的旧命令不重放，
   正文 / 多行文本 / 代码块 / 引用里出现的命令字样一律当普通提问（`parse_command`）。
2. **默认只作用于当前会话桶。** 桶由 `X-Gemini-Session` → `user` → User-Agent 决定
   （`server._session_key`）。命令**没有**「指定别的桶」的参数。
3. **只读优先、修改谨慎。** 有副作用的命令会明确写出影响范围与语义。
4. **不混淆协议。** 两条路径行为一致：Chat Completions 走 OpenAI chunk 形态，
   Responses 走命名事件流；命令回复在 usage 上按 `prompt_tokens=0` 计（没有上游 prompt）。
5. **复用现有实现。** `session reset` 调 `driver.reset_session`、`session reseed` 调
   `driver.reseed_session`、`settings save-files` 调 driver 的按桶偏好，与 HTTP / 请求
   路径共用同一套业务逻辑，不另写规则。
6. **失败要可解释。** 只回简短原因与用法，不打印异常栈、`.env` 值、令牌或其它桶的信息。
7. **不抢普通对话。** 非命令文本一律照常走上游；保留命名空间里的错误命令回用法提示。

## 2. 命令清单

| 命令 | 行为 | 影响范围 |
| --- | --- | --- |
| `/bridge` | 等价于 `/bridge help` | 只读 |
| `/bridge help [session\|settings]` | 列出已注册命令、说明与只读/改状态标记 | 只读 |
| `/bridge status` | 桥接、浏览器与当前桶页面/分桶/并行概况 | 只读 |
| `/bridge models` | 本桥对外公布的模型名（与 `GET /v1/models` 同源） | 只读 |
| `/bridge session [status]` | 当前桶的会话状态摘要（播种/重置/轮数/预算/上次错误/任务快照） | 只读 |
| `/bridge session reset` | 当前桶下一轮**新开网页会话**并播种完整历史 | 改当前桶状态 |
| `/bridge session reseed` | 当前桶下一轮把完整历史**再播种一遍**（不换会话） | 改当前桶状态 |
| `/bridge settings save-files [status\|on\|off\|default]` | 代码块落盘偏好，按桶持久化 | 改当前桶状态 |

- 帮助列表由 `REGISTRY` 生成，`tests/test_bridge_commands.py` 会逐条校验「帮助里列出的
  命令必须真能被解析」，避免帮助与实现漂移。
- 命令回执里都带「影响范围 / 语义 / 下一步」，例如 reset 会写明「不是删除客户端的
  对话历史、不清除 Gemini 账户数据、不影响其它桶」。

## 3. 语义细节

### 3.1 `session reset`

复用 `driver.reset_session`（与 `POST /session/reset` 同一实现）：把该桶标记为
`pending_rotation`，下一次 `send_chat` 先 `_start_new_session`，随后用**播种** prompt
重放客户端历史。命令本身**不碰页面**，因此在浏览器不可用时也能排队。

回执里给出 HTTP 等价入口：`POST /session/reset?session=<桶>`（设置了 `RESET_TOKEN`
时需带 `X-Reset-Token`）。

### 3.2 `session reseed`

本项目里「下一轮是否需要播种」的唯一权威标记就是状态里的 `has_history`：
`needs_seed(key) = not has_history`，`send_chat` 用它选增量还是播种 prompt，并在成功
产生回复后置回 `True`（`chat_io`）。

因此 reseed 就是把它置为 `False` 并落盘：

- **一次性**：下一轮成功后自动恢复，不会每轮重播；
- **跨重启**：状态写在 `SESSION_FILE` 里，重启后仍然有效；
- **并发安全**：与 reset 共用同一套状态落盘（原子写、无 await 临界区）；
- **不换页面**：与会话绑定无关，只是把历史再发一遍——回执明确提示会话里会出现重复内容。

### 3.3 `settings save-files`

优先级：**请求体里显式的 `save_files` > 本桶偏好 > `config.SAVE_FILES`**。

- 只改当前桶的状态字段（`SessionState.save_files`，三态：`True` / `False` / `None`），
  **不改进程全局配置**——否则一条来自单个客户端的聊天命令会改变其它所有客户端的行头。
- `default` 把偏好清回 `None`，即「跟随 `SAVE_FILES`」。
- 生效点只有 `/v1/chat/completions` 的**非流式**分支（流式与 `/v1/responses` 目前不落盘），
  命令回执如实说明这一点，而不是宣称「所有回复都会落盘」。

## 4. 与 ChatGPTBridge 的差异（有意为之）

| ChatGPTBridge 命令 | 本项目 | 原因 |
| --- | --- | --- |
| `/link <URL>` / `/unlink` / `session link` / `session unlink` | **不实现** | 本项目不做网页会话绑定：每桶始终「新开对话 + 播种历史」，没有可绑定的会话 URL。硬造一个会给出错误的语义。 |
| `settings think` | **不实现** | Gemini 侧没有可靠的思考模式控件与状态读取；文档要求「控件不存在或切换失败时返回失败」，在没有验证前宁可不做。 |
| `/reset`、`/clear`、`/retry`、`/cancel`、跨桶列举、配置输出、`/shell`、`/edit-file`、`/switch-user` | **不实现** | 与 ChatGPTBridge 文档 §4 同因：语义模糊、可能重复执行外部操作、需要任务取消协议、会泄露其它桶或敏感配置、扩大本地执行权限。 |

## 5. 作用域、并发与权限

- 命令只作用于请求解析出的当前桶；`MAX_SESSION_BUCKETS` 等既有约束照常生效。
- 只读命令**不导航网页、不触发生成**，也不创建桶页面（`/bridge status` 会显示
  「当前桶页面：未打开（该桶有请求时按需惰性创建，不代表服务不可用）」）。
- 浏览器未就绪时，路由层的可用性检查对命令放行（`bridge_commands.is_command`），
  普通请求照旧 503——这正是命令作为排障入口的价值所在。
- 聊天入口**不接** `RESET_TOKEN`（它保护的是 HTTP 端点）。默认只监听 `127.0.0.1`，
  且命令不能读取秘密值、不能输出登录 Cookie、不能写文件。
- 「同机所有客户端互相信任」是错误假设：因此 reset / reseed 的回执都写清作用范围，
  并发控制沿用既有的同桶锁。

## 6. 测试清单（`tests/test_bridge_commands.py`）

- 合法完整命令被识别并直接应答，`send_chat` 全程不被调用（不触发生成）。
- 非法 / 缺失 / 多余参数、未知子命令、未实现的 link 命令返回可理解的用法，且不改状态。
- 多行文本、代码块、正文里的命令字样不执行；只有最后一条 `user` 消息参与识别（历史里的
  `/bridge session reset` 不会被重放）。
- 两条协议路径（含流式）行为一致：Chat 走 `role`/`finish_reason`/`[DONE]`，Responses 走
  命名事件流；命令的 `prompt_tokens` / `input_tokens` 为 0。
- 指定 `X-Gemini-Session` 时只影响该桶，其它桶状态不变。
- 浏览器未初始化时只读命令仍给出可靠状态，普通请求照旧 503。
- reseed / 落盘偏好落盘后跨驱动实例仍有效，且不互相感染。
- 回复不包含 `RESET_TOKEN` / `BRIDGE_TOKEN` 的值，也不包含其它桶的名字。
