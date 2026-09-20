"""会话状态：短令牌映射 + 待确认操作。

**为什么需要这个模块**：Telegram 的 callback data 有 **64 字节**硬上限，
而一个 OCID 就 100+ 字符，根本塞不进按钮。所以按钮里只放短令牌，
真实对象存在内存里，点按钮时再反查。

顺带解决第二个问题：**危险操作的二次确认**。所有破坏性操作先生成一个
``PendingAction``，按钮里带它的令牌；用户点「执行」时我们再从存储里取出
原始意图去执行 —— 这样用户改不了参数，也就无法绕过确认流程。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 令牌有效期（秒）。过期后按钮失效，必须重新走一遍流程。
TOKEN_TTL = 30 * 60

#: 每个会话最多保留多少个令牌（防内存膨胀）
MAX_TOKENS_PER_CHAT = 200


@dataclass
class PendingAction:
    """一个等待用户确认的操作。"""

    token: str
    chat_id: int
    user_id: int
    kind: str                       # launch / terminate / power / bucket_delete / ...
    account_index: int
    summary: str                    # 展示给人看的计划文本
    payload: dict[str, Any] = field(default_factory=dict)
    destructive: bool = False       # 是否需要「不可逆」开关也打开
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > TOKEN_TTL


class Store:
    """进程内的会话存储。

    刻意不做持久化：重启后所有令牌失效、所有待确认操作作废，
    这是**安全上的正确行为** —— 一个重启前的「删库」确认不该在重启后还生效。
    """

    def __init__(self) -> None:
        self._tokens: dict[int, dict[str, Any]] = {}
        self._token_time: dict[int, dict[str, float]] = {}
        self._pending: dict[str, PendingAction] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._awaiting: dict[int, str] = {}
        self._wizards: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    #  短令牌
    # ------------------------------------------------------------------
    def put(self, chat_id: int, payload: Any) -> str:
        """存一个对象，返回 6 字符短令牌。"""
        with self._lock:
            bucket = self._tokens.setdefault(chat_id, {})
            times = self._token_time.setdefault(chat_id, {})
            self._gc(chat_id)

            for _ in range(20):
                token = secrets.token_urlsafe(4)[:6]
                if token not in bucket:
                    bucket[token] = payload
                    times[token] = time.time()
                    break
            else:
                # 极端情况：连续撞名，退化成加长令牌
                token = secrets.token_urlsafe(8)
                bucket[token] = payload
                times[token] = time.time()

            if len(bucket) > MAX_TOKENS_PER_CHAT:
                oldest = sorted(times, key=times.get)[:len(bucket) - MAX_TOKENS_PER_CHAT]
                for t in oldest:
                    bucket.pop(t, None)
                    times.pop(t, None)
            return token

    def get(self, chat_id: int, token: str) -> Any:
        with self._lock:
            self._gc(chat_id)
            return self._tokens.get(chat_id, {}).get(token)

    def _gc(self, chat_id: int) -> None:
        times = self._token_time.get(chat_id)
        if not times:
            return
        now = time.time()
        stale = [t for t, ts in times.items() if now - ts > TOKEN_TTL]
        bucket = self._tokens.get(chat_id, {})
        for t in stale:
            bucket.pop(t, None)
            times.pop(t, None)

    # ------------------------------------------------------------------
    #  待确认操作
    # ------------------------------------------------------------------
    def add_pending(self, pending: PendingAction) -> str:
        with self._lock:
            self._pending[pending.token] = pending
            self._sweep_pending()
        return pending.token

    def get_pending(self, token: str) -> PendingAction | None:
        with self._lock:
            self._sweep_pending()
            return self._pending.get(token)

    def drop_pending(self, token: str) -> None:
        with self._lock:
            self._pending.pop(token, None)

    def _sweep_pending(self) -> None:
        now = time.time()
        stale = [t for t, p in self._pending.items() if now - p.created_at > TOKEN_TTL]
        for t in stale:
            self._pending.pop(t, None)

    # ------------------------------------------------------------------
    #  等待用户输入（开机向导里的自由文本步骤：用户名、密码等）
    # ------------------------------------------------------------------
    def set_awaiting(self, chat_id: int, kind: str) -> None:
        """标记该会话正在等待一条文本输入。``kind`` 描述用途，便于提示。"""
        with self._lock:
            self._awaiting[chat_id] = kind

    def get_awaiting(self, chat_id: int) -> str | None:
        with self._lock:
            return self._awaiting.get(chat_id)

    def clear_awaiting(self, chat_id: int) -> None:
        with self._lock:
            self._awaiting.pop(chat_id, None)

    # ------------------------------------------------------------------
    #  开机向导状态（每个会话一份，存已选参数）
    # ------------------------------------------------------------------
    def put_wizard(self, chat_id: int, state: dict[str, Any]) -> None:
        with self._lock:
            self._wizards[chat_id] = state

    def get_wizard(self, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._wizards.get(chat_id)

    def del_wizard(self, chat_id: int) -> None:
        with self._lock:
            self._wizards.pop(chat_id, None)

    # ------------------------------------------------------------------
    #  并发保护
    # ------------------------------------------------------------------
    def lock_for(self, chat_id: int) -> threading.Lock:
        """取该会话的锁。

        用途：防止用户手快连点两下「执行」，把同一个销毁操作跑两遍。
        """
        with self._lock:
            if chat_id not in self._locks:
                self._locks[chat_id] = threading.Lock()
            return self._locks[chat_id]

    def claim(self, chat_id: int, token: str) -> bool:
        """原子地「认领」一个待确认操作。已被认领则返回 False。

        执行前调用它，执行完（无论成败）都要 drop_pending。
        """
        with self._lock:
            pending = self._pending.get(token)
            if pending is None:
                return False
            if getattr(pending, "_claimed", False):
                return False
            pending._claimed = True  # type: ignore[attr-defined]
            return True
