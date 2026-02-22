import asyncio
import re
import urllib.parse
from datetime import datetime
from typing import Any, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from core.logging_ops import log_event
from core.session import end_session, last_seen_fb_url, touch_session, user_sessions
from features.pr_text_flow import maybe_process_pr_text
from core.time_utils import now_hk
from integrations.gmail import get_gmail_service, send_email_with_fb_url
from ui.keyboard import build_settings_keyboard
from ui.messages import SESSION_EXPIRED_TEXT, try_edit_message_text_markup

FB_SETTINGS_KEYS = ("type", "language")


def _fb_settings_options() -> Dict[str, list]:
    return {
        key: config.SETTINGS_OPTIONS[key]
        for key in FB_SETTINGS_KEYS
        if key in config.SETTINGS_OPTIONS
    }


def _build_fb_url_confirm_text(fb_url: str, settings: dict, *, detected: bool = False) -> str:
    prefix = "已偵測到 FB URL：" if detected else "已收到 FB URL："
    return (
        f"{prefix}\n{fb_url}\n\n類型：{settings.get('type')}\n\n是否傳送到 {config.TARGET_EMAIL}？"
    )


def _build_fb_url_confirm_markup(session_key: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ 傳送 FB URL", callback_data=f"fb_url_send|{session_key}"),
            InlineKeyboardButton("✏️ 重新輸入", callback_data=f"fb_url_reset|{session_key}"),
        ],
        [
            InlineKeyboardButton("⚙️ 設定", callback_data=f"fb_url_settings|{session_key}"),
            InlineKeyboardButton("⬅️ 返回主選單", callback_data=f"back_to_main|{session_key}"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def _extract_first_url(text: str) -> Optional[str]:
    """
    从文本中提取第一个 http(s) URL。
    """
    if not text:
        return None
    m = re.search(r"(https?://[^\s<>\]\)\"']+)", text.strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def _normalize_fb_url(url: str) -> str:
    """
    - 处理 Facebook 跳转链接（l.facebook.com/l.php?u=...）
    - 做轻量清洗（去尾部标点）
    """
    if not url:
        return ""
    u = url.strip().strip(").,，。；;】]》>\"'")
    try:
        parsed = urllib.parse.urlparse(u)
        host = (parsed.netloc or "").lower()
        if host in ("l.facebook.com", "lm.facebook.com") and parsed.path.startswith("/l.php"):
            q = urllib.parse.parse_qs(parsed.query or "")
            inner = (q.get("u") or [None])[0]
            if inner:
                return urllib.parse.unquote(inner)
    except Exception:
        pass
    return u


def _looks_like_facebook_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or "").lower()
        if not host:
            return False
        return (
            host.endswith("facebook.com")
            or host == "fb.com"
            or host.endswith("fb.com")
            or host.endswith("fb.watch")
            or host.endswith("fb.me")
        )
    except Exception:
        return False


def _build_sender_info_from_message(message, fallback_user=None):
    """
    尽量从 UI message 的 reply_to_message 取到最初 @ 的发起者信息；
    取不到则回退到当前点击按钮的人。
    """
    try:
        src_msg = getattr(message, "reply_to_message", None) or message
        u = getattr(src_msg, "from_user", None) or fallback_user
        chat = getattr(message, "chat", None)
        dt = getattr(message, "date", None)
        # name
        first = getattr(u, "first_name", "") or ""
        last = getattr(u, "last_name", "") or ""
        name = (first + (" " + last if last else "")).strip() or "unknown"
        return {
            "name": name,
            "username": getattr(u, "username", None) or "unknown",
            "chat_title": getattr(chat, "title", None) or "private",
            "date": (
                dt.astimezone(now_hk().tzinfo).strftime("%Y-%m-%d %H:%M:%S")
                if dt
                else now_hk().strftime("%Y-%m-%d %H:%M:%S")
            ),
        }
    except Exception:
        return {
            "name": "unknown",
            "username": "unknown",
            "chat_title": "unknown",
            "date": now_hk().strftime("%Y-%m-%d %H:%M:%S"),
        }


async def on_fb_url_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    进入 FB URL 输入流程：提示用户发送 URL 文本。
    如果 session 已有 fb_url，则展示确认发送界面（不必重新输入）。
    """
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
    fb_url = sd.get("fb_url")

    # 已经有 URL：直接进入确认发送
    if fb_url:
        buttons = _build_fb_url_confirm_markup(session_key)
        settings = sd.get("settings") or {}
        await query.edit_message_text(
            _build_fb_url_confirm_text(fb_url, settings),
            reply_markup=buttons,
            disable_web_page_preview=True,
        )
        return

    # 尚无 URL：进入等待模式
    sd["awaiting_fb_url"] = True
    user_sessions[session_key] = sd

    log_event(
        "fb_url_awaiting_input",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
    )

    buttons = [[InlineKeyboardButton("⬅️ 返回主選單", callback_data=f"back_to_main|{session_key}")]]
    await query.edit_message_text(
        "請傳送 FB 分享連結（URL）給我。",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def on_fb_url_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    sd["fb_url"] = None
    sd["awaiting_fb_url"] = True
    user_sessions[session_key] = sd

    log_event(
        "fb_url_reset",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
    )

    buttons = [[InlineKeyboardButton("⬅️ 返回主選單", callback_data=f"back_to_main|{session_key}")]]
    await query.edit_message_text(
        "請重新傳送 FB 分享連結（URL）給我。",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def on_fb_url_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    fb_url = sd.get("fb_url")
    if not fb_url:
        await query.edit_message_text("⚠️ 尚未取得 FB URL。請先輸入連結。")
        return

    sender_info = _build_sender_info_from_message(query.message, fallback_user=query.from_user)
    settings = sd.get("settings") or {}
    gmail_service = get_gmail_service()
    success, err = await asyncio.to_thread(
        send_email_with_fb_url, gmail_service, fb_url, sender_info, settings
    )

    if success:
        log_event(
            "fb_url_send_success",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
        )

        await end_session(
            application=context.application,
            session_key=session_key,
            reason_text=f"✅ FB URL 已傳送到 {config.TARGET_EMAIL}\n會話結束。",
            reason_code="fb_url_send_success",
            user_id=query.from_user.id,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
        )
    else:
        log_event(
            "fb_url_send_failed",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
            extra={"error": err},
        )
        buttons = [[InlineKeyboardButton("⬅️ 返回主選單", callback_data=f"back_to_main|{session_key}")]]
        await query.edit_message_text(
            "❌ FB URL 傳送失敗，請稍後重試。",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    兜底文本处理：仅在用户处于“等待输入 FB URL”状态时消费下一条消息。
    """
    message = update.message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"

    # 记录“最近出现的 FB URL”（不要求 session 已存在）
    try:
        maybe_url = _extract_first_url(message.text or "")
        if maybe_url:
            norm = _normalize_fb_url(maybe_url)
            if _looks_like_facebook_url(norm):
                last_seen_fb_url[session_key] = {
                    "url": norm,
                    "dt": datetime.now(now_hk().tzinfo),
                }
    except Exception:
        pass

    sd = user_sessions.get(session_key)
    if not sd:
        consumed = await maybe_process_pr_text(
            update,
            context,
            text=message.text or "",
            source="text_message",
            rich_html=(message.text_html or ""),
        )
        if consumed:
            return
        return

    if sd.get("awaiting_fb_url"):
        touch_session(context=context, session_key=session_key, user_id=user_id, chat_id=chat_id)

        raw_url = _extract_first_url(message.text or "")
        if not raw_url:
            await message.reply_text("⚠️ 未偵測到 URL。請直接傳送一則包含 Facebook 分享連結的訊息。")
            return

        norm = _normalize_fb_url(raw_url)
        if not _looks_like_facebook_url(norm):
            await message.reply_text("⚠️ 目前只支援 Facebook 相關連結。請重新傳送 FB 分享連結。")
            return

        sd["fb_url"] = norm
        sd["awaiting_fb_url"] = False
        user_sessions[session_key] = sd

        log_event(
            "fb_url_captured",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
            extra={"fb_url": norm},
        )

        # 尝试更新 UI 消息为“确认发送”界面
        ui_chat_id = sd.get("ui_chat_id")
        ui_message_id = sd.get("ui_message_id")
        if ui_chat_id is not None and ui_message_id is not None:
            buttons = _build_fb_url_confirm_markup(session_key)
            await try_edit_message_text_markup(
                context.application,
                int(ui_chat_id),
                int(ui_message_id),
                _build_fb_url_confirm_text(norm, sd.get("settings") or {}),
                reply_markup=buttons,
                disable_web_page_preview=True,
            )
        else:
            buttons = _build_fb_url_confirm_markup(session_key)
            sent = await message.reply_text(
                _build_fb_url_confirm_text(norm, sd.get("settings") or {}),
                reply_markup=buttons,
                disable_web_page_preview=True,
            )
            touch_session(
                context=context,
                session_key=session_key,
                user_id=user_id,
                chat_id=chat_id,
                message_id=sent.message_id,
            )
        return

    if sd.get("awaiting_logs_keyword"):
        touch_session(context=context, session_key=session_key, user_id=user_id, chat_id=chat_id)

        keyword_raw = (message.text or "").strip()
        keyword = "" if keyword_raw in ("-", "－") else keyword_raw

        from features.logs_ui import render_logs_menu, set_logs_keyword

        set_logs_keyword(context, session_key, keyword)
        sd["awaiting_logs_keyword"] = False
        user_sessions[session_key] = sd

        try:
            log_event(
                "logs_keyword_set",
                session_key=session_key,
                session_id=sd.get("session_id"),
                update=update,
                extra={"keyword": keyword},
            )
        except Exception:
            pass

        ui_chat_id = sd.get("ui_chat_id")
        ui_message_id = sd.get("ui_message_id")
        text, reply_markup, _stats = render_logs_menu(context, session_key)
        if ui_chat_id is not None and ui_message_id is not None:
            await try_edit_message_text_markup(
                context.application,
                int(ui_chat_id),
                int(ui_message_id),
                text,
                reply_markup=reply_markup,
            )
        else:
            sent = await message.reply_text(text, reply_markup=reply_markup)
            touch_session(
                context=context,
                session_key=session_key,
                user_id=user_id,
                chat_id=chat_id,
                message_id=sent.message_id,
            )
        return

    consumed = await maybe_process_pr_text(
        update,
        context,
        text=message.text or "",
        source="text_message",
        rich_html=(message.text_html or ""),
    )
    if consumed:
        return

    return


async def on_fb_url_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    current_settings = (user_sessions.get(session_key) or {}).get("settings") or {}
    context.user_data[f"temp_fb_settings_{session_key}"] = current_settings.copy()

    log_event(
        "fb_settings_open",
        session_key=session_key,
        session_id=(user_sessions.get(session_key) or {}).get("session_id"),
        update=update,
    )

    reply_markup = build_settings_keyboard(
        session_key,
        current_settings,
        _fb_settings_options(),
        set_option_prefix="fb_set_option",
        confirm_prefix="fb_settings_confirm",
        cancel_prefix="fb_settings_cancel",
    )
    await query.edit_message_text("請選擇需要的選項：", reply_markup=reply_markup)


async def on_fb_set_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, key, value = query.data.split("|")

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

    temp_settings = context.user_data.get(f"temp_fb_settings_{session_key}") or {}
    temp_settings[key] = value
    context.user_data[f"temp_fb_settings_{session_key}"] = temp_settings

    log_event(
        "fb_settings_change",
        session_key=session_key,
        session_id=(user_sessions.get(session_key) or {}).get("session_id"),
        update=update,
        extra={"key": key, "value": value},
    )

    reply_markup = build_settings_keyboard(
        session_key,
        temp_settings,
        _fb_settings_options(),
        set_option_prefix="fb_set_option",
        confirm_prefix="fb_settings_confirm",
        cancel_prefix="fb_settings_cancel",
    )
    await query.edit_message_text("請選擇需要的選項：", reply_markup=reply_markup)


async def on_fb_settings_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    user_sessions[session_key]["settings"] = context.user_data[
        f"temp_fb_settings_{session_key}"
    ].copy()
    del context.user_data[f"temp_fb_settings_{session_key}"]

    log_event(
        "fb_settings_confirm",
        session_key=session_key,
        session_id=(user_sessions.get(session_key) or {}).get("session_id"),
        update=update,
        extra={"settings": user_sessions[session_key].get("settings")},
    )

    sd = user_sessions.get(session_key) or {}
    fb_url = sd.get("fb_url")
    if not fb_url:
        await query.edit_message_text("⚠️ 尚未取得 FB URL。請先輸入連結。")
        return

    await query.edit_message_text(
        _build_fb_url_confirm_text(fb_url, sd.get("settings") or {}),
        reply_markup=_build_fb_url_confirm_markup(session_key),
        disable_web_page_preview=True,
    )


async def on_fb_settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    original_settings = (user_sessions.get(session_key) or {}).get("settings") or {}
    temp_settings = context.user_data.get(f"temp_fb_settings_{session_key}") or {}

    if original_settings == temp_settings:
        del context.user_data[f"temp_fb_settings_{session_key}"]
        sd = user_sessions.get(session_key) or {}
        fb_url = sd.get("fb_url")
        if not fb_url:
            await query.edit_message_text("⚠️ 尚未取得 FB URL。請先輸入連結。")
            return
        await query.edit_message_text(
            _build_fb_url_confirm_text(fb_url, sd.get("settings") or {}),
            reply_markup=_build_fb_url_confirm_markup(session_key),
            disable_web_page_preview=True,
        )
        return

    log_event(
        "fb_settings_cancel_prompt",
        session_key=session_key,
        session_id=(user_sessions.get(session_key) or {}).get("session_id"),
        update=update,
    )
    buttons = [
        [
            InlineKeyboardButton(
                "是，放棄更改", callback_data=f"fb_settings_cancel_confirm|{session_key}"
            ),
            InlineKeyboardButton(
                "否，繼續編輯", callback_data=f"fb_menu_settings_back|{session_key}"
            ),
        ]
    ]
    await query.edit_message_text("設定已更改，是否放棄並返回？", reply_markup=InlineKeyboardMarkup(buttons))


async def on_fb_settings_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    del context.user_data[f"temp_fb_settings_{session_key}"]

    log_event(
        "fb_settings_cancel_confirm",
        session_key=session_key,
        session_id=(user_sessions.get(session_key) or {}).get("session_id"),
        update=update,
    )

    sd = user_sessions.get(session_key) or {}
    fb_url = sd.get("fb_url")
    if not fb_url:
        await query.edit_message_text("⚠️ 尚未取得 FB URL。請先輸入連結。")
        return

    await query.edit_message_text(
        _build_fb_url_confirm_text(fb_url, sd.get("settings") or {}),
        reply_markup=_build_fb_url_confirm_markup(session_key),
        disable_web_page_preview=True,
    )


async def on_fb_menu_settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    temp_settings = context.user_data.get(f"temp_fb_settings_{session_key}") or {}
    reply_markup = build_settings_keyboard(
        session_key,
        temp_settings,
        _fb_settings_options(),
        set_option_prefix="fb_set_option",
        confirm_prefix="fb_settings_confirm",
        cancel_prefix="fb_settings_cancel",
    )
    await query.edit_message_text("請選擇需要的選項：", reply_markup=reply_markup)
