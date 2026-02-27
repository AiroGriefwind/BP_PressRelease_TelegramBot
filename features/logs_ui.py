import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import config
from core.logging_ops import log_event
from core.session import touch_session, user_sessions
from core.time_utils import now_hk
from integrations.gmail import fetch_logs_from_gmail, get_logs_cache_info, read_logs_cache
from ui.messages import SESSION_EXPIRED_TEXT


def _build_logs_home_markup(session_key: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("Logs瀏覽", callback_data=f"logs_browse|{session_key}")],
        [InlineKeyboardButton("Excel導出", callback_data=f"logs_excel_export|{session_key}")],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"logs_back|{session_key}")],
    ]
    return InlineKeyboardMarkup(buttons)


def _normalize_keyword(keyword: Optional[str]) -> str:
    return (keyword or "").strip().lower()


def _filter_logs(
    logs: List[Dict[str, Any]], days: int, mode: str, keyword: Optional[str] = None
) -> List[Dict[str, Any]]:
    cutoff = now_hk() - timedelta(days=days)
    kw = _normalize_keyword(keyword)
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
        if kw:
            title = x.get("title") or ""
            subject = x.get("subject") or ""
            if kw not in f"{title} {subject}".lower():
                continue
        out.append(x)
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def _get_logs_view(context: ContextTypes.DEFAULT_TYPE, session_key: str) -> Dict[str, Any]:
    key = f"logs_view_{session_key}"
    if key not in context.user_data:
        context.user_data[key] = {"days": 1, "mode": "ALL", "page": 0, "keyword": ""}
    return context.user_data[key]


def set_logs_keyword(context: ContextTypes.DEFAULT_TYPE, session_key: str, keyword: str) -> Dict[str, Any]:
    view = _get_logs_view(context, session_key)
    view["keyword"] = (keyword or "").strip()
    view["page"] = 0
    return view


def render_logs_menu(
    context: ContextTypes.DEFAULT_TYPE,
    session_key: str,
    *,
    cache_info: Optional[dict] = None,
) -> tuple[str, InlineKeyboardMarkup, Dict[str, Any]]:
    view = _get_logs_view(context, session_key)
    days, mode, page = view["days"], view["mode"], view["page"]
    keyword = view.get("keyword") or ""

    logs = read_logs_cache()
    filtered = _filter_logs(logs, days=days, mode=mode, keyword=keyword)

    succ = sum(1 for x in filtered if (x.get("status") or "").upper() == "SUCCESS")
    fail = sum(1 for x in filtered if (x.get("status") or "").upper() == "ERROR")

    total = len(filtered)
    max_page = max(0, (total - 1) // config.LOGS_PER_PAGE) if total else 0
    if page > max_page:
        page = max_page
        view["page"] = page

    start = page * config.LOGS_PER_PAGE
    end = start + config.LOGS_PER_PAGE
    items = filtered[start:end]

    cache_info = cache_info or get_logs_cache_info()
    last_refresh = cache_info.get("last_refresh_ts") or "-"
    ttl_seconds = cache_info.get("ttl_seconds")
    ttl_minutes = (
        max(1, int(ttl_seconds // 60)) if isinstance(ttl_seconds, (int, float)) else "-"
    )

    text_lines = [
        f"🧾 Logs（最近{days}天 / {mode}）",
        f"成功: {succ}  失敗: {fail}  總計: {total}",
        f"頁: {page + 1} / {max_page + 1}",
        f"關鍵字: {keyword or '-'}",
        f"最後刷新: {last_refresh}  快取有效期: {ttl_minutes} 分鐘",
    ]
    if total == 0:
        text_lines.append("暫無記錄，可點擊刷新。")
    text = "\n".join(text_lines)

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
            InlineKeyboardButton("失敗", callback_data=f"logs_mode|{session_key}|ERROR"),
        ]
    )
    keyword_row = [InlineKeyboardButton("🔍 關鍵字", callback_data=f"logs_keyword|{session_key}")]
    if keyword:
        keyword_row.append(
            InlineKeyboardButton("❌ 清除", callback_data=f"logs_keyword_clear|{session_key}")
        )
    keyboard.append(keyword_row)
    keyboard.append(
        [
            InlineKeyboardButton("⬅️ 上一頁", callback_data=f"logs_page|{session_key}|-1"),
            InlineKeyboardButton("➡️ 下一頁", callback_data=f"logs_page|{session_key}|1"),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton("🔄 刷新", callback_data=f"logs_refresh|{session_key}"),
            InlineKeyboardButton("⬅️ 返回", callback_data=f"logs_back|{session_key}"),
        ]
    )

    stats = {
        "days": days,
        "mode": mode,
        "page": page,
        "keyword": keyword,
        "result_count": total,
        "succ": succ,
        "fail": fail,
    }
    return text, InlineKeyboardMarkup(keyboard), stats


async def show_logs_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_key: str,
    *,
    cache_info: Optional[dict] = None,
) -> Dict[str, Any]:
    query = update.callback_query
    text, reply_markup, stats = render_logs_menu(
        context, session_key, cache_info=cache_info
    )

    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return stats
        raise
    return stats


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
    await query.edit_message_text(
        "🧾 Logs\n請選擇功能：",
        reply_markup=_build_logs_home_markup(session_key),
    )
    try:
        log_event(
            "logs_menu_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
        )
    except Exception:
        pass


async def on_logs_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    view = _get_logs_view(context, session_key)

    cache_info = get_logs_cache_info()
    logs = read_logs_cache()
    fetched = None
    auto_refreshed = False
    if not logs or cache_info.get("stale"):
        try:
            await query.answer("自動刷新中...", cache_time=0)
        except BadRequest:
            pass
        try:
            fetched = await asyncio.to_thread(fetch_logs_from_gmail, days=view["days"], max_results=200)
            auto_refreshed = True
        except Exception:
            fetched = None
        cache_info = get_logs_cache_info()

    stats = await show_logs_menu(update, context, session_key, cache_info=cache_info)
    try:
        log_event(
            "logs_browse_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={
                "days": stats.get("days"),
                "mode": stats.get("mode"),
                "page": stats.get("page"),
                "result_count": stats.get("result_count"),
                "keyword": stats.get("keyword"),
                "auto_refreshed": auto_refreshed,
                "fetched": fetched,
            },
        )
    except Exception:
        pass


async def on_logs_excel_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    buttons = [
        [InlineKeyboardButton("RTHK Logs", callback_data=f"excel_export_rthk|{session_key}")],
        [
            InlineKeyboardButton(
                "IThinkTrending Logs（開發中）",
                callback_data=f"excel_export_placeholder|{session_key}|ithinktrending",
            )
        ],
        [
            InlineKeyboardButton(
                "FB URL Logs（開發中）",
                callback_data=f"excel_export_placeholder|{session_key}|fb_url",
            )
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data=f"excel_export_back|{session_key}")],
    ]
    await query.edit_message_text(
        "📤 Excel導出\n請選擇要導出的 Logs 類型：",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    try:
        log_event(
            "logs_excel_menu_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
        )
    except Exception:
        pass


async def on_excel_export_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await query.edit_message_text(
        "🧾 Logs\n請選擇功能：",
        reply_markup=_build_logs_home_markup(session_key),
    )


async def on_excel_export_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("開發中", show_alert=True)
    _, session_key, feature_name = query.data.split("|")
    try:
        log_event(
            "logs_excel_feature_pending_click",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={"feature": feature_name},
        )
    except Exception:
        pass


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
    stats = await show_logs_menu(update, context, session_key)
    try:
        log_event(
            "logs_days_change",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={
                "days": stats.get("days"),
                "mode": stats.get("mode"),
                "page": stats.get("page"),
                "result_count": stats.get("result_count"),
                "keyword": stats.get("keyword"),
            },
        )
    except Exception:
        pass


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
    stats = await show_logs_menu(update, context, session_key)
    try:
        log_event(
            "logs_mode_change",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={
                "days": stats.get("days"),
                "mode": stats.get("mode"),
                "page": stats.get("page"),
                "result_count": stats.get("result_count"),
                "keyword": stats.get("keyword"),
            },
        )
    except Exception:
        pass


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
    logs = _filter_logs(
        read_logs_cache(),
        days=view["days"],
        mode=view["mode"],
        keyword=view.get("keyword"),
    )
    max_page = max(0, (len(logs) - 1) // config.LOGS_PER_PAGE)
    view["page"] = min(max(0, view["page"] + int(delta)), max_page)
    stats = await show_logs_menu(update, context, session_key)
    try:
        log_event(
            "logs_page_change",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={
                "days": stats.get("days"),
                "mode": stats.get("mode"),
                "page": stats.get("page"),
                "result_count": stats.get("result_count"),
                "keyword": stats.get("keyword"),
                "delta": int(delta),
            },
        )
    except Exception:
        pass


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
        fetched = await asyncio.to_thread(fetch_logs_from_gmail, days=days, max_results=200)
    except Exception as e:
        fetched = None
        try:
            await query.answer(f"拉取失敗: {e}", show_alert=True)
        except BadRequest:
            pass

    cache_info = get_logs_cache_info()
    stats = await show_logs_menu(update, context, session_key, cache_info=cache_info)
    try:
        log_event(
            "logs_refresh",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={
                "days": stats.get("days"),
                "mode": stats.get("mode"),
                "page": stats.get("page"),
                "result_count": stats.get("result_count"),
                "keyword": stats.get("keyword"),
                "fetched": fetched,
            },
        )
    except Exception:
        pass


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
        await query.edit_message_text("⚠️ 記錄不存在或已過期。")
        return

    st = (x.get("status") or "").upper()
    code = x.get("error_code")
    err = config.ERROR_TEXT.get(int(code), "") if code is not None else ""
    ts = x.get("ts", "")
    subject = x.get("subject", "")
    title = x.get("title", "")

    text = (
        "🧾 Log 詳情\n"
        f"時間: {ts}\n"
        f"狀態: {st}\n"
        f"錯誤碼: {code or '-'} {f'({err})' if err else ''}\n"
        f"標題: {title}\n\n"
        f"Subject:\n{subject}"
    )
    keyboard = [[InlineKeyboardButton("⬅️ 返回列表", callback_data=f"logs_browse|{session_key}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    try:
        log_event(
            "logs_detail_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={"log_id": log_id, "status": st, "error_code": code},
        )
    except Exception:
        pass


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
    from features.pr_processing import handle_mention

    await handle_mention(update, context)


async def on_logs_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    sd = user_sessions.get(session_key) or {}
    sd["awaiting_logs_keyword"] = True
    user_sessions[session_key] = sd

    try:
        log_event(
            "logs_keyword_prompt",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
        )
    except Exception:
        pass

    buttons = [[InlineKeyboardButton("⬅️ 返回列表", callback_data=f"logs_browse|{session_key}")]]
    await query.edit_message_text(
        "請輸入關鍵字（匹配標題/Subject），傳送一則文字即可。傳送 '-' 可清空關鍵字。",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_logs_keyword_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    set_logs_keyword(context, session_key, "")
    stats = await show_logs_menu(update, context, session_key)
    try:
        log_event(
            "logs_keyword_clear",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={
                "days": stats.get("days"),
                "mode": stats.get("mode"),
                "page": stats.get("page"),
                "result_count": stats.get("result_count"),
            },
        )
    except Exception:
        pass
