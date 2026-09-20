"""待确认操作的执行器。

**这里是全项目唯一的写操作出口。** 所有破坏性动作都必须：

    1. 先由 handler 生成「计划」（只读）展示给人看
    2. 人点「确认执行」→ 生成 ``PendingAction`` → 本模块执行

写开关的判定也**只在这里**做一次，而不是散落在各个 handler 里 ——
散落的判断早晚会有漏网之鱼，集中一处才可能审计。

双保险：
    ``ORACLES_WRITE_ENABLED``    关掉 → 所有写操作都变成 DRY-RUN
    ``ORACLES_ALLOW_DESTRUCTIVE`` 关掉 → 销毁/删除类操作单独再拦一层
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..errors import NotAllowedError, one_line
from ..oci_gateway import ClientRegistry
from ..services import audit as audit_svc
from ..services import compute as compute_svc
from ..services import quota as quota_svc
from ..services import security as security_svc
from ..services import storage as storage_svc
from .store import PendingAction

log = logging.getLogger(__name__)


async def _in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """把阻塞的 SDK 调用丢到线程池。

    OCI SDK 是同步阻塞的，直接在事件循环里调会把 Bot 卡死 ——
    一个查 17 个账号配额的请求要跑好几分钟。
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def ensure_writable(settings: Settings, *, destructive: bool = False) -> None:
    """写操作守门人。不通过就抛 NotAllowedError，消息里说明怎么放开。"""
    if not settings.write_enabled:
        raise NotAllowedError(
            "🔒 写操作已禁用（DRY-RUN 模式）。\n"
            "计划已生成但**没有真正执行**。\n\n"
            "要真正执行，请在 `<配置目录>/oracles.env` 里设置：\n"
            "`ORACLES_WRITE_ENABLED=true`\n"
            "然后重启服务。"
        )
    if destructive and not settings.allow_destructive:
        raise NotAllowedError(
            "🔒 不可逆操作被禁用。\n"
            "`ORACLES_WRITE_ENABLED` 已开，但 `ORACLES_ALLOW_DESTRUCTIVE` 还是 false。\n\n"
            "这是**刻意设计**的双保险：防止误点一下就删掉机器。\n"
            "确认要放开的话，在 `<配置目录>/oracles.env` 里设置：\n"
            "`ORACLES_ALLOW_DESTRUCTIVE=true`"
        )


# --------------------------------------------------------------------------
#  各类型操作的执行体
# --------------------------------------------------------------------------
async def _do_launch(pending: PendingAction, settings: Settings,
                     registry: ClientRegistry) -> str:
    ensure_writable(settings)
    plan = pending.payload["plan"]
    client = registry.get(pending.account_index)

    # 执行前重新确认一次 AD 余量 —— 计划生成到用户点确认之间可能过了几分钟，
    # 期间别的实例可能已经把额度用掉了。
    try:
        cap = await _in_thread(quota_svc.account_capacity, client,
                               include_object_storage=False)
        match = next((a for a in cap.ads if a.ad == plan.availability_domain), None)
        if match is not None and match.launchable <= 0:
            return (
                f"⚠️ 计划已作废：{plan.availability_domain.split(':')[-1]} 的余量"
                f"在等待确认期间变成了 0。\n"
                f"请重新生成计划（系统会自动换一个 AD）。"
            )
    except Exception as exc:  # noqa: BLE001
        log.info("执行前复核 AD 余量失败（继续执行）：%s", exc)

    view = await _in_thread(compute_svc.execute_launch, client, plan)
    return (
        f"✅ 实例已创建\n\n"
        f"名称：`{view.display_name}`\n"
        f"状态：{view.lifecycle_state}\n"
        f"可用域：{view.ad.split(':')[-1]}\n"
        f"公网 IP：`{view.public_ip or '分配中，稍等片刻'}`\n\n"
        f"ℹ️ 刚 RUNNING 时 SSH 连不上是正常的（cloud-init 还没跑完），"
        f"等约 1 分钟再试。"
    )


async def _do_power(pending: PendingAction, settings: Settings,
                    registry: ClientRegistry) -> str:
    ensure_writable(settings)
    action = pending.payload["action"]
    instance_id = pending.payload["instance_id"]
    client = registry.get(pending.account_index)

    oci_action = compute_svc.POWER_ACTIONS[action]
    state = await _in_thread(compute_svc.execute_power, client, instance_id, oci_action)
    names = {"start": "开机", "stop": "关机", "softstop": "软关机",
             "reboot": "重启", "reset": "硬重启"}
    return f"✅ 已执行{names.get(action, action)}\n\n最终状态：{state}"


async def _do_terminate(pending: PendingAction, settings: Settings,
                        registry: ClientRegistry) -> str:
    ensure_writable(settings, destructive=True)
    instance_id = pending.payload["instance_id"]
    preserve = pending.payload.get("preserve_boot_volume", False)
    client = registry.get(pending.account_index)

    # ⚠️ 重新解析 + 重新做池成员检查。不要信任计划里的快照 ——
    #    用户可能在这期间把实例加进池了。
    view = await _in_thread(compute_svc.get_instance, client, instance_id)
    if view.in_pool:
        return (
            f"⛔ 已阻止：`{view.display_name}` 现在由实例池管理。\n"
            f"直接销毁会白删（池会立刻补一台回来）。\n"
            f"请先改池的 size 或 detach。"
        )

    plan = compute_svc.TerminatePlan(instance=view, preserve_boot_volume=preserve)
    await _in_thread(compute_svc.execute_terminate, client, plan)
    return (
        f"✅ 实例已销毁：`{view.display_name}`\n"
        f"引导卷：{'已保留（会变成孤儿卷，注意用审计功能清理）' if preserve else '已一并删除'}"
    )


async def _do_bucket_delete(pending: PendingAction, settings: Settings,
                            registry: ClientRegistry) -> str:
    ensure_writable(settings, destructive=True)
    plan = pending.payload["plan"]
    client = registry.get(pending.account_index)
    result = await _in_thread(storage_svc.execute_delete_bucket, client, plan)
    return f"✅ {result}"


async def _do_s3_create(pending: PendingAction, settings: Settings,
                        registry: ClientRegistry) -> str:
    ensure_writable(settings)
    client = registry.get(pending.account_index)
    cred = await _in_thread(
        storage_svc.create_s3_credential, client, pending.payload["display_name"]
    )
    ns = await _in_thread(storage_svc.namespace, client)
    return cred.render(client.account.region, ns)


async def _do_orphan_delete(pending: PendingAction, settings: Settings,
                            registry: ClientRegistry) -> str:
    ensure_writable(settings, destructive=True)
    volumes = pending.payload["volumes"]
    client = registry.get(pending.account_index)

    # ⚠️ 删除前**实时复核**：重新拉一次挂载关系，把已挂载的剔除。
    #    不信任扫描快照 —— 删掉运行中实例的引导卷是灾难性的。
    #    verdict 把「确认已挂载」和「没查到」分开 —— 两者都必须留下，
    #    但**原因不同**，合成一句会让用户以为查过了。
    verdict = await _in_thread(audit_svc.verify_orphans_before_delete, client, volumes)
    safe = verdict.safe
    if not safe:
        reasons = []
        if verdict.attached:
            reasons.append(f"{len(verdict.attached)} 块已挂载到实例")
        if verdict.unverified:
            reasons.append(f"{len(verdict.unverified)} 块挂载状态没查到（为安全起见不删）")
        detail = "、".join(reasons) if reasons else "候选列表为空"
        return f"⚠️ 复核后没有可删的卷（**未删除任何卷**）：{detail}。"

    deleted, failed = [], []
    for vol in safe:
        try:
            await _in_thread(audit_svc.delete_volume, client, vol)
            deleted.append(vol)
        except Exception as exc:  # noqa: BLE001
            failed.append((vol, one_line(exc, 120)))

    freed = sum(v.size_gb for v in deleted)
    lines = [f"✅ 已删除 {len(deleted)} 块孤儿卷，回收 {freed:.0f} GB"]
    if verdict.attached:
        lines.append(f"（复核剔除 {len(verdict.attached)} 块：已挂载到实例）")
    if verdict.unverified:
        lines.append(f"（复核剔除 {len(verdict.unverified)} 块：挂载关系没查到，"
                     f"未删 —— 稍后重试即可）")
    if failed:
        lines.append(f"\n⚠️ {len(failed)} 块删除失败：")
        for vol, err in failed[:5]:
            lines.append(f"  · {vol.display_name}: {err}")
    lines.append("\nℹ️ 删除是异步的，卷会短暂显示为 TERMINATED 残留，"
                 "但额度是即时回收的。")
    return "\n".join(lines)


async def _do_harden(pending: PendingAction, settings: Settings,
                     registry: ClientRegistry) -> str:
    ensure_writable(settings)
    plan = pending.payload["plan"]
    client = registry.get(pending.account_index)
    return "✅ " + await _in_thread(security_svc.execute_harden, client, plan)


# --------------------------------------------------------------------------
#  新增操作：删账号 / 删卷（2026-09 七大菜单）
# --------------------------------------------------------------------------
async def _do_account_remove(pending: PendingAction, settings: Settings,
                             registry: ClientRegistry) -> str:
    """从 accounts.json 移除一个账号。

    ⚠️ 只动配置清单 —— **不删 OCI 资源、不动私钥文件**。
    热重载由 handler 侧做（需要 context 拿 Application），这里只负责落盘。
    """
    from ..services import accounts_mgmt as acct_svc
    ensure_writable(settings, destructive=True)

    index = pending.payload["index"]
    removed_label = settings.account(index).label if any(
        a.index == index for a in settings.accounts) else f"[{index}]"

    backup = await _in_thread(acct_svc.remove_account_entry,
                              settings.accounts_file, int(index))
    log.info("账号已从配置移除：index=%s（%s）备份=%s", index, removed_label,
             backup.name if backup else "无")

    if backup is None:
        # 备份失败不阻断删除，但**必须**让人看见 —— 静默失败等于没备份
        note = ("⚠️ **备份失败**（`backups/` 不可写？）—— 本次删除不可回滚，"
                "详见 `journalctl -u oracles-bot`。")
    else:
        note = (f"🗄 原文件已备份：`{acct_svc.backup_dir(settings.accounts_file)}/"
                f"{backup.name}`\n"
                f"误删可 `sudo cp` 回去再重启服务。")

    return (f"✅ 已把 `{removed_label}` 从 `accounts.json` 移除。\n\n"
            f"{note}\n\n"
            "⚠️ 本次进程还在用旧配置 —— **重启服务后彻底生效**："
            "`sudo systemctl restart oracles-bot`\n"
            "（OCI 里的实例/卷/桶都没动，私钥文件也保留在磁盘上。）")


async def _do_volume_delete(pending: PendingAction, settings: Settings,
                            registry: ClientRegistry) -> str:
    """删除一块未挂载的块卷。执行前实时复核挂载关系 —— 挂了就直接拒绝。"""
    ensure_writable(settings, destructive=True)

    client = registry.get(pending.account_index)
    vol_id = pending.payload["volume_id"]
    await _in_thread(compute_svc.delete_volume_safe, client, vol_id)
    log.info("卷已删除：%s（account=%s）", vol_id[-24:], pending.account_index)
    return f"✅ 卷 `{vol_id[-24:]}` 已删除。释放的额度会出现在「4. 配额查询」里。"


# --------------------------------------------------------------------------
#  路由
# --------------------------------------------------------------------------
_HANDLERS: dict[str, Callable[[PendingAction, Settings, ClientRegistry],
                              Awaitable[str]]] = {
    "launch": _do_launch,
    "power": _do_power,
    "terminate": _do_terminate,
    "bucket_delete": _do_bucket_delete,
    "s3_create": _do_s3_create,
    "orphan_delete": _do_orphan_delete,
    "harden": _do_harden,
    "account_remove": _do_account_remove,
    "volume_delete": _do_volume_delete,
}


async def execute_pending(pending: PendingAction, settings: Settings,
                          registry: ClientRegistry) -> str:
    """执行一个已确认的操作，返回给人看的文本结果。"""
    handler = _HANDLERS.get(pending.kind)
    if handler is None:
        return f"❌ 未知的操作类型：{pending.kind}"

    who = f"kind={pending.kind} account={pending.account_index} user={pending.user_id}"

    # ⚠️ 措辞刻意区分「收到确认」和「已执行」。
    # 原来这里写的是「执行待确认操作」，然后被写开关拦下时**不打任何日志** ——
    # 于是 journal 里只剩一条读起来像「已经执行了」的记录，实际什么都没发生。
    # 对一个管基础设施的工具来说，「谁在什么时候试图做什么、有没有被拦住」
    # 正是最该留痕的东西；日志读起来像执行过，比没有日志更危险。
    log.info("收到执行确认（尚未执行）：%s", who)

    try:
        result = await handler(pending, settings, registry)
    except NotAllowedError as exc:
        # 被写开关拦下 —— 这是**安全事件**，必须留痕
        log.warning("写操作被拒绝：%s —— %s", who, one_line(exc))
        return str(exc)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成可读消息
        log.exception("执行 %s 失败", pending.kind)
        return f"❌ 执行失败：\n{str(exc)[:800]}"

    # 能走到这里说明真的执行了：7 个 _do_* 都以 ensure_writable 开头，
    # 没抛异常就意味着写开关是开的、动作已经下发。
    log.info("执行完成：%s", who)
    return result
