"""Telegram 命令与按钮处理器。

交互设计原则：
  · **所有写操作先出计划**，用键盘二次确认，绝不「一按就执行」
  · 按钮里只放短令牌，真实 OCID 存在 Store 里（Telegram 回调数据限 64 字节）
  · 慢操作（跨 17 个账号的配额/审计）先回一条「查询中」，跑完再编辑同一条消息
"""
from __future__ import annotations

import asyncio
import json
import logging
from functools import wraps
from typing import Any

from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..cloudinit import build_cloud_init, to_user_data
from ..config import Account, Settings
from ..errors import one_line
from ..models import A1_FLEX_SHAPE, BOOT_VOLUME_GB, E2_MICRO_SHAPE, AccountCapacity
from ..oci_gateway import ClientRegistry
from ..services import accounts_mgmt as acct_svc
from ..services import audit as audit_svc
from ..services import compute as compute_svc
from ..services import quota as quota_svc
from ..services import scan_failed
from ..services import security as security_svc
from ..services import storage as storage_svc
from ..sshkeys import LoginKey, blob_of, fingerprint_of
from ..sshkeys import ensure as ensure_login_key
from ..sshkeys import load as load_login_key
from ..sshkeys import render as render_login_key
from ..utils import humanize_state, parallel_map
from . import keyboards as kb
from . import render as R
from .dispatch import ensure_writable, execute_pending
from .store import PendingAction, Store
from .tasks import TaskRegistry

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  基础设施
# --------------------------------------------------------------------------
def ctx_of(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, ClientRegistry, Store]:
    return (
        context.bot_data["settings"],
        context.bot_data["registry"],
        context.bot_data["store"],
    )


def tasks_reg(context: ContextTypes.DEFAULT_TYPE) -> TaskRegistry:
    """取共享的任务注册表（挂在 bot_data，整个 Bot 生命周期只有一份）。"""
    reg = context.bot_data.get("tasks")
    if reg is None:   # 测试 harness 可能没预置 —— 惰性建一个并缓存
        reg = TaskRegistry()
        context.bot_data["tasks"] = reg
    return reg


def restricted(func):
    """白名单校验。

    **未配置白名单时谁都不放行** —— 而不是「没配就放开」。
    这个默认值很关键：一个能删你云主机的 Bot 一旦裸奔，
    被别人搜到就是灾难。
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        settings, _, _ = ctx_of(context)
        user = update.effective_user
        uid = user.id if user else None

        if not settings.allowed_user_ids or uid not in settings.allowed_user_ids:
            log.warning("拒绝未授权访问 user_id=%s username=%s",
                        uid, getattr(user, "username", None))
            message = (
                "⛔ 无权限。\n\n"
                f"你的 Telegram 用户 ID：`{uid}`\n"
                "把它加入 `<配置目录>/oracles.env` 的 "
                "`TELEGRAM_ALLOWED_USER_IDS` 后重启服务。"
            )
            if update.callback_query:
                await update.callback_query.answer("⛔ 无权限", show_alert=True)
            elif update.message:
                await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
            return

        return await func(update, context, *a, **kw)

    return wrapper


async def _reply(message, text: str, markup: Any = None) -> None:
    """发消息，自动分片；Markdown 解析失败时降级为纯文本。"""
    for i, part in enumerate(R.chunk(text)):
        chunk_markup = markup if i == len(R.chunk(text)) - 1 else None
        try:
            await message.reply_text(part, parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=chunk_markup,
                                     disable_web_page_preview=True)
        except BadRequest:
            await message.reply_text(part, reply_markup=chunk_markup,
                                     disable_web_page_preview=True)


async def _edit(query, text: str, markup: Any = None) -> None:
    """编辑消息，自动分片（只对第一片用编辑，其余追加发送）。"""
    parts = R.chunk(text)
    try:
        await query.edit_message_text(parts[0], parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=markup if len(parts) == 1 else None,
                                      disable_web_page_preview=True)
    except BadRequest as exc:
        # "Message is not modified" 是常态（用户重复点了同一个按钮），静默忽略
        if "not modified" not in str(exc).lower():
            try:
                await query.edit_message_text(parts[0],
                                              reply_markup=markup if len(parts) == 1 else None)
            except BadRequest:
                pass
    for part in parts[1:]:
        try:
            await query.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await query.message.reply_text(part)


def _new_pending(store: Store, chat_id: int, user_id: int, *, kind: str,
                 account_index: int, summary: str, payload: dict,
                 destructive: bool = False) -> PendingAction:
    import secrets
    pending = PendingAction(
        token=secrets.token_urlsafe(6),
        chat_id=chat_id,
        user_id=user_id,
        kind=kind,
        account_index=account_index,
        summary=summary,
        payload=payload,
        destructive=destructive,
    )
    store.add_pending(pending)
    return pending


def _account(settings: Settings, raw: str) -> Account:
    """解析账号序号，支持 ``3`` 和 ``acc3`` 两种写法。"""
    text = raw.strip().lower().removeprefix("acc")
    if not text.isdigit():
        raise ValueError(f"账号序号必须是数字，收到 {raw!r}")
    return settings.account(int(text))


# --------------------------------------------------------------------------
#  命令：总览
# --------------------------------------------------------------------------
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/start`` = 实质性入口：直接给出七大功能菜单。"""
    settings, _, _ = ctx_of(context)
    warnings = context.bot_data.get("startup_warnings", [])
    await _reply(update.message, R.welcome(settings, warnings), kb.main_menu())


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update.message, R.help_text(), kb.main_menu())


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = ctx_of(context)
    warnings = context.bot_data.get("startup_warnings", [])
    await _reply(update.message, R.status_text(settings, warnings), kb.main_menu())


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, store = ctx_of(context)
    chat_id = update.effective_chat.id
    # 清掉该会话所有待确认操作
    cleared = 0
    for token, pending in list(store._pending.items()):  # noqa: SLF001
        if pending.chat_id == chat_id:
            store.drop_pending(token)
            cleared += 1
    await _reply(update.message, f"✅ 已取消 {cleared} 个待确认操作。", kb.main_menu())


@restricted
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = ctx_of(context)
    lines = [f"📋 *账号清单*（共 {len(settings.accounts)} 个）", ""]
    for acc in settings.accounts:
        mode = "实例主体" if acc.auth_mode == "instance_principal" else "API Key"
        lines.append(f"`{acc.index:>2}` *{R.esc(acc.alias or '-')}*  "
                     f"{acc.region}  _{mode}_")
    lines.append("")
    lines.append("点下面的按钮选账号操作。")
    await _reply(update.message, "\n".join(lines), kb.account_picker(settings.accounts, 0, "a"))


# --------------------------------------------------------------------------
#  命令：配额
# --------------------------------------------------------------------------
async def _run_quota_all(message, settings: Settings, registry: ClientRegistry) -> None:
    placeholder = await message.reply_text(
        f"⏳ 正在查询 {len(settings.accounts)} 个账号的配额，"
        f"约需 {len(settings.accounts) * 2 // max(1, settings.max_concurrency)} 秒…"
    )
    caps = await asyncio.to_thread(
        parallel_map,
        lambda c: quota_svc.account_capacity(c),
        registry.all(),
        max_workers=settings.max_concurrency,
        on_error=lambda acc, exc: AccountCapacity(
            index=acc.account.index, label=acc.account.label,
            region=acc.account.region, error=str(exc)[:300]),
    )
    text = quota_svc.summarize(caps)
    text += "\n\n点按钮看单个账号的逐 AD 明细。"
    try:
        await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb.account_picker(settings.accounts, 0, "q"))
    except BadRequest:
        await placeholder.edit_text(text, reply_markup=kb.account_picker(settings.accounts, 0, "q"))


async def _run_quota_one(message, registry: ClientRegistry, index: int) -> None:
    client = registry.get(index)
    placeholder = await message.reply_text("⏳ 查询中…")
    cap = await asyncio.to_thread(quota_svc.account_capacity, client)
    text = quota_svc.explain_capacity(cap)
    if cap.total_launchable > 0:
        text += "\n\n✅ 该账号还能开机，用「➕ 开新机」创建。"
    try:
        await placeholder.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb.account_menu(index))
    except BadRequest:
        await placeholder.edit_text(text, reply_markup=kb.account_menu(index))


@restricted
async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, _ = ctx_of(context)
    args = context.args or []
    if args:
        try:
            acc = _account(settings, args[0])
        except ValueError as exc:
            await _reply(update.message, f"❌ {exc}")
            return
        await _run_quota_one(update.message, registry, acc.index)
    else:
        await _run_quota_all(update.message, settings, registry)


# --------------------------------------------------------------------------
#  命令：实例
# --------------------------------------------------------------------------
async def _show_instances(message, registry: ClientRegistry, store: Store,
                          chat_id: int, index: int, *, edit_query=None) -> None:
    client = registry.get(index)
    views = await asyncio.to_thread(compute_svc.list_instances, client)
    text = R.render_instance_list(views, client.account.label)

    pairs = [(store.put(chat_id, v.id), f"{'🟢' if v.is_running else '⚪'} "
             f"{v.display_name[:30]}") for v in views]
    markup = kb.instance_list(index, pairs)

    if edit_query is not None:
        await _edit(edit_query, text, markup)
    else:
        await _reply(message, text, markup)


@restricted
async def cmd_instances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if args:
        try:
            acc = _account(settings, args[0])
        except ValueError as exc:
            await _reply(update.message, f"❌ {exc}")
            return
        await _show_instances(update.message, registry, store,
                              update.effective_chat.id, acc.index)
    else:
        await _reply(
            update.message,
            "选一个账号看它的实例：",
            kb.account_picker(settings.accounts, 0, "il"),
        )


async def _power_plan(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      index: int, token: str, action: str) -> None:
    settings, registry, store = ctx_of(context)
    query = update.callback_query
    chat_id = update.effective_chat.id
    instance_id = store.get(chat_id, token)
    if not instance_id:
        await query.answer("会话已过期，请重新打开实例列表", show_alert=True)
        return

    await query.answer("生成计划中…")
    client = registry.get(index)

    # ⚠️ 销毁必须排在 plan_power **之前**。
    #
    #    plan_power 只认 POWER_ACTIONS（start/stop/softstop/reboot/reset），
    #    传 "terminate" 会直接抛 ValueError("未知动作 'terminate'")。
    #    2026-09-20 生产实测：把这段放在 plan_power 之后 → 它**永远走不到**，
    #    点「🗑 销毁」按钮只会回一句「❌ 未知动作 'terminate'，可选：...」。
    #    销毁和开关机是两套不同的服务函数，别让前者的校验拦住后者。
    if action == "terminate":
        try:
            plan = await asyncio.to_thread(compute_svc.plan_terminate, client, instance_id)
        except Exception as exc:  # noqa: BLE001
            await _edit(query, f"❌ {str(exc)[:400]}", kb.back_to_main())
            return
        pending = _new_pending(
            store, chat_id, update.effective_user.id,
            kind="terminate", account_index=index,
            summary=plan.render(),
            payload={"instance_id": instance_id,
                     "preserve_boot_volume": plan.preserve_boot_volume},
            destructive=True,
        )
        if not plan.can_execute:
            await _edit(query, plan.render(), kb.back_to_main())
            store.drop_pending(pending.token)
            return
        await _edit(query, plan.render() + "\n\n确认要销毁吗？",
                    kb.confirm(f"im:{index}:{token}", pending.token))
        return

    try:
        view, oci_action = await asyncio.to_thread(
            compute_svc.plan_power, client, instance_id, action)
    except Exception as exc:  # noqa: BLE001
        await _edit(query, f"❌ {str(exc)[:400]}", kb.back_to_main())
        return

    names = {"start": "开机", "stop": "关机", "softstop": "软关机",
             "reboot": "重启", "reset": "硬重启"}
    pending = _new_pending(
        store, chat_id, update.effective_user.id,
        kind="power", account_index=index,
        summary=f"{names.get(action, action)} {view.display_name}",
        payload={"action": action, "instance_id": instance_id,
                 "name": view.display_name},
        destructive=action in ("stop", "softstop", "reboot", "reset"),
    )
    body = "\n".join([
        f"⚡ *{names.get(action, action)}计划*",
        "",
        f"实例：`{R.esc(view.display_name)}`",
        f"当前状态：{view.lifecycle_state}",
        f"公网 IP：`{view.public_ip or '无'}`",
        "",
        "确认执行吗？",
    ])
    await _edit(query, body, kb.confirm(f"im:{index}:{token}", pending.token))


# --------------------------------------------------------------------------
#  命令：创建实例
# --------------------------------------------------------------------------
async def _launch_plan(message, registry: ClientRegistry, store: Store,
                       chat_id: int, user_id: int, index: int,
                       *, edit_query=None) -> None:
    client = registry.get(index)
    text = "⏳ 正在挑选可用域、查询配额、找镜像…"
    if edit_query is not None:
        await _edit(edit_query, text)
    else:
        message = await message.reply_text(text)

    try:
        plan = await asyncio.to_thread(compute_svc.plan_launch, client)
    except Exception as exc:  # noqa: BLE001
        body = f"❌ 无法生成创建计划：\n\n{str(exc)[:900]}"
        if edit_query is not None:
            await _edit(edit_query, body, kb.back_to_main())
        else:
            await _reply(message, body, kb.back_to_main())
        return

    pending = _new_pending(
        store, chat_id, user_id,
        kind="launch", account_index=index,
        summary=plan.render(),
        payload={"plan": plan},
    )
    body = plan.render() + "\n\n确认创建吗？"
    if edit_query is not None:
        await _edit(edit_query, body, kb.confirm(f"a:{index}", pending.token))
    else:
        await _reply(message, body, kb.confirm(f"a:{index}", pending.token))


@restricted
async def cmd_launch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if not args:
        await _reply(update.message, "选一个账号来开新机：",
                     kb.account_picker(settings.accounts, 0, "lp"))
        return
    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return
    await _launch_plan(update.message, registry, store,
                       update.effective_chat.id, update.effective_user.id, acc.index)


# --------------------------------------------------------------------------
#  命令：存储桶
# --------------------------------------------------------------------------
async def _show_buckets(message, registry: ClientRegistry, store: Store,
                        chat_id: int, index: int, *, edit_query=None) -> None:
    client = registry.get(index)
    text = "⏳ 正在列出存储桶并统计用量…"
    if edit_query is not None:
        await _edit(edit_query, text)
        placeholder = None
    else:
        placeholder = await message.reply_text(text)

    try:
        buckets = await asyncio.to_thread(storage_svc.list_buckets, client,
                                          with_stats=True)
        ns = await asyncio.to_thread(storage_svc.namespace, client)
    except Exception as exc:  # noqa: BLE001
        body = f"❌ {str(exc)[:600]}"
        if edit_query is not None:
            await _edit(edit_query, body, kb.back_to_main())
        else:
            await _reply(placeholder, body, kb.back_to_main())
        return

    header = (f"命名空间：`{ns[:12]}…`\n"
              f"（注意：命名空间按**租户**共享，同租户多个账号共用同一份 20 GiB）\n\n")
    body = header + R.render_bucket_list(buckets, client.account.label)
    pairs = [(store.put(chat_id, b.name), f"🪣 {b.name[:32]}") for b in buckets]
    markup = kb.bucket_list(index, pairs)

    if edit_query is not None:
        await _edit(edit_query, body, markup)
    else:
        await _reply(placeholder, body, markup)


@restricted
async def cmd_buckets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if not args:
        await _reply(update.message, "选一个账号看它的存储桶：",
                     kb.account_picker(settings.accounts, 0, "bl"))
        return
    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return
    await _show_buckets(update.message, registry, store,
                        update.effective_chat.id, acc.index)


@restricted
async def cmd_objects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, _ = ctx_of(context)
    args = context.args or []
    if len(args) < 2:
        await _reply(update.message, "用法：`/obj <账号> <桶名>`")
        return
    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return

    client = registry.get(acc.index)
    bucket = args[1]
    placeholder = await message_wait(update)
    try:
        objects = await asyncio.to_thread(storage_svc.list_objects, client, bucket)
    except Exception as exc:  # noqa: BLE001
        await _reply(placeholder, f"❌ {str(exc)[:600]}", kb.back_to_main())
        return
    await _reply(placeholder, R.render_bucket_objects(bucket, objects), kb.back_to_main())


async def message_wait(update: Update):
    return await update.message.reply_text("⏳ 查询中…")


# --------------------------------------------------------------------------
#  命令：审计
# --------------------------------------------------------------------------
async def _run_leak_audit(message, settings: Settings, registry: ClientRegistry,
                          indexes: list[int]) -> None:
    placeholder = await message.reply_text(
        f"⏳ 正在审计 {len(indexes)} 个账号的计费残留…")
    clients = [registry.get(i) for i in indexes]
    results = await asyncio.to_thread(
        parallel_map,
        lambda c: audit_svc.audit_leaks(c),
        clients,
        max_workers=settings.max_concurrency,
        on_error=scan_failed,
    )
    mapping = {registry.get(i).account.label: items
               for i, items in zip(indexes, results, strict=True)}
    await _reply(placeholder, audit_svc.render_leaks(mapping), kb.back_to_main())


@restricted
async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, _ = ctx_of(context)
    args = context.args or []
    if args:
        try:
            acc = _account(settings, args[0])
        except ValueError as exc:
            await _reply(update.message, f"❌ {exc}")
            return
        await _run_leak_audit(update.message, settings, registry, [acc.index])
    else:
        await _run_leak_audit(update.message, settings, registry,
                              [a.index for a in settings.accounts])


async def _run_security_scan(message, settings: Settings, registry: ClientRegistry,
                             indexes: list[int]) -> None:
    placeholder = await message.reply_text(
        f"⏳ 正在扫描 {len(indexes)} 个账号的安全暴露面…")
    clients = [registry.get(i) for i in indexes]

    exposures = await asyncio.to_thread(
        parallel_map,
        lambda c: security_svc.scan_ssh_exposure(c),
        clients, max_workers=settings.max_concurrency,
        on_error=scan_failed,
    )
    plaintext = await asyncio.to_thread(
        parallel_map,
        lambda c: security_svc.scan_plaintext_secrets(c),
        clients, max_workers=settings.max_concurrency,
        on_error=scan_failed,
    )

    exp_map = {registry.get(i).account.label: v
               for i, v in zip(indexes, exposures, strict=True)}
    pt_map = {registry.get(i).account.label: v
              for i, v in zip(indexes, plaintext, strict=True)}

    text = security_svc.render_exposure(exp_map) + "\n\n" + \
        security_svc.render_plaintext(pt_map) + "\n\n" + \
        "收紧 22 端口：`/harden <账号> <允许的CIDR> [安全列表名]`"
    await _reply(placeholder, text, kb.back_to_main())


@restricted
async def cmd_security(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, _ = ctx_of(context)
    args = context.args or []
    if args:
        try:
            acc = _account(settings, args[0])
        except ValueError as exc:
            await _reply(update.message, f"❌ {exc}")
            return
        await _run_security_scan(update.message, settings, registry, [acc.index])
    else:
        await _run_security_scan(update.message, settings, registry,
                                 [a.index for a in settings.accounts])


@restricted
async def cmd_harden(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/harden <账号> <允许的CIDR> [安全列表名]``"""
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if len(args) < 2:
        await _reply(update.message,
                     "用法：`/harden <账号> <允许的CIDR> [安全列表名]`\n\n"
                     "例：`/harden 15 203.0.113.7/32`\n"
                     "CIDR 填你固定的出口 IP。不填安全列表名时，"
                     "会把该账号**所有**对全网开放 22 端口的规则都收紧。")
        return

    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return
    allowed_cidr = args[1]
    sl_name = args[2] if len(args) > 2 else None

    if allowed_cidr in ("0.0.0.0/0", "::/0"):
        await _reply(update.message, "❌ CIDR 不能是全网地址，那样等于没加固。")
        return

    client = registry.get(acc.index)
    placeholder = await update.message.reply_text("⏳ 正在生成收紧计划…")

    if sl_name is None:
        # 未指定安全列表：找出该账号所有命中的安全列表，逐个生成计划
        try:
            exposures = await asyncio.to_thread(security_svc.scan_ssh_exposure, client)
        except Exception as exc:  # noqa: BLE001
            await _reply(placeholder, f"❌ {str(exc)[:400]}")
            return
        names = sorted({e.resource_name for e in exposures if e.kind == "security_list"})
        if not names:
            await _reply(placeholder, "✅ 没有需要对全网开放 22 端口的安全列表。",
                         kb.back_to_main())
            return
        if len(names) > 1:
            listing = "\n".join(f"  · `{n}`" for n in names)
            await _reply(
                placeholder,
                f"该账号有 {len(names)} 个安全列表存在全网开放的 22 端口：\n{listing}\n\n"
                f"请指定要改哪一个（一次只改一个，避免一次性改坏）：\n"
                f"`/harden {acc.index} {allowed_cidr} <安全列表名>`",
                kb.back_to_main(),
            )
            return
        sl_name = names[0]

    try:
        plan = await asyncio.to_thread(
            security_svc.plan_harden, client,
            security_list_name=sl_name, allowed_cidr=allowed_cidr)
    except Exception as exc:  # noqa: BLE001
        await _reply(placeholder, f"❌ {str(exc)[:500]}", kb.back_to_main())
        return

    if not plan.has_changes:
        await _reply(placeholder, "✅ 该安全列表没有对全网开放的 22 端口规则，无需改动。",
                     kb.back_to_main())
        return

    pending = _new_pending(
        store, update.effective_chat.id, update.effective_user.id,
        kind="harden", account_index=acc.index,
        summary=plan.render(), payload={"plan": plan},
    )
    await _reply(placeholder, plan.render() + "\n\n确认执行吗？",
                 kb.confirm(f"a:{acc.index}", pending.token))


# --------------------------------------------------------------------------
#  🚀 开机向导 —— 状态机辅助函数（wz* 路由共用）
#  状态存在 store.get_wizard(chat_id)：跨多轮按钮/文本传递参数，
#  每一步只改一个字段，最后一步汇总确认。取消即清空。
# --------------------------------------------------------------------------

def _wz_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict[str, Any]:
    """取当前向导状态（保证 account_index 存在）。"""
    _, _, store = ctx_of(context)
    st = store.get_wizard(chat_id) or {}
    if "account_index" not in st:
        raise ValueError("向导还没选账号 —— 请从「2. 开机 / 自动抢机」重新开始")
    return st


def _wz_account(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    """向导里反复用的取账号号。"""
    return int(_wz_state(context, chat_id)["account_index"])


async def _send_login_key(message: Any, key: LoginKey) -> None:
    """把私钥 + 公钥两个文件作为 Telegram 附件发给用户。

    ⚠️ 私钥**经由 Telegram 传输** —— 这是本功能固有的暴露面
       （和救援控制台的私钥一样，项目里已有先例）。
       用户点「让 Bot 生成一对新的」就是接受了这一点，
       但文案里要**明说**，不能悄悄发。
    """
    items = (
        (key.private_path, key.private_filename,
         "🔐 *私钥* —— 请 `chmod 600` 保存，登录时 `ssh -i` 指定它"),
        (key.public_path, f"{key.private_filename}.pub",
         "🔑 *公钥* —— 已注入实例的 authorized_keys，可留作核对"),
    )
    for path, name, caption in items:
        with open(path, "rb") as fh:
            await message.chat.send_document(
                document=InputFile(fh, filename=name),
                caption=f"{caption}\n指纹 `{key.fingerprint}`",
                parse_mode=ParseMode.MARKDOWN,
            )


def _safe_fp(public_line: str) -> str:
    """算公钥指纹；格式古怪就退化成 blob 前 24 字符，**绝不抛异常**。

    实例 metadata 里的公钥是历史留下的，可能是手抄错的、缺段的行。
    为了展示一行诊断信息就把整个操作炸掉，是把展示问题升级成功能故障。
    """
    try:
        return fingerprint_of(public_line)
    except ValueError:
        blob = blob_of(public_line)
        return f"{blob[:24]}…" if blob else "（无法解析的公钥行）"


#: 失败原因块的字数上限。够放下 `describe_service_error` 的三行
#: （错误码 / 原始信息 / 建议），又不至于把消息撑爆。
_FAILURE_REASON_LIMIT = 600


def _failure_reason(last_error: str | None) -> str:
    """渲染「失败原因」—— **保留完整多行文本**，不做单行截断。

    ⚠️ 2026-09-21 实测的 bug（踩坑 #59）：这里原来写的是
       ``(task.last_error or '未知')[:400]``，而 `last_error` 在
       `grab.py` 里被 ``splitlines()[0]`` 截成了单行 ——
       于是 `describe_service_error()` 精心构造的
       「原始信息 / 建议」两行**永远到不了用户眼前**。

       用户只看到「OCI 接口返回错误：InternalError (HTTP 500)」，
       而真正的原因「Out of host capacity」（该 AD 没容量）被丢掉了 ——
       一个**本来能自解释**的错误变成了需要来问我的问题。

       注意：单行槽位（任务列表、日志）要用 `errors.one_line`，
       不能图省事在这里截断。
    """
    text = (last_error or "").strip()
    if not text:
        return "未知（任务没记录到错误详情）"
    return text[:_FAILURE_REASON_LIMIT]


def _failure_tail(last_error: str | None) -> str:
    """失败页结尾的**兜底提示** —— 只在原因里没有「建议：」时才加。

    `describe_service_error()` 已经按错误码给了具体建议（例如
    「该 AD 没容量 → 用自动抢机」）。这时候再补一句泛泛的
    「若提示额度为 0，去看配额」，轻则冗余、重则**误导**
    （用户会去查一个跟问题无关的配额）—— 见踩坑 #38：
    **一句会误导人的提示，比没有提示更糟。**
    """
    if "建议：" in (last_error or ""):
        return "（上面已给出针对这个错误码的建议。）"
    return ("提示：若提示额度为 0，用「4. 配额查询」看逐 AD 余量；"
            "或改用「自动抢机」等别人释放。")


def _wz_key_source(st: dict[str, Any]) -> str:
    """向导状态 → 公钥来源（``own`` / ``gen`` / 空串）。

    ⚠️ **第一件事就是排除「不是 key 模式」**，而不是先看 ``key_source``。

       2026-09-21 测试抓到的真 bug：原来写成
       ``if key_source == "gen" and ssh_public_key: return "gen"``，
       **没检查 ``login_mode``**。于是「先选了生成、又改回用户名+密码」
       这种状态残留会让 spec 同时带上 ``ssh_public_key`` 和 ``user_data`` ——
       两条登录路径一起注入，出了事谁也说不清哪条生效。

       这个判断是最后一道防线：向导的按钮回调里也会清状态，
       但**不能只靠回调清得干净**（状态可能从别的路径进来）。
    """
    if st.get("login_mode") != "key":
        return ""
    if st.get("key_source") == "gen" and st.get("ssh_public_key"):
        return "gen"
    return "own"


def _wz_build_spec(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict[str, Any]:
    """把向导状态编译成 grab.py / plan_launch 能吃的 spec。

    校验放在这里：用户选了 A1 但没给核数/内存、用户名格式不合法等，
    **在点「现在开机」之前**就报错 —— 别等到真正调 API 才发现参数烂了。
    """
    st = _wz_state(context, chat_id)
    spec: dict[str, Any] = {
        "account_index": int(st["account_index"]),
        "count": int(st.get("count", 1)),
        # ⚠️ 用常量，别写死数字。这里原来写的是 `47` ——
        #    OCI 抬了下限之后 `models.BOOT_VOLUME_GB` 改成了 50，而这一处
        #    不会跟着变，等于在向导里埋了第二个「过时的默认值」。
        #    （同类问题见踩坑清单 #29：同一份值有两处实现，必然漂移。）
        "boot_volume_gb": int(st.get("boot_volume_gb", BOOT_VOLUME_GB)),
        "retry": False,   # wzgo:<interval> 时由执行方改写
    }

    shape = st.get("shape") or ""
    if "A1" in shape.upper():
        spec["shape"] = A1_FLEX_SHAPE
        ocpus = int(st.get("ocpus", 0) or 0)
        mem_gb = float(st.get("memory_gb", 0) or 0)
        if not (1 <= ocpus <= 4):
            raise ValueError(f"A1.Flex 的核数必须在 1~4，当前是 {ocpus or '未选'}")
        if not (0.5 * ocpus <= mem_gb <= 8.0 * ocpus):
            raise ValueError(
                f"内存 {mem_gb:g} GB 超出 A1.Flex {ocpus} 核的合法范围 "
                f"{0.5 * ocpus:.1f}~{8.0 * ocpus:.0f} GB（每核 0.5~8 GB）")
        spec["ocpus"] = ocpus
        spec["memory_gb"] = int(mem_gb)
    else:
        spec["shape"] = E2_MICRO_SHAPE

    if st.get("operating_system"):
        spec["image_os"] = st["operating_system"]

    # 登录方式：pwd → cloud-init（用户名/密码）；key → 默认 SSH 公钥逻辑
    if st.get("login_mode") == "pwd":
        username = (st.get("username") or "").strip()
        password = st.get("password") or ""
        if not username:
            raise ValueError("选了「用户名+密码」但没给用户名，请重走登录方式那一步")
        cfg_text = build_cloud_init(username=username, password=password or None)
        user_data_b64 = to_user_data(cfg_text)
        if not user_data_b64:
            raise ValueError("cloud-init 配置为空 —— 用户名/密码都没生效，检查输入")
        spec["user_data_b64"] = user_data_b64
        # ⚠️ 把用户名单独带出来，只为了**在确认页显示**。
        #    2026-09-20 用户问「新开的机器登录用户名密码是什么」——
        #    确认页原来只写「用户名+密码」，用户没法核对，事后也无从查起。
        #    密码**不进 spec**：它会进日志、进任务快照、进内存里的 GrabTask，
        #    每多一份都是多一个泄漏面。用户是自己刚输入的，忘了可以看聊天记录。
        #    （这一项不在 try_launch_once 的白名单里，不会传到 plan_launch。）
        spec["login_user"] = username

    # 公钥来源：只在 key 模式下有意义。
    # 「gen」时把**公钥文本**带进 spec —— try_launch_once 会把它交给
    # plan_launch 覆盖掉默认的解析链（见 services/grab.py 的白名单）。
    # 私钥**绝不进 spec**：spec 会进日志、进任务快照、进内存里的 GrabTask，
    # 每多一份都是多一个泄漏面（和密码那条同样的理由）。
    key_source = _wz_key_source(st)
    if key_source == "gen":
        spec["ssh_public_key"] = st["ssh_public_key"]
        spec["key_fingerprint"] = st.get("key_fingerprint") or ""
        spec["key_source"] = "gen"
    elif key_source == "own":
        spec["key_source"] = "own"

    if st.get("assign_ipv6"):
        spec["assign_ipv6"] = True

    return spec


def _wz_render_summary(spec: dict[str, Any], account_label: str) -> str:
    """汇总页文本（执行前最后确认用）。"""
    shape = spec["shape"].split(".")[-1] if "A1" not in spec["shape"] else \
        f"A1.Flex {spec.get('ocpus', '?')}C/{spec.get('memory_gb', '?')}G"
    lines = [
        f"*开机计划摘要*　账号 **{account_label}**",
        "",
        f"规格：`{shape}`",
    ]
    if spec.get("image_os"):
        lines.append(f"系统：{spec['image_os']}")
    else:
        lines.append("系统：（按账号默认镜像）")
    lines.append(f"引导卷：**{spec['boot_volume_gb']} GB**　数量：**{spec['count']} 台**")
    if spec.get("user_data_b64"):
        who = spec.get("login_user")
        lines.append(
            f"登录：用户名 `{R.esc(who)}` + 密码（cloud-init 注入，首启即用）"
            if who else "登录：用户名+密码（cloud-init 注入，首启即用）"
        )
        lines.append("　　（密码就是你刚输入的那个；它只存在这台机器上，我们不保存）")
    else:
        if spec.get("key_source") == "gen":
            fp = spec.get("key_fingerprint") or "（未取到指纹）"
            lines.append("登录：SSH 公钥（**Bot 生成的账号专属密钥对**）")
            lines.append(f"　　指纹 `{fp}`　·　私钥已作为附件发你，`ssh -i` 用")
        else:
            lines.append("登录：SSH 公钥（用你配置的那把 key_file）")
    lines.append("公网 IPv4：自动分配" + ("　·　IPv6：从子网通告 CIDR 分配" if spec.get("assign_ipv6") else "　·　IPv6：未开启"))
    lines += [
        "",
        "⚠️ 免费额度绑在单个可用域，系统会自动挑余量最足的 AD。",
        "⚠️ 若安全列表对公网开放 22 端口，这台机器会立刻暴露在扫描之下（/security 可查）。",
    ]
    return "\n".join(lines)


async def _wz_summary(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """汇总页：渲染计划 + 「现在开机 / 自动抢机」执行选项。"""
    settings, _, store = ctx_of(context)   # noqa: F841 —— store 供后续步骤用
    try:
        spec = _wz_build_spec(context, chat_id)
    except ValueError as exc:
        await query.answer("参数有问题")
        await _edit(query, f"❌ 参数校验没通过：\n{exc}\n\n从「2. 开机」重新走一遍。", kb.main_menu())
        return
    acc = settings.account(spec["account_index"])
    body = _wz_render_summary(spec, acc.label) + "\n\n" + (
        "**选一种执行方式：**\n"
        "· ✅ 单次 —— 每台只试一次，失败就收尾汇报（不空转烧 API）\n"
        "· 🔄 自动抢机 —— 每隔 N 秒重试一轮，直到抢满或你手动停"
        "（「6. 任务管理」可看进度 / 停）"
    )
    await query.answer()
    await _edit(query, body, kb.wizard_summary(interval_opts=True))


async def _wz_execute(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      user_id: int, interval_seconds: int) -> None:
    """汇总页的「现在开机 / 自动抢机」—— 真正执行。

    · ``interval_seconds == 0``：单次模式 —— 每台只试一次，失败就收尾汇报（不空转烧 API）
    · ``> 0``：注册进任务表 + 起后台循环，抢到 count 台为止或手动停（「6. 任务管理」可看/停）

    校验错误（ValueError / NotAllowedError）**抛给调用方**，由 wzgo 分支统一兜底展示。
    """
    settings, registry, store = ctx_of(context)
    spec = _wz_build_spec(context, chat_id)   # ValueError 会往上抛 —— 参数烂就别碰 API
    acc = settings.account(spec["account_index"])

    def _ensure() -> None:
        ensure_writable(settings)   # grab 循环每轮执行前调它（DRY-RUN 时在这里被拦下）

    if interval_seconds > 0:
        # ---- 自动抢机模式：后台循环，直到齐数或手动停 ----
        reg = tasks_reg(context)
        task = reg.create(chat_id, user_id, spec["account_index"], dict(spec), interval_seconds)
        ensure_writable(settings)   # 写开关关 → 现在就被拦下，别起个马上 blocked 的任务
        reg.start(task.token, settings=settings, registry=registry, ensure_writable=_ensure)
        store.del_wizard(chat_id)   # 参数已冻结进任务快照；想改就停掉重开
        await query.answer("抢机任务已启动")
        await _edit(query,
                    f"🔄 *自动抢机任务* `{task.token}` 已启动\n\n"
                    f"目标：{acc.label} × **{spec['count']}** 台 · 每 {interval_seconds}s 一轮\n"
                    "策略：抢到一台记一台，失败就下轮再试，直到齐数或你手动停。\n\n"
                    "随时在「6. 任务管理」里看进度 / 停止。",
                    kb.main_menu())
        return

    # ---- 单次模式：逐台尝试一次，失败不重试 ----
    from ..services import grab as grab_svc
    task = grab_svc.GrabTask(token="oneshot", chat_id=chat_id, user_id=user_id,
                             account_index=spec["account_index"], spec=dict(spec),
                             interval_seconds=0)
    await grab_svc.run_grab_loop(task, settings, registry, _ensure)

    if task.status == "succeeded":
        body = (f"✅ *开机成功*（{acc.label}）\n\n实例：\n"
                + "\n".join(f"  · {n}" for n in task.result_names)
                + "\n\n公网 IP 见「3. 实例管理」。")
    elif task.done_count > 0:
        body = (f"⚠️ *部分成功*（{task.done_count}/{task.want} 台，{acc.label}）\n\n"
                + "\n".join(f"  · {n}" for n in task.result_names)
                + f"\n\n最后一次失败：\n{_failure_reason(task.last_error)}\n\n"
                "可改用「自动抢机」继续补齐。")
    else:
        body = (f"❌ *开机失败*（{acc.label}）\n\n原因：\n{_failure_reason(task.last_error)}\n\n"
                + _failure_tail(task.last_error))
    store.del_wizard(chat_id)
    await query.answer()
    await _edit(query, body, kb.main_menu())


# --------------------------------------------------------------------------
#  按钮路由
# --------------------------------------------------------------------------
@restricted
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings, registry, store = ctx_of(context)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    data = query.data or ""

    parts = data.split(":")
    head = parts[0]

    try:
        # ---- 空动作 / 主菜单 / 状态 ----
        if head == "x":
            await query.answer()
            return
        if head == "m":
            await query.answer()
            warnings = context.bot_data.get("startup_warnings", [])
            await _edit(query, R.welcome(settings, warnings), kb.main_menu())
            return
        if head == "status":
            await query.answer()
            warnings = context.bot_data.get("startup_warnings", [])
            await _edit(query, R.status_text(settings, warnings), kb.main_menu())
            return

        # ---- 确认 / 取消 ----
        if head in ("ok", "no"):
            token = parts[1] if len(parts) > 1 else ""
            pending = store.get_pending(token)
            if pending is None:
                await query.answer("操作已过期，请重新发起", show_alert=True)
                return
            if pending.chat_id != chat_id:
                await query.answer("⛔ 这个操作不属于当前会话", show_alert=True)
                return
            if not store.claim(chat_id, token):
                await query.answer("该操作已执行过", show_alert=True)
                return

            if head == "no":
                store.drop_pending(token)
                await query.answer("已取消")
                await _edit(query, f"❌ 已取消。\n\n{pending.summary}", kb.back_to_main())
                return

            await query.answer("执行中…")
            await _edit(query, f"⏳ 执行中…\n\n{pending.summary}")
            result = await execute_pending(pending, settings, registry)
            store.drop_pending(token)
            await _edit(query, result, kb.back_to_main())
            return

        # ---- 账号选择器翻页 ----
        if head == "ap":
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            action = parts[2] if len(parts) > 2 else "a"
            await query.answer()
            titles = {"a": "选一个账号：", "il": "选账号看实例：",
                      "bl": "选账号看存储桶：", "q": "选账号看配额：",
                      "lp": "选账号开新机："}
            await _edit(query, titles.get(action, "选一个账号："),
                        kb.account_picker(settings.accounts, page, action))
            return

        # ---- 配额总览 ----
        if head == "q" and len(parts) == 1:
            await query.answer("查询中…")
            await _edit(query, "⏳ 正在查询全部账号配额…")
            caps = await asyncio.to_thread(
                parallel_map, lambda c: quota_svc.account_capacity(c),
                registry.all(), max_workers=settings.max_concurrency,
                on_error=lambda c, exc: AccountCapacity(
                    index=c.account.index, label=c.account.label,
                    region=c.account.region, error=str(exc)[:300]),
            )
            text = quota_svc.summarize(caps) + "\n\n点按钮看单个账号的逐 AD 明细。"
            await _edit(query, text, kb.account_picker(settings.accounts, 0, "q"))
            return

        if head == "q" and len(parts) == 2:
            index = int(parts[1])
            await query.answer("查询中…")
            await _edit(query, "⏳ 查询中…")
            client = registry.get(index)
            cap = await asyncio.to_thread(quota_svc.account_capacity, client)
            await _edit(query, quota_svc.explain_capacity(cap), kb.account_menu(index))
            return

        # ---- 账号菜单 ----
        if head == "a":
            index = int(parts[1])
            await query.answer()
            acc = settings.account(index)
            mode = "实例主体认证" if acc.auth_mode == "instance_principal" else "API Key 认证"
            body = (f"☁️ *{R.esc(acc.alias or '账号')}*  `{acc.index}`\n\n"
                    f"区域：`{acc.region}`\n认证：{mode}\n"
                    f"默认规格：`{acc.default_shape}`")
            await _edit(query, body, kb.account_menu(index))
            return

        # ---- 实例 ----
        if head == "il":
            index = int(parts[1])
            await query.answer("查询中…")
            await _show_instances(None, registry, store, chat_id, index,
                                  edit_query=query)
            return

        if head == "im":
            index = int(parts[1])
            token = parts[2]
            instance_id = store.get(chat_id, token)
            if not instance_id:
                await query.answer("会话已过期，请重新打开列表", show_alert=True)
                return
            await query.answer()
            client = registry.get(index)
            view = await asyncio.to_thread(compute_svc.get_instance, client, instance_id)
            await _edit(query, R.render_instance(view),
                        kb.instance_menu(index, token, view.is_running))
            return

        if head == "ip":
            index = int(parts[1])
            token = parts[2]
            action = parts[3]
            if action == "rescue":   # 🚑 救援：开 VNC 控制台（只读诊断，不算写操作）
                instance_id = store.get(chat_id, token)
                if not instance_id:
                    await query.answer("会话已过期，请重新打开实例列表", show_alert=True)
                    return
                client = registry.get(index)
                await query.answer("正在开救援控制台…")
                try:
                    conn = await asyncio.to_thread(
                        compute_svc.open_rescue_console, client, instance_id)
                except Exception as exc:   # noqa: BLE001
                    log.exception("救援控制台开通失败")
                    await _edit(query, f"❌ 打开失败：\n{str(exc)[:400]}", kb.back_to_main())
                    return
                await _edit(query, conn.render(), kb.back_to_main())
                return

            if action == "key":   # 🔑 下载本账号的登录密钥（**先核对指纹**）
                instance_id = store.get(chat_id, token)
                if not instance_id:
                    await query.answer("会话已过期，请重新打开实例列表", show_alert=True)
                    return
                settings, _, _ = ctx_of(context)
                await query.answer("核对中…")
                try:
                    key = await asyncio.to_thread(load_login_key, settings, index)
                except Exception as exc:   # noqa: BLE001 —— 落盘损坏/权限问题都可能炸
                    log.exception("读取登录密钥失败")
                    await _edit(query, f"❌ 读取登录密钥失败：\n{str(exc)[:400]}", kb.back_to_main())
                    return
                if key is None:
                    await _edit(query,
                                "📭 这个账号还没有 Bot 生成的登录密钥。\n\n"
                                "开机向导里选「🔑 仅 SSH 公钥 → 🆕 让 Bot 生成一对新的」"
                                "时才会生成（每个账号一对，之后复用）。",
                                kb.back_to_main())
                    return

                # ⚠️ 用**证据**回答「这把钥匙是不是这台机器的」，而不是免责声明。
                #    实例 metadata 里就存着 ssh_authorized_keys，读出来比一下即可。
                client = registry.get(index)
                try:
                    authorized = await asyncio.to_thread(
                        compute_svc.instance_authorized_keys, client, instance_id)
                except Exception as exc:   # noqa: BLE001
                    log.exception("读实例 authorized_keys 失败")
                    await _edit(query,
                                f"⚠️ 取不到这台实例的 authorized_keys，**无法核对**：\n"
                                f"{str(exc)[:300]}\n\n"
                                "文件仍会发给你，但请自行确认它适用于这台机器。",
                                kb.back_to_main())
                    await _send_login_key(query.message, key)
                    return

                if key.public_blob and key.public_blob in {blob_of(k) for k in authorized}:
                    await _edit(query,
                                "✅ *指纹核对通过* —— 这把钥匙就是这台机器的。\n\n"
                                f"指纹：`{key.fingerprint}`",
                                kb.back_to_main())
                else:
                    others = "\n".join(f"  · `{_safe_fp(k)}`" for k in authorized) \
                        or "  （实例 metadata 里没有任何公钥）"
                    await _edit(query,
                                "⚠️ *指纹不匹配* —— 这台机器用的不是这把钥匙。\n\n"
                                f"本账号 Bot 密钥：`{key.fingerprint}`\n"
                                f"实例上实际注入的：\n{others}\n\n"
                                "多半是这台机器开机时选的是「🔑 用我配置的公钥」。\n"
                                "文件还是会发给你，但它**登不进这台机器**。",
                                kb.back_to_main())
                await _send_login_key(query.message, key)
                return

            await _power_plan(update, context, index, token, action)
            return

        # ---- 开新机 ----
        if head == "lp":
            index = int(parts[1])
            await query.answer("生成计划中…")
            await _launch_plan(None, registry, store, chat_id, user_id, index,
                               edit_query=query)
            return

        # ---- 开机向导入口（主菜单「2. 开机」→ 选完账号后到这里）----
        if head == "wz":
            index = int(parts[1])
            store.put_wizard(chat_id, {"account_index": index})
            await query.answer()
            await _edit(query, "🧠 *开机向导*　第 1 步：选 vCPU / 规格类型", kb.wizard_shape())
            return

        # ---- 帮助 ----
        if head == "hlp":
            await query.answer()
            await _edit(query, R.help_text(), kb.main_menu())
            return

        # ---- 存储桶 ----
        if head == "bl":
            index = int(parts[1])
            await query.answer("查询中…")
            await _show_buckets(None, registry, store, chat_id, index, edit_query=query)
            return

        if head == "bm":
            index = int(parts[1])
            token = parts[2]
            bucket = store.get(chat_id, token)
            if not bucket:
                await query.answer("会话已过期，请重新打开列表", show_alert=True)
                return
            await query.answer()
            client = registry.get(index)
            try:
                versioning = await asyncio.to_thread(
                    storage_svc.get_versioning, client, bucket)
                pars = await asyncio.to_thread(
                    storage_svc.list_preauth_requests, client, bucket)
                objects = await asyncio.to_thread(
                    storage_svc.list_objects, client, bucket, raise_on_unknown=True)
            except Exception as exc:  # noqa: BLE001
                await _edit(query, f"❌ {str(exc)[:500]}", kb.back_to_main())
                return
            body = "\n".join([
                f"🪣 *{R.esc(bucket)}*",
                "",
                f"对象数：{len(objects)}",
                f"版本控制：{versioning}",
                f"预认证请求：{len(pars)} 个"
                + ("（删桶时会自动清理）" if pars else ""),
            ])
            await _edit(query, body, kb.bucket_menu(index, token))
            return

        if head == "ob":
            index = int(parts[1])
            token = parts[2]
            bucket = store.get(chat_id, token)
            if not bucket:
                await query.answer("会话已过期", show_alert=True)
                return
            await query.answer("查询中…")
            await _edit(query, "⏳ 正在列出对象…")
            client = registry.get(index)
            try:
                objects = await asyncio.to_thread(
                    storage_svc.list_objects, client, bucket)
            except Exception as exc:  # noqa: BLE001
                await _edit(query, f"❌ {str(exc)[:500]}", kb.back_to_main())
                return
            await _edit(query, R.render_bucket_objects(bucket, objects),
                        kb.bucket_menu(index, token))
            return

        if head == "bp":
            index = int(parts[1])
            token = parts[2]
            bucket = store.get(chat_id, token)
            if not bucket:
                await query.answer("会话已过期", show_alert=True)
                return
            await query.answer("复核中…")
            await _edit(query, "⏳ 正在实时复核桶内容…")
            client = registry.get(index)
            try:
                plan = await asyncio.to_thread(
                    storage_svc.plan_delete_bucket, client, bucket, force=True)
            except Exception as exc:  # noqa: BLE001
                await _edit(query, f"❌ {str(exc)[:500]}", kb.back_to_main())
                return
            if not plan.can_execute:
                await _edit(query, plan.render(), kb.back_to_main())
                return
            pending = _new_pending(
                store, chat_id, user_id,
                kind="bucket_delete", account_index=index,
                summary=plan.render(), payload={"plan": plan}, destructive=True,
            )
            await _edit(query, plan.render() + "\n\n确认删除吗？",
                        kb.confirm(f"bm:{index}:{token}", pending.token))
            return

        # ---- S3 密钥 ----
        if head == "sk":
            index = int(parts[1])
            await query.answer()
            pending = _new_pending(
                store, chat_id, user_id,
                kind="s3_create", account_index=index,
                summary=f"签发 S3 兼容密钥（账号 {index}）",
                payload={"display_name": f"oracles-bot-{user_id}"},
            )
            body = "\n".join([
                "🔑 *签发 S3 兼容密钥*",
                "",
                f"账号：`{index}`",
                "",
                "⚠️ Secret Key **只在创建那一刻返回一次**，之后任何 API 都读不回来。",
                "⚠️ 刚创建的密钥有 5~8 分钟传播延迟，期间会间歇性报",
                "   SignatureDoesNotMatch —— 这是正常的，不是密钥错。",
                "",
                "确认签发吗？",
            ])
            await _edit(query, body, kb.confirm(f"a:{index}", pending.token))
            return

        # ---- 审计 ----
        if head == "au":
            await query.answer("审计中…")
            await _edit(query, "⏳ 正在审计计费残留…")
            indexes = ([int(parts[1])] if len(parts) > 1
                       else [a.index for a in settings.accounts])
            clients = [registry.get(i) for i in indexes]
            # ⚠️ on_error 必须返回 None 而不是 []。
            #    返回 [] 的话，失败的账号渲染出来和"该账号很干净"一模一样 ——
            #    这里漏掉的可能是正在持续扣费的未绑定公网 IP。
            #    None = "结果未知"，渲染层会明确标出来。
            results = await asyncio.to_thread(
                parallel_map, lambda c: audit_svc.audit_leaks(c), clients,
                max_workers=settings.max_concurrency, on_error=scan_failed)
            mapping = {registry.get(i).account.label: v
                       for i, v in zip(indexes, results, strict=True)}
            await _edit(query, audit_svc.render_leaks(mapping), kb.back_to_main())
            return

        if head == "se":
            await query.answer("扫描中…")
            await _edit(query, "⏳ 正在扫描安全暴露面…")
            indexes = ([int(parts[1])] if len(parts) > 1
                       else [a.index for a in settings.accounts])
            clients = [registry.get(i) for i in indexes]
            exposures = await asyncio.to_thread(
                parallel_map, lambda c: security_svc.scan_ssh_exposure(c), clients,
                max_workers=settings.max_concurrency, on_error=scan_failed)
            plaintext = await asyncio.to_thread(
                parallel_map, lambda c: security_svc.scan_plaintext_secrets(c), clients,
                max_workers=settings.max_concurrency, on_error=scan_failed)
            exp_map = {registry.get(i).account.label: v
                       for i, v in zip(indexes, exposures, strict=True)}
            pt_map = {registry.get(i).account.label: v
                      for i, v in zip(indexes, plaintext, strict=True)}
            text = (security_svc.render_exposure(exp_map) + "\n\n"
                    + security_svc.render_plaintext(pt_map) + "\n\n"
                    + "收紧：`/harden <账号> <允许的CIDR> [安全列表名]`")
            await _edit(query, text, kb.back_to_main())
            return

        if head == "hd":
            index = int(parts[1])
            await query.answer()
            await _edit(
                query,
                f"🛡 收紧账号 `{index}` 的 22 端口\n\n"
                f"用命令指定允许的来源：\n"
                f"`/harden {index} <你的固定IP>/32 [安全列表名]`\n\n"
                f"例：`/harden {index} 203.0.113.7/32`\n\n"
                f"不指定安全列表名时，如果该账号只有一个命中的列表，"
                f"会自动选中它；有多个则列出来让你挑。",
                kb.back_to_main(),
            )
            return

        # ============================================================
        #  🚀 开机 / 抢机向导（wz*）—— 状态机存在 store 的 wizard 槽里
        # ============================================================
        if head == "wzs":   # 选规格类型：E2 | A1（向导第 1 步，账号来自入口 wz:<index>）
            index = _wz_account(context, chat_id)
            shape = parts[1]
            st = store.get_wizard(chat_id) or {"account_index": index}
            st.update({"shape": shape})
            # 换规格类型时清掉上一轮选的核数/内存（两种规格互不通用）
            st.pop("ocpus", None)
            st.pop("memory_gb", None)
            store.put_wizard(chat_id, st)
            if shape == "A1":
                await query.answer()
                await _edit(query, "🧠 选 vCPU 核数（ARM A1.Flex，免费上限 4 核）：", kb.wizard_a1_cpus())
            else:
                await query.answer()
                await _edit(query, "🐧 操作系统：", kb.wizard_os())
            return

        if head == "wza":   # A1 核数
            index = _wz_account(context, chat_id)
            ocpus = int(parts[1])
            store.put_wizard(chat_id, {"account_index": index, "shape": "A1", "ocpus": ocpus})
            await query.answer()
            await _edit(query, f"🧠 {ocpus} 核内存（每核 0.5~8 GB，免费额度内建议 ≤{ocpus * 6} GB）：", kb.wizard_a1_mem(ocpus))
            return

        if head == "wzm":   # A1 内存 → OS
            index = _wz_account(context, chat_id)
            st = store.get_wizard(chat_id) or {}
            mem_gb = int(parts[1])
            st.update({"memory_gb": mem_gb})
            store.put_wizard(chat_id, st)
            await query.answer()
            await _edit(query, "🐧 操作系统：", kb.wizard_os())
            return

        if head == "wzo":   # 操作系统 → 磁盘
            index = _wz_account(context, chat_id)
            os_idx = int(parts[1])
            st = store.get_wizard(chat_id) or {"account_index": index}
            name, os_value = kb.WIZARD_OS_CHOICES[os_idx]
            st["os_name"], st["operating_system"] = name, os_value
            store.put_wizard(chat_id, st)
            await query.answer()
            await _edit(query, f"💾 引导卷大小（{name}）：默认 50 GB", kb.wizard_disk())
            return

        if head == "wzv":   # 磁盘 → 数量
            index = _wz_account(context, chat_id)
            st = store.get_wizard(chat_id) or {"account_index": index}
            st["boot_volume_gb"] = int(parts[1])
            store.put_wizard(chat_id, st)
            await query.answer()
            await _edit(query, "🔢 开几台？（多台会依次尝试，名字自动加 -2/-3…）", kb.wizard_count())
            return

        if head == "wzn":   # 数量 → 登录方式
            index = _wz_account(context, chat_id)
            st = store.get_wizard(chat_id) or {"account_index": index}
            st["count"] = int(parts[1])
            store.put_wizard(chat_id, st)
            await query.answer()
            await _edit(query, "🔐 登录方式：", kb.wizard_login())
            return

        if head == "wzl":   # 登录方式
            index = _wz_account(context, chat_id)
            mode = parts[1]
            st = store.get_wizard(chat_id) or {"account_index": index}
            st["login_mode"] = mode
            if mode == "pwd":
                st.pop("username", None)
                st.pop("password", None)
                # 从「公钥」切到「密码」时把公钥状态清干净。
                # 不清的话 spec 里会同时带着 ssh_public_key 和 user_data ——
                # 两条登录路径一起注入，出了事谁也说不清哪条生效。
                st.pop("ssh_public_key", None)
                st.pop("key_fingerprint", None)
                st.pop("key_source", None)
                store.put_wizard(chat_id, st)
                store.set_awaiting(chat_id, "wz_user")
                await query.answer()
                await _edit(query, "👤 输入登录用户名（如 admin）：\n直接发文本即可。", kb.back_to_main())
            else:
                # ⚠️ 这里**不再直接进网络步骤** —— 公钥从哪来是用户要决定的事：
                #    用他自己配的那把，还是让 Bot 在服务器上生成一对。
                store.put_wizard(chat_id, st)
                await query.answer()
                await _edit(query, "🔑 公钥从哪来？", kb.wizard_key_source())
            return

        if head == "wzkb":   # 子步骤「上一步」→ 回登录方式
            await query.answer()
            await _edit(query, "🔐 登录方式：", kb.wizard_login())
            return

        if head == "wzk":   # 公钥来源：own（用配置的） | gen（Bot 生成）
            index = _wz_account(context, chat_id)
            source = parts[1]
            st = store.get_wizard(chat_id) or {"account_index": index}
            st["login_mode"] = "key"
            st["key_source"] = source
            if source == "gen":
                settings, _, _ = ctx_of(context)
                await query.answer("正在生成密钥…")
                try:
                    key = await asyncio.to_thread(ensure_login_key, settings, index)
                except Exception as exc:   # noqa: BLE001 —— 落盘/权限/半成品都可能炸
                    log.exception("生成登录密钥失败")
                    await _edit(query, f"❌ 生成登录密钥失败：\n{str(exc)[:400]}", kb.back_to_main())
                    return
                st["ssh_public_key"] = key.public_openssh
                st["key_fingerprint"] = key.fingerprint
                store.put_wizard(chat_id, st)
                # 先把文件发出去（生成即交付），再推进到下一步
                await _send_login_key(query.message, key)
                await _edit(query,
                            render_login_key(key, account_label=settings.account(index).label),
                            kb.wizard_network())
            else:
                st.pop("ssh_public_key", None)
                st.pop("key_fingerprint", None)
                store.put_wizard(chat_id, st)
                await query.answer()
                await _edit(query, "🌐 IPv6 地址（子网通告了 CIDR 时才会真正分配）：", kb.wizard_network())
            return

        if head == "wzv6":  # IPv6 → 汇总
            index = _wz_account(context, chat_id)
            st = store.get_wizard(chat_id) or {"account_index": index}
            st["assign_ipv6"] = (parts[1] == "on")
            store.put_wizard(chat_id, st)
            await query.answer()
            await _wz_summary(query, context, chat_id)
            return

        if head in ("wzb",):   # 上一步：回到 OS（简单起见不实现完整回退）
            index = _wz_account(context, chat_id)
            store.put_wizard(chat_id, {"account_index": index})
            await query.answer()
            await _edit(query, "🧠 选 vCPU / 规格类型：", kb.wizard_shape())
            return

        if head == "wzc":   # 取消向导
            store.del_wizard(chat_id)
            store.clear_awaiting(chat_id)
            await query.answer("已取消")
            await _edit(query, "❌ 已取消开机向导。", kb.main_menu())
            return

        if head == "wzgo":  # 「现在开机 / 自动抢机」—— 汇总页的执行按钮
            interval = int(parts[1]) if len(parts) > 1 else 0
            await query.answer("生成计划中…")
            await _edit(query, "⏳ 正在校验参数、挑选可用域…")
            try:
                await _wz_execute(query, context, chat_id, user_id, interval)
            except Exception as exc:   # noqa: BLE001 —— 参数错/写开关关，都别静默吞掉
                log.exception("开机执行失败")
                store.del_wizard(chat_id)
                await _edit(query, f"❌ 无法开机：\n\n{str(exc)[:800]}", kb.main_menu())
            return

        if head == "wzr":   # 「🔄 刷新」/占位，不应出现；兜底到主菜单
            await query.answer()
            await _edit(query, R.welcome(settings, []), kb.main_menu())
            return

        # ============================================================
        #  ⚙️ 配置管理（cfg*）—— API / 密钥的增删查
        # ============================================================
        if head == "cfg":
            await query.answer("体检中…")
            await _edit(query, "⏳ 正在检查各账号凭据链（私钥文件 / OCID / fingerprint）…")
            statuses = await asyncio.to_thread(acct_svc.check_accounts, settings)
            rows = {s.index: s for s in statuses}
            body = (f"⚙️ *配置管理*　{len(settings.accounts)} 个账号\n\n"
                    "点账号看凭据明细；「添加 / 删除」直接改 `accounts.json`（改动会备份）。")
            await _edit(query, body, kb.config_menu(settings.accounts, rows))
            return

        if head == "cfgi":   # 单个账号凭据详情
            index = int(parts[1])
            acc = settings.account(index)
            st = None
            for s in await asyncio.to_thread(acct_svc.check_accounts, settings, test_api=False):
                if s.index == index:
                    st = s
                    break
            def _mark(ok: bool | None) -> str:
                return "✅" if ok else ("❌" if ok is False else "·")
            lines = [
                f"🔑 *{R.esc(acc.alias or '账号')}*　`[{index}]`",
                "",
                f"区域：`{acc.region}`",
                f"认证：{'实例主体' if acc.auth_mode == 'instance_principal' else 'API Key'}",
                f"默认规格：`{acc.default_shape}`",
                "",
            ]
            if st:
                lines += [
                    f"{_mark(st.key_file_ok)} 私钥文件存在可读",
                    f"{_mark(st.fingerprint_set)} fingerprint 已填",
                    f"{_mark(st.ocids_complete)} user / tenancy OCID 齐全",
                    f"API 连通：{('✅' if st.api_reachable else '❌') if st.api_reachable is not None else '（未测）'}",
                ]
            await query.answer()
            # config_menu 需要 statuses；这里复用只读版即可
            from ..services.accounts_mgmt import KeyStatus
            fake = [KeyStatus(index=a.index, label=a.label, region=a.region,
                              key_file_ok=True, fingerprint_set=True, ocids_complete=True)
                    for a in settings.accounts]
            await _edit(query, "\n".join(lines), kb.config_menu(settings.accounts, {a.index: a for a in fake}))
            return

        if head == "cfgtest":   # 含 API 的重新体检
            await query.answer("联网体检中…")
            await _edit(query, "⏳ 正在逐个账号调一次最轻 API（列可用域）验证整条凭据链…")
            statuses = await asyncio.to_thread(acct_svc.check_accounts, settings, test_api=True)
            ok_n = sum(1 for s in statuses if s.api_reachable)
            bad_n = sum(1 for s in statuses if s.api_reachable is False)
            body = (f"🔁 联网体检完成：{ok_n} 通 / {bad_n} 不通\n\n"
                    "（「通」= 该账号的私钥+OCID+fingerprint 整条链能调 OCI API）")
            await _edit(query, body, kb.config_menu(settings.accounts, {s.index: s for s in statuses}))
            return

        if head == "cfgadd":   # 添加账号 → 等待文本 JSON
            store.set_awaiting(chat_id, "cfg_add")
            sample = ("{\n"
                      '  "index": 9,\n'
                      '  "alias": "新加坡-9",\n'
                      '  "region": "ap-singapore-1",\n'
                      '  "user": "ocid1.user.oc1..…",\n'
                      '  "tenancy": "ocid1.tenancy.oc1..…",\n'
                      '  "fingerprint": "aa:bb:cc…",\n'
                      '  "key_file": "/etc/oracles/keys/my9.pem"\n'
                      '}')
            await query.answer()
            body = (
                "➕ *添加账号*\n\n"
                "直接发一段 JSON（字段见下）。新账号的**私钥文件**得先放到服务器：\n"
                f"`{settings.home}/keys/` 里，并 `chmod 600`。\n\n"
                f"示例模板：\n```\n{sample}\n```\n\n"
                "⚠️ index 不能和现有重复；region 必须是 OCI 合法区域名。"
            )
            await _edit(query, body, kb.back_to_main())
            return

        if head == "cfgdel":   # 删除账号 → 选择器
            await query.answer()
            body = f"🗑 *删除账号*（共 {len(settings.accounts)} 个）\n\n点要删的那个。"
            await _edit(query, body, kb.account_delete_picker(settings.accounts))
            return

        if head == "cfd":   # 确认删某个账号
            index = int(parts[1])
            acc = settings.account(index)
            pending = _new_pending(
                store, chat_id, user_id, kind="account_remove", account_index=index,
                summary=f"从配置里移除账号 [{index}] {acc.alias or acc.region}",
                payload={"index": index}, destructive=True,
            )
            await query.answer()
            await _edit(query,
                        f"⚠️ *确认删除* 账号 `[{index}]`（{R.esc(acc.alias or acc.region)}）？\n\n"
                        "只从 `accounts.json` 移除这一条，**不会动 OCI 里的任何资源**、不删私钥文件。",
                        kb.confirm("cfg", pending.token))
            return

        # ============================================================
        #  💾 硬盘管理（vl / vm / vex / vd）
        # ============================================================
        if head == "vl":   # 列某账号全部卷
            index = int(parts[1])
            await query.answer("查询中…")
            client = registry.get(index)
            try:
                vols = await asyncio.to_thread(compute_svc.list_all_volumes, client)
            except Exception as exc:   # noqa: BLE001
                await _edit(query, f"❌ {str(exc)[:500]}", kb.back_to_main())
                return
            pairs = [(store.put(chat_id, v.id),
                      f"{v.attach_icon} {v.display_name[:28]} "
                      f"{v.size_gb:.0f}G {'[挂载]' if v.is_attached else ''}") for v in vols]
            total_gb = sum(v.size_gb for v in vols)
            unverified = sum(1 for v in vols if not v.attachment_verified)
            body = (f"💾 *{client.account.label}* 的硬盘\n\n"
                    f"共 {len(vols)} 块，合计 **{total_gb:.0f} GB**\n"
                    "（🟢=已挂载　⚪=未挂载　❓=挂载状态没查到；点进去可扩容 / 删除）")
            if unverified:
                # 不说明的话，用户会把「❓」当成普通状态，然后奇怪为什么删不掉
                body += (f"\n\n⚠️ 有 **{unverified}** 块的挂载状态没查到"
                         "（多半是限流），它们**不会**给出删除入口 —— 稍后刷新重试。")
            await _edit(query, body, kb.volume_list(index, pairs))
            return

        if head == "vm":   # 单块卷详情 + 菜单
            index = int(parts[1])
            token = parts[2]
            vol_id = store.get(chat_id, token)
            if not vol_id:
                await query.answer("会话已过期", show_alert=True)
                return
            client = registry.get(index)
            try:
                vols = await asyncio.to_thread(compute_svc.list_all_volumes, client)
                vol = next(v for v in vols if v.id == vol_id)
            except Exception as exc:   # noqa: BLE001
                await _edit(query, f"❌ {str(exc)[:400]}", kb.back_to_main())
                return
            body = (f"💾 *{R.esc(vol.display_name)}*\n\n"
                    f"类型：{'引导卷' if vol.kind == 'boot' else '数据卷'}\n"
                    f"大小：**{vol.size_gb:.0f} GB**　可用域：`{vol.ad.split(':')[-1]}`\n"
                    f"状态：{humanize_state(vol.lifecycle_state)}\n"
                    f"挂载：{vol.attach_state_label}")
            if vol.attached_instance_id:
                body += f"（`{vol.attached_instance_id[-24:]}`）"
            if not vol.attachment_verified:
                body += "\n\n⚠️ 这个可用域的挂载关系没查到，为安全起见**不提供删除**。"
            await query.answer()
            await _edit(query, body, kb.volume_menu(index, token, vol.can_delete))
            return

        if head == "vex":   # 扩容 → 询问新大小（文本）
            index = int(parts[1])
            token = parts[2]
            store.set_awaiting(chat_id, f"vol_resize:{index}:{token}")
            await query.answer()
            await _edit(query, "📈 输入新的卷大小（GB，必须比现在大）：\n直接发数字即可。", kb.back_to_main())
            return

        if head == "vd":    # 删卷确认
            index = int(parts[1])
            token = parts[2]
            vol_id = store.get(chat_id, token)
            if not vol_id:
                await query.answer("会话已过期", show_alert=True)
                return
            pending = _new_pending(
                store, chat_id, user_id, kind="volume_delete", account_index=index,
                summary=f"删除未挂载的卷 {vol_id[-24:]}",
                payload={"volume_id": vol_id}, destructive=True,
            )
            await query.answer()
            await _edit(query, "⚠️ *确认删除* 这块卷？\n\n执行时会**实时复核**它没挂任何实例，挂了就直接拒绝。",
                        kb.confirm(f"vm:{index}:{token}", pending.token))
            return

        # ============================================================
        #  🧵 任务管理（tk / tkstop）—— 自动抢机任务的看与停
        # ============================================================
        if head == "tk":
            reg = tasks_reg(context)
            my_tasks = reg.list_for(chat_id)
            running = [t for t in my_tasks if t.status == "running"]
            body_lines = ["🧵 *任务管理*（本会话）", ""]
            if not my_tasks:
                body_lines.append("还没有任何开机 / 抢机任务。用「2. 开机」里的『自动抢机』创建一个。")
            else:
                icon_map = {"running": "🟢", "succeeded": "✅", "stopped": "⚪", "blocked": "🛑"}
                for t in my_tasks[:10]:
                    body_lines.append(
                        f"{icon_map.get(t.status, '·')} `{t.token}`　"
                        f"**{t.done_count}/{t.want}** 台 · {t.attempts} 次尝试\n"
                        # ⚠️ 必须 one_line：`last_error` 现在是**多行**的
                        #    （错误码 / 原始信息 / 建议），直接 [:60] 会切到第二行，
                        #    任务列表里就多出一行没头没尾的断句。
                        f"   {R.esc(one_line(t.last_error or '', 60))}")
            body_lines += ["", "运行中的任务点右边按钮可停掉。"]
            stop_tokens = [t.token for t in running]
            await query.answer()
            await _edit(query, "\n".join(body_lines), kb.task_menu(my_tasks[:10], stop_tokens))
            return

        if head == "tkstop":   # 停一个任务
            token = parts[1]
            reg = tasks_reg(context)
            task = reg.get(token)
            if task is None:
                await query.answer("任务不存在或已过期", show_alert=True)
                return
            if task.chat_id != chat_id and user_id not in (task.user_id,):
                await query.answer("⛔ 这不是你的任务", show_alert=True)
                return
            reg.stop(token)
            await query.answer(f"已停止 {token}")
            # 刷新任务列表视图
            my_tasks = reg.list_for(chat_id)
            body = f"⏹ *{token}* 已停止（抢到 {task.done_count}/{task.want} 台）。"
            stop_tokens = [t.token for t in my_tasks if t.status == "running"]
            await _edit(query, body + "\n\n🧵 *剩余任务*：" if my_tasks else body,
                        kb.task_menu(my_tasks[:10], stop_tokens) if my_tasks else kb.main_menu())
            return

        # ---- 救援（VNC console）：ip:<index>:<token>:rescue 走 power 分支之外单独处理 ----

        await query.answer("未知操作", show_alert=True)

    except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让 Bot 静默失败
        log.exception("处理按钮 %s 失败", data)
        try:
            await query.answer("出错了，详情见消息", show_alert=False)
        except Exception:  # noqa: BLE001
            pass
        await _edit(query, f"❌ 处理失败：\n\n{str(exc)[:800]}", kb.back_to_main())


# --------------------------------------------------------------------------
#  命令：开关机 / 销毁 / 删桶 / S3（脚本友好，参数走命令行）
# --------------------------------------------------------------------------
async def _resolve_and_plan_power(message, registry: ClientRegistry, store: Store,
                                  chat_id: int, user_id: int, args: list[str],
                                  action: str) -> None:
    if len(args) < 2:
        await _reply(message, f"用法：`/{action} <账号> <实例名或OCID>`")
        return
    try:
        acc = _account(registry.settings, args[0])
    except ValueError as exc:
        await _reply(message, f"❌ {exc}")
        return

    client = registry.get(acc.index)
    needle = " ".join(args[1:])
    placeholder = await message.reply_text("⏳ 定位实例中…")
    try:
        view, _ = await asyncio.to_thread(
            compute_svc.plan_power, client, needle, action)
    except Exception as exc:  # noqa: BLE001
        await _reply(placeholder, f"❌ {str(exc)[:500]}", kb.back_to_main())
        return

    names = {"start": "开机", "stop": "关机", "softstop": "软关机",
             "reboot": "重启", "reset": "硬重启"}
    pending = _new_pending(
        store, chat_id, user_id, kind="power", account_index=acc.index,
        summary=f"{names[action]} {view.display_name}",
        payload={"action": action, "instance_id": view.id, "name": view.display_name},
        destructive=action != "start",
    )
    body = "\n".join([
        f"⚡ *{names[action]}计划*",
        "",
        f"账号：{acc.label}",
        f"实例：`{R.esc(view.display_name)}`",
        f"当前状态：{view.lifecycle_state}",
        "",
        "确认执行吗？",
    ])
    await _reply(placeholder, body, kb.confirm(f"a:{acc.index}", pending.token))


@restricted
async def cmd_start_vm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, registry, store = ctx_of(context)
    await _resolve_and_plan_power(update.message, registry, store,
                                  update.effective_chat.id, update.effective_user.id,
                                  context.args or [], "start")


@restricted
async def cmd_stop_vm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, registry, store = ctx_of(context)
    await _resolve_and_plan_power(update.message, registry, store,
                                  update.effective_chat.id, update.effective_user.id,
                                  context.args or [], "stop")


@restricted
async def cmd_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, registry, store = ctx_of(context)
    await _resolve_and_plan_power(update.message, registry, store,
                                  update.effective_chat.id, update.effective_user.id,
                                  context.args or [], "reboot")


@restricted
async def cmd_terminate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if len(args) < 2:
        await _reply(update.message,
                     "用法：`/terminate <账号> <实例名或OCID>`\n\n"
                     "⚠️ 这是不可逆操作，需要 `ORACLES_ALLOW_DESTRUCTIVE=true`。")
        return
    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return

    client = registry.get(acc.index)
    needle = " ".join(args[1:])
    placeholder = await update.message.reply_text("⏳ 定位实例并检查池成员身份…")
    try:
        plan = await asyncio.to_thread(compute_svc.plan_terminate, client, needle)
    except Exception as exc:  # noqa: BLE001
        await _reply(placeholder, f"❌ {str(exc)[:500]}", kb.back_to_main())
        return

    if not plan.can_execute:
        await _reply(placeholder, plan.render(), kb.back_to_main())
        return

    pending = _new_pending(
        store, update.effective_chat.id, update.effective_user.id,
        kind="terminate", account_index=acc.index,
        summary=plan.render(),
        payload={"instance_id": plan.instance.id,
                 "preserve_boot_volume": plan.preserve_boot_volume},
        destructive=True,
    )
    await _reply(placeholder, plan.render() + "\n\n确认销毁吗？",
                 kb.confirm(f"a:{acc.index}", pending.token))


@restricted
async def cmd_rmbucket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if len(args) < 2:
        await _reply(update.message, "用法：`/rmbucket <账号> <桶名>`")
        return
    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return

    client = registry.get(acc.index)
    bucket = args[1]
    placeholder = await update.message.reply_text("⏳ 实时复核桶内容…")
    try:
        # force=True：命令行方式视为用户已明确要删，但仍然要过二次确认和写开关
        plan = await asyncio.to_thread(
            storage_svc.plan_delete_bucket, client, bucket, force=True)
    except Exception as exc:  # noqa: BLE001
        await _reply(placeholder, f"❌ {str(exc)[:600]}", kb.back_to_main())
        return

    if not plan.can_execute:
        await _reply(placeholder, plan.render(), kb.back_to_main())
        return

    pending = _new_pending(
        store, update.effective_chat.id, update.effective_user.id,
        kind="bucket_delete", account_index=acc.index,
        summary=plan.render(), payload={"plan": plan}, destructive=True,
    )
    await _reply(placeholder, plan.render() + "\n\n确认删除吗？",
                 kb.confirm(f"a:{acc.index}", pending.token))


@restricted
async def cmd_s3key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, registry, store = ctx_of(context)
    args = context.args or []
    if not args:
        await _reply(update.message, "用法：`/s3key <账号>`")
        return
    try:
        acc = _account(settings, args[0])
    except ValueError as exc:
        await _reply(update.message, f"❌ {exc}")
        return

    pending = _new_pending(
        store, update.effective_chat.id, update.effective_user.id,
        kind="s3_create", account_index=acc.index,
        summary=f"签发 S3 兼容密钥（{acc.label}）",
        payload={"display_name": f"oracles-bot-{update.effective_user.id}"},
    )
    body = "\n".join([
        "🔑 *签发 S3 兼容密钥*",
        "",
        f"账号：{acc.label}",
        "",
        "⚠️ Secret Key **只在创建那一刻返回一次**，之后任何 API 都读不回来。",
        "⚠️ 刚创建的密钥有 5~8 分钟传播延迟，期间会间歇性报",
        "   SignatureDoesNotMatch —— 这是正常的，不是密钥错。",
        "",
        "确认签发吗？",
    ])
    await _reply(update.message, body, kb.confirm(f"a:{acc.index}", pending.token))


@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """非命令文本 → 先看有没有「等待中的输入」，没有就当闲聊提示。"""
    settings, registry, store = ctx_of(context)   # noqa: F841 —— registry 供卷扩容用
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    awaiting = store.get_awaiting(chat_id)
    if not awaiting:
        await _reply(update.message, "我不认这句话。用 /help 看命令，或 /start 打开菜单。",
                     kb.main_menu())
        return

    # ------------------------------------------------------------------
    #  向导：用户名 → 密码（wz_user）
    # ------------------------------------------------------------------
    if awaiting == "wz_user":
        st = store.get_wizard(chat_id) or {}
        username, password = text, None
        if " " in text:   # 「user pass」一次给全
            username, password = text.split(None, 1)
        try:
            cfg_text = build_cloud_init(username=username, password=password)
            to_user_data(cfg_text)   # 顺便校验 base64 体积上限
        except ValueError as exc:
            await _reply(update.message, f"❌ {exc}\n\n请重发（用户名可含字母数字 . _ -）。",
                         kb.main_menu())
            return
        st["username"] = username
        if password:
            store.clear_awaiting(chat_id)
            store.put_wizard(chat_id, st)
            await _reply(update.message, f"✅ 已记录用户 `{R.esc(username)}` + 密码。\n\n继续：",
                         kb.wizard_network())
            return
        # 只要了用户名（SSH key 模式不需要密码）—— 问一句要不要给密码
        store.set_awaiting(chat_id, "wz_pass")
        await _reply(update.message, f"✅ 已记录用户 `{R.esc(username)}`。\n\n"
                                     "再发一次就是**登录密码**；不想设就点「仅 IPv4」那步的跳过。",
                     kb.back_to_main())
        return

    if awaiting == "wz_pass":
        st = store.get_wizard(chat_id) or {}
        try:
            cfg_text = build_cloud_init(username=st.get("username") or "", password=text)
            to_user_data(cfg_text)
        except ValueError as exc:
            await _reply(update.message, f"❌ {exc}", kb.main_menu())
            return
        st["password"] = text
        store.clear_awaiting(chat_id)
        store.put_wizard(chat_id, st)
        await _reply(update.message, "✅ 密码已记录。\n\n继续：", kb.wizard_network())
        return

    # ------------------------------------------------------------------
    #  配置管理：添加账号（cfg_add）—— 收 JSON
    # ------------------------------------------------------------------
    if awaiting == "cfg_add":
        store.clear_awaiting(chat_id)
        try:
            entry = json.loads(text)
            new_index = await asyncio.to_thread(acct_svc.add_account_entry,
                                                settings.accounts_file, entry)
        except (json.JSONDecodeError, ValueError) as exc:
            await _reply(update.message, f"❌ 解析失败：{str(exc)[:300]}\n\n重发一次 JSON（从 /start → 配置管理）。",
                         kb.main_menu())
            return

        # 热重载：新 settings + registry 换进 Application，会话不断
        app = context.application
        try:
            new_settings = await asyncio.to_thread(acct_svc.reload_settings)
        except Exception as exc:   # noqa: BLE001 —— 坏配置不能把 Bot 搞崩
            log.exception("热重载失败")
            await _reply(update.message, f"⚠️ 账号已写入文件，但新配置校验没通过：{str(exc)[:300]}\n旧配置仍在生效。",
                         kb.main_menu())
            return
        old_registry = context.bot_data["registry"]
        acct_svc.swap_in_application(app, new_settings)
        try:
            await asyncio.to_thread(old_registry.close)   # 释放旧账号的 SDK client
        except Exception:   # noqa: BLE001
            pass
        await _reply(update.message, f"✅ 已添加账号 `{new_index}`，配置热重载完成（无需重启）。\n\n",
                     kb.main_menu())
        return

    # ------------------------------------------------------------------
    #  硬盘管理：卷扩容新大小（vol_resize:<index>:<store_token>）
    # ------------------------------------------------------------------
    if awaiting.startswith("vol_resize:"):
        _, index_s, vtoken = awaiting.split(":")
        index = int(index_s)
        store.clear_awaiting(chat_id)
        try:
            new_size = int(text)
        except ValueError:
            await _reply(update.message, "❌ 请发一个整数（GB）。", kb.main_menu())
            return
        vol_id = store.get(chat_id, vtoken)
        if not vol_id:
            await _reply(update.message, "⚠️ 会话已过期，重新从「5. 硬盘管理」进来。",
                         kb.main_menu())
            return
        client = registry.get(index)
        try:
            final_size = await asyncio.to_thread(
                compute_svc.extend_volume, client, vol_id, new_size)
        except Exception as exc:   # noqa: BLE001 —— 缩容/权限错误等
            log.warning("卷扩容失败 %s → %s GB：%s", vol_id[-12:], new_size, str(exc)[:200])
            await _reply(update.message, f"❌ {str(exc)[:400]}", kb.main_menu())
            return
        await _reply(update.message, f"✅ 卷已扩容到 **{final_size:.0f} GB**。\n\n",
                     kb.back_to_main())
        return

    # 兜底：awaiting 还在但类型不认识 —— 清掉防卡住
    store.clear_awaiting(chat_id)
    await _reply(update.message, "（之前的等待输入已过期）用 /start 重新来。", kb.main_menu())
