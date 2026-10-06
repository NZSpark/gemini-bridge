"""配置加载与全部可调参数。

所有可调参数集中在这里，默认值即 ``.env.example`` 中列出的那一套。
其他模块通过 ``config.<NAME>`` **在运行时取属性**（而不是 ``from config import NAME``），
这样测试可以直接 ``patch.object(config, "NAME", value)`` 生效。
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ==================== 0. 配置加载 (.env) ====================
# 所有可调参数集中在项目根目录的 .env（模板见 .env.example）。
# 这里用一个极简的 .env 解析器，避免为读取配置引入额外依赖：
#   * 已存在的真实环境变量优先于 .env（便于临时覆盖 / CI）；
#   * 支持 `KEY=value`、`#` 注释、空行、值两侧引号。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 失败，将使用默认值：%s", path, exc)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(ENV_FILE)


def env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ==================== 服务监听 ====================
HOST = env_str("HOST", "127.0.0.1")
PORT = env_int("PORT", 8001)
# 入口 URL（每桶新开对话的落点）。此前这个值只存在于 .env 与文档里，代码却硬编码在
# errors.HOME_URL —— 改 .env 不生效，是个排障陷阱（update.md P1-1）。现在真正生效。
WEBSITE = env_str("WEBSITE", "https://gemini.google.com/app")


# ==================== 路径 ====================
# 会话状态文件：记录每桶的轮数 / 体积 / 是否到顶（**不含会话 URL**）。
SESSION_FILE = Path(env_str("SESSION_FILE", "./user_data/.gemini_state"))
USER_DATA_DIR = env_str("USER_DATA_DIR", "./user_data")
OUTPUT_DIR = env_str("OUTPUT_DIR", "./output")


# ==================== 代码落盘 ====================
# 是否把回复里的代码块落盘到 OUTPUT_DIR。默认关闭：保存文件是本地扩展字段，
# 标准 OpenAI 客户端并不知情，默认开启会让每次请求都产生意外副作用。
# 请求体里的 save_files 仅在显式传入时覆盖（见 models.ChatCompletionRequest）。
SAVE_FILES = env_bool("SAVE_FILES")
# 落盘目录的保留策略（0 = 不限制）：超出后在启动时清理最旧的文件。
OUTPUT_MAX_FILES = env_int("OUTPUT_MAX_FILES", 0)
OUTPUT_MAX_AGE_DAYS = env_float("OUTPUT_MAX_AGE_DAYS", 0)
# 后台周期清理间隔（秒）。启动时必定清理一次；0 = 关闭周期任务（只保留启动清理）。
OUTPUT_PRUNE_INTERVAL_S = env_float("OUTPUT_PRUNE_INTERVAL_S", 3600.0)


# ==================== 运行模式 / 调试 ====================
# 无显示环境（CI / 服务器）可用 HEADLESS=1 启动；首次登录仍需有头模式
HEADLESS = env_bool("HEADLESS")
# 打开后每轮轮询都打印一行状态，便于定位「为什么一直判不到结束」（GEMINI_DEBUG=1）
DEBUG = env_bool("GEMINI_DEBUG")

# /session/reset 的可选访问令牌：留空则维持原行为（不校验）。
# 该端点会让指定会话桶的下一轮重开对话，属于有副作用的本地操作，
# 同机多用户环境下建议设置 RESET_TOKEN，调用时带 X-Reset-Token 头。
RESET_TOKEN = env_str("RESET_TOKEN", "")
# /v1/chat/completions 与 /v1/responses 的可选 Bearer 鉴权（T8.10）。
# 留空 = 默认关闭，保持向后兼容（本机回环使用无需鉴权）。
# 设为非空后，这两个端点必须带 `Authorization: Bearer <同值>`，否则 401。
BRIDGE_TOKEN = env_str("BRIDGE_TOKEN", "")

# /v1/chat/completions 流式生成期间的 keep-alive 注释间隔（秒）；0 = 关闭。
# 工具模式需要先缓冲整段回复才能判断 tool_calls，这期间客户端看不到内容，
# 用注释保活避免客户端超时断连（此前硬编码 10s）。
CHAT_KEEPALIVE_S = env_float("CHAT_KEEPALIVE_S", 10.0)


# ==================== 回复结束检测 / 超时 ====================
# 总超时（秒）：仅在「结束判定完全失灵 / 消息压根没发出去」时才会用到的兜底。
# 必须小于 Pi 侧 HTTP 客户端的超时，否则客户端会先报错。可用 GEMINI_TIMEOUT 覆盖。
RESPONSE_TIMEOUT_S = env_float("GEMINI_TIMEOUT", 180)
# 轮询间隔（秒）
POLL_INTERVAL_S = env_float("POLL_INTERVAL_S", 1.5)
# 兜底判定：内容（忽略首尾空白）完全相同连续这么多次即认为生成结束
STABLE_POLLS = env_int("STABLE_POLLS", 2)
# 次保守的兜底：仅凭“长度不再增长”收尾时要多等几轮，
# 避免生成中途的长停顿（如长思考）被误判成结束
LEN_STABLE_POLLS = env_int("LEN_STABLE_POLLS", 4)
# 连续多少次轮询既无正文也无「生成中」信号即判定页面卡死，提前失败（不再干等到总超时）
STALL_POLLS = env_int("STALL_POLLS", 20)


# ==================== 重试 ====================
# 上游超时的最大尝试次数与退避基数（秒）
MAX_UPSTREAM_RETRIES = env_int("GEMINI_RETRIES", 2)
RETRY_BACKOFF_S = env_float("RETRY_BACKOFF_S", 1.0)


# ==================== 会话生命周期 ====================
# 启动时忽略已保存的会话，直接开一个新会话。搭配“播种”使用才安全（首轮会重放历史）。
NEW_SESSION_ON_START = env_bool("GEMINI_NEW_SESSION")
# 「会话到顶」提示语的匹配规则（"||" 分隔多条正则，大小写不敏感）。
# 网页版到顶时会弹提示并停止响应，必须能与“真的卡住”区分开。
CAP_NOTICE_PATTERNS = [
    p.strip()
    for p in env_str(
        "CAP_NOTICE_PATTERNS",
        "达到对话长度上限||对话长度上限||已达到长度限制||达到长度限制||"
        "开启新对话||开始新的聊天||context length limit||start a new chat",
    ).split("||")
    if p.strip()
]
# 每 N 轮轮询检查一次“是否到顶”（避免每轮都对整页做 innerText 扫描）
CAP_CHECK_EVERY = env_int("CAP_CHECK_EVERY", 4)
# 「按任务隔离会话」使用的请求头：同一取值的请求共用一条网页会话，
# 不同取值各自维护独立的会话状态与页面（互不污染上下文）。
SESSION_KEY_HEADER = env_str("SESSION_KEY_HEADER", "X-Gemini-Session")
# 关闭后所有请求共用默认会话（旧行为）
SESSION_SCOPING = env_bool("SESSION_SCOPING", True)
# 当请求头与 user 字段都缺失时，是否允许**按 User-Agent 自动分桶**（不同客户端自动隔离）。
# 默认开：不同 AI 编程助手自动各用一条 Gemini 会话。关闭后退回旧的「默认桶，全局共用」行为。
# 注意：自动分桶会使桶数随客户端数量增长，实际受 MAX_SESSION_BUCKETS 约束（超出按 LRU 回收页面，状态保留）。
SESSION_SCOPING_BY_UA = env_bool("SESSION_SCOPING_BY_UA", True)
# 单个 key 的长度上限（防止超长头部变成文件名/JSON 键）
SESSION_KEY_MAX_LEN = env_int("SESSION_KEY_MAX_LEN", 64)
# 同时在用的会话桶数量上限。超出时**回收最久未用**的页面（状态保留，下次按 URL 恢复）。
# 0 表示不允许额外会话桶（所有请求都走默认桶）；想彻底关闭分桶用 SESSION_SCOPING=false。
MAX_SESSION_BUCKETS = env_int("MAX_SESSION_BUCKETS", 8)
# 内存里缓存的会话状态上限（超出按最久未用逐出）。
# 状态本就落盘（session_store._state 未命中会从磁盘恢复），逐出内存副本是安全的，
# 避免长跑时 _sessions / _last_prompts / _locks 三个 dict 无界增长。0 = 不限制。
MAX_SESSION_STATE_CACHE = env_int("MAX_SESSION_STATE_CACHE", 64)
# 空闲页面的回收间隔（秒）：超过这个时间没被用过的桶页面会被关闭（0 = 不按空闲回收）。
# 页面关掉不等于丢上下文：状态里的 url / turns 仍在，下次会重新打开并决定是否播种。
BUCKET_IDLE_TTL_S = env_float("BUCKET_IDLE_TTL_S", 900)
# 是否允许**按桶并发**（每个会话桶一把锁）。默认 false = 所有桶串行（更安全）。
# 打开后会同时驱动多个网页会话，可能触发风控，请自行评估。
PARALLEL_BUCKETS = env_bool("PARALLEL_BUCKETS")
# 等待某个会话桶锁的最长时间（秒）：0 = 一直等（默认，保持旧行为）。
# >0 时，若同一会话桶已有请求在跑（同一 key 并发/重试堆叠），超过该时间就快速失败，
# 返回「上游繁忙」而不是无限排队、拖到客户端自己超时。不同桶互不影响。
BUCKET_LOCK_TIMEOUT_S = env_float("BUCKET_LOCK_TIMEOUT_S", 0)
# 新建/恢复页面后等待输入框就绪的超时（毫秒）
READY_TIMEOUT_MS = env_int("READY_TIMEOUT_MS", 15000)
# 播种（新会话时重放历史）的最大字符数预算；超出时保留最近的消息
SEED_MAX_CHARS = env_int("SEED_MAX_CHARS", 12000)
# 播种时**单条 system 消息**的最大字符数。harness（Codex / Pi）每轮都会把
# 完整的系统提示作为 system 消息发来，动辄上万字；播种时若原样重放，
# 会把简单请求灌成一大段系统提示。超出即截断。0 = 不限制（不推荐）。
SEED_SYSTEM_MAX_CHARS = env_int("SEED_SYSTEM_MAX_CHARS", 2000)
# 单条 tool 结果（role=="tool"）注入 prompt 时的最大字符数。
# Codex/Pi 的 read 结果动辄几十万字符，直接 fill 会撑爆网页版输入框。
# 超出即**只保留开头一段**并标注“已截断 N 字符”（模型据此知道自己看到的是片段）。
# 50K：用户定的上限（够放下一份完整的大文件/日志开头，又不至于单条就吃掉整段预算）。
# 多条结果合计另有成品预算：拼装后超过 PROMPT_MAX_CHARS 时按 `prompting._fit_segments_to_budget`
# 继续压缩（同样只留开头 + 标注）。0 = 不限制（不推荐）。
TOOL_RESULT_MAX_CHARS = env_int("TOOL_RESULT_MAX_CHARS", 50000)
# 单次 fill() 入参（整段 prompt）的最大字符数硬上限，兜底防止输入框溢出。
# 这是发送侧最后一道护栏：无论上游怎么拼 prompt，都不超过它。0 = 不限制。
PROMPT_MAX_CHARS = env_int("PROMPT_MAX_CHARS", 100000)
# 单次 ``fill()`` 的超时（毫秒）。Playwright 默认 30s：网页版每次重挂载 composer
# 都会换掉节点，而我们手里的旧句柄会一直停在“等它变得可见/可编辑”上直到 30s 超时
# （真实故障：ElementHandle.fill: Timeout 30000ms exceeded，客户端连试 4 次、
# 每次白等 30s，整个窗口期全丢）。收紧到秒级，失败就重新定位再试。
FILL_TIMEOUT_MS = env_int("FILL_TIMEOUT_MS", 10000)
# ``fill`` 的重试次数；**每次尝试都重新定位输入框**，专门针对 React 重挂载导致的
# 失效句柄。参照姊妹项目 ChatGPTBridge 的同名开关（chatgpt_web/chat_io.py 的
# FILL_TIMEOUT_MS / FILL_RETRIES）。退避复用 RETRY_BACKOFF_S。
FILL_RETRIES = env_int("FILL_RETRIES", 3)
# 写入输入框时**每块**插入的字符数。真实故障：客户端的 find/read 结果很长时，
# 一次性 `fill` 会在网页主线程上排成一个长任务（React 重渲染 + 富文本编辑器同步），
# 期间连「元素是否可编辑」都探测不到——报 `waiting for element to be visible,
# enabled and editable`，网页本身也卡住。分块插入把长任务切碎（每块之间让出主线程），
# 并在每块前重新确认已写入的文本，可断点续写、也不会重复写入。
FILL_CHUNK_CHARS = env_int("FILL_CHUNK_CHARS", 4000)
# 提交（Enter / 点发送按钮）后：等待「输入框已清空 / 页面已开始生成」的最长时间（毫秒）。
# 真实故障：文字已经在输入框里，但消息**没被提交**（旧实现里 Enter 派发后无条件认为成功，
# 从不点发送按钮），网页不产生任何回复，客户端干等到超时。现在每次尝试后都要验证，
# 最长等这么久；读不到输入框内容时不做判断（不空等）。
SUBMIT_VERIFY_MS = env_int("SUBMIT_VERIFY_MS", 3000)
# 网页会话超过以下任一阈值后，下一轮自动轮转到新会话（0 表示禁用该维度）
SESSION_MAX_TURNS = env_int("SESSION_MAX_TURNS", 60)
SESSION_MAX_TOKENS = env_int("SESSION_MAX_TOKENS", 60000)


# ==================== Responses API（Codex CLI）====================
# 是否启用 /v1/responses 路由。默认开启；关闭后该端点返回 404，
# 且 /v1/chat/completions（Pi）完全不受影响。
ENABLE_RESPONSES_API = env_bool("ENABLE_RESPONSES_API", True)
# 流式生成期间发送 keep-alive 注释的间隔（秒）；0 = 关闭。
# 网页版生成慢，Codex 侧 stream_idle_timeout_ms 较大时用它保活连接。
RESPONSES_KEEPALIVE_S = env_float("RESPONSES_KEEPALIVE_S", 10.0)
# ==================== 内置工具：edit_markdown ====================
# 是否允许桥接层在本地执行模型发出的 edit_markdown（Markdown 锚点编辑）。
# 关闭时 edit_markdown 仍可作为普通工具名被解析，由客户端自行执行。
EDIT_MARKDOWN_LOCAL = env_bool("EDIT_MARKDOWN_LOCAL", False)
# edit_markdown 落盘前的备份目录。
EDIT_MARKDOWN_BACKUP_DIR = env_str("EDIT_MARKDOWN_BACKUP_DIR", "output/backups")
# 客户端**没有声明任何工具**时，是否仍把内置 edit_markdown 注入本轮工具列表。
# 默认 false：自动注入会给每个请求附带 ~970 tokens 的脚手架（实测，见 update.md P1-5），
# 还可能让客户端收到自己从未声明过的 tool_calls。设为 true 恢复旧行为。
EDIT_MARKDOWN_ALWAYS_REGISTER = env_bool("EDIT_MARKDOWN_ALWAYS_REGISTER", False)
# edit_markdown 允许读写的**工作区根**（T8.9）。留空 = 项目根目录。
# 路径会先 resolve，再校验必须落在该目录内：`../` 逃逸、指向外部的绝对路径、
# 经软链接跳出根目录的路径一律拒绝，避免模型把本地编辑跑到工作区之外。
EDIT_MARKDOWN_ROOT = env_str("EDIT_MARKDOWN_ROOT", "") or str(PROJECT_ROOT)


# 工具模式下：是否先缓冲整段回复再判断 tool_calls（true = 需要缓冲，
# 因为要等完整文本才能解析出 function_call；false = 直接透传文本增量）。
RESPONSES_TOOL_BUFFER = env_bool("RESPONSES_TOOL_BUFFER", True)


# ==================== 任务快照（轮转后续接任务）====================
# 是否启用任务快照：每个会话桶在 user_data/.gemini_tasks/ 下维护一份轻量任务状态
# （任务目标 + 最近进展）。网页会话轮转播种时优先注入它，确保任务目标不被
# SEED_MAX_CHARS 截断，从而“不丢任务”。关闭后回到纯历史播种的旧行为。
TASK_SNAPSHOT_ENABLED = env_bool("TASK_SNAPSHOT_ENABLED", True)
# 任务快照存放目录。
TASK_FILE_DIR = env_str("TASK_FILE_DIR", "./user_data/.gemini_tasks")
# 任务快照命名空间：同一台机器上若跑着多个「桥」项目（如 DeepseekBridge /
# GeminiBridge）并共用 TASK_FILE_DIR，同名会话桶（default、ua:xxx）会互相覆盖，
# 表现为“A 项目读到 B 项目的任务”。快照会写到 TASK_FILE_DIR/<namespace>/ 下，
# 并在文件里记录 namespace，读取时校验归属。
# 留空则自动从包名派生（gemini_web -> gemini，deepseek_web -> deepseek）。
TASK_NAMESPACE = env_str("TASK_NAMESPACE", "") or (
    Path(__file__).resolve().parent.name.split("_")[0] or "default"
)
# 任务目标（第一条 user 消息）保留的最大字符数；超出截断。
TASK_GOAL_MAX_CHARS = env_int("TASK_GOAL_MAX_CHARS", 2000)
# 快照里滚动保留的最近消息条数（用于“最近进展”）。
TASK_KEEP_MESSAGES = env_int("TASK_KEEP_MESSAGES", 8)
# 任务快照里单条 recent 文本的最大字符数；防止 harness 注入的超长系统块
# （skills / permissions / collaboration_mode 等）撑爆快照，轮转播种时把 prompt 灌满。
TASK_RECENT_ITEM_MAX_CHARS = env_int("TASK_RECENT_ITEM_MAX_CHARS", 500)


# ==================== DOM 选择器 ====================
# 统一集中在这里，网页版改版时只需改这一处（也可用 .env 覆盖而无需改代码）。
# 回复节点的候选选择器（逗号分隔的 CSS 列表，直接交给 query_selector_all）
RESPONSE_SELECTORS = env_str(
    "RESPONSE_SELECTORS",
    'message-content, .model-response-text, .markdown, div[class*="response"]',
)
# 输入框候选选择器（.env 中用 "||" 分隔多个候选）
INPUT_SELECTORS = [
    s.strip()
    for s in env_str(
        "INPUT_SELECTORS",
        'rich-textarea [contenteditable="true"]||div[contenteditable="true"]||'
        'textarea[placeholder*="Ask"]||textarea',
    ).split("||")
    if s.strip()
]
# 发送按钮候选选择器（.env 中用 "||" 分隔多个候选）。
# 优先用 DOM 事件发送（见 chat_io._submit_prompt），只有派发事件无效时才点它。
SEND_BUTTON_SELECTORS = [
    s.strip()
    for s in env_str(
        "SEND_BUTTON_SELECTORS",
        'button[aria-label*="Send"]||button[aria-label*="send"]||'
        'button[aria-label*="发送"]||button[data-test-id*="send"]||'
        'button[type="submit"]',
    ).split("||")
    if s.strip()
]
# 页面就绪（输入框出现）用的选择器
READY_SELECTOR = env_str(
    "READY_SELECTOR", 'rich-textarea, [contenteditable="true"], textarea'
)
# 新建对话入口：每桶首次请求与轮转时点击，确保从干净会话开始。
NEW_CHAT_SELECTOR = env_str(
    "NEW_CHAT_SELECTOR",
    'button[aria-label*="New chat"]||button[aria-label*="new chat"]||'
    'button[aria-label*="新对话"]||button[aria-label*="新聊天"]',
)
# 代码块 DOM
CODE_BLOCK_SELECTOR = env_str("CODE_BLOCK_SELECTOR", "pre")
CODE_TAG_SELECTOR = env_str("CODE_TAG_SELECTOR", "code")
