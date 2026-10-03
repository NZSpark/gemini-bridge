"""页面池与并发锁（``PagePoolMixin``）。

负责：会话桶页面的惰性创建、空闲回收 / LRU 淘汰、按桶加锁，
以及供 ``/healthz`` 观察的占用统计。页面关闭只关页面，会话状态保留。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from . import config
from .errors import DEFAULT_SESSION_KEY, HOME_URL, GeminiBusyError


class PagePoolMixin:
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
