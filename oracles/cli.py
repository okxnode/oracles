"""运维 CLI —— 不依赖 Telegram 的命令行入口。

用途有三：
  1. 部署前自检（``doctor``）
  2. 在 VPS 上直接用命令行查东西，不用打开 Telegram
  3. 被 cron / 监控脚本调用（``--json`` 输出）

    python -m oracles.cli doctor
    python -m oracles.cli quota --json /tmp/quota.json
    python -m oracles.cli instances --account 3
    python -m oracles.cli audit --account 11

⚠️ 本 CLI **只读**。所有写操作走 Telegram Bot，因为那边有二次确认流程。
   命令行没有确认环节，一按回车就执行，太容易出事。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .config import Settings, load_settings
from .errors import ConfigError, OraclesError, one_line
from .log import setup_logging
from .oci_gateway import ClientRegistry
from .services import audit as audit_svc
from .services import compute as compute_svc
from .services import quota as quota_svc
from .services import scan_failed
from .services import security as security_svc
from .services import storage as storage_svc
from .utils import parallel_map


def _load() -> tuple[Settings, ClientRegistry]:
    settings = load_settings(require_token=False)
    setup_logging(settings.log_level)
    return settings, ClientRegistry(settings)


def _targets(settings: Settings, args: argparse.Namespace) -> list[int]:
    if getattr(args, "account", None):
        acc = settings.account_or_none(args.account)
        if acc is None:
            raise ConfigError(f"账号清单里没有序号 {args.account}")
        return [acc.index]
    return [a.index for a in settings.accounts]


def _print(text: str) -> None:
    print(text)
    sys.stdout.flush()


# --------------------------------------------------------------------------
#  doctor
# --------------------------------------------------------------------------
def cmd_doctor(settings: Settings, registry: ClientRegistry, args) -> int:
    """连通性自检：配置 + 每个账号的鉴权 + 可用域。"""
    _print("🩺 环境自检")
    _print(f"  配置目录：{settings.home}")
    _print(f"  账号清单：{settings.accounts_file}")
    _print(f"  账号数量：{len(settings.accounts)}")
    _print(f"  写操作：{'开启' if settings.write_enabled else '关闭(DRY-RUN)'}")
    _print("")
    _print(f"正在逐个验证 {len(settings.accounts)} 个账号的鉴权…")

    def check(index: int) -> tuple[int, str, list[str] | str]:
        client = registry.get(index)
        try:
            ads = client.availability_domains()
            return index, "ok", ads
        except Exception as exc:  # noqa: BLE001
            return index, "fail", one_line(exc, 160)

    results = parallel_map(check, [a.index for a in settings.accounts],
                           max_workers=settings.max_concurrency)
    ok = [r for r in results if r[1] == "ok"]
    bad = [r for r in results if r[1] != "ok"]

    _print("")
    for index, status, payload in results:
        label = settings.account(index).label
        if status == "ok":
            _print(f"  ✅ {label:<28} {len(payload)} 个可用域")
        else:
            _print(f"  ❌ {label:<28} {payload}")

    _print("")
    _print(f"结果：{len(ok)} 个正常，{len(bad)} 个失败")
    return 1 if bad else 0


# --------------------------------------------------------------------------
#  accounts
# --------------------------------------------------------------------------
def cmd_accounts(settings: Settings, registry: ClientRegistry, args) -> int:
    _print(f"📋 账号清单（{len(settings.accounts)} 个）")
    for acc in settings.accounts:
        key = "instance_principal" if acc.auth_mode == "instance_principal" else "api_key"
        _print(f"  {acc.index:>3}  {acc.alias or '-':<16} {acc.region:<18} {key}")
    return 0


# --------------------------------------------------------------------------
#  quota
# --------------------------------------------------------------------------
def cmd_quota(settings: Settings, registry: ClientRegistry, args) -> int:
    indexes = _targets(settings, args)
    _print(f"⏳ 查询 {len(indexes)} 个账号的配额…")
    caps = parallel_map(
        lambda i: quota_svc.account_capacity(registry.get(i)),
        indexes,
        max_workers=settings.max_concurrency,
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(c) for c in caps], f, ensure_ascii=False, indent=2)
        _print(f"✅ JSON 已写入 {args.json}")

    if args.summary:
        _print("")
        _print(quota_svc.summarize(caps))
    else:
        for cap in caps:
            _print("")
            _print(quota_svc.explain_capacity(cap))
    return 0


# --------------------------------------------------------------------------
#  instances
# --------------------------------------------------------------------------
def cmd_instances(settings: Settings, registry: ClientRegistry, args) -> int:
    indexes = _targets(settings, args)

    def fetch(index: int):
        client = registry.get(index)
        try:
            return client.account.label, compute_svc.list_instances(client), None
        except Exception as exc:  # noqa: BLE001
            return client.account.label, [], one_line(exc, 200)

    for label, views, error in parallel_map(fetch, indexes,
                                            max_workers=settings.max_concurrency):
        if error:
            _print(f"❌ {label}: {error}")
            continue
        _print(f"\n【{label}】{len(views)} 台")
        for v in sorted(views, key=lambda x: x.display_name):
            pool = " [池]" if v.in_pool else ""
            _print(f"  {v.lifecycle_state:<12} {v.display_name:<32} "
                   f"{v.ad.split(':')[-1]:<14} {v.public_ip or '-'}{pool}")
    return 0


# --------------------------------------------------------------------------
#  buckets
# --------------------------------------------------------------------------
def cmd_buckets(settings: Settings, registry: ClientRegistry, args) -> int:
    for index in _targets(settings, args):
        client = registry.get(index)
        try:
            ns = storage_svc.namespace(client)
            buckets = storage_svc.list_buckets(client, with_stats=args.stats)
        except Exception as exc:  # noqa: BLE001
            _print(f"❌ {client.account.label}: {one_line(exc, 200)}")
            continue
        _print(f"\n【{client.account.label}】命名空间 {ns[:10]}… "
               f"{len(buckets)} 个桶")
        for b in buckets:
            extra = ""
            if args.stats:
                extra = (f"  对象 {b.object_count}  体积 {b.size_gib:.3f} GiB"
                         f"  版本 {b.versioning}  PAR {b.par_count}")
            _print(f"  🪣 {b.name}{extra}")
    return 0


# --------------------------------------------------------------------------
#  orphans
# --------------------------------------------------------------------------
def cmd_orphans(settings: Settings, registry: ClientRegistry, args) -> int:
    indexes = _targets(settings, args)
    _print(f"⏳ 扫描 {len(indexes)} 个账号的孤儿卷…")
    reports = parallel_map(
        lambda i: audit_svc.scan_orphans(registry.get(i)),
        indexes, max_workers=settings.max_concurrency,
    )
    _print("")
    _print(audit_svc.render_orphans(reports))
    return 0


# --------------------------------------------------------------------------
#  audit
# --------------------------------------------------------------------------
def cmd_audit(settings: Settings, registry: ClientRegistry, args) -> int:
    indexes = _targets(settings, args)
    _print(f"⏳ 审计 {len(indexes)} 个账号的计费残留…")
    # ⚠️ on_error 返回 None（=结果未知），不是 []（=确实没有）。
    #    以前这里不传 on_error，17 个账号里有一个密钥过期，整条命令就崩了 ——
    #    另外 16 个的结果也一起丢掉。现在失败的账号单独标出来，其余照常输出。
    results = parallel_map(
        lambda i: audit_svc.audit_leaks(registry.get(i)),
        indexes, max_workers=settings.max_concurrency, on_error=scan_failed,
    )
    mapping = {registry.get(i).account.label: v
               for i, v in zip(indexes, results, strict=True)}
    _print("")
    _print(audit_svc.render_leaks(mapping))
    return 0


# --------------------------------------------------------------------------
#  security
# --------------------------------------------------------------------------
def cmd_security(settings: Settings, registry: ClientRegistry, args) -> int:
    indexes = _targets(settings, args)
    _print(f"⏳ 扫描 {len(indexes)} 个账号的安全暴露面…")
    exposures = parallel_map(
        lambda i: security_svc.scan_ssh_exposure(registry.get(i)),
        indexes, max_workers=settings.max_concurrency, on_error=scan_failed,
    )
    plaintext = parallel_map(
        lambda i: security_svc.scan_plaintext_secrets(registry.get(i)),
        indexes, max_workers=settings.max_concurrency, on_error=scan_failed,
    )
    exp_map = {registry.get(i).account.label: v
               for i, v in zip(indexes, exposures, strict=True)}
    pt_map = {registry.get(i).account.label: v
              for i, v in zip(indexes, plaintext, strict=True)}
    _print("")
    _print(security_svc.render_exposure(exp_map))
    _print("")
    _print(security_svc.render_plaintext(pt_map))
    return 0


# --------------------------------------------------------------------------
#  入口
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m oracles.cli",
        description="OCI 多账号运维 CLI（只读）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="环境与鉴权自检").set_defaults(func=cmd_doctor)
    sub.add_parser("accounts", help="列出账号").set_defaults(func=cmd_accounts)

    p = sub.add_parser("quota", help="配额与补机余量")
    p.add_argument("--account", type=int, help="只查指定账号序号")
    p.add_argument("--json", help="把结果写入 JSON 文件")
    p.add_argument("--summary", action="store_true", help="只输出汇总")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("instances", help="实例列表")
    p.add_argument("--account", type=int)
    p.set_defaults(func=cmd_instances)

    p = sub.add_parser("buckets", help="存储桶列表")
    p.add_argument("--account", type=int)
    p.add_argument("--stats", action="store_true", help="统计对象数/体积/PAR")
    p.set_defaults(func=cmd_buckets)

    p = sub.add_parser("orphans", help="孤儿卷扫描")
    p.add_argument("--account", type=int)
    p.set_defaults(func=cmd_orphans)

    p = sub.add_parser("audit", help="计费残留审计")
    p.add_argument("--account", type=int)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("security", help="安全暴露面扫描")
    p.add_argument("--account", type=int)
    p.set_defaults(func=cmd_security)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings, registry = _load()
        return args.func(settings, registry, args)
    except ConfigError as exc:
        print(f"❌ 配置错误：\n{exc}", file=sys.stderr)
        return 2
    except OraclesError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
