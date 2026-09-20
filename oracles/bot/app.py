"""Bot 启动器。"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from ..config import Settings, load_settings, validate_startup
from ..log import setup_logging
from ..oci_gateway import ClientRegistry
from . import handlers
from .store import Store

log = logging.getLogger(__name__)


def build_application(settings: Settings) -> Application:
    """组装 Application。抽出来是为了能被测试直接调用，不用起轮询。"""
    app = Application.builder().token(settings.telegram_token).build()

    # 共享状态挂在 bot_data 上，handler 通过 context.bot_data 取
    app.bot_data["settings"] = settings
    app.bot_data["registry"] = ClientRegistry(settings)
    app.bot_data["store"] = Store()
    app.bot_data["startup_warnings"] = validate_startup(settings)

    # ---- 命令 ----
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("h", handlers.cmd_help))
    app.add_handler(CommandHandler("status", handlers.cmd_status))
    app.add_handler(CommandHandler("cancel", handlers.cmd_cancel))
    app.add_handler(CommandHandler("a", handlers.cmd_accounts))
    app.add_handler(CommandHandler("accounts", handlers.cmd_accounts))
    app.add_handler(CommandHandler("q", handlers.cmd_quota))
    app.add_handler(CommandHandler("quota", handlers.cmd_quota))
    app.add_handler(CommandHandler("i", handlers.cmd_instances))
    app.add_handler(CommandHandler("instances", handlers.cmd_instances))
    app.add_handler(CommandHandler("launch", handlers.cmd_launch))
    app.add_handler(CommandHandler("b", handlers.cmd_buckets))
    app.add_handler(CommandHandler("buckets", handlers.cmd_buckets))
    app.add_handler(CommandHandler("obj", handlers.cmd_objects))
    app.add_handler(CommandHandler("audit", handlers.cmd_audit))
    app.add_handler(CommandHandler("security", handlers.cmd_security))
    app.add_handler(CommandHandler("harden", handlers.cmd_harden))

    # ---- 开关机别名（/start 已被 /start 占用，所以实例动作用带下划线的名字）----
    app.add_handler(CommandHandler("start_vm", handlers.cmd_start_vm))
    app.add_handler(CommandHandler("stop_vm", handlers.cmd_stop_vm))
    app.add_handler(CommandHandler("reboot", handlers.cmd_reboot))
    app.add_handler(CommandHandler("terminate", handlers.cmd_terminate))
    app.add_handler(CommandHandler("rmbucket", handlers.cmd_rmbucket))
    app.add_handler(CommandHandler("s3key", handlers.cmd_s3key))

    # ---- 按钮 & 兜底 ----
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))
    app.add_error_handler(on_error)
    return app


async def on_error(update: object, context) -> None:
    """全局错误处理：把异常写进日志，并尽量回一条人话给用户。"""
    log.exception("处理更新时发生未捕获异常", exc_info=context.error)
    text = f"❌ 出了点问题：\n\n{str(context.error)[:600]}"
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(text)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    warnings = validate_startup(settings)
    log.info("=" * 60)
    log.info("OCI 多账号管理台启动")
    log.info("配置目录：%s", settings.home)
    log.info("账号数：%d", len(settings.accounts))
    log.info("写操作：%s | 不可逆操作：%s",
             "开启" if settings.write_enabled else "关闭(DRY-RUN)",
             "允许" if settings.allow_destructive else "禁止")
    for w in warnings:
        log.warning(w)
    log.info("=" * 60)

    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
