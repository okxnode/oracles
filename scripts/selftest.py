#!/usr/bin/env python3
"""全流程自检 —— 只读演练每一个模块。

用途：
  · 部署后确认所有功能在**你这个账号池**上都能正常工作
  · 改完代码确认没把哪条路径改坏
  · 贡献者没有真实凭据时，跑这个能看到完整流程长什么样

⚠️ 本脚本**绝不执行任何写操作**。它只做两件事：
     1. 调用所有 ``plan_*`` 函数（只读），把计划打印出来
     2. 调用写执行器，**验证它被写开关正确拦下**

用法：
    ORACLES_HOME=/etc/oracles python scripts/selftest.py
    python scripts/selftest.py --account 3     # 只测指定账号
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracles.bot.dispatch import ensure_writable, execute_pending  # noqa: E402
from oracles.bot.store import PendingAction  # noqa: E402
from oracles.config import load_settings, validate_startup  # noqa: E402
from oracles.errors import NotAllowedError  # noqa: E402
from oracles.log import setup_logging  # noqa: E402
from oracles.oci_gateway import ClientRegistry  # noqa: E402
from oracles.redact import scrub  # noqa: E402
from oracles.services import compute as compute_svc  # noqa: E402
from oracles.services import quota as quota_svc  # noqa: E402
from oracles.services import security as security_svc  # noqa: E402
from oracles.services import storage as storage_svc  # noqa: E402
from oracles.utils import parallel_map  # noqa: E402

GREEN, RED, YELLOW, DIM, NC = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
PASSED, FAILED, SKIPPED = 0, 0, 0


def ok(name: str, detail: str = "") -> None:
    global PASSED
    PASSED += 1
    print(f"  {GREEN}✓{NC} {name}" + (f"  {DIM}{detail}{NC}" if detail else ""))


def fail(name: str, detail: str = "") -> None:
    global FAILED
    FAILED += 1
    print(f"  {RED}✗{NC} {name}" + (f"\n      {RED}{detail}{NC}" if detail else ""))


def skip(name: str, why: str = "") -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  {YELLOW}·{NC} {name}  {DIM}（跳过：{why}）{NC}")


def section(title: str) -> None:
    print(f"\n{'─' * 66}\n{title}\n{'─' * 66}")


def show(text: str, indent: str = "      ") -> None:
    for line in str(text).splitlines():
        print(f"{DIM}{indent}{line}{NC}")


def pair_hook(what: str):
    """构造 ``parallel_map`` 的 ``on_error``：把失败记为 ✗，并返回 ``(item, None)``。

    ⚠️ 为什么不用 ``lambda i, exc: (i, [])``：
       那样失败的账号会被算成「0 台实例 / 0 个桶 / 0 条暴露规则」，
       总数照样打印 ✅ —— 自检给出一个**偏小却不报警**的数字，
       比直接崩掉更糟，因为你正拿它当"一切正常"的依据。
       ``None`` 让调用方能区分「确实没有」和「没查到」。
    """
    def _hook(item, exc: Exception):
        fail(f"{what} · 账号 {item}", str(exc).splitlines()[0][:150])
        return (item, None)
    return _hook


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", type=int, help="只测试指定账号")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    t0 = time.time()
    print("=" * 66)
    print("  oracles 全流程自检（只读）")
    print("=" * 66)

    # ------------------------------------------------------------------
    section("1. 配置加载")
    # ------------------------------------------------------------------
    try:
        settings = load_settings(require_token=False)
    except Exception as exc:
        fail("配置加载", str(exc))
        return 1

    setup_logging("DEBUG" if args.verbose else "ERROR")
    ok("配置加载", f"{len(settings.accounts)} 个账号")
    ok("配置目录", str(settings.home))
    ok("账号清单", str(settings.accounts_file))
    ok("写操作开关", "开启" if settings.write_enabled else "关闭（DRY-RUN）")
    ok("不可逆开关", "允许" if settings.allow_destructive else "禁止")

    for warning in validate_startup(settings):
        print(f"      {YELLOW}{warning}{NC}")

    registry = ClientRegistry(settings)
    indexes = [args.account] if args.account else [a.index for a in settings.accounts]
    if args.account and settings.account_or_none(args.account) is None:
        fail("账号序号", f"清单里没有 {args.account}")
        return 1

    # ------------------------------------------------------------------
    section("2. 脱敏（开源安全的底线）")
    # ------------------------------------------------------------------
    sample = ("租户 ocid1.tenancy.oc1..aaaaaaaabbbbccccddddeeeeffffgggghhhh\n"  # secret-scan:allow
              "指纹 00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff\n"
              "token 1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")  # secret-scan:allow
    cleaned = scrub(sample)
    leaks = [token for token in ("ocid1.tenancy", "00:11:22", "1234567890:")
             if token in cleaned]
    if leaks:
        fail("脱敏", f"以下内容没被过滤：{leaks}")
    else:
        ok("脱敏", "OCID / 指纹 / Token 均已打码")

    # ------------------------------------------------------------------
    section("3. 鉴权连通性")
    # ------------------------------------------------------------------
    good_accounts: list[int] = []
    for index in indexes:
        client = registry.get(index)
        try:
            ads = client.availability_domains()
            good_accounts.append(index)
            ok(f"{client.account.label}", f"{len(ads)} 个可用域")
        except Exception as exc:  # noqa: BLE001
            fail(f"{client.account.label}", str(exc).splitlines()[0][:150])

    if not good_accounts:
        fail("没有任何账号可用，后续测试无法进行")
        return 1

    # 挑一个有额度的账号做后续测试，没有就用第一个可用的
    launch_target: int | None = None
    for index in good_accounts:
        try:
            cap = quota_svc.account_capacity(registry.get(index),
                                             include_object_storage=False)
            if cap.total_launchable > 0:
                launch_target = index
                break
        except Exception:  # noqa: BLE001
            continue

    # ------------------------------------------------------------------
    section("4. 配额与容量（逐 AD）")
    # ------------------------------------------------------------------
    caps = parallel_map(
        lambda i: quota_svc.account_capacity(registry.get(i)),
        good_accounts,
        max_workers=settings.max_concurrency,
    )
    caps = [c for c in caps if c is not None]
    for c in caps:
        if c.error:
            fail(f"账号 {c.index} 配额", c.error[:150])

    if caps:
        total = sum(c.total_launchable for c in caps)
        ok("配额查询", f"{len(caps)} 个账号，合计可开 {total} 台")
        show(quota_svc.summarize(caps))

        # 验证「逐 AD 求和」这条核心规则真的生效
        multi_ad = next((c for c in caps if len(c.ads) > 1), None)
        if multi_ad:
            per_ad = sum(a.launchable for a in multi_ad.ads)
            if per_ad == multi_ad.total_launchable:
                ok("逐 AD 求和", f"{multi_ad.label}：{len(multi_ad.ads)} 个 AD 合计 {per_ad} 台")
            else:
                fail("逐 AD 求和", f"{per_ad} != {multi_ad.total_launchable}")

    # ------------------------------------------------------------------
    section("5. 实例管理（计划生成，不执行）")
    # ------------------------------------------------------------------
    # 并发列出所有账号的实例，再挑一个有实例的做后续测试 ——
    # 串行逐个找的话，最坏情况要扫完 17 个账号。
    listed = parallel_map(
        lambda i: (i, compute_svc.list_instances(registry.get(i))),
        good_accounts,
        max_workers=settings.max_concurrency,
        on_error=pair_hook("实例列表"),
    )
    scanned = [(i, v) for i, v in listed if v is not None]
    total_instances = sum(len(v) for _, v in scanned)
    detail = f"{len(scanned)} 个账号，共 {total_instances} 台"
    if len(scanned) != len(listed):
        detail += f"（{len(listed) - len(scanned)} 个账号查询失败，见上方 ✗）"
    ok("实例列表", detail)

    instance_target = next(
        ((i, views[0]) for i, views in scanned if views), None)

    if instance_target is None:
        skip("实例详情 / 开关机计划 / 销毁计划", "没有找到任何实例")
    else:
        index, view = instance_target
        client = registry.get(index)
        ok("实例详情", f"{view.display_name} @ {view.public_ip or '无公网IP'}")

        if view.is_running:
            try:
                pv, oci_action = compute_svc.plan_power(client, view.id, "stop")
                ok("关机计划", f"{pv.display_name} → {oci_action}")
            except Exception as exc:  # noqa: BLE001
                fail("关机计划", str(exc)[:200])
        else:
            try:
                pv, oci_action = compute_svc.plan_power(client, view.id, "start")
                ok("开机计划", f"{pv.display_name} → {oci_action}")
            except Exception as exc:  # noqa: BLE001
                fail("开机计划", str(exc)[:200])

        try:
            plan = compute_svc.plan_terminate(client, view.id)
            if plan.instance.in_pool:
                ok("销毁计划", "正确识别出池成员并阻止")
            else:
                ok("销毁计划", "已生成（未执行）")
            show(plan.render())
        except Exception as exc:  # noqa: BLE001
            fail("销毁计划", str(exc)[:200])

    if launch_target is None:
        skip("创建实例计划", "所有账号都没有可开额度")
    else:
        client = registry.get(launch_target)
        try:
            plan = compute_svc.plan_launch(client, name="selftest-plan")
            ok("创建实例计划", f"自动选中 {plan.availability_domain.split(':')[-1]}")
            show(plan.render())
        except Exception as exc:  # noqa: BLE001
            fail("创建实例计划", str(exc)[:400])

    # ------------------------------------------------------------------
    section("6. 存储桶（计划生成，不执行）")
    # ------------------------------------------------------------------
    # 先并发做一次「轻量」列桶（不带统计），找到有桶的账号，
    # 再对那一个做重量级统计 —— 避免为了找一个桶把 17 个账号全扫一遍。
    light = parallel_map(
        lambda i: (i, storage_svc.list_buckets(registry.get(i))),
        good_accounts,
        max_workers=settings.max_concurrency,
        on_error=pair_hook("桶列表"),
    )
    found = next(((i, bs) for i, bs in light if bs), None)

    if found is None:
        # 失败已经被 pair_hook 记成 ✗ 了，这里的措辞不能把它们说成"没有桶"
        n_failed = sum(1 for _, bs in light if bs is None)
        skip("删桶计划 / 对象列举",
             f"没有找到任何存储桶（{n_failed} 个账号查询失败）" if n_failed
             else "没有找到任何存储桶")
    else:
        index, _ = found
        client = registry.get(index)
        try:
            buckets = storage_svc.list_buckets(client, with_stats=True)
            ok(f"{client.account.label} 桶列表", f"{len(buckets)} 个")
            for b in buckets:
                ok(f"  桶 {b.name}",
                   f"对象 {b.object_count} / {b.size_gib:.3f} GiB / "
                   f"版本 {b.versioning} / PAR {b.par_count}")
            bucket = buckets[0]
        except Exception as exc:  # noqa: BLE001
            fail("桶统计", str(exc)[:200])
            bucket = None

        if bucket is not None:
            try:
                objs = storage_svc.list_objects(client, bucket.name)
                ok("对象列举", f"{len(objs)} 个对象（空桶返回 []，不是 None）")
            except Exception as exc:  # noqa: BLE001
                fail("对象列举", str(exc)[:300])

            try:
                plan = storage_svc.plan_delete_bucket(client, bucket.name)
                if plan.can_execute:
                    ok("删桶计划", "已生成（未执行）")
                else:
                    ok("删桶计划", "正确识别出风险并阻止")
                show(plan.render())
            except Exception as exc:  # noqa: BLE001
                fail("删桶计划", str(exc)[:300])

    # ------------------------------------------------------------------
    section("7. 安全加固（计划生成，不执行）")
    # ------------------------------------------------------------------
    scans = parallel_map(
        lambda i: (i, security_svc.scan_ssh_exposure(registry.get(i))),
        good_accounts,
        max_workers=settings.max_concurrency,
        on_error=pair_hook("暴露面扫描"),
    )
    scanned = [(i, v) for i, v in scans if v is not None]
    total_exposed = sum(len(v) for _, v in scanned)
    affected = sum(1 for _, v in scanned if v)
    detail = (f"{affected}/{len(scanned)} 个账号存在全网开放的 22 端口"
              f"（共 {total_exposed} 条规则）")
    if len(scanned) != len(scans):
        detail += f"；{len(scans) - len(scanned)} 个账号扫描失败，结论不完整"
    ok("暴露面扫描", detail)

    hit = next(((i, v) for i, v in scanned if v), None)
    if hit is None:
        skip("收紧计划", "没有发现对全网开放的 22 端口")
    else:
        index, exposures = hit
        client = registry.get(index)
        names = sorted({e.resource_name for e in exposures if e.kind == "security_list"})
        if not names:
            skip("收紧计划", "命中的规则都在 NSG 里，本示例只演示安全列表")
        else:
            try:
                plan = security_svc.plan_harden(
                    client, security_list_name=names[0],
                    allowed_cidr="203.0.113.7/32")
                ok("收紧计划", f"{plan.security_list_name}：{len(plan.changes)} 条待改")
                show(plan.render())
            except Exception as exc:  # noqa: BLE001
                fail("收紧计划", str(exc)[:300])

    # ------------------------------------------------------------------
    section("8. 写开关拦截（关键安全验证）")
    # ------------------------------------------------------------------
    fake = PendingAction(
        token="selftest", chat_id=1, user_id=1, kind="terminate",
        account_index=good_accounts[0], summary="自检用的假销毁请求",
        payload={"instance_id": "ocid1.instance.oc1..fake"},
        destructive=True,
    )

    try:
        ensure_writable(settings, destructive=True)
        if settings.write_enabled and settings.allow_destructive:
            skip("写开关拦截", "当前已开启写权限，无法验证拦截（这是正常的）")
        else:
            fail("写开关拦截", "写操作竟然被放行了！")
    except NotAllowedError as exc:
        ok("写开关拦截", "破坏性操作被正确拒绝")
        show(str(exc))

    # 再验证「通过执行器调用」也会被拦下（而不是只有直接调 ensure_writable 才拦）
    try:
        result = asyncio.run(execute_pending(fake, settings, registry))
        if "🔒" in result or "禁用" in result:
            ok("执行器拦截", "通过 dispatch 调用同样被拦下")
        else:
            fail("执行器拦截", f"执行器没拦住：{result[:200]}")
    except Exception as exc:  # noqa: BLE001
        fail("执行器拦截", str(exc)[:200])

    # ------------------------------------------------------------------
    section("9. Telegram Bot 组装")
    # ------------------------------------------------------------------
    try:
        from oracles.bot.app import build_application
        saved = settings.telegram_token
        settings.telegram_token = "1234567890:SELFTEST_ONLY_not_a_real_token"  # secret-scan:allow
        app = build_application(settings)
        handler_count = sum(len(v) for v in app.handlers.values())
        ok("Application 组装", f"{handler_count} 个 handler 已注册")
        commands = sorted(
            next(iter(h.commands)) for h in app.handlers[0]
            if getattr(h, "commands", None)
        )
        ok("命令注册", f"{len(commands)} 条：" + " ".join(f"/{c}" for c in commands[:12]) + " …")
        settings.telegram_token = saved
    except Exception as exc:  # noqa: BLE001
        fail("Application 组装", str(exc)[:300])

    # ------------------------------------------------------------------
    elapsed = time.time() - t0
    print(f"\n{'=' * 66}")
    print(f"  结果：{GREEN}{PASSED} 通过{NC}  "
          f"{RED}{FAILED} 失败{NC}  "
          f"{YELLOW}{SKIPPED} 跳过{NC}   耗时 {elapsed:.1f}s")
    print("=" * 66)
    if FAILED == 0:
        print(f"\n{GREEN}✅ 全流程自检通过。未执行任何写操作。{NC}")
        if not settings.write_enabled:
            print(f"{DIM}   当前是只读模式。确认计划内容无误后，可在 oracles.env 里"
                  f"打开 ORACLES_WRITE_ENABLED。{NC}")
        return 0
    print(f"\n{RED}❌ 有 {FAILED} 项失败，请看上面的输出。{NC}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
