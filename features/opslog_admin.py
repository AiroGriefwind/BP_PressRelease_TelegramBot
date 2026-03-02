from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from core.logging_ops import log_event
from core.runtime_config import load_runtime_config_from_file
from integrations.ops_log_archive import (
    format_upload_result,
    is_archive_admin,
    resolve_day_yyyymmdd,
    upload_ops_log_by_day,
)


def _pick_day_arg(context: ContextTypes.DEFAULT_TYPE, fallback: str = "today") -> str:
    args = getattr(context, "args", None) or []
    if not args:
        return fallback
    return (args[0] or "").strip().lower() or fallback


async def _run_push(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    forced_day: Optional[str] = None,
):
    user = update.effective_user
    msg = update.effective_message

    if not msg:
        return

    # 命令触发前重载一次配置，避免 Bot 常驻进程持有旧配置
    load_runtime_config_from_file("config.json")

    if not is_archive_admin(getattr(user, "id", None)):
        await msg.reply_text("你没有权限执行该命令。")
        return

    day_arg = forced_day or _pick_day_arg(context, fallback="today")
    try:
        # 先校验，确保错误信息可读
        resolve_day_yyyymmdd(day_arg)
    except Exception:
        await msg.reply_text("参数错误。请使用：today / yesterday / YYYYMMDD")
        return

    result = upload_ops_log_by_day(day_arg)
    text = format_upload_result(result)
    await msg.reply_text(text)

    try:
        log_event(
            "opslog_archive_manual_tg",
            update=update,
            extra={
                "input_day": day_arg,
                **result,
            },
        )
    except Exception:
        pass


async def on_opslog_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_push(update, context, forced_day=None)


async def on_opslog_push_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_push(update, context, forced_day="today")
