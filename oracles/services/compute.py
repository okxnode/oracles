"""实例管理：列表 / 开机 / 关机 / 重启 / 创建 / 销毁。

设计上刻意拆成**两段式**：
    plan_xxx()     →  只读，产出一个「计划对象」，不产生任何副作用
    execute_xxx()  →  拿着计划去真正执行

这样 DRY-RUN 不是「在写操作里加个 if」，而是**结构上就不可能误执行**：
调用方必须先拿到计划、展示给人看、人确认之后才调 execute。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ..errors import one_line
from ..models import (
    BOOT_VOLUME_GB,
    BOOT_VOLUME_MIN_GB,
    DEFAULT_PRESERVE_BOOT_VOLUME,
    E2_MICRO_SHAPE,
    InstanceView,
)
from ..oci_gateway import AccountClient
from ..sshkeys import fingerprint_of
from ..utils import humanize_state
from . import quota as quota_svc

log = logging.getLogger(__name__)

#: 动作名 → OCI API 的 action 常量
POWER_ACTIONS: dict[str, str] = {
    "start": "START",
    "stop": "STOP",          # 正常关机（ACPI）
    "softstop": "SOFTSTOP",  # 软关机（先发 ACPI，超时再强制）
    "reboot": "SOFTRESET",   # 软重启
    "reset": "RESET",        # 硬重启（相当于拔电源）
}

#: 会中断服务的动作，Bot 里需要二次确认
INTERRUPTIVE_ACTIONS = {"stop", "softstop", "reboot", "reset"}


# --------------------------------------------------------------------------
#  查询
# --------------------------------------------------------------------------
def _pool_membership(client: AccountClient) -> dict[str, str]:
    """返回 {实例 OCID: 实例池 OCID}。

    ⚠️ 为什么必须做这件事：实例池管理的实例**不能直接删** ——
       删掉后池会在几十秒内再开一台一模一样的，等于白删。
       要减机必须改池的 size 或先 detach。

    ⚠️ 另一个坑：``instance-pool-instance`` **没有 list 子命令**，
       必须用 ``list_instance_pool_instances``。返回项里的 ``id``
       就是实例 OCID（不是 ``instance_id`` 字段）。
    """
    mapping: dict[str, str] = {}
    try:
        pools = client.call_all(
            client.compute_management.list_instance_pools,
            compartment_id=client.compartment_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 没权限时不应阻断列表
        log.info("列实例池失败（忽略）：%s", exc)
        return mapping

    for pool in pools:
        try:
            members = client.call_all(
                client.compute_management.list_instance_pool_instances,
                compartment_id=client.compartment_id,
                instance_pool_id=pool.id,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("列池 %s 成员失败：%s", pool.display_name, exc)
            continue
        for m in members:
            # SDK 里字段是 id；某些版本可能是 instance_id，两个都兜
            iid = getattr(m, "id", None) or getattr(m, "instance_id", None)
            if iid:
                mapping[iid] = pool.id
    return mapping


def public_ip_of(client: AccountClient, instance_id: str) -> tuple[str | None, str | None]:
    """取实例主 VNIC 的 (公网 IP, 内网 IP)。"""
    try:
        attachments = client.call_all(
            client.compute.list_vnic_attachments,
            compartment_id=client.compartment_id,
            instance_id=instance_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("列 VNIC 挂载失败 %s：%s", instance_id, exc)
        return None, None

    for att in attachments:
        if att.lifecycle_state != "ATTACHED":
            continue
        try:
            vnic = client.call(client.network.get_vnic, att.vnic_id)
        except Exception:  # noqa: BLE001
            continue
        if vnic.public_ip:
            return vnic.public_ip, vnic.private_ip
    # 没有公网 IP 的实例，也把内网 IP 带出来
    for att in attachments:
        if att.lifecycle_state != "ATTACHED":
            continue
        try:
            vnic = client.call(client.network.get_vnic, att.vnic_id)
            return None, vnic.private_ip
        except Exception:  # noqa: BLE001
            continue
    return None, None


def _to_view(client: AccountClient, inst, pools: dict[str, str],
             *, with_ips: bool) -> InstanceView:
    pub = priv = None
    if with_ips:
        pub, priv = public_ip_of(client, inst.id)
    return InstanceView(
        id=inst.id,
        display_name=inst.display_name,
        shape=inst.shape,
        lifecycle_state=inst.lifecycle_state,
        ad=inst.availability_domain or "-",
        region=client.account.region,
        time_created=str(inst.time_created) if inst.time_created else None,
        public_ip=pub,
        private_ip=priv,
        pool_id=pools.get(inst.id),
    )


def list_instances(client: AccountClient, *, with_ips: bool = True,
                   include_terminated: bool = False) -> list[InstanceView]:
    """列实例。

    ⚠️ ``list_instances`` 会返回已销毁的残留记录（lifecycle_state=TERMINATED），
       默认过滤掉，否则列表里全是幽灵。
    """
    raw = client.call_all(client.compute.list_instances,
                          compartment_id=client.compartment_id)
    if not include_terminated:
        raw = [i for i in raw if i.lifecycle_state != "TERMINATED"]
    pools = _pool_membership(client)
    return [_to_view(client, i, pools, with_ips=with_ips) for i in raw]


def get_instance(client: AccountClient, instance_id: str) -> InstanceView:
    inst = client.call(client.compute.get_instance, instance_id)
    pools = _pool_membership(client)
    return _to_view(client, inst, pools, with_ips=True)


def resolve_instance(client: AccountClient, needle: str) -> InstanceView:
    """按 OCID 或名称片段定位一台实例。

    名字做**唯一前缀匹配**：匹配到多台就报错让人补全，绝不猜。
    猜错的代价是删错机器。
    """
    needle = needle.strip()
    if needle.startswith("ocid1."):
        return get_instance(client, needle)

    views = list_instances(client, with_ips=False)
    exact = [v for v in views if v.display_name == needle]
    if len(exact) == 1:
        return get_instance(client, exact[0].id)

    partial = [v for v in views if needle.lower() in v.display_name.lower()]
    if len(partial) == 1:
        return get_instance(client, partial[0].id)
    if not partial:
        raise LookupError(f"{client.account.label} 里找不到匹配 {needle!r} 的实例")
    names = "、".join(v.display_name for v in partial[:5])
    raise LookupError(
        f"{needle!r} 匹配到 {len(partial)} 台实例，请用更精确的名字或 OCID：{names}"
    )


# --------------------------------------------------------------------------
#  开机（创建实例）
# --------------------------------------------------------------------------
@dataclass
class LaunchPlan:
    """创建实例的执行计划。只读阶段产出，供人确认。"""

    account_index: int
    account_label: str
    region: str
    display_name: str
    availability_domain: str
    shape: str
    boot_volume_gb: int
    image_id: str
    image_name: str
    subnet_id: str
    subnet_name: str
    ssh_public_key: str | None = None
    #: 注入公钥的 SHA256 指纹（格式与 ``ssh-keygen -lf`` 一致）。
    #: 光看「已注入」核对不出到底注的是哪一把 —— 多账号时尤其需要，
    #: 因为每把 ed25519 公钥的前几十个字符长得一模一样。
    ssh_key_fingerprint: str | None = None
    warnings: list[str] = field(default_factory=list)
    # ---- 向导扩展项（默认全空，等价于旧的 E2.1.Micro 快捷开机）----
    shape_config: dict[str, Any] | None = None   # A1.Flex: {"ocpus": int, "memory_gb": int}
    user_data_b64: str | None = None             # cloud-init（用户名/密码等），已 base64
    assign_ipv6: bool = False                    # 子网通告了 IPv6 CIDR 时才生效
    ipv6_note: str | None = None                 # IPv6 不可用时的解释，渲染进计划

    def render(self) -> str:
        lines = [
            f"📋 创建实例计划（账号 {self.account_label} / {self.region}）",
            f"  名称：{self.display_name}",
            f"  规格：{self.shape}"
            + (f"（{self.shape_config['ocpus']} OCPU / "
               f"{self.shape_config['memory_gb']} GB）" if self.shape_config else ""),
            f"  可用域：{self.availability_domain.split(':')[-1]}",
            f"  镜像：{self.image_name}",
            f"  子网：{self.subnet_name}（IPv4 公网 IP）",
        ]
        if self.assign_ipv6:
            lines.append("  IPv6：将从 VCN 通告的 CIDR 中分配")
        elif self.ipv6_note:
            lines.append(f"  ⚠️ IPv6：{self.ipv6_note}")
        lines += [
            f"  引导卷：{self.boot_volume_gb} GB",
            f"  cloud-init：{'已配置（含用户名/密码）' if self.user_data_b64 else '无'}",
        ]
        if self.ssh_public_key:
            fp = f"　{self.ssh_key_fingerprint}" if self.ssh_key_fingerprint else ""
            lines.append(f"  SSH 公钥：已注入{fp}")
        else:
            lines.append("  SSH 公钥：⚠️ 未注入（将无法直接 SSH 登录）")
        lines.extend(f"  ⚠️ {w}" for w in self.warnings)
        return "\n".join(lines)


def _resolve_ssh_public_key(client: AccountClient) -> str | None:
    """按优先级找 SSH 公钥：环境变量 > 账号条目 > ~/.ssh/id_ed25519.pub。

    ⚠️ 我们**只**注入公钥，绝不把密码写进 user_data。
       历史教训：有实例配置的 user_data 里存着明文 root 密码，
       而 user_data 是 base64 而非加密，等于把密码公开，
       且每次开新机都会重放一遍。
    """
    inline = os.environ.get("ORACLES_SSH_PUBLIC_KEY")
    if inline and inline.strip().startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-")):
        return inline.strip()

    candidates = []
    if client.account.default_ssh_key:
        candidates.append(Path(client.account.default_ssh_key).expanduser())
    candidates.extend([
        Path.home() / ".ssh" / "id_ed25519.pub",
        Path.home() / ".ssh" / "id_rsa.pub",
    ])
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-")):
                    return text
        except OSError:
            continue
    return None


def _fingerprint_or_none(public_key: str | None) -> str | None:
    """算公钥指纹；公钥格式不标准时返回 None，**不抛异常**。

    ⚠️ 这里刻意不阻断：``_resolve_ssh_public_key`` 可能返回账号配置里
       一把手写的、格式不规范的公钥。那种机器**本来能开**（OCI 自己会校验），
       因为显示不出指纹就整个开不了机，是把「展示问题」升级成「功能故障」。
       指纹只是给人核对用的，拿不到就不显示。
    """
    if not public_key:
        return None
    try:
        return fingerprint_of(public_key)
    except ValueError as exc:
        log.warning("公钥指纹算不出来（不影响开机）：%s", exc)
        return None


def instance_authorized_keys(client: AccountClient, instance_id: str) -> list[str]:
    """读实例 metadata 里的 ``ssh_authorized_keys``，返回公钥行列表。只读。

    用途：下载登录密钥前**核对**这把钥匙是不是这台机器的 ——
    比一句「可能不适用」的免责声明有用得多。

    ⚠️ 没注入公钥时返回 ``[]``（确实没有），**不是** ``None``。
       取不到 metadata 会抛异常 —— 那才是「结果未知」，
       两者绝不能混（见踩坑清单里 ``[]`` vs ``None`` 那条）。
    """
    inst = client.call(client.compute.get_instance, instance_id)
    raw = (inst.metadata or {}).get("ssh_authorized_keys") or ""
    return [line.strip() for line in str(raw).splitlines() if line.strip()]


def newest_image(client: AccountClient, *, shape: str = E2_MICRO_SHAPE,
                 operating_system: str | None = None) -> tuple[str, str]:
    """取该规格可用的最新官方镜像。

    ⚠️ **必须带 shape 参数**，否则会列出该 OS 下所有镜像，
       包含跟这个规格不兼容的，挑错了 launch 会失败。
    """
    kwargs: dict[str, object] = {
        "compartment_id": client.compartment_id,
        "shape": shape,
        "sort_by": "TIMECREATED",
        "sort_order": "DESC",
    }
    if operating_system:
        kwargs["operating_system"] = operating_system

    images = client.call_all(client.compute.list_images, **kwargs)
    for img in images:
        if img.lifecycle_state == "AVAILABLE":
            return img.id, img.display_name
    raise LookupError(
        f"没找到适用于 {shape} 的镜像"
        + (f"（OS={operating_system}）" if operating_system else "")
    )


def default_subnet(client: AccountClient) -> tuple[str, str]:
    """取默认子网（免费账号通常只有一个）。"""
    subnets = client.call_all(client.network.list_subnets,
                              compartment_id=client.compartment_id)
    if not subnets:
        raise LookupError(
            f"{client.account.label} 没有任何子网。"
            "需要先在 OCI 控制台建一个 VCN（含子网）才能开机。"
        )
    s = subnets[0]
    return s.id, s.display_name


def plan_launch(client: AccountClient, *, name: str | None = None,
                ad: str | None = None, shape: str | None = None,
                image_os: str | None = None,
                boot_volume_gb: int = BOOT_VOLUME_GB,
                subnet_id: str | None = None,
                ocpus: int | None = None, memory_gb: int | None = None,
                ssh_public_key: str | None = None) -> LaunchPlan:
    """生成创建实例的计划（只读，不创建任何东西）。

    ``ad`` 不指定时会**自动挑一个有额度的 AD** —— 这正是避开
    「免费额度绑单 AD」这个坑的关键步骤。

    ``ocpus`` / ``memory_gb``：仅 A1.Flex 需要（E2.1.Micro 是固定规格）。

    ``ssh_public_key``：显式指定要注入的公钥（向导里选「让 Bot 生成一对新的」
    时传进来，见 ``oracles/sshkeys.py``）。**默认 None 时行为完全不变** ——
    仍然走 ``_resolve_ssh_public_key`` 那条「环境变量 > 账号条目 > ~/.ssh」的链。

    ⚠️ 本函数**只读**：即使要生成密钥对，也必须在调用方先做好
       （``sshkeys.ensure``），把**公钥文本**传进来。
       在这里生成+落盘会破坏「plan 阶段不产生任何副作用」这条契约 ——
       而这条契约是 DRY-RUN 能被信任的前提。
    """
    acc = client.account
    shape = shape or acc.default_shape or E2_MICRO_SHAPE
    image_os = image_os or acc.default_image_os or None
    warnings: list[str] = []

    # --- 引导卷下限预检 ---
    # 🔴 OCI 现在拒绝小于 50 GB 的引导卷。不拦的话会在 launch 那一步拿到
    #    「InvalidParameter: Requested volume size 47GB is not in the allowed range」——
    #    报错里全是 OCI 的话术，看不出是「我们默认值过时了」。
    #    2026-09-20 实测：存量实例是 47 GB，但新开机 47 会被拒。
    if boot_volume_gb < BOOT_VOLUME_MIN_GB:
        raise ValueError(
            f"引导卷 {boot_volume_gb} GB 低于 OCI 的下限 {BOOT_VOLUME_MIN_GB} GB。\n"
            f"  OCI 会直接拒：Requested volume size {boot_volume_gb}GB is not in "
            f"the allowed range.\n"
            f"  请用 ≥ {BOOT_VOLUME_MIN_GB} GB（向导里的 50/100/200 都是合法的）。"
        )

    # --- 挑 AD ---
    # ⚠️ 挑 AD **必须带上规格**：E2 和 A1 是两套独立的免费限额，
    #    同一个账号完全可能「E2 已开满、A1 还剩 2 核」。
    #    2026-09-20 真机验证撞出来的：账号 15 正是这个状态，而按 E2 口径挑 AD
    #    会直接返回 None → 抛「没有任何可用域还有额度」→
    #    向导里的「ARM A1.Flex」选项在这个账号上**永远开不出机器**。
    needed_cores = (ocpus or 2) if "A1" in shape.upper() else 1
    cap = quota_svc.account_capacity(client, include_object_storage=False)

    if ad:
        chosen_ad = ad
        match = next((a for a in cap.ads if a.ad == ad), None)
        if match is not None and match.launchable_for(shape, needed_cores) <= 0:
            warnings.append(
                f"指定的 {ad.split(':')[-1]} 按 {shape} 算余量为 0，开机大概率失败。"
                f"该账号还有额度的 AD：{cap.best_ad_for(shape, needed_cores) or '无'}"
            )
    else:
        chosen_ad = quota_svc.pick_availability_domain(
            cap, shape=shape, cores=needed_cores) or ""
        if not chosen_ad:
            detail = "\n".join(
                f"    {a.ad.split(':')[-1]}: E2 余 {a.e2_available}"
                f"、A1 余 {a.a1_available}"
                for a in cap.ads
            ) or "    （未取到 AD 列表）"
            raise LookupError(
                f"{acc.label} 没有任何可用域还有额度，无法开机。\n"
                f"  要开的规格：{shape}（每台 {needed_cores} 核）\n"
                f"  逐 AD 余量：\n{detail}\n"
                f"  块存储可用：{cap.storage_available_gb} GB\n"
                f"  提示：免费额度绑在单个 AD 上，而且 **E2 与 A1 是两套独立限额**"
                f"（E2 满了不代表 A1 也满，反之亦然）。若这个规格全是 0，"
                f"说明该规格已开满 —— 换个规格或换个账号；"
                f"也可能是块存储被孤儿卷占满（用 /audit 查）。"
            )
        warnings.append(
            f"自动挑选了 {shape} 余量最充足的可用域 {chosen_ad.split(':')[-1]}"
        )

    # --- 块存储预检 ---
    if cap.storage_available_gb is not None and cap.storage_available_gb < boot_volume_gb:
        warnings.append(
            f"块存储仅剩 {cap.storage_available_gb:.1f} GB，不足 {boot_volume_gb} GB，"
            f"创建会被 bootVolumeQuota 拒绝。先用 /audit 清理孤儿卷。"
        )

    # --- 镜像 ---
    image_id, image_name = newest_image(client, shape=shape, operating_system=image_os)

    # --- 子网 ---
    if subnet_id:
        subnets = client.call_all(client.network.list_subnets,
                                  compartment_id=client.compartment_id)
        sn = next((s for s in subnets if s.id == subnet_id), None)
        if sn is None:
            raise LookupError(f"子网 {subnet_id} 不在该账号下")
        subnet_id_final, subnet_name = sn.id, sn.display_name
    else:
        subnet_id_final, subnet_name = default_subnet(client)

    # --- 公网 IP 提醒 ---
    warnings.append("将自动分配公网 IP；若安全列表对 0.0.0.0/0 开放 22 端口，"
                    "该机器会立刻暴露在公网扫描之下（/security 可查）")

    # --- 公钥：显式指定优先，否则走原来的解析链 ---
    pubkey = ssh_public_key or _resolve_ssh_public_key(client)

    # --- A1.Flex 规格配置（CPU / 内存）---
    shape_config: dict[str, Any] | None = None
    if "A1" in shape.upper():
        n_ocpus = ocpus or 2
        mem_gb = memory_gb or max(8, n_ocpus)   # A1 每核至少配到可用
        if not (1 <= n_ocpus <= 4):
            raise ValueError(f"A1.Flex 的 OCPU 数必须在 1~4（免费额度上限），收到 {n_ocpus}")
        if not (0.5 * n_ocpus <= mem_gb <= 8.0 * n_ocpus):
            raise ValueError(
                f"内存 {mem_gb} GB 超出 A1.Flex {n_ocpus} OCPU 的合法范围 "
                f"{0.5 * n_ocpus:.1f}~{8.0 * n_ocpus:.0f} GB（每核 0.5~8 GB）"
            )
        shape_config = {"ocpus": n_ocpus, "memory_gb": mem_gb}

    return LaunchPlan(
        account_index=acc.index,
        account_label=acc.label,
        region=acc.region,
        display_name=name or f"oracles-{acc.index}-{int(_now_ts())}",
        availability_domain=chosen_ad,
        shape=shape,
        boot_volume_gb=boot_volume_gb,
        image_id=image_id,
        image_name=image_name,
        subnet_id=subnet_id_final,
        subnet_name=subnet_name,
        ssh_public_key=pubkey,
        ssh_key_fingerprint=_fingerprint_or_none(pubkey),
        warnings=warnings,
        shape_config=shape_config,
    )


def _now_ts() -> float:
    import time
    return time.time()


def _launch_details_kwargs(client: AccountClient, plan: LaunchPlan) -> dict[str, Any]:
    """把 LaunchPlan 翻成 ``LaunchInstanceDetails`` 的 kwargs。

    ⚠️ 单独抽成函数是为了能被**真实 SDK 校验**：
       ``tests/test_launch_details.py`` 会把返回的 dict 直接喂给
       ``oci.core.models.LaunchInstanceDetails(**kwargs)``，
       任何参数名写错都会在那里炸掉，而不是等到真开机才炸。

    🔴 **user_data 必须放进 ``metadata``，它不是顶层参数。**

    2026-09-20 真机验证撞出来的：``LaunchInstanceDetails`` 的 32 个合法参数里
    **没有 ``user_data``**（只有 ``metadata`` / ``extended_metadata``）。
    写成顶层参数会直接抛::

        TypeError: Unrecognized keyword arguments: user_data

    —— 也就是「向导里选用户名+密码」这条路**在 SDK 调用层就崩了**，
    机器根本开不出来。而当时只读代码是看不出来的：
    ``plan.user_data_b64`` 一路从向导 → spec → grab → plan 都接得好好的，
    只有最后这一句 ``LaunchInstanceDetails(**kwargs)`` 是错的。

    OCI 的约定：cloud-init 文本 base64 后放进 ``metadata["user_data"]``，
    和 ``metadata["ssh_authorized_keys"]`` 同一个字典。
    """
    metadata: dict[str, str] = {}
    if plan.ssh_public_key:
        metadata["ssh_authorized_keys"] = plan.ssh_public_key
    # ⚠️ user_data 只放 cloud-init（用户名/密码），不放密钥。
    #    密码是用户在向导里明文输入的，属于知情行为；
    #    但**绝不**把 API 私钥塞进 user_data。
    if plan.user_data_b64:
        metadata["user_data"] = plan.user_data_b64

    vnic_kwargs: dict[str, Any] = {"subnet_id": plan.subnet_id, "assign_public_ip": True}
    if plan.assign_ipv6:
        # ⚠️ 只有子网通告了 IPv6 CIDR 时才有效，否则 OCI 直接报错。
        #   向导里已预检（见 wizard 的 IPv6 步骤），这里是执行路径。
        vnic_kwargs["assign_ipv6_ip"] = True

    kwargs: dict[str, Any] = {
        "availability_domain": plan.availability_domain,
        "compartment_id": client.compartment_id,
        "display_name": plan.display_name,
        "shape": plan.shape,
        "source_details": oci.core.models.InstanceSourceViaImageDetails(
            image_id=plan.image_id,
            boot_volume_size_in_gbs=plan.boot_volume_gb,
        ),
        "create_vnic_details": oci.core.models.CreateVnicDetails(**vnic_kwargs),
    }
    if plan.shape_config:
        kwargs["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(plan.shape_config["ocpus"]),
            memory_in_gbs=float(plan.shape_config["memory_gb"]),
        )
    if metadata:
        kwargs["metadata"] = metadata
    return kwargs


def execute_launch(client: AccountClient, plan: LaunchPlan, *,
                   wait: bool = True) -> InstanceView:
    """按计划创建实例。

    🔴 **「等待变成 RUNNING 超时」不等于「开机失败」。**

    2026-09-20 真机验证撞出来的：``wait_for_state`` 因为一个 SDK 语义 bug
    （传 list 而非 tuple，见 ``oci_gateway.wait_for_state``）**必然超时**，
    而这里原来没包 ``try`` —— 于是**机器开出来了、函数却抛异常**。
    抢机循环（``grab.run_grab_loop``）把异常当成「这次没抢到」，
    下一轮会**再开一台**：E2 只有 1 个名额所以第二次撞配额，
    但 A1 有 2 核，**会实打实地开出重复实例**。

    ``launch_instance`` 一旦返回，实例就已经在 OCI 侧存在了 ——
    这是**唯一的成功判据**。等待只是「让调用方知道它什么时候就绪」，
    属于尽力而为，超时只记一条警告。

    这和 ``execute_power`` / ``execute_terminate`` 的既有约定一致
    （「动作已下发，等待超时不算失败」）。
    """
    details_kwargs = _launch_details_kwargs(client, plan)

    inst = client.call(client.compute.launch_instance,
                       oci.core.models.LaunchInstanceDetails(**details_kwargs))
    log.info("实例已受理：%s (%s)", inst.display_name, inst.id)

    if wait:
        try:
            client.wait_for_state(client.compute, client.compute.get_instance,
                                  inst.id, "RUNNING", max_wait_seconds=600)
        except Exception as exc:  # noqa: BLE001 —— 已受理，等待超时不算失败
            log.warning("等待实例进入 RUNNING 超时（实例已受理，不影响结果）：%s", exc)
    return get_instance(client, inst.id)


# --------------------------------------------------------------------------
#  开关机
# --------------------------------------------------------------------------
def plan_power(client: AccountClient, needle: str, action: str) -> tuple[InstanceView, str]:
    """生成开关机计划，返回 (实例, OCI action 常量)。"""
    if action not in POWER_ACTIONS:
        raise ValueError(f"未知动作 {action!r}，可选：{sorted(POWER_ACTIONS)}")
    view = resolve_instance(client, needle)
    if view.lifecycle_state in ("TERMINATING", "TERMINATED"):
        raise RuntimeError(f"{view.display_name} 正在/已经销毁，无法执行 {action}")
    if action == "start" and view.is_running:
        raise RuntimeError(f"{view.display_name} 已经在运行中")
    if action in INTERRUPTIVE_ACTIONS and view.lifecycle_state != "RUNNING":
        raise RuntimeError(
            f"{view.display_name} 当前状态是 {view.lifecycle_state}，不需要 {action}"
        )
    return view, POWER_ACTIONS[action]


def execute_power(client: AccountClient, instance_id: str, oci_action: str, *,
                  wait: bool = True) -> str:
    """执行开关机动作，返回最终生命周期状态。"""
    resp = client.call(client.compute.instance_action, instance_id, oci_action)
    log.info("实例 %s 动作 %s → %s", instance_id, oci_action, resp.lifecycle_state)
    if wait:
        target = "RUNNING" if oci_action in ("START", "SOFTRESET", "RESET") else "STOPPED"
        try:
            client.wait_for_state(client.compute, client.compute.get_instance,
                                  instance_id, target, max_wait_seconds=420)
        except Exception as exc:  # noqa: BLE001 —— 动作已下发，等待超时不算失败
            log.warning("等待状态超时（动作已下发）：%s", exc)
    try:
        return client.call(client.compute.get_instance, instance_id).lifecycle_state
    except Exception:  # noqa: BLE001
        return resp.lifecycle_state


# --------------------------------------------------------------------------
#  销毁
# --------------------------------------------------------------------------
@dataclass
class TerminatePlan:
    instance: InstanceView
    preserve_boot_volume: bool = DEFAULT_PRESERVE_BOOT_VOLUME
    blocked_reason: str | None = None

    @property
    def can_execute(self) -> bool:
        return self.blocked_reason is None

    def render(self) -> str:
        i = self.instance
        lines = [
            "🗑 销毁实例计划",
            f"  名称：{i.display_name}",
            f"  规格：{i.shape}",
            f"  状态：{humanize_state(i.lifecycle_state)}",
            f"  可用域：{i.ad.split(':')[-1]}",
            f"  公网 IP：{i.public_ip or '无'}",
            f"  引导卷：{'保留（会继续占块存储额度，变成孤儿卷）' if self.preserve_boot_volume else '一并删除'}",
        ]
        if self.blocked_reason:
            lines.append(f"  ⛔ 已阻止：{self.blocked_reason}")
        else:
            lines.append("  ⚠️ 此操作不可逆，实例上的数据全部丢失。")
        return "\n".join(lines)


def plan_terminate(client: AccountClient, needle: str, *,
                   preserve_boot_volume: bool = DEFAULT_PRESERVE_BOOT_VOLUME) -> TerminatePlan:
    """生成销毁计划，并做**池成员拦截**。"""
    view = resolve_instance(client, needle)
    blocked = None
    if view.in_pool:
        blocked = (
            "该实例由实例池管理。直接销毁会白删——池会在几十秒内再开一台一样的。\n"
            "  要减机请改池的 size，或先 detach。"
        )
    return TerminatePlan(instance=view, preserve_boot_volume=preserve_boot_volume,
                         blocked_reason=blocked)


def execute_terminate(client: AccountClient, plan: TerminatePlan, *,
                      wait: bool = True) -> str:
    """执行销毁。

    ⚠️ 引导卷默认行为在**控制台和 SDK/CLI 之间是相反的**：
       控制台默认「保留」（这是孤儿卷的主要来源），
       SDK 的 ``preserve_boot_volume`` 默认 ``False`` 即「删除」。
       我们显式传参，不依赖默认值。
    """
    if not plan.can_execute:
        raise RuntimeError(plan.blocked_reason or "计划不可执行")

    instance_id = plan.instance.id
    client.call(client.compute.terminate_instance, instance_id,
                preserve_boot_volume=plan.preserve_boot_volume)
    log.info("实例 %s 已下发销毁（保留引导卷=%s）", instance_id, plan.preserve_boot_volume)

    if wait:
        try:
            client.wait_for_state(client.compute, client.compute.get_instance,
                                  instance_id, "TERMINATED", max_wait_seconds=420)
        except Exception as exc:  # noqa: BLE001 —— 动作已下发，等待超时不算失败
            log.warning("等待销毁完成超时：%s", exc)
    return "TERMINATED"


# --------------------------------------------------------------------------
#  救援控制台（串口 + VNC）—— SSH 不通时的兜底通道
# --------------------------------------------------------------------------
#: 用哪把公钥开救援控制台。设了就用它 —— 私钥在你自己手里，不经过 Telegram。
CONSOLE_KEY_ENV = "ORACLES_CONSOLE_PUBLIC_KEY"

#: 现场生成密钥时，回给用户的私钥建议存成这个名字
CONSOLE_KEY_FILE = "oracles-rescue.pem"


def _generate_rescue_keypair() -> tuple[str, str]:
    """现场生成一对临时 RSA-2048，返回 ``(OpenSSH 公钥, PEM 私钥)``。

    ⚠️ 必须是 **RSA**。OCI 的 ``create_instance_console_connection`` 只收 RSA，
       实测 ed25519 会被服务端拒（2026-09-20 在 sa-vinhedo-1 实机验证）：
           InvalidParameter: Invalid ssh public key type "ssh-ed25519"
       所以这里不能用项目里那把开机的 ed25519 公钥。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return public, private


def _console_public_key(explicit: str | None) -> tuple[str, str | None]:
    """决定交给 OCI 的公钥，返回 ``(公钥, 私钥或 None)``。

    私钥**只有现场生成时**才有。那种情况下必须把它回给用户 ——
    连接串是一条 SSH 命令，没有配对的私钥就进不去。
    """
    inline = (explicit or os.environ.get(CONSOLE_KEY_ENV) or "").strip()
    if inline:
        if not inline.startswith("ssh-rsa"):
            raise RuntimeError(
                f"救援控制台只接受 RSA 公钥，当前配的是 {inline.split()[0]!r}。"
                f"请把 {CONSOLE_KEY_ENV} 换成 ssh-rsa 开头的公钥；"
                f"不配的话我们会现场生成一把临时 RSA（私钥会显示在 Telegram 里）。"
            )
        return inline, None
    return _generate_rescue_keypair()


def _ssh_with_key(command: str, key_file: str) -> str:
    """把 ``-i <key_file>`` 插进 ssh 命令里（**外层和内层都要**），省得用户自己找位置。

    ⚠️ **只给外层加是不够的。** 连接串的形状是::

        ssh -o ProxyCommand='ssh -W %h:%p -p 443 <console-ocid>@<host>' <instance-ocid>

    **真正跟 OCI 控制台服务做公钥认证的是 `ProxyCommand` 里那一跳（内层）**；
    外层那一跳只是连到实例的伪终端。只给外层加 `-i`，内层就会退回去
    试默认身份（`~/.ssh/id_*`、agent）→ 报

        Permission denied (publickey)

    而私钥指纹跟控制台报告的**完全一致** —— 于是人会去怀疑密钥文件、
    怀疑权限、怀疑 ssh 版本，就是不怀疑「钥匙没递给对的那一跳」。

    另加 `IdentitiesOnly=yes`：不许内层把 agent 里的其它身份也递上去
    （OCI 控制台连接只认这一把，多递反而会被拒）。

    ``key_file`` 含空格会破坏引号 —— 默认值是不带路径的
    ``oracles-rescue.pem``，别改成带空格的路径。
    """
    if not command.startswith("ssh "):
        return command
    opts = f"-i {key_file} -o IdentitiesOnly=yes"
    out = f"ssh {opts} " + command[len("ssh "):]
    # 内层（ProxyCommand 引号里的那条 ssh）也要注入同一把钥匙。
    if "ProxyCommand='ssh " in out:
        out = out.replace("ProxyCommand='ssh ", f"ProxyCommand='ssh {opts} ", 1)
    return out


def _existing_console(client: AccountClient, instance_id: str) -> Any | None:
    """找这个实例已有的、**还没被删掉**的控制台连接（没有就返回 None）。

    ⚠️ ``list_instance_console_connections`` 会把 ``DELETED`` 的记录也列出来
       —— 和实例列表里的 ``TERMINATED`` 幽灵是同一个毛病。
       不过滤掉的话，会把一条早删掉的连接当成「已有连接」，
       于是用户永远拿不到能用的私钥。
    """
    existing = client.call_all(
        client.compute.list_instance_console_connections,
        compartment_id=client.compartment_id,
        instance_id=instance_id,
    )
    alive = [c for c in existing if c.lifecycle_state != "DELETED"]
    return alive[0] if alive else None


@dataclass
class RescueConsole:
    """一台实例的救援入口。

    ⚠️ 2026-09-20 之前这里只有一个 ``connection_string``，渲染时告诉用户
       「装个 VNC 客户端，把连接串粘进去」—— 那是照着想象写的。
       真实返回的是**一条 SSH 命令**（``ssh -o ProxyCommand='ssh -W %h:%p …'``），
       不是 ``vnc://`` 链接，VNC 客户端根本粘不进去。
    """

    instance_id: str
    display_name: str
    connection_string: str                     # 串口：ssh -o ProxyCommand='…' <instance>
    fingerprint: str | None = None             # 主机密钥指纹，首次连接核对用
    vnc_connection_string: str | None = None   # VNC 隧道：ssh … -N -L localhost:5900:…
    private_key_pem: str | None = None         # 仅当现场生成密钥时才有
    replaced_existing: bool = False            # 是否顶掉了之前那条连接

    def render(self) -> str:
        serial = self.connection_string
        vnc = self.vnc_connection_string
        if self.private_key_pem:
            serial = _ssh_with_key(serial, CONSOLE_KEY_FILE)
            if vnc:
                vnc = _ssh_with_key(vnc, CONSOLE_KEY_FILE)

        lines = ["🛟 *救援控制台已开通*", "", f"实例：`{self.display_name}`"]
        if self.private_key_pem:
            lines += [
                "",
                "🔑 *私钥 —— 只显示这一次*：",
                f"```\n{self.private_key_pem}```",
                f"先把它存成 `{CONSOLE_KEY_FILE}`，再 `chmod 600 {CONSOLE_KEY_FILE}`。",
                "（用 `ORACLES_CONSOLE_PUBLIC_KEY` 配一把你自己的 RSA 公钥，"
                "就不会再往这里发私钥。）",
            ]
        lines += [
            "",
            "① *串口控制台* —— SSH 进去，能进维护模式 / 改配置 / 重置密码：",
            f"```\n{serial}\n```",
        ]
        if vnc:
            lines += [
                "",
                "② *VNC 图形界面* —— 先跑这条建隧道，再用 VNC 客户端连 `localhost:5900`：",
                f"```\n{vnc}\n```",
            ]
        if self.fingerprint:
            lines += ["", f"首次连接会问主机密钥，核对指纹：\n`{self.fingerprint}`"]
        if self.replaced_existing:
            lines += [
                "",
                "ℹ️ 这台机器上原来那条控制台连接已被顶掉重建"
                "（旧连接的私钥对不上新连接）。",
            ]
        lines += [
            "",
            "⚠️ 同一实例**只能有一条**控制台连接；会话 **60 分钟**后自动关闭。",
        ]
        return "\n".join(lines)


def open_rescue_console(client: AccountClient, instance_id: str, *,
                        public_key: str | None = None) -> RescueConsole:
    """为一台 RUNNING 的实例开救援控制台（串口 + VNC 隧道）。

    ⚠️ ``publicKey`` 是**必填**字段。2026-09-20 之前这里把它当可选，
       不传就发请求 → OCI 直接 400：
           InvalidParameter: publicKey must not be null
       于是「🚑 救援」按钮点了只回一句报错。现在不传就现场生成一对 RSA。

    ``public_key``：可选。给一把 **RSA** 公钥（私钥你自己留着），
    这样就不会有任何私钥经过 Telegram。

    ⚠️ 免费账号**只能同时开一条** console connection。已经有一条时：
       · 你给了公钥 → 复用那条（幂等）
       · 我们现场生成的 → 那条是用**别的**公钥开的，复用它等于给你一把
         打不开门的钥匙，所以删掉重建。

    ⚠️ **这是一个阻塞调用，最多可能等 120 秒**（等连接从 `CREATING` 变 `ACTIVE`）。
       调用方必须把它放到线程里跑（`asyncio.to_thread`），
       并先回一句「正在开救援控制台…」——否则 Telegram 那边会看起来卡死。
       实测正常路径几秒到几十秒（2026-09-20，sa-vinhedo-1）。
    """
    view = get_instance(client, instance_id)
    if view.lifecycle_state not in ("RUNNING", "PROVISIONING"):
        raise RuntimeError(
            f"{view.display_name} 当前状态 {view.lifecycle_state}，救援控制台只对运行中实例可用"
        )

    pub, private_pem = _console_public_key(public_key)
    details = oci.core.models.CreateInstanceConsoleConnectionDetails(
        instance_id=instance_id, public_key=pub)

    replaced = False
    try:
        conn = client.call(
            client.compute.create_instance_console_connection, details)
    except Exception as exc:  # noqa: BLE001 —— 已存在时按下面的规则处理
        msg = str(exc).lower()
        already = ("already" in msg or "exists" in msg
                   or getattr(exc, "status", None) == 409)
        if not already:
            raise
        existing = _existing_console(client, instance_id)
        if existing is None:
            raise
        if existing.lifecycle_state == "CREATING":
            # 用户连点了两下 —— 上一条还没建好。给一句人话，别把 409 原样丢出去。
            raise RuntimeError(
                f"{view.display_name} 上一条救援控制台还在创建中，"
                "等十几秒再点一次（同一实例只能有一条）。"
            ) from None
        if private_pem is None:
            conn = existing                    # 用户自己的公钥 → 直接复用
        else:
            client.call(client.compute.delete_instance_console_connection,
                        existing.id)
            conn = client.call(
                client.compute.create_instance_console_connection, details)
            replaced = True

    # ⚠️ **必须等 ACTIVE 再返回。**
    #
    # 2026-09-20 现场（`<实例公网IP>`）：`create_instance_console_connection`
    # 返回的是 `CREATING` 状态的连接，**这时候公钥还没在控制台侧生效**。
    # 拿它返回的那串命令立刻去连，得到的是
    # `Permission denied (publickey)` —— 而私钥指纹明明是对的
    # （`ssh-keygen -lf` 与控制台报告的 `SHA256:y9AS…/Ig` 完全一致），
    # 于是排查方向会被彻底带偏（去怀疑密钥文件权限、怀疑 ssh 参数、怀疑代理）。
    # 实测：隔 30 秒再连就通了。
    #
    # 这就是「🚑 救援」按钮**从来没真正跑通过**的根因 —— 用户拿到连接串
    # 立刻粘贴，必然被拒，只会以为「这个功能是坏的」。
    #
    # `FAILED` 也要收进目标状态：只等 `ACTIVE` 的话它会一直轮询到超时，
    # 把「OCI 明确说失败了」变成一句含糊的「等超时」。
    if conn.lifecycle_state != "ACTIVE":
        conn = client.wait_for_state(
            client.compute, client.compute.get_instance_console_connection,
            conn.id, ("ACTIVE", "FAILED"), max_wait_seconds=120)
        if conn.lifecycle_state != "ACTIVE":
            raise RuntimeError(
                f"{view.display_name} 的救援控制台创建失败"
                f"（状态 {conn.lifecycle_state}）—— 稍后重试，"
                "或先用实例操作里的「重启」把实例拉起来再试。"
            )

    return RescueConsole(
        instance_id=instance_id,
        display_name=view.display_name,
        connection_string=conn.connection_string or "",
        fingerprint=conn.fingerprint,
        vnc_connection_string=getattr(conn, "vnc_connection_string", None),
        private_key_pem=private_pem,
        replaced_existing=replaced,
    )


def close_rescue_console(client: AccountClient, instance_id: str) -> None:
    """关闭该实例上所有活跃的 VNC 救援控制台。"""
    existing = client.call_all(
        client.compute.list_instance_console_connections,
        compartment_id=client.compartment_id,
        instance_id=instance_id,
    )
    closed = 0
    for c in existing:
        if c.lifecycle_state == "ACTIVE":
            try:
                client.call(client.compute.delete_instance_console_connection, c.id)
                closed += 1
            except Exception as exc:  # noqa: BLE001
                log.info("关闭 console %s 失败：%s", c.id, exc)
    if not closed:
        raise LookupError(f"{instance_id} 上没有活跃的救援控制台")


# --------------------------------------------------------------------------
#  块卷（硬盘管理）
# --------------------------------------------------------------------------
#: 挂载关系里，哪些状态算「这个卷正被实例占着」。
#:
#: ⚠️ 只认 ``ATTACHED`` 是不够的 —— ``ATTACHING`` 是「正在挂上去」
#: （那台实例多半正在启动），``DETACHING`` 是「还没真正脱离」
#: （卸载失败会退回 ATTACHED）。把它们当成「没挂」，
#: 就会在开机过程中/卸载未完成时把卷判成可删的孤儿。
#:
#: 合法值见 SDK 模型文档 ``oci/core/models/volume_attachment.py``：
#: ``ATTACHING / ATTACHED / DETACHING / DETACHED``。
LIVE_ATTACHMENT_STATES = ("ATTACHING", "ATTACHED", "DETACHING")


@dataclass
class AttachmentIndex:
    """「哪些卷正被实例占着」的索引。

    存在的理由：**区分「确实没挂」和「没查成」**。
    原来各处都把后者当成前者 —— 一次查询抖动（429 / 500 / 权限）
    就会让复核静默放行，删掉运行中实例的系统盘。
    """

    boot: dict[str, str] = field(default_factory=dict)    # vol_id -> instance_id
    block: dict[str, str] = field(default_factory=dict)
    #: 挂载关系**没查成**的可用域。里面的卷状态未知。
    unverified_ads: list[str] = field(default_factory=list)

    def live_instance_of(self, volume_id: str) -> str | None:
        """这个卷被哪个实例占着？引导卷/数据卷都查。没占则 ``None``。"""
        return self.boot.get(volume_id) or self.block.get(volume_id)

    @property
    def complete(self) -> bool:
        """所有可用域的挂载关系都查到了吗？"""
        return not self.unverified_ads

    @property
    def attached_count(self) -> int:
        return len(self.boot) + len(self.block)


def attached_index(client: AccountClient, *, ads: list[str] | None = None) -> AttachmentIndex:
    """拉一份「哪些卷正被实例占着」的索引，**逐 AD 记录查没查成**。

    ``ads`` 不给时自己列可用域（列失败会抛，由调用方决定怎么处理）。

    调用方**必须**把 ``unverified_ads`` 当成「不可删」而不是「没挂」：
    ``complete`` 为 False 时不能给出任何「安全可删」的结论。
    """
    if ads is None:
        ads = client.availability_domains()

    index = AttachmentIndex()
    for ad in ads:
        try:
            for att in client.call_all(
                client.compute.list_boot_volume_attachments,
                compartment_id=client.compartment_id, availability_domain=ad,
            ):
                if att.lifecycle_state in LIVE_ATTACHMENT_STATES:
                    index.boot[att.boot_volume_id] = att.instance_id
            for att in client.call_all(
                client.compute.list_volume_attachments,
                compartment_id=client.compartment_id, availability_domain=ad,
            ):
                if att.lifecycle_state in LIVE_ATTACHMENT_STATES:
                    index.block[att.volume_id] = att.instance_id
        except Exception as exc:  # noqa: BLE001 —— 单个 AD 失败不阻断整体
            index.unverified_ads.append(ad)
            log.warning("可用域 %s 的挂载关系查询失败 —— 该域内的卷状态未知：%s",
                        ad, one_line(exc))
    return index


def list_all_volumes(client: AccountClient) -> list[Any]:
    """列出该账号所有引导卷 + 数据卷（含挂载关系），过滤掉 TERMINATED。

    返回 ``VolumeView`` 列表（见 models.py）。逐 AD 拉取，和 audit.py 同款姿势。

    挂载关系没查成的 AD，其卷会带 ``attachment_verified=False`` ——
    UI 据此**不给出删除入口**（见 ``VolumeView.can_delete``）。
    """
    from ..models import VolumeView  # 延迟导入避免循环依赖

    try:
        ads = client.availability_domains()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"列可用域失败：{exc}") from exc

    index = attached_index(client, ads=ads)

    volumes: list[VolumeView] = []
    for ad in ads:
        # 挂载关系没查成 → 这个域的卷一律标为「状态未知」
        verified = ad not in index.unverified_ads
        for lister, table, kind in (
            (client.blockstorage.list_boot_volumes, index.boot, "boot"),
            (client.blockstorage.list_volumes, index.block, "block"),
        ):
            try:
                for v in client.call_all(
                    lister,
                    compartment_id=client.compartment_id, availability_domain=ad,
                ):
                    if v.lifecycle_state in ("TERMINATED", "TERMINATING"):
                        continue
                    volumes.append(VolumeView(
                        id=v.id, display_name=v.display_name or v.id[:20],
                        size_gb=float(v.size_in_gbs or 0), ad=ad, kind=kind,
                        lifecycle_state=v.lifecycle_state,
                        attached_instance_id=table.get(v.id),
                        attachment_verified=verified,
                        time_created=(str(v.time_created)
                                      if getattr(v, "time_created", None) else None),
                    ))
            except Exception as exc:  # noqa: BLE001
                # 不能静默 —— 少列一块卷会让人以为它已经没了
                log.warning("%s 的 %s 卷列表查询失败（该域卷可能列不全）：%s",
                            ad, kind, one_line(exc))

    volumes.sort(key=lambda v: (v.kind, v.display_name))
    return volumes


def extend_volume(client: AccountClient, volume_id: str, new_size_gb: int) -> float:
    """扩容一块卷（只能变大，不能缩小）。返回新大小。"""
    if new_size_gb < 0 or new_size_gb > 32768:
        raise ValueError(f"卷大小必须在 1~32768 GB，收到 {new_size_gb}")

    # 先查当前大小和类型
    try:
        cur = client.call(client.blockstorage.get_volume, volume_id)
        kind = "block"
    except Exception:  # noqa: BLE001 —— 可能是引导卷
        cur = client.call(client.blockstorage.get_boot_volume, volume_id)
        kind = "boot"

    cur_size = float(cur.size_in_gbs or 0)
    if new_size_gb <= cur_size:
        raise ValueError(f"新大小 {new_size_gb} GB 必须大于当前 {cur_size:.0f} GB（卷只能扩容）")

    if kind == "boot":
        client.call(client.blockstorage.update_boot_volume, volume_id, size_in_gbs=new_size_gb)
    else:
        client.call(client.blockstorage.update_volume, volume_id, size_in_gbs=new_size_gb)
    log.info("卷 %s 扩容到 %d GB", volume_id, new_size_gb)
    return float(new_size_gb)


def delete_volume_safe(client: AccountClient, volume_id: str) -> None:
    """删除一块**确认没被占用**的卷。任何不确定都拒绝 —— 删运行中实例的系统盘是灾难。

    这是 UI 之外的最后一道闸：列表页到点确认之间可能过了几分钟，
    期间卷可能被挂到别的实例上（或者挂载关系查询自己会失败）。

    **fail-closed**：只要挂载关系没查全，一律不删。宁可让用户重试一次，
    也不能靠「查不到就当没挂」赌一块系统盘。
    """
    try:
        index = attached_index(client)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法确认挂载状态（列可用域失败），为安全起见不删：{exc}") from exc

    if not index.complete:
        raise RuntimeError(
            "无法确认挂载状态，为安全起见不删。\n"
            f"这些可用域的挂载关系没查到：{', '.join(index.unverified_ads)}\n"
            "（多半是限流或权限问题，稍后重试即可。）"
        )

    holder = index.live_instance_of(volume_id)
    if holder:
        raise RuntimeError(f"该卷已挂载到实例 {holder}，不能删。先卸载或销毁实例。")

    try:
        client.call(client.blockstorage.delete_volume, volume_id)
    except Exception as exc:  # noqa: BLE001 —— 可能是引导卷
        if getattr(exc, "status", None) in (404, 501):
            client.call(client.blockstorage.delete_boot_volume, volume_id)
        else:
            raise
    log.info("卷 %s 已删除", volume_id)
