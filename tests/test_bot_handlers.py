"""Bot handler 层测试 —— 用假 Telegram 对象跑**真实的** handler。

为什么需要这一层：`tests/` 里其他文件都是纯函数级的（脱敏、规则判定、
存储语义、渲染）。但下面这些是**安全属性**，只有把 handler 真正跑一遍才能回答，
读代码是确信不了的：

  · 白名单是不是挡住了**每一条**命令（包括以后新加的）
  · 只读模式下，破坏性操作是不是真的拦住了 —— 而不是「代码里看起来拦了」
  · 点了「确认执行」之后到底发生了什么
  · 扫描失败的账号在报告里长什么样

这里不连 Telegram、不碰 OCI：键盘对象用真的（纯本地构造），
Update/Context 用 `tests/telegram_harness.py` 的替身，
OCI 调用用 monkeypatch 打桩。

⚠️ 不用 pytest-asyncio：直接用 `asyncio.run()` 把协程跑掉，
   零新增依赖，任何环境都能跑。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from typing import Any

import oci.exceptions
import pytest
from cryptography.hazmat.primitives import serialization
from telegram.ext import CommandHandler

from oracles import sshkeys
from oracles.bot import handlers
from oracles.bot import keyboards as kb
from oracles.bot.app import build_application
from oracles.bot.store import PendingAction
from oracles.errors import to_api_error
from oracles.models import AccountCapacity, AdCapacity, InstanceView
from oracles.services import audit as audit_svc
from oracles.services import compute as compute_svc
from oracles.services import grab as grab_svc
from oracles.services import quota as quota_svc
from oracles.services import security as security_svc
from oracles.services import storage as storage_svc
from tests.telegram_harness import (
    ConsoleConnection,
    FakeClient,
    Harness,
    make_account,
    make_settings,
)

AUTHORIZED = 100


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
#  命令表：从**真实装配结果**里取，不硬编码
#
#  硬编码的命令列表会漂移：app.py 里加了新命令，测试却不知道，
#  于是新命令有没有 @restricted 就没人管了。从 build_application 取就没有这个问题。
# ---------------------------------------------------------------------------
def _registered_commands() -> list[tuple[str, Any]]:
    app = build_application(make_settings())
    found: dict[str, Any] = {}
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                for name in h.commands:
                    found[name] = h.callback
    return sorted(found.items())


COMMANDS = _registered_commands()
COMMAND_IDS = [name for name, _ in COMMANDS]


def test_command_table_is_not_empty():
    """自检：命令表取不到东西的话，下面那些参数化测试会「全绿但没测任何东西」。"""
    assert len(COMMANDS) >= 20, f"只取到 {len(COMMANDS)} 条命令，装配或取值方式有问题"


# ---------------------------------------------------------------------------
#  A. 白名单 —— 每条命令都要挡住
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,handler", COMMANDS, ids=COMMAND_IDS)
def test_command_rejected_when_whitelist_empty(name: str, handler: Any):
    """**空白名单 = 谁都不放行**（而不是「没配就放开」）。"""
    h = Harness(make_settings(allowed=()))
    msg = run(h.command(handler))
    text = msg.all_text
    assert "⛔" in text, f"/{name} 在空白名单下没有被拒绝"
    assert "无权限" in text, f"/{name} 的拒绝文案不对"


@pytest.mark.parametrize("name,handler", COMMANDS, ids=COMMAND_IDS)
def test_command_rejected_for_non_whitelisted_user(name: str, handler: Any):
    """不在白名单里的用户同样拒绝。

    这条同时也是「有没有忘记加 @restricted」的检查：
    忘了加的命令会真的往下跑，然后在这里炸掉 —— 而不是静默放行。
    """
    h = Harness(make_settings(allowed=(AUTHORIZED,)))
    msg = run(h.command(handler, user_id=999))
    text = msg.all_text
    assert "⛔" in text, f"/{name} 对未授权用户没有被拒绝（是不是漏了 @restricted？）"


@pytest.mark.parametrize("name,handler", COMMANDS, ids=COMMAND_IDS)
def test_rejection_does_not_leak_anything_else(name: str, handler: Any):
    """拒绝时只回一条无权限提示，不能顺带输出任何业务数据。"""
    h = Harness(make_settings(allowed=()))
    msg = run(h.command(handler))
    assert len(msg.texts) == 1, f"/{name} 被拒绝时发了 {len(msg.texts)} 条消息"
    assert "配额" not in msg.all_text
    assert "实例" not in msg.all_text or "无权限" in msg.all_text


def test_authorized_user_passes_whitelist():
    """白名单内的用户不会被挡（用不联网的命令验证）。"""
    h = Harness(make_settings(allowed=(AUTHORIZED,)))
    for handler in (handlers.cmd_start, handlers.cmd_help, handlers.cmd_status,
                    handlers.cmd_accounts, handlers.cmd_cancel):
        msg = run(h.command(handler))
        assert "⛔" not in msg.all_text, f"{handler.__name__} 误伤了授权用户"
        assert msg.all_text.strip(), f"{handler.__name__} 什么都没回"


# ---------------------------------------------------------------------------
#  B. 状态与账号
# ---------------------------------------------------------------------------
def test_status_shows_readonly_mode():
    h = Harness(make_settings(write=False))
    text = run(h.command(handlers.cmd_status)).all_text
    assert "DRY-RUN" in text
    assert "禁止" in text          # 不可逆操作
    assert "3" in text             # 账号数量


def test_status_shows_write_on_but_destructive_off():
    """写开着但不可逆关着 —— 这是推荐的中间状态，状态里要能看出来。"""
    h = Harness(make_settings(write=True, destructive=False))
    text = run(h.command(handlers.cmd_status)).all_text
    assert "✅ 开启" in text
    assert "❌ 禁止" in text


def test_accounts_lists_every_account():
    h = Harness(make_settings(indexes=(1, 2, 3, 7)))
    text = run(h.command(handlers.cmd_accounts)).all_text
    for i in (1, 2, 3, 7):
        assert f"测试账号{i}" in text


def test_startup_warnings_are_surfaced():
    h = Harness(make_settings(write=False), warnings=["⚠️ 未配置白名单"])
    text = run(h.command(handlers.cmd_status)).all_text
    assert "未配置白名单" in text


def test_start_is_actionable_not_just_welcome():
    """/start 是实质性入口：必须直接带上七大功能菜单按钮，而不是纯欢迎语。"""
    h = Harness(make_settings(indexes=(3, 7)))
    msg = run(h.command(handlers.cmd_start))
    assert "⛔" not in msg.all_text
    markup = msg.sent[-1].kwargs.get("reply_markup")
    assert markup is not None, "/start 没带任何键盘（应直接给七大功能菜单）"
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    # 七大入口都在：配置/开机/实例/配额/硬盘/任务/存储桶（+状态、帮助）
    assert "cfg" in data, "缺「1. 配置管理」按钮"
    assert "ap:0:wz" in data, "缺「2. 开机/自动抢机」按钮"
    assert "ap:0:il" in data, "缺「3. 实例管理」按钮"
    assert "q" in data, "缺「4. 配额查询」按钮"
    assert "ap:0:vl" in data, "缺「5. 硬盘管理」按钮"
    assert "tk" in data, "缺「6. 任务管理」按钮"
    assert "ap:0:bl" in data, "缺「7. 存储桶管理」按钮"


def test_help_lists_the_operation_manual():
    """/help 必须是完整的操作帮助：核心命令一个不能少。"""
    h = Harness(make_settings())
    text = run(h.command(handlers.cmd_help)).all_text
    for cmd in ("/start", "/q", "/i", "/b", "/obj", "/audit", "/security",
                "/launch", "/start_vm", "/stop_vm", "/reboot", "/terminate",
                "/rmbucket", "/s3key", "/harden", "/cancel"):
        assert cmd in text, f"帮助里缺少 {cmd}"
    # 两段式确认的说明也要在（安全设计的一部分）
    assert "确认执行" in text or "二次确认" in text


# ---------------------------------------------------------------------------
#  C. 参数校验 —— 错参数要给友好提示，不能崩
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("handler,args", [
    (handlers.cmd_quota, ["abc"]),
    (handlers.cmd_instances, ["abc"]),
    (handlers.cmd_buckets, ["abc"]),
    (handlers.cmd_launch, ["abc"]),
    (handlers.cmd_terminate, ["abc", "vm1"]),
    (handlers.cmd_start_vm, ["abc", "vm1"]),
    (handlers.cmd_rmbucket, ["abc", "bucket1"]),
    (handlers.cmd_s3key, ["abc"]),
    (handlers.cmd_harden, ["abc", "203.0.113.7/32"]),
], ids=lambda v: getattr(v, "__name__", str(v)))
def test_bad_account_argument_is_rejected(handler: Any, args: list[str]):
    h = Harness(make_settings())
    text = run(h.command(handler, *args)).all_text
    assert "❌" in text, f"{handler.__name__} 对非法账号序号没有报错"
    assert "必须是数字" in text


@pytest.mark.parametrize("handler", [
    handlers.cmd_objects, handlers.cmd_terminate, handlers.cmd_rmbucket,
    handlers.cmd_harden, handlers.cmd_start_vm, handlers.cmd_stop_vm,
    handlers.cmd_reboot,
], ids=lambda v: v.__name__)
def test_missing_arguments_shows_usage(handler: Any):
    """参数不足时要给用法，而不是 IndexError。"""
    h = Harness(make_settings())
    text = run(h.command(handler)).all_text
    assert "用法" in text, f"{handler.__name__} 缺参数时没给用法提示"


def test_cancel_clears_pending_actions():
    h = Harness(make_settings())
    h.store.add_pending(PendingAction(
        token="tok1", chat_id=1, user_id=AUTHORIZED, kind="terminate",
        account_index=1, summary="假的销毁请求", destructive=True))
    h.store.add_pending(PendingAction(
        token="tok2", chat_id=2, user_id=AUTHORIZED, kind="terminate",
        account_index=1, summary="别的会话的请求", destructive=True))

    text = run(h.command(handlers.cmd_cancel, chat_id=1)).all_text
    assert "已取消 1 个" in text
    assert h.store.get_pending("tok1") is None
    # 别的会话不受影响 —— 取消只清自己这个 chat
    assert h.store.get_pending("tok2") is not None


# ---------------------------------------------------------------------------
#  D. 破坏性操作：生成计划 → 点确认 → 被写开关拦住
#
#  这是整个 Bot 最重要的安全属性。用打桩的 plan_* 把流程跑到「点确认」那一步，
#  再验证 execute_pending 在开关关闭时确实不执行。
# ---------------------------------------------------------------------------
def _fake_instance_view(*, pool_id: str | None = None) -> InstanceView:
    return InstanceView(
        id="ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE",
        display_name="测试机", lifecycle_state="RUNNING",
        shape="VM.Standard.E2.1.Micro", region="example-region-1",
        ad="EXAMPLE-AD-1", public_ip="192.0.2.10", pool_id=pool_id,
    )


def test_terminate_plan_is_shown_with_confirm_button(monkeypatch):
    """点 /terminate 应该只生成计划 + 确认按钮，**绝不直接执行**。"""
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    h = Harness(make_settings(write=True, destructive=True))
    text = run(h.command(handlers.cmd_terminate, "1", "测试机")).all_text
    assert "销毁" in text
    assert "不可逆" in text
    # 生成了待确认操作，但还没执行
    assert len(h.store._pending) == 1  # noqa: SLF001


def test_terminate_confirm_is_blocked_in_readonly(monkeypatch):
    """只读模式下点「确认执行」→ 必须被拦住，且不能调用任何写 API。"""
    called = []
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda *a, **kw: called.append("executed"))

    h = Harness(make_settings(write=False))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    _query, msg = run(h.callback(handlers.on_callback, f"ok:{token}"))
    assert "写操作已禁用" in msg.all_text
    assert not called, "只读模式下居然调用了 execute_terminate！"


# ---------------------------------------------------------------------------
# D2. 写操作的**日志留痕**
#
# 为什么单独测这个：写开关拦下一个操作时，用户会在 Telegram 里收到拒绝消息，
# 但服务端日志里**什么都没有**。而「谁在什么时候试图做什么、有没有被拦住」
# 恰恰是运维最需要的东西 —— 真机部署时就撞上了：日志里只剩一条
# 「执行待确认操作」，读起来像已经执行了，实际什么都没发生。
#
# **日志读起来像执行过，比没有日志更危险。**
# ---------------------------------------------------------------------------
def test_blocked_write_leaves_a_rejection_in_the_log(monkeypatch, caplog):
    """被写开关拦下必须留痕（WARNING），且带上 kind / account / user。"""
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    h = Harness(make_settings(write=False))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="oracles.bot.dispatch"):
        run(h.callback(handlers.on_callback, f"ok:{token}"))

    rejects = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert rejects, "写操作被拦下了，日志里却没有任何记录"
    text = " ".join(r.getMessage() for r in rejects)
    assert "terminate" in text, f"日志没带上操作类型：{text}"
    assert "account=1" in text, f"日志没带上账号：{text}"
    assert "被拒绝" in text, f"日志没说清是被拒绝了：{text}"


def test_intent_log_does_not_claim_the_operation_executed(monkeypatch, caplog):
    """「收到确认」那条日志不能读起来像「已经执行」。"""
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    h = Harness(make_settings(write=False))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="oracles.bot.dispatch"):
        run(h.callback(handlers.on_callback, f"ok:{token}"))

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("尚未执行" in m for m in infos), f"意图日志措辞有歧义：{infos}"
    assert not any("执行完成" in m for m in infos), "被拦下的操作却记了「执行完成」"


def test_successful_execution_is_logged(monkeypatch, caplog):
    """真执行成功时也要留痕 —— 否则拦截日志无从对照。"""
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda client, plan: None)
    h = Harness(make_settings(write=True, destructive=True))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="oracles.bot.dispatch"):
        run(h.callback(handlers.on_callback, f"ok:{token}"))

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("执行完成" in m for m in infos), f"执行成功却没有留痕：{infos}"


def test_terminate_confirm_blocked_when_destructive_switch_off(monkeypatch):
    """写开着但不可逆关着 —— 销毁类操作仍要单独拦一层（双保险）。"""
    called = []
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda *a, **kw: called.append("executed"))

    h = Harness(make_settings(write=True, destructive=False))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    _query, msg = run(h.callback(handlers.on_callback, f"ok:{token}"))
    assert "不可逆操作被禁用" in msg.all_text
    assert not called, "不可逆开关关闭时居然执行了销毁！"


def test_terminate_confirm_executes_when_fully_enabled(monkeypatch):
    """两个开关都打开时确实会执行 —— 证明前面的拦截不是因为流程本身跑不通。"""
    called = []
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda client, plan: called.append("executed"))

    h = Harness(make_settings(write=True, destructive=True))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    _query, msg = run(h.callback(handlers.on_callback, f"ok:{token}"))
    assert called == ["executed"]
    assert "已销毁" in msg.all_text


def test_terminate_confirm_is_blocked_if_instance_entered_pool(monkeypatch):
    """计划生成后被加进实例池 → 执行前复核必须拦住（池会立刻补一台回来）。"""
    called = []
    in_pool = _fake_instance_view(pool_id="ocid1.instancepool.oc1..EXAMPLEEXAMPLEEXAMPLE")

    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "get_instance", lambda client, iid: in_pool)
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda client, plan: called.append("executed"))

    h = Harness(make_settings(write=True, destructive=True))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    _query, msg = run(h.callback(handlers.on_callback, f"ok:{token}"))
    assert "实例池管理" in msg.all_text
    assert not called


def test_cancel_button_does_not_execute(monkeypatch):
    """点「取消」不能执行任何东西。"""
    called = []
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda *a, **kw: called.append("executed"))

    h = Harness(make_settings(write=True, destructive=True))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    _query, msg = run(h.callback(handlers.on_callback, f"no:{token}"))
    assert "已取消" in msg.all_text
    assert not called


def test_confirm_token_cannot_be_replayed(monkeypatch):
    """同一个令牌点两次，第二次必须被拒（防止连点两下跑两遍）。"""
    called = []
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda client, plan: called.append("executed"))

    h = Harness(make_settings(write=True, destructive=True))
    run(h.command(handlers.cmd_terminate, "1", "测试机"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    run(h.callback(handlers.on_callback, f"ok:{token}"))
    _q2, _m2 = run(h.callback(handlers.on_callback, f"ok:{token}"))
    assert called == ["executed"], f"执行了 {len(called)} 次，重放没有被拦住"


def test_s3key_confirm_blocked_in_readonly():
    """S3 密钥签发属于写操作，只读模式下确认按钮也要拦住。"""
    called = []
    h = Harness(make_settings(write=False))
    run(h.command(handlers.cmd_s3key, "1"))
    token = next(iter(h.store._pending))  # noqa: SLF001

    import oracles.bot.dispatch as dispatch
    orig = storage_svc.create_s3_credential
    storage_svc.create_s3_credential = lambda *a, **kw: called.append("executed")
    try:
        _q, msg = run(h.callback(handlers.on_callback, f"ok:{token}"))
    finally:
        storage_svc.create_s3_credential = orig
    assert "写操作已禁用" in msg.all_text
    assert not called
    assert dispatch is not None


# ---------------------------------------------------------------------------
#  E. 按钮路由
# ---------------------------------------------------------------------------
def _button_callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_every_main_menu_button_is_handled():
    """主菜单里每个按钮的回调数据都必须有路由处理 —— 否则点了没反应。

    判据：路由的兜底分支会 `answer("未知操作")`，所以只要出现这个提示，
    就说明那个 callback_data 没被处理。
    """
    h = Harness(make_settings())
    for data in _button_callbacks(kb.main_menu()):
        query, _msg = run(h.callback(handlers.on_callback, data))
        assert "未知操作" not in query.alerts, f"按钮 {data!r} 没有对应的路由处理"


def test_every_account_picker_button_is_handled():
    """账号选择器（含翻页）的按钮也要都能点。"""
    h = Harness(make_settings(indexes=tuple(range(1, 9))))
    for action in ("a", "q", "il", "bl", "lp"):
        markup = kb.account_picker(h.settings.accounts, 0, action)
        for data in _button_callbacks(markup):
            query, _msg = run(h.callback(handlers.on_callback, data))
            assert "未知操作" not in query.alerts, (
                f"账号选择器 action={action} 的按钮 {data!r} 没有路由")


def test_unknown_callback_is_answered_not_crashed():
    h = Harness(make_settings())
    query, _msg = run(h.callback(handlers.on_callback, "definitely_not_a_real_action"))
    assert "未知操作" in query.alerts


# ---------------------------------------------------------------------------
#  E2. 实例菜单的按钮 —— 每一个都要**真的能走通**
#
#  为什么单独一组：2026-09-20 生产上点「🗑 销毁」返回
#      ❌ 未知动作 'terminate'，可选：['reboot', 'reset', 'softstop', 'start', 'stop']
#
#  根因：`_power_plan` 里 `plan_power` 的校验排在 `terminate` 分支**之前**。
#  plan_power 只认 POWER_ACTIONS，传 "terminate" 直接抛异常 →
#  那条分支是**永远走不到的死代码**。
#
#  为什么没被现有测试抓到：
#    · `test_every_main_menu_button_is_handled` / `..._account_picker_...` 覆盖了
#      主菜单和账号选择器，**唯独漏了实例菜单**；
#    · 而且它们只断言「没有『未知操作』提示」—— 那是**路由**层面的检查。
#      这个 bug 的路由是通的（`ip:` 分支接住了），是**动作本身**跑不通。
#    · `/terminate` 命令走的是另一条路（`cmd_terminate`），那条是好的、也有测试。
#      **同一个功能两个入口，只测了一个。**
#
#  所以这里的判据是：回话里**不能出现 ❌**，且每个动作要真的产出待确认操作。
# ---------------------------------------------------------------------------
def _stub_instance_services(monkeypatch, *, is_running: bool = True):
    """只桩最下层，让 plan_power / plan_terminate / open_rescue_console 的校验真跑。

    ⚠️ 实例状态要跟菜单状态**对齐** —— 否则点「▶️ 开机」会因为假实例
    已经是 RUNNING 而报「已经在运行中」，测试自己制造假失败。

    ⚠️ 这里**不桩** `open_rescue_console`。第一版桩掉了它，于是
    「🚑 救援」按钮的 publicKey 缺失问题在测试里完全看不见 ——
    **桩打得太高，会把被测行为一起桩掉。** 现在它拿夹具里的假网关跑真实代码。
    """
    state = "RUNNING" if is_running else "STOPPED"

    def _view(*_a, **_kw):
        v = _fake_instance_view()
        v.lifecycle_state = state
        return v

    monkeypatch.setattr(compute_svc, "resolve_instance", lambda client, needle: _view())
    monkeypatch.setattr(compute_svc, "get_instance", lambda client, iid: _view())
    monkeypatch.setattr(compute_svc, "list_instances", lambda client, **kw: [_view()])
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_view()))
    # 让救援路径走「现场生成 RSA」分支，不受开发机环境变量影响
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)


@pytest.mark.parametrize("is_running", [True, False])
def test_every_instance_menu_button_works(monkeypatch, is_running):
    """实例菜单里每个按钮点下去都不能报错，且要产出计划/回话。

    ⚠️ 别把 `plan_power` 本身也桩掉 —— 第一版就是这么写的，于是复现「通过」了。
    **桩打得太高，会把被测行为一起桩掉。**
    """
    _stub_instance_services(monkeypatch, is_running=is_running)
    h = Harness(make_settings(write=True, destructive=True))
    token = h.store.put(1, "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE")

    buttons = _button_callbacks(kb.instance_menu(1, token, is_running))
    assert buttons, "实例菜单没有任何按钮"

    for data in buttons:
        _q, msg = run(h.callback(handlers.on_callback, data))
        assert "❌" not in msg.all_text, (
            f"实例菜单按钮 {data!r} 报错了：\n{msg.all_text}"
        )
        assert msg.all_text.strip(), f"按钮 {data!r} 点了没有任何回话"


@pytest.mark.parametrize("action", ["stop", "reboot", "softstop", "reset", "terminate"])
def test_instance_action_button_creates_a_pending(monkeypatch, action):
    """每个实例动作都必须生成待确认操作 —— 按钮不能「看起来能用其实没生效」。"""
    _stub_instance_services(monkeypatch)
    h = Harness(make_settings(write=True, destructive=True))
    token = h.store.put(1, "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE")

    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:{action}"))

    assert "❌" not in msg.all_text, f"动作 {action} 报错：{msg.all_text}"
    assert len(h.store._pending) == 1, (  # noqa: SLF001
        f"动作 {action} 没有生成待确认操作"
    )


def test_terminate_button_offers_a_confirm_button(monkeypatch):
    """销毁按钮必须给出二次确认按钮 —— 不可逆操作不能一键直通。"""
    _stub_instance_services(monkeypatch)
    h = Harness(make_settings(write=True, destructive=True))
    token = h.store.put(1, "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE")

    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:terminate"))

    markup = msg.sent[-1].kwargs.get("reply_markup")
    assert markup is not None, "销毁计划没有附带确认键盘"
    datas = _button_callbacks(markup)
    assert any(d.startswith("ok:") for d in datas), f"没有确认按钮：{datas}"
    assert any(d.startswith("no:") for d in datas), f"没有取消按钮：{datas}"
    assert "不可逆" in msg.all_text


def test_terminate_button_does_not_execute_immediately(monkeypatch):
    """点销毁按钮**只能出计划**，绝不能当场执行 —— 这是全项目最重要的安全属性。"""
    called = []
    _stub_instance_services(monkeypatch)
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda *a, **kw: called.append("executed"))
    h = Harness(make_settings(write=True, destructive=True))
    token = h.store.put(1, "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE")

    run(h.callback(handlers.on_callback, f"ip:1:{token}:terminate"))

    assert not called, "点一下销毁按钮就把机器删了！"


def test_terminate_button_full_journey_executes(monkeypatch):
    """走完整旅程：点「🗑 销毁」→ 出计划 → 点「✅ 确认执行」→ 真的执行。

    这条覆盖的正是 2026-09-20 用户实际操作的路径。
    上面那些测试只走到「出计划」，这条把最后一跳也串起来 ——
    否则可能出现「计划出得来、确认按钮却点不动」这种半截修好的状态。
    """
    called = []
    _stub_instance_services(monkeypatch)
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda client, plan: called.append("executed"))
    h = Harness(make_settings(write=True, destructive=True))
    token = h.store.put(1, "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE")

    _q, plan_msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:terminate"))
    assert "❌" not in plan_msg.all_text, f"第一步就报错了：{plan_msg.all_text}"
    assert not called, "出计划阶段就执行了"

    pending_token = next(iter(h.store._pending))          # noqa: SLF001
    _q2, done_msg = run(h.callback(handlers.on_callback, f"ok:{pending_token}"))

    assert called == ["executed"], "确认后没有真的执行销毁"
    assert "已销毁" in done_msg.all_text, f"回话不对：{done_msg.all_text}"


# ---------------------------------------------------------------------------
#  E3. 救援控制台 —— publicKey 是必填，而且只收 RSA
#
#  为什么单独一组：2026-09-20 生产日志里
#      OciApiError: InvalidParameter (HTTP 400)
#      原始信息：publicKey must not be null
#  点「🚑 救援」只回一句报错。
#
#  根因：`open_rescue_console` 把 `public_key` 当**可选**参数，
#       不传就发请求。而 OCI 的 create_instance_console_connection
#       要求 publicKey 必填。
#
#  为什么 E2 没抓到：`_stub_instance_services` 把 `open_rescue_console`
#  整个桩掉了 —— 桩打在被测函数上，它内部干什么都测不到。
#
#  第二个 bug（同一段代码）：`render()` 告诉用户「装个 VNC 客户端，
#  把连接串粘进去」。但真实返回的是一条 **SSH 命令**，不是 vnc:// 链接。
#  说明这段代码从来没被真正执行过，是照着想象写的。
#
#  实测结论（sa-vinhedo-1，2026-09-20）：
#    · ssh-ed25519 → 被拒：Invalid ssh public key type "ssh-ed25519"
#    · ssh-rsa     → 接受
#    · 连接串 = ssh -o ProxyCommand='ssh -W %h:%p -p 443 <conn>@instance-console…'
# ---------------------------------------------------------------------------
def _public_from_pem(pem: str) -> str:
    """从 PEM 私钥推出它的 OpenSSH 公钥 —— 用来验证「给的钥匙能开这扇门」。"""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()


def test_rescue_button_works_with_the_real_service_code(monkeypatch):
    """「🚑 救援」按钮要走通，且**真的**给 OCI 发了一把 RSA 公钥。

    这条就是那个 bug 的护栏 —— 它让 `open_rescue_console` 真实执行，
    而不是被桩掉。删掉实现里的 public_key 参数，这条会红。
    """
    _stub_instance_services(monkeypatch)
    h = Harness(make_settings(write=True, destructive=True))
    token = h.store.put(1, "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE")

    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:rescue"))

    assert "❌" not in msg.all_text, f"救援按钮报错了：\n{msg.all_text}"
    client = h.registry.get(1)
    sent = client.find("create_instance_console_connection")
    assert len(sent) == 1, "没有向 OCI 发创建控制台连接的请求"
    assert sent[0].public_key.startswith("ssh-rsa "), (
        f"publicKey 不是 RSA（OCI 只收 RSA）：{sent[0].public_key!r}"
    )


def test_rescue_console_never_sends_an_empty_public_key(monkeypatch):
    """publicKey 不能为空 —— 空就是原 bug（服务端 400 publicKey must not be null）。"""
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))

    compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    sent = client.find("create_instance_console_connection")[0]
    assert sent.public_key and sent.public_key.strip(), "publicKey 是空的"
    assert sent.instance_id == "ocid1.instance.oc1..EXAMPLE"


def test_rescue_console_private_key_matches_the_public_key_it_sent(monkeypatch):
    """给用户的私钥必须能开它自己交上去的那把锁。

    这条是**用户视角**的属性：私钥和公钥对不上 = 拿到钥匙也进不去。
    （只断言「返回了私钥」是不够的 —— 那不保证两者配对。）
    """
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))

    console = compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert console.private_key_pem, "现场生成的密钥必须把私钥回给用户"
    assert "PRIVATE KEY" in console.private_key_pem
    sent_key = client.find("create_instance_console_connection")[0].public_key
    assert _public_from_pem(console.private_key_pem) == sent_key.strip(), (
        "回给用户的私钥和交给 OCI 的公钥不是一对 —— 拿到也进不去"
    )


def test_rescue_console_uses_a_configured_rsa_key_without_leaking_private_key(
        monkeypatch):
    """配了自己的 RSA 公钥时，不该再往 Telegram 发私钥。"""
    configured = FakeClient(make_account(1))
    monkeypatch.setenv(compute_svc.CONSOLE_KEY_ENV, "ssh-rsa AAAAEXAMPLEKEY comment")
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())

    console = compute_svc.open_rescue_console(configured, "ocid1.instance.oc1..EXAMPLE")

    assert console.private_key_pem is None, "用户自己配了公钥，不该再发私钥"
    assert configured.find("create_instance_console_connection")[0].public_key == \
        "ssh-rsa AAAAEXAMPLEKEY comment"


def test_rescue_console_refuses_a_non_rsa_configured_key(monkeypatch):
    """配了 ed25519 要**明确报错说清原因**，不能让它变成一句 OCI 的 400。"""
    monkeypatch.setenv(compute_svc.CONSOLE_KEY_ENV,
                       "ssh-ed25519 AAAAEXAMPLEKEY example-oci")
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))

    with pytest.raises(RuntimeError) as err:
        compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert "RSA" in str(err.value)
    assert not client.find("create_instance_console_connection"), \
        "明知道会被拒，还是发了请求"


def test_rescue_console_render_talks_about_ssh_not_a_vnc_client(monkeypatch):
    """渲染文案要说 SSH —— 真实连接串是 ssh 命令，VNC 客户端粘不进去。"""
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))

    text = compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE").render()

    assert "vnc://" not in text, "渲染里还在用不存在的 vnc:// 链接"
    assert "ssh" in text.lower()
    assert "串口" in text
    # 现场生成密钥时，命令里要已经带上 -i，省得用户自己找位置
    assert f"-i {compute_svc.CONSOLE_KEY_FILE}" in text
    # 指纹要完整给出，截断了就没法核对
    assert "SHA256:EXAMPLEfingerprintEXAMPLEfingerprintEXAMPLE" in text


def test_rescue_console_replaces_a_stale_connection_when_it_generated_the_key(
        monkeypatch):
    """已有旧连接 + 我们新生成密钥 → 必须删掉重建。

    否则复用旧连接，而旧连接是用**别的**公钥开的 —— 用户拿着新私钥进不去。
    """
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))
    stale = ConsoleConnection()
    client.existing_consoles = [stale]
    client.console_create_error = RuntimeError("console connection already exists")

    console = compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert client.deleted_consoles == [stale.id], "没有删掉那条对不上号的旧连接"
    assert len(client.find("create_instance_console_connection")) == 2, \
        "删完应该再建一条"
    assert console.replaced_existing is True
    assert "顶掉" in console.render()


def test_rescue_console_reuses_the_existing_connection_when_a_key_is_supplied(
        monkeypatch):
    """用户自己的公钥 + 已有连接 → 直接复用，不删不建（幂等）。"""
    monkeypatch.setenv(compute_svc.CONSOLE_KEY_ENV, "ssh-rsa AAAAEXAMPLEKEY comment")
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))
    client.existing_consoles = [ConsoleConnection()]
    client.console_create_error = RuntimeError("console connection already exists")

    console = compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert client.deleted_consoles == [], "不该删别人的连接"
    assert len(client.find("create_instance_console_connection")) == 1
    assert console.replaced_existing is False


def test_rescue_console_ignores_deleted_connections(monkeypatch):
    """已删掉的连接记录不能被当成「已有连接」复用。

    `list_instance_console_connections` 会把 `DELETED` 的记录也列出来
    （和实例列表里的 `TERMINATED` 幽灵一个毛病）——
    复用一条已删连接，用户拿到的连接串是死的。
    """
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))
    client.existing_consoles = [
        ConsoleConnection(id="ocid1.instanceconsoleconnection.oc1..DELETEDONE",
                          lifecycle_state="DELETED")]
    client.console_create_error = RuntimeError("console connection already exists")

    with pytest.raises(RuntimeError):
        compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert client.deleted_consoles == [], "不该去删一条已经删掉的连接"


def test_rescue_console_explains_when_a_connection_is_still_creating(monkeypatch):
    """连点两下时给一句人话，而不是把 409 原样丢给用户。"""
    monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
    monkeypatch.setattr(compute_svc, "get_instance",
                        lambda client, iid: _fake_instance_view())
    client = FakeClient(make_account(1))
    client.existing_consoles = [ConsoleConnection(lifecycle_state="CREATING")]
    client.console_create_error = RuntimeError("console connection already exists")

    with pytest.raises(RuntimeError) as err:
        compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert "创建中" in str(err.value)
    assert "already exists" not in str(err.value), "把底层报错原样透出去了"
    assert client.deleted_consoles == [], "正在创建的连接不能删"


def test_rescue_console_refuses_a_stopped_instance(monkeypatch):
    """停机实例开不了救援控制台，要给出人话解释而不是 OCI 的报错。"""
    monkeypatch.setattr(compute_svc, "get_instance", lambda client, iid: InstanceView(
        id="ocid1.instance.oc1..EXAMPLE", display_name="测试机",
        shape="VM.Standard.E2.1.Micro", lifecycle_state="STOPPED",
        ad="AD-1", region="example-region-1"))
    client = FakeClient(make_account(1))

    with pytest.raises(RuntimeError) as err:
        compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

    assert "STOPPED" in str(err.value)
    assert not client.find("create_instance_console_connection")


def test_empty_callback_data_does_not_crash():
    h = Harness(make_settings())
    query, _msg = run(h.callback(handlers.on_callback, ""))
    assert query.answered, "空回调数据没有得到任何响应"


def test_callback_from_other_chat_cannot_execute_pending(monkeypatch):
    """别的会话拿着令牌也不能执行 —— 令牌要绑定 chat_id。"""
    called = []
    monkeypatch.setattr(compute_svc, "plan_terminate",
                        lambda client, needle, **kw: compute_svc.TerminatePlan(
                            instance=_fake_instance_view()))
    monkeypatch.setattr(compute_svc, "execute_terminate",
                        lambda *a, **kw: called.append("executed"))

    h = Harness(make_settings(write=True, destructive=True))
    run(h.command(handlers.cmd_terminate, "1", "测试机", chat_id=1))
    token = next(iter(h.store._pending))  # noqa: SLF001

    query, _msg = run(h.callback(handlers.on_callback, f"ok:{token}", chat_id=999))
    assert any("不属于当前会话" in a for a in query.alerts)
    assert not called


# ---------------------------------------------------------------------------
#  F. 渲染管线（打桩数据）
# ---------------------------------------------------------------------------
def _fake_capacity(index: int = 1) -> AccountCapacity:
    return AccountCapacity(
        index=index, label=f"[{index}] 测试账号{index}", region=f"example-region-{index}",
        ads=[AdCapacity(ad="EXAMPLE-AD-1", e2_available=2, e2_used=0,
                        storage_available_gb=200.0)])


def test_quota_render_shows_launchable_count(monkeypatch):
    monkeypatch.setattr(quota_svc, "account_capacity", lambda c, **kw: _fake_capacity(c.account.index))
    h = Harness(make_settings(indexes=(1, 2)))
    text = run(h.command(handlers.cmd_quota)).all_text
    assert "合计可开" in text
    assert "测试账号1" in text


def test_quota_failure_is_marked_as_error_not_zero(monkeypatch):
    """配额查询失败的账号必须显示成「失败」，不能混进「可开 0 台」。"""
    def boom(client, **kw):
        raise RuntimeError("鉴权过期")

    monkeypatch.setattr(quota_svc, "account_capacity", boom)
    h = Harness(make_settings(indexes=(1, 2)))
    text = run(h.command(handlers.cmd_quota)).all_text
    assert "查询失败" in text
    assert "测试账号1" in text


def test_audit_marks_failed_accounts_as_unknown_not_clean(monkeypatch):
    """**这是本轮修掉的那个 bug 的回归测试。**

    以前 `on_error=lambda c, exc: []` 会把失败的账号渲染成「该账号很干净」，
    而这里漏掉的可能是**正在持续扣费**的未绑定公网 IP。
    现在必须显式标出「审计失败，结果未知」。
    """
    def flaky(client, **kw):
        if client.account.index == 2:
            raise RuntimeError("鉴权过期")
        return []

    monkeypatch.setattr(audit_svc, "audit_leaks", flaky)
    h = Harness(make_settings(indexes=(1, 2)))
    text = run(h.command(handlers.cmd_audit)).all_text
    assert "审计失败" in text, "失败账号没有被标出来 —— 又被当成「干净」了"
    assert "结果未知" in text
    assert "结论不完整" in text


def test_security_marks_failed_accounts_as_unknown_not_clean(monkeypatch):
    """安全扫描同理：失败的账号不能被当成「没有暴露面」。"""
    def flaky(client, **kw):
        if client.account.index == 2:
            raise RuntimeError("鉴权过期")
        return []

    monkeypatch.setattr(security_svc, "scan_ssh_exposure", flaky)
    monkeypatch.setattr(security_svc, "scan_plaintext_secrets", lambda c, **kw: [])
    h = Harness(make_settings(indexes=(1, 2)))
    text = run(h.command(handlers.cmd_security)).all_text
    assert "扫描失败" in text
    assert "结果未知" in text


def test_all_accounts_failing_does_not_claim_clean(monkeypatch):
    """**全军覆没时最危险的输出是「✅ 没有发现」。**"""
    def boom(client, **kw):
        raise RuntimeError("全部鉴权过期")

    monkeypatch.setattr(audit_svc, "audit_leaks", boom)
    h = Harness(make_settings(indexes=(1, 2, 3)))
    text = run(h.command(handlers.cmd_audit)).all_text
    assert "✅ 没有发现残留项。" not in text
    assert "结果未知" in text


# ---------------------------------------------------------------------------
#  G. 兜底：自由文本与全局错误
# ---------------------------------------------------------------------------
def test_unknown_text_gets_guidance():
    h = Harness(make_settings())
    text = run(h.text(handlers.on_text, "随便说点什么")).all_text
    assert text.strip()
    assert "⛔" not in text


def test_unknown_text_from_unauthorized_user_is_rejected():
    h = Harness(make_settings())
    text = run(h.text(handlers.on_text, "你好", user_id=999)).all_text
    assert "⛔" in text


def test_global_error_handler_survives_without_message():
    """全局错误处理不能自己再抛异常 —— 否则 Bot 就静默失败了。

    这条用假对象：`on_error` 内部有 `isinstance(update, Update)` 检查，
    假对象会跳过「回消息」那一步，但**不能因此崩掉**。
    """
    from oracles.bot.app import on_error
    from tests.telegram_harness import FakeContext, FakeUpdate

    run(on_error(FakeUpdate(), FakeContext({}, error=RuntimeError("boom"))))
    run(on_error(object(), FakeContext({}, error=RuntimeError("boom"))))
    run(on_error(None, FakeContext({}, error=ValueError("也没有消息"))))


def test_global_error_handler_replies_with_human_text(monkeypatch):
    """有真实 Update 时，要回一条人话（而不是只在日志里）。

    这里必须用**真的** ``telegram.Update`` —— `on_error` 里有
    ``isinstance(update, Update)`` 检查，假对象过不去。
    而 PTB 的类都定义了 ``__slots__``，没法给实例挂属性，
    所以只能在类上打桩 ``reply_text``。
    """
    from datetime import datetime

    from telegram import Chat, Message, Update

    from oracles.bot.app import on_error
    from tests.telegram_harness import FakeContext

    sent: list[str] = []

    async def fake_reply_text(self, text, **kwargs):  # noqa: ANN001, ARG001
        sent.append(text)

    monkeypatch.setattr(Message, "reply_text", fake_reply_text)

    message = Message(message_id=1, date=datetime(2026, 9, 18), chat=Chat(id=1, type="private"))
    update = Update(update_id=1, message=message)

    run(on_error(update, FakeContext({}, error=RuntimeError("boom"))))
    assert sent, "全局错误处理没有回消息"
    assert "出了点问题" in sent[0]
    assert "boom" in sent[0]


# ---------------------------------------------------------------------------
#  E4. 救援控制台：`-i` 必须**内外两跳都给**，且必须等连接 ACTIVE
#
#  为什么单独一组：2026-09-20 真机（<实例公网IP>）现场撞出来的两个 bug。
#
#  bug 1 —— `_ssh_with_key` 只给**外层** ssh 加了 `-i`。
#    连接串的形状是
#      ssh -o ProxyCommand='ssh -W %h:%p -p 443 <console>@<host>' <instance>
#    真正跟 OCI 控制台服务做公钥认证的是 **ProxyCommand 里那一跳**。
#    只给外层加 → 内层退回去试默认身份 → `Permission denied (publickey)`，
#    而私钥指纹跟控制台报告的**一模一样**（SHA256:y9AS…/Ig）。
#    于是排查会一路跑偏：怀疑密钥文件、怀疑权限、怀疑 ssh 版本……
#
#  bug 2 —— `open_rescue_console` 建完连接**立刻返回**，此时连接还是
#    `CREATING`，公钥在控制台侧还没生效。用户拿到连接串马上粘 → 必然被拒。
#    这才是「🚑 救援按钮从来没真正跑通过」的根因。
#    实测：隔 30 秒再连就通了。
#
#  这两条都**不是**「代码写错了」—— 语法、类型、异常处理全对，
#  单元测试也全绿。只有真去连一次才暴露。
# ---------------------------------------------------------------------------
_FAKE_CONNECTION_STRING = (
    "ssh -o ProxyCommand='ssh -W %h:%p -p 443 "
    "ocid1.instanceconsoleconnection.oc1.sa-vinhedo-1.EXAMPLEEXAMPLEEXAMPLEEXAMPLE"
    "@instance-console.sa-vinhedo-1.oci.oraclecloud.com' "
    "ocid1.instance.oc1.sa-vinhedo-1.EXAMPLEEXAMPLEEXAMPLEEXAMPLE"
)


def _inner_hop(command: str) -> str:
    """取出 `ProxyCommand='…'` 引号里那条**内层** ssh 命令。

    内层才是真正做公钥认证的那一跳。取不到就断言失败 ——
    不然「取不到」和「取到了但没有 -i」会混成一种结果。
    """
    assert "ProxyCommand='" in command, f"命令里没有 ProxyCommand：{command!r}"
    inner = command.split("ProxyCommand='", 1)[1].split("'", 1)[0]
    assert inner.strip(), "ProxyCommand 是空的"
    return inner


def test_ssh_with_key_injects_the_key_into_both_hops() -> None:
    """🔴 核心回归：外层**和**内层都要有 `-i`。

    修复前只有外层有 —— 这条会红。
    """
    out = compute_svc._ssh_with_key(_FAKE_CONNECTION_STRING, "oracles-rescue.pem")

    outer = out.split("-o ProxyCommand=", 1)[0]
    assert "-i oracles-rescue.pem" in outer, f"外层没拿到 -i：{outer!r}"

    inner = _inner_hop(out)
    assert "-i oracles-rescue.pem" in inner, (
        "内层（ProxyCommand 里那一跳）没拿到 -i —— "
        "真正做公钥认证的就是它，会报 Permission denied (publickey)"
    )


def test_ssh_with_key_also_pins_identities_on_the_inner_hop() -> None:
    """内层要 `IdentitiesOnly=yes`，别把 agent 里的其它身份也递上去。

    OCI 控制台连接只认创建时那把公钥；多递身份反而可能被拒。
    """
    out = compute_svc._ssh_with_key(_FAKE_CONNECTION_STRING, "k.pem")
    assert "IdentitiesOnly=yes" in _inner_hop(out), "内层没有 IdentitiesOnly=yes"


def test_ssh_with_key_does_not_disable_host_key_checking() -> None:
    """**不许**给用户塞 `StrictHostKeyChecking=no`。

    `RescueConsole.render()` 会打印主机密钥指纹，就是让用户核对的 ——
    一边给指纹一边关校验，等于把这道防线自己拆了。
    （探针脚本里用 `no` 是为了自动化，那是另一回事，不能外溢到这里。）
    """
    out = compute_svc._ssh_with_key(_FAKE_CONNECTION_STRING, "k.pem")
    assert "StrictHostKeyChecking" not in out
    assert "UserKnownHostsFile" not in out


def test_ssh_with_key_leaves_non_ssh_commands_untouched() -> None:
    """对照：不是 ssh 开头的命令原样返回（防止把别的东西改坏）。"""
    for cmd in ("", "vncviewer localhost:5900", "bash -lc 'ssh x'"):
        assert compute_svc._ssh_with_key(cmd, "k.pem") == cmd


def test_ssh_with_key_is_idempotent_enough_to_survive_no_proxy() -> None:
    """没有 ProxyCommand 的普通 ssh 命令：只加外层，不能崩。"""
    out = compute_svc._ssh_with_key("ssh ubuntu@1.2.3.4", "k.pem")
    assert out.startswith("ssh -i k.pem -o IdentitiesOnly=yes ubuntu@1.2.3.4")


def test_rendered_rescue_command_puts_the_key_on_the_inner_hop() -> None:
    """端到端：`RescueConsole.render()` 里那条命令，内层必须带 `-i`。

    用户复制粘贴的就是 `render()` 的输出 —— 上面几条测的是辅助函数，
    这条测的是「用户真正拿到的东西」。
    """
    console = compute_svc.RescueConsole(
        instance_id="ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE",
        display_name="demo",
        connection_string=_FAKE_CONNECTION_STRING,
        fingerprint="SHA256:abc",
        vnc_connection_string=_FAKE_CONNECTION_STRING.replace("-W %h:%p", "-N -L 5900:%h:%p"),
        private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nEXAMPLE\n-----END RSA PRIVATE KEY-----\n",
    )
    text = console.render()
    ssh_lines = [ln for ln in text.splitlines() if ln.startswith("ssh ")]
    assert ssh_lines, f"render() 里没有任何 ssh 命令：\n{text}"
    for line in ssh_lines:
        assert "-i " in _inner_hop(line), f"这条命令的内层缺 -i：\n{line}"


def test_open_rescue_console_waits_for_active() -> None:
    """🔴 核心回归：`open_rescue_console` 返回前必须等到连接 ACTIVE。

    修复前：`create_instance_console_connection` 返回什么就返回什么，
    此时 lifecycle_state 是 `CREATING`，公钥还没生效 → 用户立刻连必然被拒。

    断言的是 `wait_calls` 里**目标状态**是什么，不只是「等过」——
    否则把目标改成 `DELETED` 也能过。
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(compute_svc, "get_instance",
                            lambda client, iid: _fake_instance_view())
        monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
        client = FakeClient(make_account(1))
        # 真实现场：新建出来的连接就是 CREATING
        client.console_connection = ConsoleConnection(lifecycle_state="CREATING")

        console = compute_svc.open_rescue_console(
            client, "ocid1.instance.oc1..EXAMPLE")

        assert client.wait_calls, (
            "没有等 ACTIVE —— 返回的连接还在 CREATING，用户立刻连会被拒"
        )
        _rid, states, kwargs = client.wait_calls[0]
        assert "ACTIVE" in states, f"等的目标状态里没有 ACTIVE：{states}"
        assert "FAILED" in states, (
            "没把 FAILED 收进目标状态 —— 真失败时会一直轮询到超时，"
            "把「OCI 明确说失败了」变成一句含糊的「等超时」"
        )
        assert kwargs.get("max_wait_seconds", 0) > 0
        # 等完之后必须返回**可用的**连接（ACTIVE 那条），不是 CREATING 那条
        assert console.connection_string, "等完了却没把连接串带出来"
    finally:
        monkeypatch.undo()


def test_open_rescue_console_does_not_wait_when_already_active() -> None:
    """对照：返回的已经是 ACTIVE 就别白等（幂等复用那条路径）。"""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(compute_svc, "get_instance",
                            lambda client, iid: _fake_instance_view())
        monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
        client = FakeClient(make_account(1))          # 默认就是 ACTIVE

        compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")

        assert client.wait_calls == [], (
            f"已经是 ACTIVE 了还在等 —— 白等：{client.wait_calls}"
        )
    finally:
        monkeypatch.undo()


def test_open_rescue_console_raises_when_the_console_ends_up_failed() -> None:
    """`FAILED` 要给一句人话，不能把连接串照发出去。

    发出去 = 用户拿到一条永远连不上的命令，而提示里没有任何线索。
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(compute_svc, "get_instance",
                            lambda client, iid: _fake_instance_view())
        monkeypatch.delenv(compute_svc.CONSOLE_KEY_ENV, raising=False)
        client = FakeClient(make_account(1))
        client.console_connection = ConsoleConnection(lifecycle_state="CREATING")
        client.wait_return = ConsoleConnection(lifecycle_state="FAILED")

        with pytest.raises(RuntimeError, match="失败"):
            compute_svc.open_rescue_console(client, "ocid1.instance.oc1..EXAMPLE")
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
#  登录密钥：向导子步骤 + 实例菜单下载（2026-09-21 新增）
# ---------------------------------------------------------------------------
WZ_CHAT = 1
FAKE_INSTANCE_ID = "ocid1.instance.oc1..EXAMPLEEXAMPLEEXAMPLEEXAMPLE"


def _wizard_harness(tmp_path):
    """带**可写** ORACLES_HOME 的 harness —— 密钥要真的落盘。

    ⚠️ 默认夹具的 home 是 ``/tmp/EXAMPLE_home``（不存在）。密钥生成要写盘，
       不换成 tmp_path 会以 PermissionError 炸掉，而不是给出干净的断言。
    """
    return Harness(replace(make_settings(), home=tmp_path))


def _seed_wizard(h, **state):
    base = {"account_index": 1}
    base.update(state)
    h.store.put_wizard(WZ_CHAT, base)


def _seed_instance(h, index: int = 1) -> str:
    return h.store.put(index, FAKE_INSTANCE_ID)


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- 向导子步骤 ----
def test_login_step_asks_where_the_key_comes_from(tmp_path):
    """点「🔑 仅 SSH 公钥」不该直接跳网络步骤 —— 要先问公钥从哪来。

    2026-09-21 之前这里直接进 IPv6 那一步：用户没有机会选择，
    而「让 Bot 生成一对」是**必须他明确点**才该发生的事
    （它会把私钥落到服务器上）。
    """
    h = _wizard_harness(tmp_path)
    _seed_wizard(h)
    _q, msg = run(h.callback(handlers.on_callback, "wzl:key"))

    datas = [b.callback_data for row in kb.wizard_key_source().inline_keyboard for b in row]
    assert "wzk:gen" in datas and "wzk:own" in datas
    assert "公钥" in msg.all_text
    assert h.store.get_wizard(WZ_CHAT).get("login_mode") == "key"


def test_choosing_generated_key_writes_it_and_sends_both_files(tmp_path):
    """🔴 点「让 Bot 生成」→ 落盘 + **把两个文件都发出去**。

    这是「bot 要提供下载功能」这个需求的主闸门：光生成不发文件，
    用户拿不到私钥，机器开出来就进不去。
    """
    h = _wizard_harness(tmp_path)
    _seed_wizard(h)
    _q, msg = run(h.callback(handlers.on_callback, "wzk:gen"))

    key = sshkeys.load(h.settings, 1)
    assert key is not None, "密钥没落盘"
    assert key.private_path.is_file()

    docs = {d.filename: d for d in msg.documents}
    assert sorted(docs) == sorted(
        [key.private_filename, f"{key.private_filename}.pub"]
    ), f"发出去的文件不对：{sorted(docs)}"

    # ⚠️ 光断言文件名是不够的：文件名对、内容却是空文件（或把公钥当私钥发了），
    #    用户拿到照样登不进。所以逐字节比**内容**。
    assert docs[key.private_filename].sha256 == _sha256(key.private_path)
    assert docs[f"{key.private_filename}.pub"].sha256 == _sha256(key.public_path)
    assert all(d.size > 0 for d in msg.documents), "发出去的是空文件"
    assert all(key.fingerprint in d.caption for d in docs.values()), "附件没带指纹，用户没法核对"

    st = h.store.get_wizard(WZ_CHAT)
    assert st["ssh_public_key"] == key.public_openssh
    assert key.private_pem not in repr(st), "私钥被塞进向导状态了"


def test_choosing_own_key_generates_and_sends_nothing(tmp_path):
    """对照组：选「用我配置的公钥」时**不生成、不发文件**。

    ⚠️ 缺了这条，「无条件生成并发文件」的实现也能让上面那条通过 ——
       而那正是最坏的形态：用户只想用自己的钥匙，
       却被动接受了一把落到服务器上、还经过了 Telegram 的私钥。
    """
    h = _wizard_harness(tmp_path)
    _seed_wizard(h)
    _q, msg = run(h.callback(handlers.on_callback, "wzk:own"))

    assert msg.documents == [], "选「用我配置的公钥」却发了密钥文件"
    assert sshkeys.load(h.settings, 1) is None, "选「用我配置的公钥」却生成了密钥"
    st = h.store.get_wizard(WZ_CHAT)
    assert "ssh_public_key" not in st
    assert st["key_source"] == "own"


def test_generating_twice_reuses_the_same_key(tmp_path):
    """第二次点「生成」必须复用同一把 —— 换钥匙会让已开出去的机器全部失联。"""
    h = _wizard_harness(tmp_path)
    _seed_wizard(h)
    run(h.callback(handlers.on_callback, "wzk:gen"))
    first = sshkeys.load(h.settings, 1).fingerprint

    _seed_wizard(h)
    _q, msg = run(h.callback(handlers.on_callback, "wzk:gen"))
    assert sshkeys.load(h.settings, 1).fingerprint == first
    assert "复用" in msg.all_text, f"没告诉用户这是复用已有的：\n{msg.all_text}"


def test_back_button_only_changes_the_screen(tmp_path):
    """「上一步」回登录方式那一步，且**不动**向导状态。"""
    h = _wizard_harness(tmp_path)
    _seed_wizard(h, shape="E2", count=2)
    _q, msg = run(h.callback(handlers.on_callback, "wzkb"))
    assert "登录方式" in msg.all_text
    assert h.store.get_wizard(WZ_CHAT) == {"account_index": 1, "shape": "E2", "count": 2}, (
        "「上一步」改了向导状态 —— 它只该换一屏"
    )


def test_switching_to_password_clears_the_generated_key_state(tmp_path):
    """从公钥切回密码时，向导状态里的公钥要被清掉。

    不清的话 spec 会同时带上公钥和 cloud-init —— 两条登录路径一起注入。
    """
    h = _wizard_harness(tmp_path)
    _seed_wizard(h)
    run(h.callback(handlers.on_callback, "wzk:gen"))
    assert "ssh_public_key" in h.store.get_wizard(WZ_CHAT)

    run(h.callback(handlers.on_callback, "wzl:pwd"))
    st = h.store.get_wizard(WZ_CHAT)
    assert "ssh_public_key" not in st
    assert "key_source" not in st
    assert st["login_mode"] == "pwd"


# ---- 实例菜单下载 ----
def test_key_download_verifies_against_the_instance(tmp_path):
    """🔴 指纹一致 → 说「核对通过」并把文件发出去。

    判据是**实例 metadata 里真实的公钥**，不是一句
    「这个功能大概适用于这台机器」的免责声明。
    """
    h = _wizard_harness(tmp_path)
    key = sshkeys.ensure(h.settings, 1)
    h.registry.get(1).instance_metadata = {"ssh_authorized_keys": key.public_openssh}
    token = _seed_instance(h)

    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:key"))
    assert "核对通过" in msg.all_text, msg.all_text
    assert len(msg.documents) == 2
    docs = {d.filename: d for d in msg.documents}
    assert docs[key.private_filename].sha256 == _sha256(key.private_path), (
        "核对通过了，但发出去的不是磁盘上那把私钥"
    )


def test_key_download_flags_a_mismatch_instead_of_pretending(tmp_path):
    """🔴 指纹不一致时必须**明说不匹配**，不能悄悄发文件了事。

    这台机器可能开机时选的是「用我配置的公钥」—— 那把钥匙根本开不了它。
    静默发文件会让用户拿着错的钥匙去试，然后怀疑是 OCI 或机器的问题。
    """
    h = _wizard_harness(tmp_path)
    sshkeys.ensure(h.settings, 1)
    other_pub, _ = sshkeys.generate_ed25519("oracles-login-9")
    h.registry.get(1).instance_metadata = {"ssh_authorized_keys": other_pub}
    token = _seed_instance(h)

    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:key"))
    assert "不匹配" in msg.all_text, msg.all_text
    assert sshkeys.fingerprint_of(other_pub) in msg.all_text, (
        "没把实例上**实际**注的公钥指纹列出来，用户无从判断该用哪把"
    )


def test_key_download_lists_every_key_when_several_are_injected(tmp_path):
    """实例上注了多把时要**全部**列出来，不能只看第一把。"""
    h = _wizard_harness(tmp_path)
    sshkeys.ensure(h.settings, 1)
    p1, _ = sshkeys.generate_ed25519("one")
    p2, _ = sshkeys.generate_ed25519("two")
    h.registry.get(1).instance_metadata = {"ssh_authorized_keys": f"{p1}\n{p2}"}
    token = _seed_instance(h)

    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:key"))
    assert sshkeys.fingerprint_of(p1) in msg.all_text
    assert sshkeys.fingerprint_of(p2) in msg.all_text


def test_key_download_says_so_when_no_key_was_ever_generated(tmp_path):
    """账号还没有密钥 → 明说「没有」并指出怎么生成，**不发空文件**。"""
    h = _wizard_harness(tmp_path)
    token = _seed_instance(h)
    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:key"))
    assert "还没有" in msg.all_text
    assert msg.documents == []


def test_key_download_still_delivers_when_the_check_cannot_run(tmp_path, monkeypatch):
    """核对不了（OCI 报错）时**仍然把文件给出去**，但要说清核对没做成。

    拦着不给，用户就卡死了；静默给，用户会以为核对过了。
    正确做法是「给 + 明说没核对成」。
    """
    h = _wizard_harness(tmp_path)
    sshkeys.ensure(h.settings, 1)

    def boom(client, instance_id):
        raise RuntimeError("OCI 说 500")

    monkeypatch.setattr(compute_svc, "instance_authorized_keys", boom)
    token = _seed_instance(h)
    _q, msg = run(h.callback(handlers.on_callback, f"ip:1:{token}:key"))
    assert "无法核对" in msg.all_text, msg.all_text
    assert len(msg.documents) == 2, "核对失败就把文件扣住，用户拿不到钥匙"


# ---------------------------------------------------------------------------
#  🔴 用户视角：开机失败那一屏必须带上 OCI 的**原始信息**
# ---------------------------------------------------------------------------
def _raise_capacity(*_args: Any, **_kwargs: Any):
    """假的 ``try_launch_once``：抛一个**真实包装过**的「容量不足」。

    ⚠️ 用 ``to_api_error`` 而不是手搓 ``OciApiError("...")`` ——
       手搓的没有 ``.code``，会把「不可重试就停」那条逻辑悄悄测成
       「永远可重试」（2026-09-21 的真实 bug，见踩坑 #60）。
    """
    raise to_api_error(oci.exceptions.ServiceError(
        status=500, code="InternalError", headers={},
        message="Out of host capacity.",
    ))


def test_a_failed_launch_page_shows_the_original_oci_message(tmp_path, monkeypatch):
    """🔴 用户报告的那个 bug 的**出口**测试。

    用户看到的原来是「OCI 接口返回错误：InternalError (HTTP 500)」，
    而 OCI 的原始信息「Out of host capacity.」（该 AD 没容量）
    被 ``splitlines()[0]`` 截掉了 —— 于是一个能自解释的错误
    变成了「甲骨文挂了」。

    这里从**真实 handler** 走到**真实渲染**：`wzgo:0` → `_wz_execute`
    → 真实 `run_grab_loop` → 真实 `_failure_reason` → 用户看到的那一屏。
    唯一的假对象是 OCI 调用本身（离线跑不了真 API）。
    """
    h = Harness(replace(make_settings(write=True), home=tmp_path))
    _seed_wizard(h, shape="E2", count=1)
    monkeypatch.setattr(grab_svc, "try_launch_once", _raise_capacity)

    _q, msg = run(h.callback(handlers.on_callback, "wzgo:0"))
    text = msg.all_text

    assert "Out of host capacity." in text, (
        f"那一屏里没有 OCI 的原始信息 —— 用户没法知道是「没容量」：\n{text}"
    )
    assert "原始信息" in text
    assert "建议" in text, "也没给出下一步该怎么办"


def test_the_failed_launch_page_does_not_contradict_its_own_hint(tmp_path, monkeypatch):
    """原因里已经给了具体建议时，结尾不再补泛泛的「去看配额」。

    同踩坑 #38：一句会误导人的提示，比没有提示更糟。
    """
    h = Harness(replace(make_settings(write=True), home=tmp_path))
    _seed_wizard(h, shape="E2", count=1)
    monkeypatch.setattr(grab_svc, "try_launch_once", _raise_capacity)

    _q, msg = run(h.callback(handlers.on_callback, "wzgo:0"))
    text = msg.all_text

    assert "物理容量" in text, "针对容量不足的建议没出现"
    assert "配额查询" not in text, (
        f"同时又叫用户去查配额 —— 与上面的建议矛盾：\n{text}"
    )


def test_a_non_retryable_launch_failure_tells_the_user_to_stop(tmp_path, monkeypatch):
    """不可重试的错误（认证/权限）要**明说别再试了**。

    这类错误重试一万次也不会变。原来 ``is_retryable`` 恒为 True，
    分支永远走不到 —— 用户会一直点、一直失败，却得不到任何结论。
    """
    def boom(*_a: Any, **_kw: Any):
        raise to_api_error(oci.exceptions.ServiceError(
            status=401, code="NotAuthenticated", headers={},
            message="The required information to complete authentication was not provided",
        ))

    h = Harness(replace(make_settings(write=True), home=tmp_path))
    _seed_wizard(h, shape="E2", count=1)
    monkeypatch.setattr(grab_svc, "try_launch_once", boom)

    _q, msg = run(h.callback(handlers.on_callback, "wzgo:0"))
    text = msg.all_text

    assert "NotAuthenticated" in text, f"没把错误码给出来：\n{text}"
    assert "API 私钥" in text or "fingerprint" in text, (
        f"认证类错误没给出可操作的建议：\n{text}"
    )
