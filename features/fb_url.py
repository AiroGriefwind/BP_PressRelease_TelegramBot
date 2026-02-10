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
from core.time_utils import now_hk
from integrations.gmail import get_gmail_service, send_email_with_fb_url
from ui.messages import SESSION_EXPIRED_TEXT, try_edit_message_text_markup


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
        buttons = [
            [
                InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}"),
                InlineKeyboardButton("✏️ 重新输入", callback_data=f"fb_url_reset|{session_key}"),
            ],
            [
                InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}"),
            ],
        ]
        await query.edit_message_text(
            f"当前 FB URL：\n{fb_url}\n\n是否发送到 {config.TARGET_EMAIL}？",
            reply_markup=InlineKeyboardMarkup(buttons),
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

    buttons = [[InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}")]]
    await query.edit_message_text(
        "请发送 FB 分享链接（URL）给我。",
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

    buttons = [[InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}")]]
    await query.edit_message_text(
        "请重新发送 FB 分享链接（URL）给我。",
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
        await query.edit_message_text("⚠️ 尚未获取到 FB URL。请先输入链接。")
        return

    sender_info = _build_sender_info_from_message(query.message, fallback_user=query.from_user)
    gmail_service = get_gmail_service()
    success, err = await asyncio.to_thread(send_email_with_fb_url, gmail_service, fb_url, sender_info)

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
            reason_text=f"✅ FB URL 已发送到 {config.TARGET_EMAIL}\n会话结束。",
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
        buttons = [[InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}")]]
        await query.edit_message_text(
            "❌ FB URL 发送失败，请稍后重试。",
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
    if not sd or not sd.get("awaiting_fb_url"):
        return

    touch_session(context=context, session_key=session_key, user_id=user_id, chat_id=chat_id)

    raw_url = _extract_first_url(message.text or "")
    if not raw_url:
        await message.reply_text("⚠️ 没检测到 URL。请直接发送一条包含 Facebook 分享链接的消息。")
        return

    norm = _normalize_fb_url(raw_url)
    if not _looks_like_facebook_url(norm):
        await message.reply_text("⚠️ 目前只支持 Facebook 相关链接。请重新发送 FB 分享链接。")
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
        buttons = [
            [
                InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}"),
                InlineKeyboardButton("✏️ 重新输入", callback_data=f"fb_url_reset|{session_key}"),
            ],
            [
                InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}"),
            ],
        ]
        await try_edit_message_text_markup(
            context.application,
            int(ui_chat_id),
            int(ui_message_id),
            f"已收到 FB URL：\n{norm}\n\n是否发送到 {config.TARGET_EMAIL}？",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
    else:
        buttons = [[InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}")]]
        sent = await message.reply_text(
            f"已收到 FB URL：\n{norm}\n\n是否发送到 {config.TARGET_EMAIL}？",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
        touch_session(
            context=context,
            session_key=session_key,
            user_id=user_id,
            chat_id=chat_id,
            message_id=sent.message_id,
        )
