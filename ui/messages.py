from typing import Optional

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application

SESSION_EXPIRED_TEXT = "⚠️ 會話已結束，請重新@我開始。"


async def try_edit_message_text(
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
):
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except BadRequest:
        # 可能被用户删了 / 已不可编辑，直接忽略
        return
    except Exception:
        return


async def try_edit_message_text_markup(
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    disable_web_page_preview: bool = False,
):
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except BadRequest:
        return
    except Exception:
        return


async def try_edit_query_message(query, text: str):
    try:
        await query.edit_message_text(text)
    except Exception:
        return


def render_progress_bar(percent: int, width: int = 10) -> str:
    p = max(0, min(100, int(percent)))
    filled = int(round(p / 100 * width))
    return f"[{'#' * filled}{'-' * (width - filled)}] {p}%"
