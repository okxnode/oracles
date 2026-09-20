"""抢机任务的进程内注册表：start / stop / list。

**进程级单例**（``get_task_registry()``）：Bot 是单进程，handlers 和 dispatch
必须看到同一份任务表 —— 挂在 bot_data 上容易在测试/热重载时丢引用，直接全局唯一更稳。

重启即清空：和 pending token 一个语义 —— 进程外的世界没有「还在跑」的开机任务，
别让用户误以为后台还活着。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading

from ..errors import one_line
from ..services.grab import GrabTask, run_grab_loop

log = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, GrabTask] = {}
        self._handles: dict[str, asyncio.Task] = {}   # type: ignore[name-defined]
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def create(self, chat_id: int, user_id: int, account_index: int,
               spec: dict, interval_seconds: int) -> GrabTask:
        token = secrets.token_urlsafe(4)[:6]
        task = GrabTask(token=token, chat_id=chat_id, user_id=user_id,
                        account_index=account_index, spec=dict(spec),
                        interval_seconds=interval_seconds)
        with self._lock:
            self._tasks[token] = task
        return task

    # ------------------------------------------------------------------
    def start(self, token: str, *, settings, registry, ensure_writable) -> None:
        """在事件循环里拉起该任务的协程。重复 start 幂等（已有就忽略）。"""
        with self._lock:
            task = self._tasks.get(token)
            if task is None or token in self._handles:
                return

        async def _loop():
            try:
                await run_grab_loop(task, settings, registry, ensure_writable)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 循环本身不该炸掉 Bot
                task.status = "stopped"
                task.last_error = f"任务异常退出：{one_line(exc, 120)}"
                log.exception("抢机任务 %s 异常", token)

        # 注意：start() 只在事件循环线程里被调用（handler 内），可以安全 create_task
        self._handles[token] = asyncio.get_running_loop().create_task(_loop())

    def stop(self, token: str) -> bool:
        """取消协程并标记 stopped。返回是否真的停了一个在跑的任务。"""
        with self._lock:
            task = self._tasks.get(token)
            handle = self._handles.pop(token, None)
        if handle is not None and not handle.done():
            handle.cancel()
            if task is not None and task.status == "running":
                task.status = "stopped"
                return True
        return False

    def stop_all_for(self, chat_id: int) -> list[str]:
        """停掉某会话的全部在跑任务（/cancel 用）。"""
        stopped: list[str] = []
        for token in list(self._handles):
            with self._lock:
                task = self._tasks.get(token)
            if task is not None and task.chat_id == chat_id:
                if self.stop(token):
                    stopped.append(token)
        return stopped

    # ------------------------------------------------------------------
    def get(self, token: str) -> GrabTask | None:
        with self._lock:
            return self._tasks.get(token)

    def list_for(self, chat_id: int | None = None) -> list[GrabTask]:
        """按创建时间倒序。``chat_id=None`` 返回全部（管理视图）。"""
        with self._lock:
            tasks = [t for t in self._tasks.values() if chat_id is None or t.chat_id == chat_id]
        return sorted(tasks, key=lambda t: -t.created_at)

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == "running")


# --------------------------------------------------------------------------
#  进程级单例 —— handlers / dispatch 共享同一份任务表
# --------------------------------------------------------------------------
_registry_singleton: TaskRegistry | None = None
_singleton_lock = threading.Lock()


def get_task_registry() -> TaskRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        with _singleton_lock:
            if _registry_singleton is None:
                _registry_singleton = TaskRegistry()
    return _registry_singleton
