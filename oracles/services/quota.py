"""配额与容量。

本模块承载了整个项目**最核心的一条业务知识**：

    免费额度是按「账号 + 可用域(AD)」绑定的，不是整个区域。

同一个区域内，账号 A 的配额和账号 B 的配额互不相干；
同一个账号内，AD-1 有 2 核、AD-2 是 0 核也是常态。
所以任何「取第一个 AD 就开机」的实现都是错的 —— 它会永远挑到 0 配额的 AD，
报错还会被误判成「主机容量不足」。

正确流程：**逐 AD 查余量 → 逐 AD 算可开台数 → 挑有额度的 AD 再开机**。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ..errors import one_line
from ..models import (
    A1_FLEX_SHAPE,
    BOOT_VOLUME_GB,
    E2_MICRO_SHAPE,
    LIMIT_A1_CORES,
    LIMIT_BUCKET_COUNT,
    LIMIT_E2_CORES,
    LIMIT_FREE_STORAGE,
    LIMIT_STORAGE_BYTES,
    AccountCapacity,
    AdCapacity,
)
from ..oci_gateway import AccountClient

log = logging.getLogger(__name__)


@dataclass
class LimitValue:
    """限额查询结果。``available is None`` 表示查询失败或该限额不存在。"""

    used: int | float | None = None
    available: int | float | None = None
    total: int | float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.available is not None


def get_limit(client: AccountClient, service: str, limit_name: str,
              *, availability_domain: str | None = None,
              compartment_id: str | None = None) -> LimitValue:
    """查一个限额的已用/剩余。

    ⚠️ 三个必踩的坑：
      1. 限额名是 ``standard-e2-micro-core-count``，**不是** ``standard-e2-micro``；
      2. ``resource_availability`` 对 AD 级限额**必须**传 availability_domain，
         否则报 InvalidParameter；
      3. AD 名带随机前缀且大小写不统一，必须用 API 返回的原值，不能自己拼。
    """
    try:
        data = client.call(
            client.limits.get_resource_availability,
            service_name=service,
            limit_name=limit_name,
            compartment_id=compartment_id or client.tenancy_id,
            availability_domain=availability_domain,
        )
    except Exception as exc:  # noqa: BLE001 —— 单点失败不拖垮整账号
        return LimitValue(error=one_line(exc, 200))

    return LimitValue(
        used=getattr(data, "used", None),
        available=getattr(data, "available", None),
    )


# --------------------------------------------------------------------------
#  块存储
# --------------------------------------------------------------------------
def block_storage_limit(client: AccountClient) -> LimitValue:
    """块存储免费额度（区域级，**不需要** AD 参数）。

    限额名 ``total-free-storage-gb-regional``。用它而不是 AD 级的
    ``total-free-storage-gb``，一次调用就能拿到全区总量。

    ⚠️ 这个额度是「引导卷 + 数据卷」共用的。实例报
       ``bootVolumeQuota Service limit reached`` 时，
       问题**不在 CPU 配额**，而是这里的块存储池满了 —— 常见原因是孤儿引导卷。
    """
    return get_limit(client, "block-storage", LIMIT_FREE_STORAGE)


# --------------------------------------------------------------------------
#  单个账号的容量全景
# --------------------------------------------------------------------------
def account_capacity(client: AccountClient, *, include_object_storage: bool = True) -> AccountCapacity:
    """查一个账号的完整容量。任何单点失败都记录到 ``error`` 字段而不抛出。"""
    acc = client.account
    cap = AccountCapacity(index=acc.index, label=acc.label, region=acc.region)

    try:
        cap.tenancy = client.tenancy_id
    except Exception as exc:  # noqa: BLE001
        cap.error = f"认证/配置失败：{exc}"
        return cap

    # --- 1) 可用域列表 ---
    try:
        ads = client.availability_domains()
    except Exception as exc:  # noqa: BLE001
        cap.error = f"列可用域失败：{exc}"
        return cap

    # --- 2) 块存储（区域级，先查一次，供每个 AD 复用）---
    storage = block_storage_limit(client)
    cap.storage_used_gb = _to_gb(storage.used)
    cap.storage_available_gb = _to_gb(storage.available)
    if storage.used is not None and storage.available is not None:
        cap.storage_total_gb = round(float(storage.used) + float(storage.available), 1)

    # --- 3) 逐 AD 查 E2 / A1 余量 ---
    for ad in ads:
        item = AdCapacity(
            ad=ad,
            storage_available_gb=cap.storage_available_gb,
        )
        e2 = get_limit(client, "compute", LIMIT_E2_CORES, availability_domain=ad)
        item.e2_available = _to_int(e2.available)
        item.e2_used = _to_int(e2.used)
        if not e2.ok and e2.error:
            item.error = e2.error

        a1 = get_limit(client, "compute", LIMIT_A1_CORES, availability_domain=ad)
        item.a1_available = _to_int(a1.available)

        cap.ads.append(item)

    # --- 4) 对象存储 ---
    if include_object_storage:
        try:
            cap.namespace = client.namespace()
            buckets = get_limit(client, "object-storage", LIMIT_BUCKET_COUNT)
            cap.bucket_count_used = _to_int(buckets.used)
            cap.bucket_count_total = (
                _to_int(buckets.available + (buckets.used or 0))
                if buckets.ok and buckets.used is not None else None
            )
            size = get_limit(client, "object-storage", LIMIT_STORAGE_BYTES)
            if size.used is not None:
                cap.object_storage_used_gb = _to_gb(size.used)
        except Exception as exc:  # noqa: BLE001
            # 对象存储未开通时 get_namespace 会失败，不影响实例容量结论
            log.info("%s 对象存储查询失败（可能未开通）：%s", acc.label, exc)

    return cap


def _to_int(value: int | float | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_gb(value: int | float | None) -> float | None:
    """限额单位是 GB，这里只做归一化。"""
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def storage_bytes_to_gib(value: int | float | None) -> float | None:
    """对象存储限额的单位是**字节**，需要单独换算。"""
    if value is None:
        return None
    return round(float(value) / 1024 ** 3, 3)


# --------------------------------------------------------------------------
#  补机决策
# --------------------------------------------------------------------------
def pick_availability_domain(cap: AccountCapacity, *,
                             shape: str = E2_MICRO_SHAPE,
                             cores: int = 1) -> str | None:
    """从容量报告里挑一个「按**这个规格**真的能开机」的 AD。

    这是替代「取第一个 AD」的正确做法。挑可开台数最多的那个。

    🔴 **必须带上规格**（2026-09-20 实测撞出来的 bug）：

    E2 和 A1 是两套独立限额，所以「哪个 AD 能开」是跟规格绑定的。
    账号 15 的实际状态是 **E2 已 2/2 用满、A1 还剩 2 核**——
    用 E2 的口径去挑 AD，会返回 None，`plan_launch` 随即抛
    「没有任何可用域还有额度」，向导里的 A1.Flex 选项永远开不出机器。
    """
    if "A1" in shape.upper():
        return cap.best_ad_for(shape, cores)
    return cap.best_ad


def explain_capacity(cap: AccountCapacity) -> str:
    """把容量报告转成一段人话，用于 Bot 消息和 CLI 输出。"""
    if cap.error:
        return f"❌ {cap.label} ({cap.region}) 查询失败：{cap.error}"

    lines = [f"【{cap.label}】{cap.region}"]
    if cap.storage_used_gb is not None:
        lines.append(
            f"  块存储：已用 {cap.storage_used_gb:.1f} GB / "
            f"可用 {cap.storage_available_gb:.1f} GB"
            + (f" / 共 {cap.storage_total_gb:.1f} GB" if cap.storage_total_gb else "")
        )

    for ad in cap.ads:
        short = ad.ad.split(":")[-1]
        if ad.e2_available is None:
            lines.append(f"  {short}: 查询失败 {ad.error or ''}")
            continue
        mark = "✅" if ad.launchable > 0 else "·"
        detail = f"E2 余 {ad.e2_available} 核（已用 {ad.e2_used}）"
        if ad.storage_available_gb is not None:
            by_disk = int(ad.storage_available_gb // BOOT_VOLUME_GB)
            if by_disk < (ad.e2_available or 0):
                detail += f"，但块存储只够 {by_disk} 台"
        lines.append(f"  {mark} {short}: {detail} → 可开 {ad.launchable} 台")

    lines.append(f"  ⇒ 本账号可开合计：{cap.total_launchable} 台")
    if cap.namespace:
        lines.append(f"  对象存储：命名空间 {cap.namespace[:8]}…"
                     f"，桶 {cap.bucket_count_used}/{cap.bucket_count_total}"
                     f"，已用 {cap.object_storage_used_gb or 0:.3f} GiB")
    return "\n".join(lines)


def summarize(caps: list[AccountCapacity]) -> str:
    """多账号汇总：还差多少台、额度在谁手上。"""
    ok = [c for c in caps if not c.error]
    failed = [c for c in caps if c.error]
    total = sum(c.total_launchable for c in ok)

    lines = [f"📊 共 {len(caps)} 个账号，合计可开 {total} 台 E2.1.Micro"]
    rich = [c for c in ok if c.total_launchable > 0]
    if rich:
        lines.append("有余量的账号：")
        for c in sorted(rich, key=lambda x: -x.total_launchable):
            ad_names = "、".join(a.ad.split(":")[-1] for a in c.ads if a.launchable > 0)
            lines.append(f"  · {c.label}（{c.region}）→ {c.total_launchable} 台，在 {ad_names}")
    else:
        lines.append("没有账号还有余量。")
    if failed:
        lines.append(f"⚠️ {len(failed)} 个账号查询失败："
                     + "、".join(c.label for c in failed))
    return "\n".join(lines)


def estimate_launchable(cores_available: int | None, storage_available_gb: float | None) -> int:
    """容量公式（单 AD）。两个约束取小值。

    ``可开台数 = min(CPU 余量, floor(块存储余量 / BOOT_VOLUME_GB))``

    ⚠️ 分母是**常量** ``BOOT_VOLUME_GB``（现在是 50），别在文档里写死数字 ——
       写死的那个数字不会跟着常量变（这个项目已经在 3 处踩过，见踩坑 #48）。
    """
    if cores_available is None:
        return 0
    if storage_available_gb is None:
        return max(0, int(cores_available))
    return max(0, min(int(cores_available), int(math.floor(storage_available_gb / BOOT_VOLUME_GB))))


__all__ = [
    "A1_FLEX_SHAPE",
    "BOOT_VOLUME_GB",
    "E2_MICRO_SHAPE",
    "LimitValue",
    "account_capacity",
    "block_storage_limit",
    "estimate_launchable",
    "explain_capacity",
    "get_limit",
    "pick_availability_domain",
    "storage_bytes_to_gib",
    "summarize",
]
