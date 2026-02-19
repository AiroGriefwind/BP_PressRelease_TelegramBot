import os
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from core.logging_ops import log_event
from core.session import new_session_struct, touch_session, user_sessions
from features.pr_text_detect import analyze_pr_text
from integrations.docx_builder import build_pr_docx
from integrations.drive import _make_unique_filename


def _safe_docx_name(title: str, prefix: str) -> str:
    base = (title or "").strip() or "新聞稿"
    base = re.sub(r"[\\/:*?\"<>|]+", "_", base)
    base = re.sub(r"\s+", " ", base).strip()
    if not base:
        base = "新聞稿"
    if len(base) > 64:
        base = base[:64].rstrip()
    cleaned_prefix = "公關稿__" if prefix == "公關稿" else "新聞稿__"
    return f"{cleaned_prefix}{base}.docx"


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


def _clear_pending_pr(sd: dict):
    sd["awaiting_pr_confirm"] = False
    sd["pending_pr_text"] = None
    sd["pending_pr_meta"] = None


def _append_pr_docx_for_session(session_data: dict, analysis: dict, text: str) -> tuple[str, str]:
    os.makedirs("temp", exist_ok=True)
    existing_names = [name for _, name in session_data.get("files", [])]
    existing_names += [name for _, name in session_data.get("sending_snapshot", [])]
    marker_keyword = str(analysis.get("marker_keyword") or "")
    file_name = _make_unique_filename(
        _safe_docx_name(analysis.get("title") or "", marker_keyword),
        existing_names,
    )
    file_path = os.path.join("temp", file_name)
    build_pr_docx(
        # 文档内不保留“新聞稿/公關稿”标记行，只保留标题与正文
        header="",
        title=str(analysis.get("title") or ""),
        title_lines=list(analysis.get("title_lines") or []),
        body_lines=list(analysis.get("body_lines") or []),
        output_path=file_path,
    )
    session_data["files"].append((file_path, file_name))
    return file_path, file_name


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
    if mode not in ("auto", "ask"):
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

    if mode == "auto":
        pr_label = str(analysis.get("marker_keyword") or "新聞稿")
        log_event(
            "pr_text_detected_auto",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
            extra={
                "source": source,
                "compact_len": analysis.get("compact_len"),
                "has_marker": analysis.get("has_marker"),
            },
        )
        _clear_pending_pr(sd)
        user_sessions[session_key] = sd
        wait_msg = await message.reply_text(
            f"已偵測到{pr_label}，正在轉換為 DOCX 檔案並加入附件列表……"
        )
        touch_session(
            context=context,
            session_key=session_key,
            user_id=user_id,
            chat_id=chat_id,
        )
        try:
            _file_path, file_name = _append_pr_docx_for_session(sd, analysis, text)
            user_sessions[session_key] = sd
            await message.reply_text(f"✅ {pr_label}已轉成 DOCX 並加入附件列表：{file_name}")
            log_event(
                "pr_docx_generated",
                session_key=session_key,
                session_id=sd.get("session_id"),
                update=update,
                extra={
                    "mode": "auto",
                    "source": source,
                    "file_name": file_name,
                    "has_marker": analysis.get("has_marker"),
                    "compact_len": analysis.get("compact_len"),
                },
            )
            return True
        except Exception as e:
            await message.reply_text(f"⚠️ 新聞稿轉檔失敗：{e}")
            log_event(
                "pr_docx_generate_failed",
                session_key=session_key,
                session_id=sd.get("session_id"),
                update=update,
                extra={"mode": "auto", "source": source, "error": str(e)},
            )
            return True

    sd["awaiting_pr_confirm"] = True
    sd["pending_pr_text"] = text
    sd["pending_pr_meta"] = {
        "source": source,
        "message_id": message.message_id,
        "compact_len": analysis.get("compact_len"),
        "has_marker": analysis.get("has_marker"),
    }
    user_sessions[session_key] = sd

    log_event(
        "pr_text_detected_need_confirm",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
        extra={
            "source": source,
            "compact_len": analysis.get("compact_len"),
            "has_marker": analysis.get("has_marker"),
            "date_tail": analysis.get("date_tail"),
            "has_org_kw": analysis.get("has_org_kw"),
        },
    )
    log_event(
        "pr_text_confirm_prompted",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
        extra={
            "source": source,
            "compact_len": analysis.get("compact_len"),
            "marker_keyword": analysis.get("marker_keyword"),
        },
    )
    buttons = [
        [
            InlineKeyboardButton("✅ 是，轉為公關稿 DOCX", callback_data=f"pr_confirm_yes|{session_key}"),
            InlineKeyboardButton("❌ 不是，忽略", callback_data=f"pr_confirm_no|{session_key}"),
        ]
    ]
    ask_msg = await message.reply_text(
        "偵測到這是一段較長內容，是否當作公關稿並轉成 DOCX 附件？",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    touch_session(
        context=context,
        session_key=session_key,
        user_id=user_id,
        chat_id=chat_id,
    )
    return True


async def on_pr_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    sd = user_sessions.get(session_key)
    if not sd:
        await query.edit_message_text("⚠️ 會話已過期，請重新傳送。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
    )
    text = sd.get("pending_pr_text")
    if not text:
        _clear_pending_pr(sd)
        user_sessions[session_key] = sd
        await query.edit_message_text("⚠️ 找不到待處理文本，請重新傳送。")
        return

    analysis = analyze_pr_text(text)
    pending_meta = dict(sd.get("pending_pr_meta") or {})
    await query.edit_message_text("正在轉換公關稿為 DOCX，請稍候……")
    try:
        _file_path, file_name = _append_pr_docx_for_session(sd, analysis, text)
        _clear_pending_pr(sd)
        user_sessions[session_key] = sd
        await query.edit_message_text(f"✅ 已轉成 DOCX 並加入附件列表：{file_name}")
        log_event(
            "pr_text_confirm_yes",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
            extra={"file_name": file_name},
        )
        log_event(
            "pr_docx_generated",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
            extra={
                "mode": "confirm_yes",
                "source": pending_meta.get("source"),
                "file_name": file_name,
                "compact_len": analysis.get("compact_len"),
            },
        )
    except Exception as e:
        await query.edit_message_text(f"⚠️ 轉檔失敗：{e}")
        log_event(
            "pr_docx_generate_failed",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
            extra={
                "mode": "confirm_yes",
                "source": pending_meta.get("source"),
                "error": str(e),
            },
        )


async def on_pr_confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    sd = user_sessions.get(session_key)
    if not sd:
        await query.edit_message_text("⚠️ 會話已過期。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
    )
    _clear_pending_pr(sd)
    user_sessions[session_key] = sd
    await query.edit_message_text("已忽略此消息，不當作公關稿處理。")
    log_event(
        "pr_text_confirm_no",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
    )
