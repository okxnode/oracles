"""跨模块共享的数据模型与常量。

这些常量都是从实际踩坑中固化下来的，改动前请先读 docs/06-踩坑清单.md。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
#  常量
# --------------------------------------------------------------------------

#: Always Free 的 AMD 微型实例规格
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

#: Always Free 的 ARM 实例规格（额度是**全区共享** 4 OCPU / 24 GB，别按 AD 乘）
A1_FLEX_SHAPE = "VM.Standard.A1.Flex"

#: A1.Flex 单核内存上限（GB）。总额度 4 核 / 24 GB。
A1_MAX_OCPUS = 4
A1_MEM_PER_CORE_GB = (0.5, 8.0)   # 每核可选内存范围

#: 一台 E2.1.Micro 的引导卷默认大小（GB）。**容量公式里的分母**。
#:
#: 🔴 2026-09-20 实测：**OCI 现在拒绝小于 50 GB 的引导卷**。
#: 原来是 47（历史上一直是这个值，存量实例也确实是 47 GB），但新开机时：
#:
#:     InvalidParameter (HTTP 400)
#:     Requested volume size 47GB is not in the allowed range.
#:     Boot volume should be greater than or equal to 50GB
#:     and less than or equal to 32,768GB.
#:
#: 后果是**默认开机路径（不显式传磁盘大小）100% 失败** ——
#: 向导里选了 50/100/200 的反而没事，只有「默认」这一条路死掉。
#: 所以这个数既要用对，也要跟着 API 的约束走：**取 50**。
BOOT_VOLUME_GB = 50

#: OCI 对引导卷的硬性下限（GB）。低于它必被拒。
BOOT_VOLUME_MIN_GB = 50

#: 免费对象存储额度：20 GiB
FREE_OBJECT_STORAGE_BYTES = 20 * 1024 ** 3

# ---- 限额名（⚠️ 写错会直接报 InvalidParameter）----
LIMIT_E2_CORES = "standard-e2-micro-core-count"   # 不是 standard-e2-micro
LIMIT_A1_CORES = "standard-a1-core-count"
LIMIT_FREE_STORAGE = "total-free-storage-gb-regional"  # 区域级，查询**不需要** AD
LIMIT_BUCKET_COUNT = "bucket-count"
LIMIT_STORAGE_BYTES = "storage-bytes"

# ---- 需要 availability_domain 参数的限额 ----
LIMIT_NEEDS_AD = {LIMIT_E2_CORES, LIMIT_A1_CORES}

#: 销毁实例时，引导卷的默认行为。SDK/CLI 默认**删除**，控制台默认**保留**。
#: 我们显式声明，避免依赖默认值。
DEFAULT_PRESERVE_BOOT_VOLUME = False


# --------------------------------------------------------------------------
#  数据模型
# --------------------------------------------------------------------------
@dataclass
class AdCapacity:
    """单个可用域(AD)的补机余量。"""

    ad: str
    e2_available: int | None = None      # 该 AD 还剩几个微核心（= 可开几台）
    e2_used: int | None = None
    a1_available: int | None = None
    storage_available_gb: float | None = None
    error: str | None = None

    @property
    def launchable(self) -> int:
        """该 AD 实际还能开几台 **E2.1.Micro**。

        公式：``min(CPU 余量, floor(块存储余量 / BOOT_VOLUME_GB))``
        两个约束都要满足，取小值。块存储余量是**区域级**的，
        所以每个 AD 都要跟它取一次 min。

        ⚠️ 这个属性只认 E2。要按别的规格算，用 :meth:`launchable_for`。
        """
        return self.launchable_for(E2_MICRO_SHAPE, 1)

    def launchable_for(self, shape: str, cores: int = 1) -> int:
        """该 AD 按**指定规格**还能开几台。

        🔴 为什么需要这个方法（2026-09-20 实测撞出来的）：

        免费额度里 **E2 和 A1 是两套完全独立的限额**
        （``standard-e2-micro-core-count`` / ``standard-a1-core-count``）。
        账号 15 的实际情况是 **E2 已 2/2 用满、A1 还剩 2 核**。

        而挑 AD 的代码原来只有「按 E2 算」这一个口径，于是
        ``plan_launch`` 直接抛「没有任何可用域还有额度」——
        把 A1 的余量当成不存在，向导里的「ARM A1.Flex」选项
        **在任何 E2 开满的账号上都永远开不出机器**。

        ``cores``：这台机器要吃几个 OCPU（E2.1.Micro 固定 1，
        A1.Flex 由用户选 1~4）。A1 要按台数除一下，
        否则会把「剩 2 核」当成「能开 2 台 4 核机器」。
        """
        if "A1" in shape.upper():
            if self.a1_available is None:
                return 0
            by_cpu = max(0, int(self.a1_available) // max(1, int(cores)))
        else:
            if self.e2_available is None:
                return 0
            by_cpu = max(0, int(self.e2_available))
        if self.storage_available_gb is None:
            return by_cpu
        # 块存储约束是估算（每台按默认引导卷大小算），只用于提前给警告；
        # 真超了 OCI 会用 bootVolumeQuota 拒绝，不会静默成功。
        by_disk = int(self.storage_available_gb // BOOT_VOLUME_GB)
        return max(0, min(by_cpu, by_disk))


@dataclass
class AccountCapacity:
    """一个账号的容量全景。"""

    index: int
    label: str
    region: str
    tenancy: str = ""
    ads: list[AdCapacity] = field(default_factory=list)
    storage_used_gb: float | None = None
    storage_total_gb: float | None = None
    storage_available_gb: float | None = None
    namespace: str | None = None
    bucket_count_used: int | None = None
    bucket_count_total: int | None = None
    object_storage_used_gb: float | None = None
    error: str | None = None

    @property
    def total_launchable(self) -> int:
        """账号可开台数 = Σ 各 AD 可开台数。

        ⚠️ **必须逐 AD 求和**。免费额度绑死在单个 AD 上（比如某账号只有
           AD-3 有 2 核，另外两个 AD 是 0），只看总数或只看第一个 AD 都会算错。
        """
        return sum(ad.launchable for ad in self.ads)

    @property
    def best_ad(self) -> str | None:
        """可开台数最多的 AD（**按 E2 口径**）。用于「自动挑一个能开的 AD」。"""
        return self.best_ad_for(E2_MICRO_SHAPE, 1)

    def best_ad_for(self, shape: str, cores: int = 1) -> str | None:
        """可开台数最多的 AD，按**指定规格**算。

        E2 与 A1 是两套独立限额，所以「哪个 AD 能开」是**跟规格绑定的**：
        同一个账号完全可能「E2 一个 AD 都开不了、A1 在 AD-1 还能开 2 台」。
        """
        candidates = [ad for ad in self.ads if ad.launchable_for(shape, cores) > 0]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.launchable_for(shape, cores)).ad


@dataclass
class InstanceView:
    """实例的展示视图（已把 SDK 的原始结构压平成需要的字段）。"""

    id: str
    display_name: str
    shape: str
    lifecycle_state: str
    ad: str
    region: str
    time_created: str | None = None
    public_ip: str | None = None
    private_ip: str | None = None
    pool_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self.lifecycle_state == "RUNNING"

    @property
    def in_pool(self) -> bool:
        """是否由实例池管理。

        ⚠️ 池成员**不能直接删** —— 删掉后池会在几十秒内再开一台一模一样的。
           要减机必须先改池的 size 或 detach。
        """
        return bool(self.pool_id)


@dataclass
class BucketView:
    """存储桶展示视图。"""

    name: str
    namespace: str
    created: str | None = None
    storage_tier: str | None = None
    versioning: str | None = None
    object_count: int | None = None
    size_bytes: int | None = None
    #: 桶上挂着的预认证请求(PAR)数量 —— 不为 0 时删桶会被 409 拒绝
    par_count: int | None = None

    @property
    def size_gib(self) -> float:
        return round((self.size_bytes or 0) / 1024 ** 3, 3)


@dataclass
class VolumeView:
    """块卷展示视图（引导卷 / 数据卷统一）。"""

    id: str
    display_name: str
    size_gb: float
    ad: str
    kind: str = "block"          # boot | block
    lifecycle_state: str = ""
    attached_instance_id: str | None = None
    #: 这个 AD 的**挂载关系查到了吗**？
    #: ``False`` = 查询失败、挂载状态未知 —— 必须按「可能挂着」处理。
    attachment_verified: bool = True
    time_created: str | None = None

    @property
    def is_attached(self) -> bool:
        """是否正被实例占着。

        ⚠️ 判据只有 ``attached_instance_id`` —— 调用方**只**在挂载关系为
        ``ATTACHING`` / ``ATTACHED`` / ``DETACHING`` 时才填它。

        这里原来还 AND 了一个 ``self.lifecycle_state == "ATTACHED"``，
        而那个条件**永远不可能成立**：``lifecycle_state`` 取的是**卷自己**的状态，
        OCI 的 ``Volume`` / ``BootVolume`` 合法值是
        ``PROVISIONING / RESTORING / AVAILABLE / TERMINATING / TERMINATED / FAULTY``
        —— **里面没有 ``ATTACHED``**（那是 ``VolumeAttachment`` 的状态，
        见 SDK 模型文档 ``oci/core/models/volume.py`` 与 ``volume_attachment.py``）。
        后果：``is_attached`` 恒为 False，已挂载的卷在列表里显示「未挂载」，
        菜单还照样给出「🗑 删除卷」按钮（2026-09-21 实测）。
        """
        return bool(self.attached_instance_id)

    @property
    def can_delete(self) -> bool:
        """能不能允许删这块卷。

        **只有「确认它没挂在任何实例上」才允许。** 挂载状态没查到时不放行 ——
        「不知道」必须按「有风险」处理，否则一次查询抖动就能删掉运行中实例的系统盘。
        """
        return self.attachment_verified and not self.is_attached

    @property
    def attach_icon(self) -> str:
        """列表里用的状态图标。"""
        if not self.attachment_verified:
            return "❓"
        return "🟢" if self.is_attached else "⚪"

    @property
    def attach_state_label(self) -> str:
        """给人看的一句话挂载状态。"""
        if not self.attachment_verified:
            return "❓ 挂载状态未查到"
        return "🟢 已挂载" if self.is_attached else "⚪ 未挂载"


@dataclass
class OrphanVolume:
    """孤儿卷：没挂在任何实例上的引导卷/块卷，白占块存储额度。"""

    id: str
    display_name: str
    size_gb: float
    ad: str
    kind: str = "boot"          # boot | block
    lifecycle_state: str = ""
    time_created: str | None = None


@dataclass
class LeakItem:
    """隐性计费残留项。"""

    category: str
    name: str
    detail: str
    severity: str = "warn"      # warn | critical
    resource_id: str | None = None
    delete_hint: str | None = None


@dataclass
class SshExposure:
    """22 端口暴露面。"""

    vcn_id: str
    vcn_name: str
    source: str                 # 0.0.0.0/0 或 ::/0
    kind: str                   # security_list | nsg
    resource_name: str
    covers_port_22: bool
    is_ipv6: bool = False


@dataclass
class PlaintextSecretFinding:
    """实例配置的 user_data 里出现明文密码。"""

    config_id: str
    config_name: str
    patterns: list[str] = field(default_factory=list)
    snippet: str = ""
