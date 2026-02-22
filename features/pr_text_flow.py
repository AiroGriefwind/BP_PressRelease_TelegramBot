from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import config
from core.logging_ops import log_event
from core.session import new_session_struct, touch_session, user_sessions
from features.pr_text_detect import analyze_pr_text


def _ensure_session(update: Update, *, session_key: str):
    if session_key in user_sessions:
        return
    user_sessions[session_key] = new_session_struct()
    log_event(
        "session_start",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )


async def maybe_process_pr_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, text: Optional[str], source: str
) -> bool:
    message = update.message
    if not message or not text:
        return False

    analysis = analyze_pr_text(text)
    mode = analysis.get("mode")
    if config.PR_TEXT_DEBUG and (
        analysis.get("has_marker")
        or int(analysis.get("compact_len") or 0) >= max(80, int(config.PR_TEXT_MIN_CHARS / 2))
    ):
        try:
            await message.reply_text(
                "🧪 PR debug\n"
                f"source={source}\n"
                f"mode={mode}\n"
                f"len={analysis.get('compact_len')}, lines={analysis.get('non_empty_lines')}\n"
                f"has_marker={analysis.get('has_marker')}, "
                f"date_tail={analysis.get('date_tail')}, has_org_kw={analysis.get('has_org_kw')}"
            )
        except Exception:
            pass
    if mode != "auto":
        return False

    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"
    _ensure_session(update, session_key=session_key)
    sd = user_sessions.get(session_key) or {}
    log_event(
        "pr_text_long_detected",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
        extra={
            "source": source,
            "mode": mode,
            "compact_len": analysis.get("compact_len"),
            "non_empty_lines": analysis.get("non_empty_lines"),
            "has_marker": analysis.get("has_marker"),
            "marker_keyword": analysis.get("marker_keyword"),
        },
    )

    # 直接保留用户长信息原文，避免丢失开头标题行
    pr_body_text = (text or "").strip()
    sd["pr_body_text"] = pr_body_text
    user_sessions[session_key] = sd

    log_event(
        "pr_text_detected_auto",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
        extra={
            "source": source,
            "compact_len": analysis.get("compact_len"),
            "has_marker": analysis.get("has_marker"),
            "stored_body_chars": len(pr_body_text),
        },
    )
    await message.reply_text("✅ 已偵測到長信息公關稿，內容已暫存，確認送出時會加入郵件正文。")
    touch_session(
        context=context,
        session_key=session_key,
        user_id=user_id,
        chat_id=chat_id,
    )
    return True
