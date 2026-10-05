"""会话状态与持久化（``SessionStoreMixin``）。

负责：按任务分桶的 :class:`SessionState`、磁盘读写、轮转判定、
以及默认桶的属性别名（``session_has_history`` 等），保持既有调用与测试不变。
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from . import config
from .errors import DEFAULT_SESSION_KEY, HOME_URL

logger = logging.getLogger(__name__)


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


class SessionStoreMixin:
    # ---------- 会话桶 ----------
    def _state(self, key: Optional[str] = None) -> SessionState:
        """取出某个会话桶的状态；首次访问时从磁盘恢复。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._sessions.get(bucket)
        if state is None:
            state = SessionState.from_payload(self._load_session_state(bucket))
            self._sessions[bucket] = state
            self._evict_session_cache()
        return state

    def _evict_session_cache(self) -> None:
        """内存会话缓存超限时按最久未用逐出（状态已落盘，安全）。

        默认桶永不逐出；正在持有锁 / 活跃的桶也不逐出，避免打断进行中的请求。
        """
        limit = config.MAX_SESSION_STATE_CACHE
        if limit <= 0 or len(self._sessions) <= limit:
            return
        candidates = [
            bucket for bucket in self._sessions
            if bucket != DEFAULT_SESSION_KEY and not self._bucket_busy(bucket)
        ]
        # 用页面最近使用时间作为 LRU 依据；没有页面记录的排在最前（最旧）。
        candidates.sort(key=lambda b: self._page_last_used.get(b, 0.0))
        for bucket in candidates[: max(0, len(self._sessions) - limit)]:
            self._sessions.pop(bucket, None)
            self._last_prompts.pop(bucket, None)

    def _page_for(self, key: Optional[str] = None):
        """取出某个会话桶的页面；默认桶就是 ``self.page``。"""
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            return self.page
        return self._pages.get(bucket)

    def sent_prompt(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

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

    # ---------- 会话状态持久化（不再涉及会话 URL）----------
    def _read_state_file(self) -> Dict[str, Any]:
        """读取原始状态文件（解析失败或非 JSON 时返回空字典）。

        注意：解析失败一律回退成 {}——上层会把“读不到”当成“没有状态”，因此**写坏
        文件 = 状态凭空清零**。写侧必须保证原子性（见 _save_session_state）。
        """
        try:
            if not config.SESSION_FILE.exists():
                return {}
            raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("读取会话状态文件失败：%s", exc)
            return {}
        if not raw.startswith("{"):
            logger.warning("会话状态文件内容异常（不以 { 开头），已忽略：%s", config.SESSION_FILE)
            return {}
        try:
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话状态文件 JSON 解析失败（将视为无状态）：%s", exc)
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
        """落盘某个会话桶的状态，供轮转决策与跨重启延续预算使用。

        **不变量（勿破坏）**：本方法必须保持“纯同步、无 await”。单事件循环下，
        没有 await 的临界区是不可被打断的——不同会话桶并发调用时，读-改-写因此
        天然互斥（`tests/test_sessions.py::StateFileConcurrencyTests` 已锁住这一点）。
        一旦在里面引入 await，就必须补一把跨桶的 asyncio.Lock。

        写入采用“临时文件 + os.replace”原子替换：直接 write_text 会在崩溃 / 被 kill
        时留下被截断的 JSON，而 `_read_state_file` 对解析失败一律返回 {}，
        表现为**状态（轮数 / token 预算）凭空清零**。
        """
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
            tmp = config.SESSION_FILE.with_name(config.SESSION_FILE.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, config.SESSION_FILE)  # 原子替换（同目录同文件系统）
        except OSError as exc:
            logger.warning("会话状态落盘失败（key=%s）：%s", bucket, exc)

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
