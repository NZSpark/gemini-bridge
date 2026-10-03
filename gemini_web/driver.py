"""Playwright 浏览器 Driver：把网页对话驱动成可编程的请求/响应。

所有可调参数都从 ``config`` 模块按属性读取（``config.X``），
因此测试可以直接 ``patch.object(config, "X", ...)`` 生效，无需重新 import。
"""

import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from . import config, prompting
from .models import ChatMessage
from .prompting import _delta_piece, estimate_tokens


class GeminiTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


class GeminiContextLimitError(RuntimeError):
    """网页会话已达上下文长度上限（网页版会停止响应，必须换新会话）。"""


class GeminiBusyError(RuntimeError):
    """某个会话桶正忙（同一会话已有请求在跑且等待超时）。

    与「上游出错」区分开：这是本地的排队保护，客户端稍后重试即可，
    因此会被映射成 HTTP 503 / SSE ``upstream_busy``，而**不会**触发重试阶梯。
    """


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# Gemini 网页版入口（每桶新开对话的落点）。
HOME_URL = "https://gemini.google.com/app"


@dataclass
class SessionState:
    """单个会话桶的状态。会话的“是否新开 / 能否复用”都由它决定。"""

    has_history: bool = False
    turns: int = 0
    est_tokens: int = 0
    cap_hit: bool = False
    pending_rotation: bool = False
    last_error: Optional[str] = None
    updated_at: int = 0

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionState":
        state = cls()
        if not isinstance(payload, dict):
            return state
        for name in ("has_history", "cap_hit", "pending_rotation"):
            if name in payload:
                setattr(state, name, bool(payload.get(name)))
        for name in ("turns", "est_tokens", "updated_at"):
            try:
                setattr(state, name, int(payload.get(name) or 0))
            except (TypeError, ValueError):
                setattr(state, name, 0)
        last_error = payload.get("last_error")
        state.last_error = last_error if isinstance(last_error, str) else None
        return state


class GeminiWebDriver:
    def __init__(self, user_data_dir: str = None):
        user_data_dir = user_data_dir or config.USER_DATA_DIR
        self.user_data_dir = user_data_dir
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        # 仅用于“惰性创建会话桶页面”，避免并发请求为同一 key 重复建页
        self._page_lock = asyncio.Lock()
        # 浏览器初始化失败时记录原因，让服务仍能启动并对外暴露可读错误
        self.init_error: Optional[str] = None
        # 每个会话桶最近一次 send_chat **实际**发给网页版的 prompt（增量或播种
        # 由 driver 内部按会话是否有历史决定）。server / streaming 用它估算 usage，
        # 避免用调用方“预估”的那份；必须按桶隔离，否则并发时会互相覆盖。
        self._last_prompts: Dict[str, str] = {}

        # ---- 会话状态（按任务分桶，见 config.SESSION_KEY_HEADER）----
        # 每个桶持有：一条独立页面 + 独立会话状态。
        # 「默认桶」继续使用 self.page，因此不涉及分桶的旧调用/测试行为不变。
        # 会话状态里的 has_history 为 False 时必须“播种”完整上下文，
        # 否则 build_prompt 只发增量会让模型收到一条没有前因的孤立消息。
        self._sessions: Dict[str, SessionState] = {}
        self._pages: Dict[str, Any] = {}
        # 每个桶页面的最后一次使用时间（time.monotonic），用于空闲回收 / LRU 淘汰
        self._page_last_used: Dict[str, float] = {}
        # 按桶并发时的锁（PARALLEL_BUCKETS=true 才启用；默认桶始终用 self.lock）
        self._locks: Dict[str, asyncio.Lock] = {}
        # 正在处理请求（已拿到锁、正在生成）的会话桶，供 /healthz 观察多 Agent 占用。
        # 不能用“锁是否被持有”来推断：串行模式下所有桶共用一把锁，会把所有桶都算成忙。
        self._active_buckets: set = set()

    # ---------- 会话桶 ----------
    def _state(self, key: Optional[str] = None) -> SessionState:
        """取出某个会话桶的状态；首次访问时从磁盘恢复。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._sessions.get(bucket)
        if state is None:
            state = SessionState.from_payload(self._load_session_state(bucket))
            self._sessions[bucket] = state
        return state

    def _page_for(self, key: Optional[str] = None):
        """取出某个会话桶的页面；默认桶就是 ``self.page``。"""
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            return self.page
        return self._pages.get(bucket)

    def sent_prompt(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

    def busy_keys(self) -> List[str]:
        """当前正在处理请求（已拿到锁、正在生成）的会话桶，供多 Agent 场景观察占用。"""
        return sorted(self._active_buckets)

    def cluster_stats(self) -> Dict[str, Any]:
        """多会话 / 多 Agent 运行概况（并发开关、桶上限、占用、已开页面数）。"""
        return {
            "parallel": config.PARALLEL_BUCKETS,
            "max_buckets": config.MAX_SESSION_BUCKETS,
            "open_pages": len(self._pages),
            "bucket_lock_timeout_s": config.BUCKET_LOCK_TIMEOUT_S,
            "busy": self.busy_keys(),
            "keys": self.session_keys(),
        }

    def _lock_for(self, key: Optional[str] = None) -> asyncio.Lock:
        """取某个会话桶的锁。

        默认（``PARALLEL_BUCKETS=false``）所有桶共用 ``self.lock``，即**串行**：
        分桶只是上下文隔离，不是并发能力。只有显式打开开关才会按桶各持一把锁。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if not config.PARALLEL_BUCKETS or bucket == DEFAULT_SESSION_KEY:
            return self.lock
        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bucket] = lock
        return lock

    @asynccontextmanager
    async def _session_lock(self, key: Optional[str] = None):
        """获取某个会话桶的锁；超过 ``BUCKET_LOCK_TIMEOUT_S`` 则抛 ``GeminiBusyError``。

        ``BUCKET_LOCK_TIMEOUT_S=0``（默认）表示一直等，保持旧行为；
        设成正数后，同一会话桶的请求堆叠时会快速失败，而不是排到客户端超时之后。
        """
        lock = self._lock_for(key)
        timeout = config.BUCKET_LOCK_TIMEOUT_S
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                bucket = key or DEFAULT_SESSION_KEY
                raise GeminiBusyError(
                    f"会话桶 {bucket} 正在处理另一个请求（等待超过 {timeout:g}s）。"
                    "请稍后重试；若要并发访问，请为每个 Agent 使用不同的会话标识。"
                ) from None
        else:
            await lock.acquire()
        bucket = key or DEFAULT_SESSION_KEY
        self._active_buckets.add(bucket)
        try:
            yield
        finally:
            self._active_buckets.discard(bucket)
            lock.release()

    def _touch_page(self, key: Optional[str] = None) -> None:
        self._page_last_used[key or DEFAULT_SESSION_KEY] = time.monotonic()

    def _bucket_busy(self, bucket: str) -> bool:
        """该桶是否正在生成回复（锁被持有）。用它代替额外的“活跃桶”标志。"""
        return self._lock_for(bucket).locked()

    async def _close_bucket_page(self, bucket: str, reason: str) -> bool:
        """关闭某个会话桶的页面（**只关页面，状态保留**）。

        返回是否真的关掉了一个页面。状态里的 ``url`` / ``turns`` 不动，
        因此下次用到该桶时会重新打开同一个会话并按需播种上下文。
        """
        page = self._pages.pop(bucket, None)
        self._page_last_used.pop(bucket, None)
        if page is None:
            return False
        try:
            await page.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[回收] 关闭 key={bucket} 的页面时出错（已忽略）：{exc}")
        else:
            print(f"[回收] 已关闭 key={bucket} 的页面（{reason}），会话状态保留。")
        return True

    async def _recycle_idle_pages(self, exclude: Optional[str] = None) -> int:
        """关闭空闲超过 ``BUCKET_IDLE_TTL_S`` 的桶页面，返回关闭数量。"""
        ttl = config.BUCKET_IDLE_TTL_S
        if ttl <= 0:
            return 0
        now = time.monotonic()
        closed = 0
        for bucket in list(self._pages):
            if bucket == exclude or self._bucket_busy(bucket):
                continue
            last_used = self._page_last_used.get(bucket, now)
            if now - last_used > ttl:
                closed += 1 if await self._close_bucket_page(bucket, f"空闲超过 {int(ttl)}s") else 0
        return closed

    async def _evict_lru_page(self, exclude: Optional[str] = None) -> bool:
        """淘汰最久未用的桶页面（状态保留），腾出一个位置。

        正在生成回复的桶与 ``exclude`` 永不淘汰：淘汰它们会直接中断正在进行的一轮对话。
        """
        candidates = [
            bucket for bucket in self._pages
            if bucket != exclude and not self._bucket_busy(bucket)
        ]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda b: self._page_last_used.get(b, 0.0))
        return await self._close_bucket_page(oldest, "超出会话桶上限，按 LRU 淘汰")

    async def _wait_ready(self, page) -> bool:
        """等页面的输入框就绪；超时只警告，不抛错（调用方还有自己的等待）。"""
        try:
            await page.wait_for_selector(
                config.READY_SELECTOR, timeout=config.READY_TIMEOUT_MS, state="visible"
            )
            return True
        except Exception:
            print("[会话] 页面已打开，但未检测到输入框，请检查登录状态。")
            return False

    async def _ensure_page(self, key: Optional[str]) -> None:
        """为额外会话桶惰性创建页面并回到它上次的会话（如存在）。

        桶数量达到 ``MAX_SESSION_BUCKETS`` 时**不再直接报错**：先回收空闲页面，
        再按 LRU 淘汰最久未用的页面（**只关页面、状态保留**，下次会自动重开同一会话
        并按需播种）。只有显式把 ``MAX_SESSION_BUCKETS=0`` 设成“不允许额外桶”时才拒绝。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY or bucket in self._pages:
            return
        async with self._page_lock:
            if bucket in self._pages:  # 并发请求可能已经建好了
                return
            if self.context is None:
                raise RuntimeError("浏览器尚未初始化，无法创建新的会话页面。")
            limit = config.MAX_SESSION_BUCKETS
            if limit <= 0:
                raise RuntimeError(
                    "MAX_SESSION_BUCKETS=0 表示不允许额外的会话桶（所有请求共用默认会话）。"
                    "如需按任务隔离，请把它设为 >=1；想彻底关闭分桶请用 SESSION_SCOPING=false。"
                )
            # 先回收空闲页面，仍不够就按 LRU 淘汰最久未用的（两者都不丢会话状态）
            await self._recycle_idle_pages(exclude=bucket)
            while len(self._pages) >= limit:
                if not await self._evict_lru_page(exclude=bucket):
                    raise RuntimeError(
                        f"会话桶数量已达上限（{limit}），且当前没有可回收的页面"
                        "（正在生成回复的会话不会被淘汰）。请稍后重试。"
                    )
            page = await self.context.new_page()
            self._pages[bucket] = page
            self._touch_page(bucket)
            state = self._state(bucket)
            # 每桶始终新开对话；上下文靠本轮的「播种」重建
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await self._open_new_chat(page)
            await self._wait_ready(page)
            state.has_history = False
        print(f"[会话] 已为 key={bucket} 创建独立会话页面（{HOME_URL}）")

    # ---- 默认桶的状态：保留为属性，兼容既有调用与测试 ----
    @property
    def session_has_history(self) -> bool:
        return self._state().has_history

    @session_has_history.setter
    def session_has_history(self, value: bool) -> None:
        self._state().has_history = bool(value)

    @property
    def session_turns(self) -> int:
        return self._state().turns

    @session_turns.setter
    def session_turns(self, value: int) -> None:
        self._state().turns = int(value)

    @property
    def session_est_tokens(self) -> int:
        return self._state().est_tokens

    @session_est_tokens.setter
    def session_est_tokens(self, value: int) -> None:
        self._state().est_tokens = int(value)

    @property
    def session_cap_hit(self) -> bool:
        return self._state().cap_hit

    @session_cap_hit.setter
    def session_cap_hit(self, value: bool) -> None:
        self._state().cap_hit = bool(value)

    @property
    def last_error(self) -> Optional[str]:
        return self._state().last_error

    @last_error.setter
    def last_error(self, value: Optional[str]) -> None:
        self._state().last_error = value

    @property
    def _pending_rotation(self) -> bool:
        return self._state().pending_rotation

    @_pending_rotation.setter
    def _pending_rotation(self, value: bool) -> None:
        self._state().pending_rotation = bool(value)

    async def init(self):
        """初始化浏览器实例"""
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=config.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception as exc:  # noqa: BLE001
            # persistent context 不能被两个进程共用；给出可操作的提示而不是原始堆栈
            await self.playwright.stop()
            self.playwright = None
            message = str(exc)
            if (
                "existing browser session" in message
                or "profile is already in use" in message
                or "SingletonLock" in message
            ):
                raise RuntimeError(
                    f"浏览器用户目录 {self.user_data_dir} 已被另一个 Chromium 实例占用。\n"
                    "通常是因为已有一个 gemini_api_server.py 仍在运行，"
                    "或上一次的浏览器窗口没有关闭。\n"
                    "请先结束旧实例再重试：\n"
                    "  pkill -f gemini_api_server.py\n"
                    "或直接关闭占用该 profile 的 Chromium 窗口。"
                ) from exc
            raise
        self.page = await self.context.new_page()
        await self._restore_session_on_startup()

    async def _restore_session_on_startup(self) -> None:
        """启动时一律新开对话（不做 URL 恢复）。

        会话连续性由「每桶新开对话 + 历史播种」保证：桶状态里只记录轮数 /
        体积 / 是否到顶，页面关闭后下次重开会用 build_prompt(seed=True)
        重放历史。因此这里不再读取或回填任何会话地址。
        """
        state = self._load_session_state()
        self.session_turns = int(state.get("turns") or 0)
        self.session_est_tokens = int(state.get("est_tokens") or 0)
        self.session_cap_hit = bool(state.get("cap_hit"))
        self.last_error = state.get("last_error") or None

        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._open_new_chat(self.page)
        await self._wait_ready(self.page)
        # 页面刚从空白对话开始，必须播种完整上下文
        self.session_has_history = False
        self.session_turns = 0
        self.session_est_tokens = 0
        self.session_cap_hit = False
        print("[系统提示] 服务启动成功！请确保 Gemini 页面保持登录状态。\n")

    async def _open_new_chat(self, page) -> None:
        """点击「新建对话」，确保从干净会话开始（点不到就沿用当前页）。"""
        for selector in config.NEW_CHAT_SELECTOR.split("||"):
            selector = selector.strip()
            if not selector:
                continue
            try:
                button = await page.wait_for_selector(selector, timeout=3000)
                if button:
                    await button.click()
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue
        if config.DEBUG:
            print("[debug] 未找到新建对话按钮，沿用当前会话页。")

    # ---------- 会话上下文 -> 单条 prompt ----------
    @staticmethod
    def build_prompt(
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> str:
        """把客户端发来的消息数组转换成要发给网页输入框的文本。

        具体实现见 :func:`gemini_web.prompting.build_prompt`（这里保留为
        静态方法是为了向后兼容既有的调用方式）。
        """
        return prompting.build_prompt(messages, tools, tool_choice)

    # ---------- 会话状态持久化（不再涉及会话 URL）----------
    def _read_state_file(self) -> Dict[str, Any]:
        """读取原始状态文件（解析失败或非 JSON 时返回空字典）。"""
        try:
            if not config.SESSION_FILE.exists():
                return {}
            raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            return {}
        if not raw.startswith("{"):
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _load_session_state(self, key: Optional[str] = None) -> Dict[str, Any]:
        """读取某个会话桶的状态（轮数 / 体积 / 是否到顶 / 上次错误）。"""
        bucket = key or DEFAULT_SESSION_KEY
        data = self._read_state_file()
        if bucket == DEFAULT_SESSION_KEY:
            return {k: v for k, v in data.items() if k not in ("sessions", "version")}
        extra = data.get("sessions")
        own = extra.get(bucket) if isinstance(extra, dict) else None
        return own if isinstance(own, dict) else {}

    def _save_session_state(self, key: Optional[str] = None) -> None:
        """落盘某个会话桶的状态，供轮转决策与跨重启延续预算使用。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.updated_at = int(time.time())
        payload = state.to_payload()

        data = self._read_state_file()
        extra = data.get("sessions")
        extra = dict(extra) if isinstance(extra, dict) else {}
        if bucket == DEFAULT_SESSION_KEY:
            payload["sessions"] = extra
        else:
            extra[bucket] = payload
            data = {k: v for k, v in data.items() if k != "sessions"}
            data["sessions"] = extra
            payload = data
        try:
            config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.SESSION_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def _remember_session(self, key: Optional[str] = None) -> None:
        """刷新落盘的会话状态（保留此名字，兼容既有调用）。"""
        self._save_session_state(key=key)

    def session_keys(self) -> List[str]:
        """当前在用的会话桶（至少包含默认桶）。"""
        return sorted({DEFAULT_SESSION_KEY, *self._sessions})

    def needs_seed(self, key: Optional[str] = None) -> bool:
        """某个会话桶的网页会话里没有可用上下文时，需要把完整历史播种进去。"""
        return not self._state(key).has_history

    def session_stats(self, key: Optional[str] = None) -> Dict[str, Any]:
        """供 /healthz 观察会话增长情况。"""
        state = self._state(key)
        return {
            "has_history": state.has_history,
            "needs_seed": self.needs_seed(key),
            "turns": state.turns,
            "est_tokens": state.est_tokens,
            "cap_hit": state.cap_hit,
            "pending_rotation": state.pending_rotation,
            "last_error": state.last_error,
            "buckets": self.session_keys(),
        }

    def _session_over_budget(self, key: Optional[str] = None) -> bool:
        """会话体积是否已达到轮转阈值（0 表示禁用该维度）。"""
        state = self._state(key)
        if config.SESSION_MAX_TURNS and state.turns >= config.SESSION_MAX_TURNS:
            return True
        if config.SESSION_MAX_TOKENS and state.est_tokens >= config.SESSION_MAX_TOKENS:
            return True
        return False

    async def _start_new_session(self, key: Optional[str] = None) -> None:
        """轮转到新会话，并重置会话状态（调用方必须使用“播种”prompt）。"""
        page = self._page_for(key)
        if page is None:
            return
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._open_new_chat(page)
        await self._wait_ready(page)
        state = self._state(key)
        state.has_history = False
        state.turns = 0
        state.est_tokens = 0
        state.cap_hit = False
        state.pending_rotation = False
        state.last_error = None
        self._save_session_state(key=key)
        print("[轮转] 已开启新的网页会话（本轮会用完整历史播种上下文）。")

    _CAP_CHECK_JS_TEMPLATE = (
        "() => { let text = document.body ? (document.body.innerText || '') : '';"
        " for (const node of document.querySelectorAll(%s)) {"
        " const t = node.innerText || ''; if (t) text = text.replace(t, ' '); }"
        " return text; }"
    )

    async def _page_shows_context_limit(self, key: Optional[str] = None) -> bool:
        """页面是否出现“对话长度上限”类提示。

        先把模型回复节点的文本从整页文本里剔除，避免把回复正文里提到
        “长度上限”误判成网页版的提示。
        """
        page = self._page_for(key)
        if page is None:
            return False
        js = self._CAP_CHECK_JS_TEMPLATE % json.dumps(config.RESPONSE_SELECTORS)
        try:
            page_text = await page.evaluate(js)
        except Exception:
            return False
        for pattern in config.CAP_NOTICE_PATTERNS:
            try:
                if re.search(pattern, page_text or "", re.IGNORECASE):
                    return True
            except re.error:
                continue
        return False

    def _mark_context_limit(self, key: Optional[str] = None) -> None:
        state = self._state(key)
        state.cap_hit = True
        state.last_error = "context_length_exceeded"
        self._save_session_state(key=key)

    def _context_limit_error(self) -> "GeminiContextLimitError":
        return GeminiContextLimitError(
            "Gemini 网页会话已达上下文长度上限（网页版会停止响应）。"
            "本服务会自动轮转到新会话并播种历史；若仍失败，请检查登录状态。"
        )

    async def _recover_session(self, key: Optional[str] = None) -> bool:
        """超时后重开一个干净对话（不做 URL 恢复），成功返回 True。

        上下文不会丢：调用方在本轮失败后会以「播种」prompt 重发历史。
        """
        page = self._page_for(key)
        if page is None:
            print("[恢复] 没有可用页面，无法恢复。")
            return False
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await self._open_new_chat(page)
            if not await self._wait_ready(page):
                print("[恢复] 已打开页面，但未检测到输入框，请检查登录状态。")
                return False
            self._state(key).has_history = False
            print("[恢复] 已重开新对话（本轮将重新播种上下文）。")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[恢复] 重开会话失败: {exc}")
            return False

    async def send_chat(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
    ) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        :param prompt: 增量 prompt（网页会话已有上下文时使用）
        :param seeded_prompt: 带完整历史的“播种”prompt（需要新开会话时使用，
                              未提供则退回 ``prompt``）
        :param key: 会话桶标识（按任务隔离会话）。不同 key 各自持有一条独立
                    的网页会话与页面，互不污染上下文；None 表示默认桶。

        **重试阶梯**（本项目不做 URL 恢复，重试即重开对话 + 播种）：

        1. 首级：直接用现有会话（若已达体积预算或上次到顶，先轮转到新会话）；
        2. 中间级：退避后重试同一个页面；
        3. 最高一级：**重开新对话 + 用播种 prompt 重放历史**。

        页面完全没有新回复（超时）最常见的原因是会话已到顶 / 已失效；
        由于我们无法可靠回填会话 URL，与其重开同一个会话，不如直接新开 + 播种。
        """
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        last_error: Optional[RuntimeError] = None

        # 额外的会话桶需要自己的页面（默认桶就是 self.page，不涉及创建）
        await self._ensure_page(bucket)

        for attempt in range(1, max_attempts + 1):
            state = self._state(bucket)
            if state.pending_rotation:
                # 体积超预算或上次检测到“到顶”：先轮转，再播种
                await self._start_new_session(bucket)
            elif attempt == 1:
                pass
            elif attempt < max_attempts:
                # 不做 URL 恢复：退避后在同一页面重试一次（页面可能只是慢）
                print(f"[恢复] 第 {attempt}/{max_attempts} 次重试：等待后重试……")
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
            else:
                print("[恢复] 重试无效，改为开启新对话并重放历史……")
                await self._start_new_session(bucket)

            # 会话是新开的（或被轮转过）-> 必须播种，否则模型收不到任何上下文
            active_prompt = seeded if not self._state(bucket).has_history else prompt
            # 记录真正要发出的那份 prompt，供上层估算 usage（按桶隔离，避免并发串台）
            self._last_prompts[bucket] = active_prompt

            await self._remember_session(bucket)
            try:
                return await self._send_chat_locked(active_prompt, on_delta, key=bucket)
            except GeminiContextLimitError as exc:
                # 到顶了：下次不要再恢复同一个会话，直接轮转
                last_error = exc
                self._state(bucket).pending_rotation = True
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：会话已达上下文上限。")
            except GeminiTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                self._state(bucket).last_error = str(exc)
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：等待回复超时。")

        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

    # 主判定所用的 JS：扫描页面上可见的「停止生成」控件
    _GENERATING_JS = """
    () => {
      const words = ['\u505c\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        'button, [role="button"], div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
      );
      for (const el of nodes) {
        const label = [
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          (el.textContent || '').slice(0, 40),
        ].join(' ');
        const cls = typeof el.className === 'string' ? el.className : '';
        if (!words.some((w) => label.includes(w)) && !/stop/i.test(cls)) continue;
        const rect = el.getBoundingClientRect();
        // 必须可见，且位于视口下半部（停止按钮就在底部输入框区域），
        // 避免把正文里含有 stop / 停止 字样的元素误判成生成中
        if (rect.width > 0 && rect.height > 0 && rect.top > window.innerHeight * 0.5) {
          return true;
        }
      }
      return false;
    }
    """

    async def _page_is_generating(self, key: Optional[str] = None) -> Optional[bool]:
        """检测页面是否仍在生成回复。

        True=生成中；False=页面上找不到「停止生成」控件；None=检测失败/无法判断。
        注意：只有在观测到过 True 之后，False 才可信，调用方需自行记录。
        """
        page = self._page_for(key)
        if page is None:
            return None
        try:
            return bool(await page.evaluate(self._GENERATING_JS))
        except Exception:
            return None

    _STOP_CANDIDATES_JS = """
    () => {
      const words = ['\u505c\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        'button, [role="button"], div[class*="stop"], span[class*="stop"], svg[class*="stop"], [aria-label]'
      );
      const out = [];
      for (const el of nodes) {
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        const text = (el.textContent || '').slice(0, 40);
        const cls = typeof el.className === 'string' ? el.className : '';
        const label = [aria, title, text].join(' ');
        if (!words.some((w) => label.includes(w)) && !/stop/i.test(cls)) continue;
        const r = el.getBoundingClientRect();
        out.push({
          tag: el.tagName,
          cls: cls.slice(0, 120),
          aria,
          title,
          text: text.slice(0, 40),
          visible: r.width > 0 && r.height > 0,
          top: Math.round(r.top),
          vh: window.innerHeight,
        });
        if (out.length >= 20) break;
      }
      return out;
    }
    """

    async def debug_stop_candidates(self, key: Optional[str] = None) -> List[dict]:
        """诊断用：列出页面上所有「可能表示生成中」的控件及其位置。"""
        page = self._page_for(key)
        if page is None:
            return []
        try:
            return await page.evaluate(self._STOP_CANDIDATES_JS)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]

    async def _extract_code_blocks(self, element) -> List[dict]:
        """从某条回复的 DOM 节点中提取代码块（语言 + 纯代码文本）。"""
        extracted: List[dict] = []
        if element is None:
            return extracted
        code_elements = await element.query_selector_all(config.CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(config.CODE_TAG_SELECTOR)
            lang = "txt"
            if code_tag:
                class_attr = await code_tag.get_attribute('class') or ""
                lang_match = re.search(r'language-(\w+)', class_attr)
                if lang_match:
                    lang = lang_match.group(1)

            code_content = await (code_tag or code_el).inner_text()
            clean_code = re.sub(
                r'^(?:' + lang + r'|bash|python|json|html|javascript)?\s*(?:Copy|Download)\s*\n',
                '', code_content, flags=re.IGNORECASE
            ).strip()

            extracted.append({"lang": lang, "code": clean_code})
        return extracted

    async def _send_chat_locked(self, prompt: str, on_delta=None,
                                key: Optional[str] = None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        :param key: 会话桶标识（决定使用哪一条页面）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        page = self._page_for(bucket)
        state = self._state(bucket)
        # 默认所有桶共用 self.lock（串行）；只有 PARALLEL_BUCKETS=true 才按桶各持一把锁
        async with self._session_lock(bucket):
            if page is None:
                raise RuntimeError("浏览器尚未初始化：找不到可用于发送的会话页面。")
            self._touch_page(bucket)  # 正在用的页面不会被空闲回收 / LRU 淘汰
            # 1. 定位并填入输入框
            chat_input = None
            for selector in config.INPUT_SELECTORS:
                try:
                    chat_input = await page.wait_for_selector(selector, timeout=3000)
                    if chat_input:
                        break
                except Exception:
                    continue

            if not chat_input:
                raise RuntimeError("无法找到对话输入框，请检查 Gemini 网页是否打开或处于登录状态。")

            # 记录发送前最后一条回复的文本，用来判断“新回复是否已经出现”。
            # 注意：绝不能用“回复节点数量变多”来判断。
            # Gemini 的消息列表会回收/替换节点，长会话下节点数可能恒为 2，
            # 新回复只会把旧节点内容改掉而不会让数量增长，
            # 那样会导致永远读不到本轮回复直接等到超时。
            before_text = ""
            try:
                before_nodes = await page.query_selector_all(config.RESPONSE_SELECTORS)
                if before_nodes:
                    before_text = (await before_nodes[-1].inner_text()).strip()
            except Exception:
                before_text = ""

            await chat_input.fill(prompt)
            await page.keyboard.press("Enter")

            # 2. 轮询等待回复完成
            await asyncio.sleep(config.POLL_INTERVAL_S)
            last_text = ""
            last_normalized = ""
            last_len = -1
            streamed = ""            # 已经通过 on_delta 发给客户端的内容
            stable_count = 0
            saw_generating = False      # 本轮是否观测到过页面「生成中」状态
            latest_node = None          # 本轮最新的回复节点
            poll = 0
            deadline = asyncio.get_event_loop().time() + config.RESPONSE_TIMEOUT_S

            cap_check_every = max(1, config.CAP_CHECK_EVERY)

            while True:
                poll += 1
                responses = await page.query_selector_all(config.RESPONSE_SELECTORS)
                current_text = ""
                generating = None
                if responses:
                    latest_node = responses[-1]
                    current_text = await latest_node.inner_text()
                normalized = current_text.strip()

                # 1. 本轮回复是否已经出现：只要最后一条回复的内容与发送前不同即可。
                #    （不看节点数量：长会话下新回复会原地替换旧节点，数量不增长）
                reply_seen = bool(normalized) and normalized != before_text

                # 1.1 还没有新回复时，周期性检查是否“会话到顶”。
                #     到顶与“真的卡住”在外表上完全一样（页面不再产生新回复），
                #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                if not reply_seen and poll % cap_check_every == 0:
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()

                if reply_seen:
                    # 2.1 主判定：页面「生成中」状态。一旦观测到过「停止生成」
                    #     控件、又发现它消失，就说明生成真正结束，可立即收尾
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True
                    elif generating is False and saw_generating:
                        last_text = current_text
                        if config.DEBUG:
                            print(f"[debug] poll={poll} 停止按钮已消失，判定结束")
                        break

                    # 2.2 兜底判定：文本一模一样算一轮不变；
                    #     仅长度不再增长也算，但要更保守（多等几轮），
                    #     以免尾部重排 / 工具栏插入导致永远等不到逐字相等
                    same_text = bool(normalized) and normalized == last_normalized
                    same_len = bool(normalized) and len(normalized) == last_len
                    if same_text or same_len:
                        stable_count += 1
                        threshold = config.STABLE_POLLS if same_text else config.LEN_STABLE_POLLS
                        if stable_count >= threshold:
                            last_text = current_text
                            if config.DEBUG:
                                print(
                                    f"[debug] poll={poll} 内容稳定 {stable_count} 次"
                                    f"（same_text={same_text}），判定结束"
                                )
                            break
                    else:
                        stable_count = 0

                    # 2.3 生成过程中吐出增量，供 SSE 使用。
                    #     用「已发送内容」的公共前缀做 diff，即使节点中途重排也不会漏字
                    if on_delta is not None:
                        piece, streamed = _delta_piece(streamed, current_text)
                        if piece:
                            await on_delta(piece)

                    last_text = current_text
                    last_normalized = normalized
                    last_len = len(normalized)

                if config.DEBUG:
                    print(
                        f"[debug] poll={poll} nodes={len(responses)} len={len(normalized)} "
                        f"stable={stable_count} generating={generating} saw={saw_generating} "
                        f"before_len={len(before_text)}"
                    )

                # 总超时判定：若这期间其实已经读到实质回复，就直接返回已产生的内容，
                # 绝不再把同一句 prompt 重发一遍（避免网页多出一轮、与客户端状态错位）
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session(bucket)
                    if last_text:
                        print("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    # 超时前最后确认一次是否“到顶”，否则错误信息会误导排查方向
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise GeminiTimeoutError(
                        f"等待 Gemini 响应超时（{int(config.RESPONSE_TIMEOUT_S)}s）。"
                    )

                await asyncio.sleep(config.POLL_INTERVAL_S)

            # 3. 从最新回复节点中提取代码块
            extracted_blocks = await self._extract_code_blocks(latest_node)

            # 4. 更新会话状态：已建立历史，并累计体积；超预算则下一轮轮转
            state.has_history = True
            state.turns += 1
            state.est_tokens += estimate_tokens(prompt) + estimate_tokens(last_text)
            state.last_error = None
            if self._session_over_budget(bucket):
                state.pending_rotation = True
                print(
                    f"[轮转] 会话已达预算（轮数={state.turns}，"
                    f"估算 token={state.est_tokens}），"
                    "下一轮将开启新会话并播种上下文。"
                )

            # 成功产生回复后：刷新会话状态（可能刚创建了新会话）并续期页面使用时间
            self._touch_page(bucket)
            await self._remember_session(bucket)
            return last_text, extracted_blocks

    @staticmethod
    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        # 同一秒内的多个请求会拿到同样的 timestamp，必须再加一段随机后缀，
        # 否则 code_<ts>_1.py / response_<ts>.md 会互相覆盖（多任务并行后很常见）
        unique = uuid.uuid4().hex[:6]

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}_{unique}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                print(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved

    def reset_session(self, key: Optional[str] = None) -> None:
        """把某个会话桶标记为“下一轮开新会话”（手动逃生口）。

        只改状态、不碰页面：下一轮的 ``send_chat`` 会先轮转，并用“播种”
        prompt 重放历史，所以不会丢上下文。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.pending_rotation = True
        state.cap_hit = False
        state.has_history = False
        self._save_session_state(key=bucket)
        print(f"[会话] 已请求重置 key={bucket} 的会话，下一轮将开启新会话并播种上下文。")

    async def close(self):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
