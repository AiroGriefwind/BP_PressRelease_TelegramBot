import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import config
from core.session import touch_session, user_sessions
from core.time_utils import now_hk
from features.pr_processing import handle_mention
from integrations.gmail import fetch_logs_from_gmail, read_logs_cache
from ui.messages import SESSION_EXPIRED_TEXT


def _filter_logs(logs: List[Dict[str, Any]], days: int, mode: str) -> List[Dict[str, Any]]:
    cutoff = now_hk() - timedelta(days=days)
    out = []
    for x in logs:
        try:
            ts = datetime.fromisoformat(x.get("ts", ""))
        except Exception:
            continue
        if ts < cutoff:
            continue
        st = (x.get("status") or "").upper()
        if mode == "SUCCESS" and st != "SUCCESS":
            continue
        if mode == "ERROR" and st != "ERROR":
            continue
        out.append(x)
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def _get_logs_view(context: ContextTypes.DEFAULT_TYPE, session_key: str) -> Dict[str, Any]:
    key = f"logs_view_{session_key}"
    if key not in context.user_data:
        context.user_data[key] = {"days": 1, "mode": "ALL", "page": 0}
    return context.user_data[key]


async def show_logs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, session_key: str):
    query = update.callback_query
    view = _get_logs_view(context, session_key)
    days, mode, page = view["days"], view["mode"], view["page"]

    logs = read_logs_cache()
    filtered = _filter_logs(logs, days=days, mode=mode)

    succ = sum(1 for x in filtered if (x.get("status") or "").upper() == "SUCCESS")
    fail = sum(1 for x in filtered if (x.get("status") or "").upper() == "ERROR")

    total = len(filtered)
    start = page * config.LOGS_PER_PAGE
    end = start + config.LOGS_PER_PAGE
    items = filtered[start:end]

    text = (
        f"🧾 Logs（最近{days}天 / {mode}）\n"
        f"成功: {succ}  失败: {fail}  总计: {total}\n"
        f"页: {page + 1} / {max(1, (total + config.LOGS_PER_PAGE - 1)//config.LOGS_PER_PAGE)}"
    )

    keyboard = []
    for x in items:
        st = (x.get("status") or "").upper()
        prefix = "✅" if st == "SUCCESS" else "❌"
        short_title = (x.get("title") or "")[:8]
        btn_text = f"{prefix} {short_title}"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=f"log_detail|{session_key}|{x.get('id')}")]
        )

    keyboard.append(
        [
            InlineKeyboardButton("1天", callback_data=f"logs_days|{session_key}|1"),
            InlineKeyboardButton("3天", callback_data=f"logs_days|{session_key}|3"),
            InlineKeyboardButton("7天", callback_data=f"logs_days|{session_key}|7"),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton("全部", callback_data=f"logs_mode|{session_key}|ALL"),
            InlineKeyboardButton("成功", callback_data=f"logs_mode|{session_key}|SUCCESS"),
            InlineKeyboardButton("失败", callback_data=f"logs_mode|{session_key}|ERROR"),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton("⬅️ 上一页", callback_data=f"logs_page|{session_key}|-1"),
            InlineKeyboardButton("➡️ 下一页", callback_data=f"logs_page|{session_key}|1"),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton("🔄 刷新", callback_data=f"logs_refresh|{session_key}"),
            InlineKeyboardButton("⬅️ 返回", callback_data=f"logs_back|{session_key}"),
        ]
    )

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def on_menu_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text(SESSION_EXPIRED_TEXT)
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    _get_logs_view(context, session_key)
    await show_logs_menu(update, context, session_key)


async def on_logs_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, days = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text(SESSION_EXPIRED_TEXT)
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    view = _get_logs_view(context, session_key)
    view["days"] = int(days)
    view["page"] = 0
    await show_logs_menu(update, context, session_key)


async def on_logs_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, mode = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text(SESSION_EXPIRED_TEXT)
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    view = _get_logs_view(context, session_key)
    view["mode"] = mode
    view["page"] = 0
    await show_logs_menu(update, context, session_key)


async def on_logs_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, delta = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text(SESSION_EXPIRED_TEXT)
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    view = _get_logs_view(context, session_key)
    logs = _filter_logs(read_logs_cache(), days=view["days"], mode=view["mode"])
    max_page = max(0, (len(logs) - 1) // config.LOGS_PER_PAGE)
    view["page"] = min(max(0, view["page"] + int(delta)), max_page)
    await show_logs_menu(update, context, session_key)


async def on_logs_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer("刷新中...", cache_time=0)
    except BadRequest:
        pass

    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        try:
            await query.edit_message_text(SESSION_EXPIRED_TEXT)
        except Exception:
            pass
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    days = _get_logs_view(context, session_key)["days"]

    try:
        await asyncio.to_thread(fetch_logs_from_gmail, days=days, max_results=200)
    except Exception as e:
        try:
            await query.answer(f"拉取失败: {e}", show_alert=True)
        except BadRequest:
            pass

    await show_logs_menu(update, context, session_key)


async def on_log_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, log_id = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text(SESSION_EXPIRED_TEXT)
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    logs = read_logs_cache()
    x = next((r for r in logs if str(r.get("id")) == str(log_id)), None)
    if not x:
        await query.edit_message_text("⚠️ 记录不存在或已过期。")
        return

    st = (x.get("status") or "").upper()
    code = x.get("error_code")
    err = config.ERROR_TEXT.get(int(code), "") if code is not None else ""
    ts = x.get("ts", "")
    subject = x.get("subject", "")
    title = x.get("title", "")

    text = (
        "🧾 Log 详情\n"
        f"时间: {ts}\n"
        f"状态: {st}\n"
        f"错误码: {code or '-'} {f'({err})' if err else ''}\n"
        f"标题: {title}\n\n"
        f"Subject:\n{subject}"
    )
    keyboard = [[InlineKeyboardButton("⬅️ 返回列表", callback_data=f"menu_logs|{session_key}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def on_logs_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text(SESSION_EXPIRED_TEXT)
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    await handle_mention(update, context)
