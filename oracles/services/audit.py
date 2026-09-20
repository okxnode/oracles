"""审计：孤儿卷 + 隐性计费残留。

两类问题都不会报错、但会**持续吃掉额度或直接产生费用**：

  1. **孤儿引导卷** —— 删实例时没勾「永久删除块卷」，卷留下继续占块存储额度。
     常见形态是 100G/150G 的默认时间戳名卷，一块就能吃掉 50~75% 的免费额度。
     表现是：明明 CPU 配额还有剩，开机却报 ``bootVolumeQuota Service limit reached``。
     （注意这个限额名**不在 limits 列表里**，是内部 quota 家族名，别去 limits 里找。）

  2. **隐性计费残留** —— 未绑定的预留公网 IP（**直接计费**）、自定义镜像、
     卷备份、非运行状态的实例。这些在账单出来之前完全没有提示。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import BOOT_VOLUME_GB, LeakItem, OrphanVolume
from ..oci_gateway import AccountClient
from ..utils import humanize_state
from . import compute as compute_svc

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  孤儿卷
# --------------------------------------------------------------------------
@dataclass
class OrphanReport:
    index: int
    label: str
    region: str
    orphans: list[OrphanVolume] = field(default_factory=list)
    attached_count: int = 0
    error: str | None = None
    #: 挂载关系**没查成**的可用域。这些域里的卷一律没被判为孤儿。
    unverified_ads: list[str] = field(default_factory=list)
    #: 因为「挂载关系没查到」而被排除的卷数量。
    unverified_count: int = 0

    @property
    def partial(self) -> bool:
        """扫描是不完整的（有可用域没查成）。"""
        return bool(self.unverified_ads)

    @property
    def wasted_gb(self) -> float:
        return round(sum(o.size_gb for o in self.orphans), 1)

    @property
    def wasted_launchable(self) -> int:
        """这些被浪费的空间还能开几台机器。"""
        return int(self.wasted_gb // BOOT_VOLUME_GB)


def scan_orphans(client: AccountClient) -> OrphanReport:
    """扫描孤儿引导卷/块卷。

    ⚠️ ``boot_volume_attachment`` 和 ``volume_attachment`` 都**必须传 AD**，
       所以要按 AD 遍历。只查一次会漏掉大部分挂载关系，
       把正在使用的卷误判成孤儿 —— 那会导致删掉运行中实例的系统盘。
    """
    acc = client.account
    report = OrphanReport(index=acc.index, label=acc.label, region=acc.region)

    try:
        ads = client.availability_domains()
    except Exception as exc:  # noqa: BLE001
        report.error = f"列可用域失败：{exc}"
        return report

    # 1) 已挂载的卷集合（逐 AD）。
    #    ⚠️ 用 compute.attached_index —— 它会把「没查成的 AD」单独记下来。
    #       原来这里查询失败只 log 一行就跳过，于是那个 AD 的**所有**卷
    #       都被算成孤儿（2026-09-21 实测：假孤儿会出现在清理列表里）。
    index = compute_svc.attached_index(client, ads=ads)
    report.attached_count = index.attached_count
    report.unverified_ads = list(index.unverified_ads)

    # 2) 所有卷，减掉已挂载的 = 孤儿
    for ad in ads:
        verified = ad not in index.unverified_ads
        try:
            boot_volumes = client.call_all(
                client.blockstorage.list_boot_volumes,
                compartment_id=client.compartment_id, availability_domain=ad,
            )
            for v in boot_volumes:
                if v.lifecycle_state in ("TERMINATED", "TERMINATING"):
                    continue
                if not verified:
                    # 挂载关系没查到 → **不能**判成孤儿，只计数
                    report.unverified_count += 1
                    continue
                if v.id in index.boot:
                    continue
                report.orphans.append(OrphanVolume(
                    id=v.id,
                    display_name=v.display_name,
                    size_gb=float(v.size_in_gbs or 0),
                    ad=ad,
                    kind="boot",
                    lifecycle_state=v.lifecycle_state,
                    time_created=str(v.time_created) if getattr(v, "time_created", None) else None,
                ))

            block_volumes = client.call_all(
                client.blockstorage.list_volumes,
                compartment_id=client.compartment_id, availability_domain=ad,
            )
            for v in block_volumes:
                if v.lifecycle_state in ("TERMINATED", "TERMINATING"):
                    continue
                if not verified:
                    report.unverified_count += 1
                    continue
                if v.id in index.block:
                    continue
                report.orphans.append(OrphanVolume(
                    id=v.id,
                    display_name=v.display_name,
                    size_gb=float(v.size_in_gbs or 0),
                    ad=ad,
                    kind="block",
                    lifecycle_state=v.lifecycle_state,
                    time_created=str(v.time_created) if getattr(v, "time_created", None) else None,
                ))
        except Exception as exc:  # noqa: BLE001
            log.info("%s 的 %s 卷列表查询失败：%s", acc.label, ad, exc)

    return report


@dataclass
class DeleteVerdict:
    """删除前复核的结论。

    ``safe`` 才是可以删的。``attached`` / ``unverified`` 都必须留下 ——
    而且**要分开报**：「确认已挂载」和「没查到」是两种完全不同的情况，
    合成一句「已挂载的已剔除」会让用户以为查过了（2026-09-21 实测）。
    """

    safe: list[OrphanVolume] = field(default_factory=list)
    attached: list[OrphanVolume] = field(default_factory=list)
    unverified: list[OrphanVolume] = field(default_factory=list)


def verify_orphans_before_delete(client: AccountClient,
                                 candidates: list[OrphanVolume]) -> DeleteVerdict:
    """删除前**实时复核**：重新拉一次挂载关系，把已经挂上的剔除。

    ⚠️ 这一步不能省。不要信任扫描时的快照 —— 扫描和删除之间可能
       刚好有人把卷挂上去了。删掉运行中实例的引导卷是灾难性的。

    **fail-closed**：挂载关系没查成的可用域，里面的卷一律进 ``unverified``
    而不是 ``safe``。原来这里是 `except: continue` —— 查询失败等于放行，
    正好把这道复核变成「结构上不可能说不」。
    """
    try:
        index = compute_svc.attached_index(client)
    except Exception:  # noqa: BLE001
        # 连可用域都列不出来 → 一台都不删，全部记为「没查成」
        return DeleteVerdict(unverified=list(candidates))

    verdict = DeleteVerdict()
    for vol in candidates:
        if vol.ad in index.unverified_ads:
            verdict.unverified.append(vol)
        elif index.live_instance_of(vol.id):
            verdict.attached.append(vol)
        else:
            verdict.safe.append(vol)

    if verdict.attached or verdict.unverified:
        log.warning("删除前复核：剔除已挂载 %d 块、挂载状态未知 %d 块",
                    len(verdict.attached), len(verdict.unverified))
    return verdict


def delete_volume(client: AccountClient, vol: OrphanVolume) -> None:
    """删除一块卷。调用方必须先跑 verify_orphans_before_delete。"""
    if vol.kind == "boot":
        client.call(client.blockstorage.delete_boot_volume, vol.id)
    else:
        client.call(client.blockstorage.delete_volume, vol.id)


def render_orphans(reports: list[OrphanReport]) -> str:
    total = sum(len(r.orphans) for r in reports)
    wasted = sum(r.wasted_gb for r in reports)
    partial = [r for r in reports if r.partial and not r.error]
    lines = [f"🧹 孤儿卷扫描：{len(reports)} 个账号，发现 {total} 块，"
             f"浪费 {wasted:.1f} GB（约 {int(wasted // BOOT_VOLUME_GB)} 台机器的空间）"]
    for r in reports:
        if r.error:
            lines.append(f"  ❌ {r.label}: {r.error}")
            continue
        if r.orphans:
            # 把「正挂着的卷数」也报出来 —— 它是唯一能让用户判断
            # 「这份孤儿列表看着合理吗」的对照量（原来算了从不显示）。
            attached = f"，另有 {r.attached_count} 块正挂在实例上" if r.attached_count else ""
            lines.append(f"  【{r.label}】{r.region} —— {len(r.orphans)} 块，"
                         f"{r.wasted_gb:.1f} GB{attached}")
            for v in r.orphans[:10]:
                created = (v.time_created or "")[:10]
                lines.append(f"    · {v.display_name} | {v.size_gb:.0f} GB | "
                             f"{v.ad.split(':')[-1]} | {created}")
            if len(r.orphans) > 10:
                lines.append(f"    … 另有 {len(r.orphans) - 10} 块")
    for r in partial:
        # ⚠️ 必须单独说 —— 不说的后果是：查询抖了一下，
        #    用户就以为「已经扫干净了」，而那个域的卷根本没被检查。
        lines.append(
            f"  ⚠️ 【{r.label}】{len(r.unverified_ads)} 个可用域的挂载关系**没查到**"
            f"（{', '.join(a.split(':')[-1] for a in r.unverified_ads)}）——"
            f"该域 {r.unverified_count} 块卷**没有**参与孤儿判定，请稍后重扫。")
    if total == 0 and not partial:
        lines.append("  ✅ 没有孤儿卷，块存储额度没有被浪费。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  隐性计费残留
# --------------------------------------------------------------------------
def audit_leaks(client: AccountClient) -> list[LeakItem]:
    """扫描会悄悄花钱的残留。"""
    acc = client.account
    items: list[LeakItem] = []

    # --- 1) 未绑定的预留公网 IP（唯一直接计费的项）---
    try:
        ips = client.call_all(
            client.network.list_public_ips,
            compartment_id=client.compartment_id,
            scope="REGION",
        )
        for ip in ips:
            if ip.lifetime != "RESERVED":
                continue
            if getattr(ip, "assigned_entity_id", None):
                continue
            created = str(ip.time_created)[:10] if getattr(ip, "time_created", None) else "?"
            items.append(LeakItem(
                category="未绑定的预留公网 IP",
                name=ip.display_name or ip.ip_address,
                detail=f"{ip.ip_address}（{created} 创建）—— 预留但未绑定，**直接计费**",
                severity="critical",
                resource_id=ip.id,
                delete_hint=f"释放：network.delete_public_ip('{ip.id}')",
            ))
    except Exception as exc:  # noqa: BLE001
        log.info("%s 公网 IP 查询失败：%s", acc.label, exc)

    # --- 2) 非运行状态实例（不耗 CPU 但占引导卷额度）---
    try:
        instances = client.call_all(client.compute.list_instances,
                                    compartment_id=client.compartment_id)
        for inst in instances:
            if inst.lifecycle_state in ("STOPPED", "STOPPING"):
                items.append(LeakItem(
                    category="已停止但仍占额度",
                    name=inst.display_name,
                    detail=f"状态 {humanize_state(inst.lifecycle_state)}，"
                           f"引导卷仍占块存储额度",
                    severity="warn",
                    resource_id=inst.id,
                ))
    except Exception as exc:  # noqa: BLE001
        log.info("%s 实例列表查询失败：%s", acc.label, exc)

    # --- 3) 自定义镜像（占对象存储额度）---
    try:
        images = client.call_all(client.compute.list_images,
                                 compartment_id=client.compartment_id)
        for img in images:
            if getattr(img, "lifecycle_state", "") != "AVAILABLE":
                continue
            # Oracle 官方镜像的 operating_system 是 Ubuntu/Oracle Linux 等；
            # 自定义镜像通常是 "Custom" 或空。用这个判定最稳。
            os_name = (getattr(img, "operating_system", None) or "").strip()
            if os_name and os_name.lower() != "custom":
                continue
            items.append(LeakItem(
                category="自定义镜像",
                name=img.display_name,
                detail="自定义镜像占用对象存储额度",
                severity="warn",
                resource_id=img.id,
            ))
    except Exception as exc:  # noqa: BLE001
        log.info("%s 镜像列表查询失败：%s", acc.label, exc)

    # --- 4) 卷备份（独立计费）---
    try:
        backups = client.call_all(client.blockstorage.list_boot_volume_backups,
                                  compartment_id=client.compartment_id)
        for b in backups:
            if b.lifecycle_state == "AVAILABLE":
                items.append(LeakItem(
                    category="引导卷备份",
                    name=b.display_name,
                    detail="卷备份独立计费",
                    severity="warn",
                    resource_id=b.id,
                ))
    except Exception as exc:  # noqa: BLE001
        log.info("%s 卷备份查询失败：%s", acc.label, exc)

    # --- 5) 实例配置残留（无费用但会堆积）---
    try:
        configs = client.call_all(
            client.compute_management.list_instance_configurations,
            compartment_id=client.compartment_id,
        )
        for c in configs:
            items.append(LeakItem(
                category="实例配置残留",
                name=c.display_name,
                detail="无状态、不产生费用，但会持续堆积",
                severity="info",
                resource_id=c.id,
            ))
    except Exception:  # noqa: BLE001
        pass

    return items


def render_leaks(results: dict[str, list[LeakItem] | None]) -> str:
    """渲染计费残留审计。

    ⚠️ ``None`` 表示**该账号扫描失败**，和 ``[]``（扫描成功、确实没有残留）
    是两件事。以前失败被吞成空列表，渲染出来和"这个账号很干净"一模一样 ——
    而这里漏报的可能是**正在持续扣费**的未绑定公网 IP。
    """
    failed = [k for k, v in results.items() if v is None]
    ok = {k: v for k, v in results.items() if v is not None}
    critical = sum(1 for items in ok.values() for i in items if i.severity == "critical")
    lines = [f"💸 计费残留审计：{len(ok)} 个账号，"
             f"{sum(len(v) for v in ok.values())} 项"
             + (f"，其中 **{critical} 项正在直接计费**" if critical else "")]
    if failed:
        lines.append(f"⚠️ 另有 {len(failed)} 个账号**审计失败，结果未知**"
                     f"（不代表干净，需重跑）：{'、'.join(failed)}")
    for label, items in ok.items():
        if not items:
            continue
        lines.append(f"\n【{label}】")
        for i in items:
            icon = {"critical": "🔴", "warn": "🟠", "info": "⚪"}.get(i.severity, "·")
            lines.append(f"  {icon} {i.category}：{i.name}\n     {i.detail}")
    if not any(ok.values()):
        if failed:
            lines.append(f"  ✅ 在成功审计的 {len(ok)} 个账号里没有发现残留项"
                         f"（但 {len(failed)} 个账号结果未知，结论不完整）。")
        else:
            lines.append("  ✅ 没有发现残留项。")
    return "\n".join(lines)
