"""小工具：并发执行、格式化。"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Sequence[T], *, max_workers: int = 4,
                 on_error: Callable[[T, Exception], R] | None = None) -> list[R]:
    """并发跑 fn，**保持输入顺序**返回结果。

    单个任务抛异常不会中断整批 —— 这是刻意设计：
    号池里 17 个账号，其中一个的密钥过期了，不该让另外 16 个的结果都拿不到。

    ``on_error`` 提供时，用它的返回值替代异常项；否则重新抛出。
    """
    if not items:
        return []
    results: list[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        future_to_pos = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for fut in as_completed(future_to_pos):
            pos = future_to_pos[fut]
            item = items[pos]
            try:
                results[pos] = fut.result()
            except Exception as exc:  # noqa: BLE001
                if on_error is None:
                    raise
                log.warning("并发任务失败（第 %d 项）：%s", pos, exc)
                results[pos] = on_error(item, exc)
    return results


def fmt_gb(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f} GB"


def fmt_gib_from_bytes(nbytes: int | float | None) -> str:
    if nbytes is None:
        return "-"
    return f"{nbytes / 1024 ** 3:.3f} GiB"


def humanize_state(state: str | None) -> str:
    """生命周期状态 → 中文 + 图标。"""
    mapping = {
        "RUNNING": "🟢 运行中",
        "STOPPED": "⚪ 已停止",
        "STOPPING": "🟡 停止中",
        "STARTING": "🟡 启动中",
        "PROVISIONING": "🟡 创建中",
        "TERMINATING": "🔴 销毁中",
        "TERMINATED": "⚫ 已销毁",
        "AVAILABLE": "🟢 可用",
        "ATTACHED": "🟢 已挂载",
        "PROVISIONING_OR_FAILED": "🟠 异常",
    }
    return mapping.get(state or "", state or "未知")


def chunked(seq: Iterable[T], size: int) -> list[list[T]]:
    items = list(seq)
    return [items[i:i + size] for i in range(0, len(items), size)]
