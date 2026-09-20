"""服务层：每个模块对应 OCI 的一个能力域，全部基于官方 SDK。"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def scan_failed(_client, exc: Exception) -> None:
    """批量扫描里单个账号失败时的占位值 —— 固定返回 ``None``。

    这是整个项目最重要的一条纪律，值得单独放一个函数而不是写成 lambda：

        ``[]``    = 扫描成功，确实什么都没有
        ``None``  = 扫描失败，结果**未知**

    一开始这里写的是 ``lambda c, exc: []``。后果是：某个账号鉴权过期时，
    它会被渲染成「该账号很干净」。在计费审计里这等于漏掉正在扣费的资源，
    在安全审计里等于漏掉对全网开放的 22 端口 —— **而且是静默的**。

    更糟的是这个 lambda 在两个调用点各写了一遍（Bot 和 CLI），
    修一处漏一处是迟早的事。所以抽到这里，让 Bot 和 CLI 共用同一个语义。

    ``None`` 的渲染由 ``audit.render_leaks`` / ``security.render_exposure`` /
    ``security.render_plaintext`` 负责，它们都会明确标出「结果未知」。
    """
    log.warning("账号扫描失败，结果标记为未知（不等于干净）：%s", exc)
    return None
