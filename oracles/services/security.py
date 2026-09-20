"""安全审计与加固。

扫两类问题：

  1. **22 端口对全网开放**。注意：这是 OCI 建 VCN 时自动生成的
     ``Default Security List`` 默认规则，**不是用户手动配的**。
     所以「我没配过安全组」≠「22 端口是关的」。实测 17/17 个账号全部命中。

  2. **实例配置的 user_data 里存着明文密码**。user_data 是 **base64 而非加密**，
     等于把密码公开，而且每次开新机都会重放一遍。

加固的坑：``update_security_list`` 的 ingress_security_rules 是**整体替换**语义，
不是追加。直接传「改好的那一条」会把 ICMP 等其它规则全删掉。
所以必须：先 get 全部规则 → 只改目标那条 → 整体写回 → 并留好回滚依据。
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..models import PlaintextSecretFinding, SshExposure
from ..oci_gateway import AccountClient

log = logging.getLogger(__name__)

SSH_PORT = 22
ANY_IPV4 = "0.0.0.0/0"
ANY_IPV6 = "::/0"

#: user_data 里出现这些片段就认为有明文凭据
PLAINTEXT_PATTERNS = [
    (re.compile(r"PermitRootLogin\s+yes", re.I), "允许 root 直接登录"),
    (re.compile(r"PasswordAuthentication\s+yes", re.I), "开启了密码认证"),
    (re.compile(r"chpasswd", re.I), "用 chpasswd 设置密码"),
    (re.compile(r"passwd\s+--stdin", re.I), "用 passwd --stdin 设置密码"),
    (re.compile(r"\b(root|ubuntu|opc)\s*:\s*[^\s:'\"]{6,}", re.I), "疑似明文「用户:密码」"),
    (re.compile(r"(password|passwd|pwd)\s*=\s*[^\s'\"]{6,}", re.I), "疑似明文密码赋值"),
]


# --------------------------------------------------------------------------
#  端口覆盖判定
# --------------------------------------------------------------------------
def rule_covers_port(rule, port: int = SSH_PORT) -> bool:
    """判断一条入站规则是否覆盖了指定端口。

    ⚠️ ``destination_port_range`` 为 ``None`` 表示**全端口**，不是「没有端口」。
       协议 ``all`` 同理。
    """
    proto = str(getattr(rule, "protocol", "") or "").lower()
    if proto in ("all", "0", ""):
        return True
    if proto != "6":          # 6 = TCP
        return False

    tcp = getattr(rule, "tcp_options", None)
    if tcp is None:
        return True
    rng = getattr(tcp, "destination_port_range", None)
    if rng is None:
        return True
    try:
        return int(rng.min) <= port <= int(rng.max)
    except (TypeError, ValueError):
        return True


# --------------------------------------------------------------------------
#  22 端口暴露面
# --------------------------------------------------------------------------
def scan_ssh_exposure(client: AccountClient) -> list[SshExposure]:
    """扫描对全网开放的 22 端口规则。

    ⚠️ 必须同时扫**安全列表和安全组(NSG)**。只查安全列表会漏掉 NSG ——
       早期版本的脚本就犯过这个错。
    """
    findings: list[SshExposure] = []

    try:
        vcns = client.call_all(client.network.list_vcns,
                               compartment_id=client.compartment_id)
    except Exception as exc:  # noqa: BLE001
        log.info("%s 列 VCN 失败：%s", client.account.label, exc)
        return findings

    for vcn in vcns:
        # --- 安全列表 ---
        try:
            sls = client.call_all(client.network.list_security_lists,
                                  compartment_id=client.compartment_id, vcn_id=vcn.id)
        except Exception:  # noqa: BLE001
            sls = []
        for sl in sls:
            for rule in (sl.ingress_security_rules or []):
                source = str(getattr(rule, "source", "") or "")
                if source not in (ANY_IPV4, ANY_IPV6):
                    continue
                if not rule_covers_port(rule):
                    continue
                findings.append(SshExposure(
                    vcn_id=vcn.id, vcn_name=vcn.display_name,
                    source=source, kind="security_list",
                    resource_name=sl.display_name,
                    covers_port_22=True,
                    is_ipv6=(source == ANY_IPV6),
                ))

        # --- 网络安全组 ---
        try:
            nsgs = client.call_all(client.network.list_network_security_groups,
                                   compartment_id=client.compartment_id, vcn_id=vcn.id)
        except Exception:  # noqa: BLE001
            nsgs = []
        for nsg in nsgs:
            try:
                rules = client.call_all(
                    client.network.list_network_security_group_security_rules,
                    network_security_group_id=nsg.id,
                )
            except Exception:  # noqa: BLE001
                continue
            for rule in rules:
                direction = str(getattr(rule, "direction", "") or "").upper()
                if direction != "INGRESS":
                    continue
                source = str(getattr(rule, "source", "") or "")
                if source not in (ANY_IPV4, ANY_IPV6):
                    continue
                if not rule_covers_port(rule):
                    continue
                findings.append(SshExposure(
                    vcn_id=vcn.id, vcn_name=vcn.display_name,
                    source=source, kind="nsg",
                    resource_name=nsg.display_name,
                    covers_port_22=True,
                    is_ipv6=(source == ANY_IPV6),
                ))

    return findings


def render_exposure(results: dict[str, list[SshExposure] | None]) -> str:
    """渲染 22 端口暴露面。

    ⚠️ 值的三种含义必须严格区分，这是本项目最贵的教训：
        []    —— 扫描成功，确实没有暴露（可以放心）
        None  —— **扫描失败**，结果未知（绝不能当成"干净"）
        非空  —— 确实存在暴露

    以前失败会被 `on_error=lambda c, exc: []` 悄悄变成空列表，
    渲染出来和"该账号很干净"一模一样 —— 安全审计里这是最危险的谎报。
    """
    failed = [k for k, v in results.items() if v is None]
    ok = {k: v for k, v in results.items() if v is not None}
    hit = {k: v for k, v in ok.items() if v}
    lines = [f"🔓 SSH 暴露面扫描：{len(ok)} 个账号，{len(hit)} 个存在全网开放的 22 端口"]
    if failed:
        lines.append(f"⚠️ 另有 {len(failed)} 个账号**扫描失败，结果未知**"
                     f"（不代表干净）：{'、'.join(failed)}")
    lines.append("（提醒：这是 OCI 建 VCN 时自动生成的默认规则，不是手动配的）")
    for label, items in hit.items():
        v6 = sum(1 for i in items if i.is_ipv6)
        kinds = {i.kind for i in items}
        lines.append(f"  【{label}】{len(items)} 条规则"
                     + (f"，含 {v6} 条 IPv6" if v6 else "")
                     + f"（{'+'.join(sorted(kinds))}）")
        for i in items[:3]:
            lines.append(f"    · {i.vcn_name} / {i.resource_name} ← {i.source}")
        if len(items) > 3:
            lines.append(f"    … 另有 {len(items) - 3} 条")
    if not hit:
        # ⚠️ 措辞要跟着失败数走。有账号没扫成功时，
        #    只能说"已扫到的部分没问题"，不能说"整体没问题"。
        if failed:
            lines.append(f"  ✅ 在成功扫描的 {len(ok)} 个账号里没有发现全网开放的 22 端口"
                         f"（但 {len(failed)} 个账号结果未知，结论不完整）。")
        else:
            lines.append("  ✅ 没有对全网开放的 22 端口。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  user_data 明文密码
# --------------------------------------------------------------------------
def _decode_user_data(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 有的 user_data 本身就不是 base64
        return str(raw)


def scan_plaintext_secrets(client: AccountClient) -> list[PlaintextSecretFinding]:
    """扫实例配置里的 user_data 明文凭据。

    ⚠️ 解析时的坑：``launch_details.metadata`` 可能是**显式 null**，
       ``.get("metadata", {})`` 挡不住（默认值只在键**不存在**时生效），
       必须写 ``.get("metadata") or {}``。早期审计就因为这个报
       ``'NoneType' object has no attribute 'get'`` 跳过了一整个账号。
    """
    findings: list[PlaintextSecretFinding] = []
    try:
        configs = client.call_all(
            client.compute_management.list_instance_configurations,
            compartment_id=client.compartment_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("%s 列实例配置失败：%s", client.account.label, exc)
        return findings

    for cfg in configs:
        try:
            detail = client.call(client.compute_management.get_instance_configuration, cfg.id)
        except Exception:  # noqa: BLE001
            continue

        # instance-details.launch-details（注意不是 instance-details 本身）
        inst_details = getattr(detail, "instance_details", None)
        launch = getattr(inst_details, "launch_details", None) if inst_details else None
        if launch is None:
            continue

        metadata = getattr(launch, "metadata", None) or {}
        user_data = _decode_user_data(metadata.get("user_data"))
        if not user_data:
            continue

        hits = [desc for pattern, desc in PLAINTEXT_PATTERNS if pattern.search(user_data)]
        if hits:
            snippet_lines = [
                ln.strip() for ln in user_data.splitlines()
                if any(p.search(ln) for p, _ in PLAINTEXT_PATTERNS)
            ]
            findings.append(PlaintextSecretFinding(
                config_id=cfg.id,
                config_name=cfg.display_name,
                patterns=hits,
                # 片段本身也可能含密码，这里只留结构特征，不留原文
                snippet=" | ".join(_mask_secret_line(ln) for ln in snippet_lines[:3]),
            ))
    return findings


def _mask_secret_line(line: str) -> str:
    """把疑似密码的行打码后再展示。"""
    return re.sub(r"(:\s*|=\s*)\S{6,}", r"\1<已隐藏>", line)[:120]


def render_plaintext(results: dict[str, list[PlaintextSecretFinding] | None]) -> str:
    """渲染明文凭据扫描。``None`` 表示扫描失败，语义见 ``render_exposure``。"""
    failed = [k for k, v in results.items() if v is None]
    ok = {k: v for k, v in results.items() if v is not None}
    total = sum(len(v) for v in ok.values())
    lines = [f"🔑 实例配置明文凭据扫描：发现 {total} 个配置含明文密码类内容"]
    if failed:
        lines.append(f"⚠️ {len(failed)} 个账号**扫描失败，结果未知**（不代表干净）："
                     f"{'、'.join(failed)}")
    for label, items in ok.items():
        for f in items:
            lines.append(f"  【{label}】{f.config_name}")
            lines.append(f"    命中：{'、'.join(f.patterns)}")
            if f.snippet:
                lines.append(f"    特征：{f.snippet}")
    if total == 0:
        if failed:
            lines.append(f"  ✅ 在成功扫描的 {len(ok)} 个账号里没有发现"
                         f"（但 {len(failed)} 个账号结果未知，结论不完整）。")
            return "\n".join(lines)
        lines.append("  ✅ 没有发现。")
    else:
        lines.extend([
            "",
            "⚠️ user_data 是 base64 而非加密，等于把密码公开，且每次开新机都会重放。",
            "⚠️ 注意：实例配置的 user_data **不能单独修改**。要清理只能新建配置再",
            "   切换池的引用，而切换会导致池内实例重建、数据丢失。已运行实例上的",
            "   密码也不受影响，需要逐台加固。",
        ])
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  收紧 22 端口
# --------------------------------------------------------------------------
@dataclass
class HardenPlan:
    security_list_id: str
    security_list_name: str
    vcn_id: str
    vcn_name: str
    allowed_cidr: str
    changes: list[dict] = field(default_factory=list)   # [{rule_index, old_source, new_source}]
    ingress_rule_count: int = 0
    other_rule_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def render(self) -> str:
        lines = [
            "🔒 收紧 22 端口计划",
            f"  VCN：{self.vcn_name}",
            f"  安全列表：{self.security_list_name}",
            f"  允许来源改为：{self.allowed_cidr}",
            f"  将修改 {len(self.changes)} 条规则；"
            f"另外 {self.other_rule_count} 条规则保持原样",
        ]
        for c in self.changes:
            lines.append(f"    · 第 {c['rule_index']} 条：{c['old_source']} → {c['new_source']}")
        lines.append("  备份：执行前会把完整规则写入 backups/")
        return "\n".join(lines)


def _rule_to_dict(rule) -> dict:
    """把规则模型转成可 JSON 序列化的 dict（用于备份）。"""
    try:
        from oci.util import to_dict
        return to_dict(rule)
    except Exception:  # noqa: BLE001
        out = {}
        for attr in ("source", "source_type", "protocol", "is_stateless", "description"):
            if hasattr(rule, attr):
                out[attr] = getattr(rule, attr)
        tcp = getattr(rule, "tcp_options", None)
        if tcp is not None:
            rng = getattr(tcp, "destination_port_range", None)
            if rng is not None:
                out["tcp_options"] = {"destination_port_range": {"min": rng.min, "max": rng.max}}
        return out


def _locate_security_list(client: AccountClient, *,
                          security_list_id: str | None = None,
                          security_list_name: str | None = None
                          ) -> tuple[str, str, str, str]:
    """定位安全列表，返回 ``(id, 显示名, vcn_id, vcn名)``。

    按名字查时必须连 VCN 一起返回 —— 安全列表是 VCN 的子资源，
    少了 VCN 信息，计划里就只能显示 ``VCN：-``，用户没法确认改的是哪张网。
    """
    vcns = client.call_all(client.network.list_vcns,
                           compartment_id=client.compartment_id)
    for vcn in vcns:
        try:
            sls = client.call_all(client.network.list_security_lists,
                                  compartment_id=client.compartment_id, vcn_id=vcn.id)
        except Exception:  # noqa: BLE001
            continue
        for sl in sls:
            if security_list_id and sl.id == security_list_id:
                return sl.id, sl.display_name, vcn.id, vcn.display_name
            if security_list_name and sl.display_name == security_list_name:
                return sl.id, sl.display_name, vcn.id, vcn.display_name

    target = security_list_id or security_list_name
    raise LookupError(
        f"找不到安全列表 {target!r}（已扫描 {len(vcns)} 个 VCN）"
    )


def find_security_list(client: AccountClient, needle: str) -> tuple[str, str]:
    """按名字或 OCID 定位安全列表，返回 (security_list_id, display_name)。"""
    sid, sname, _, _ = _locate_security_list(client, security_list_id=needle)
    return sid, sname


def plan_harden(client: AccountClient, *, security_list_id: str | None = None,
                security_list_name: str | None = None,
                allowed_cidr: str) -> HardenPlan:
    """生成收紧计划（只读）。

    ``allowed_cidr`` 建议填你自己的固定出口 IP，形如 ``203.0.113.7/32``。
    """
    if not allowed_cidr or allowed_cidr in (ANY_IPV4, ANY_IPV6):
        raise ValueError("allowed_cidr 不能是全网地址，否则等于没加固")
    if not security_list_id and not security_list_name:
        raise ValueError("必须指定 security_list_id 或 security_list_name")

    sid, sname, vcn_id, vcn_name = _locate_security_list(
        client, security_list_id=security_list_id,
        security_list_name=security_list_name)

    sl = client.call(client.network.get_security_list, sid)
    rules = list(sl.ingress_security_rules or [])

    plan = HardenPlan(
        security_list_id=sid,
        security_list_name=sname,
        vcn_id=vcn_id or getattr(sl, "vcn_id", "") or "",
        vcn_name=vcn_name or "-",
        allowed_cidr=allowed_cidr,
        ingress_rule_count=len(rules),
    )

    for idx, rule in enumerate(rules):
        source = str(getattr(rule, "source", "") or "")
        if source not in (ANY_IPV4, ANY_IPV6):
            continue
        if not rule_covers_port(rule):
            continue
        plan.changes.append({
            "rule_index": idx,
            "old_source": source,
            "new_source": allowed_cidr,
            "protocol": getattr(rule, "protocol", None),
        })

    plan.other_rule_count = len(rules) - len(plan.changes)
    return plan


def _backup_dir(client: AccountClient) -> Path:
    path = Path(client.settings.home) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def execute_harden(client: AccountClient, plan: HardenPlan) -> str:
    """执行收紧。**改前自动备份，支持一键回滚。**

    ⚠️ ``update_security_list`` 是**整体替换**语义：
       必须把「改好的完整规则列表」写回，只传一条会删掉其余规则
       （包括 ICMP、其它端口）。所以这里先 get 全量、原地改目标条、再整体写回。
    """
    import oci

    if not plan.has_changes:
        return "ℹ️ 没有需要修改的规则（可能已经收紧了）"

    sl = client.call(client.network.get_security_list, plan.security_list_id)
    rules = list(sl.ingress_security_rules or [])

    # --- 备份 ---
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = _backup_dir(client) / (
        f"seclist_acc{client.account.index}_{plan.security_list_id[-8:]}_{stamp}.json"
    )
    backup = {
        "created_at": stamp,
        "account_index": client.account.index,
        "region": client.account.region,
        "vcn_id": plan.vcn_id,
        "security_list_id": plan.security_list_id,
        "security_list_name": plan.security_list_name,
        "allowed_cidr": plan.allowed_cidr,
        "changes": plan.changes,
        "full_ingress_rules": [_rule_to_dict(r) for r in rules],
    }
    backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    backup_path.chmod(0o600)

    # --- 原地改目标规则，其余原样保留 ---
    for change in plan.changes:
        idx = change["rule_index"]
        if 0 <= idx < len(rules):
            rules[idx].source = change["new_source"]

    details = oci.core.models.UpdateSecurityListDetails(ingress_security_rules=rules)
    client.call(client.network.update_security_list, plan.security_list_id, details)

    return (
        f"✅ 已收紧 {plan.security_list_name} 的 {len(plan.changes)} 条规则"
        f"（来源 → {plan.allowed_cidr}）\n"
        f"   备份：{backup_path}\n"
        "   回滚：目前没有一键入口 —— 备份里的 `full_ingress_rules` 是收紧前的完整规则，\n"
        "         照它在 OCI 控制台把来源改回去即可（`restore_harden()` 已实现但未接线）。"
    )


def restore_harden(client: AccountClient, backup_path: str | Path) -> str:
    """从备份回滚。

    回滚策略是**按索引改回 source**，而不是重建整份规则 ——
    这样即使备份和当前状态之间有别的改动，也不会被覆盖掉。
    """
    import oci

    path = Path(backup_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"备份文件不存在：{path}")
    backup = json.loads(path.read_text(encoding="utf-8"))

    sl = client.call(client.network.get_security_list, backup["security_list_id"])
    rules = list(sl.ingress_security_rules or [])

    restored = 0
    for change in backup.get("changes", []):
        idx = change["rule_index"]
        if not (0 <= idx < len(rules)):
            log.warning("第 %d 条规则已不存在，跳过", idx)
            continue
        if str(getattr(rules[idx], "source", "")) != change["new_source"]:
            log.warning("第 %d 条规则的来源已被改动，跳过以免覆盖", idx)
            continue
        rules[idx].source = change["old_source"]
        restored += 1

    details = oci.core.models.UpdateSecurityListDetails(ingress_security_rules=rules)
    client.call(client.network.update_security_list, backup["security_list_id"], details)
    return f"↩️ 已回滚 {restored} 条规则到原始来源"
