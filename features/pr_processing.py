import asyncio
import os
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from core.logging_ops import log_event
from core.session import end_session, last_seen_fb_url, new_session_struct, touch_session, user_sessions
from core.time_utils import now_hk
from features.batch_images import send_drive_mode
from features.fb_url import (
    _build_fb_url_confirm_markup,
    _build_fb_url_confirm_text,
    _extract_first_url,
    _looks_like_facebook_url,
    _normalize_fb_url,
)
from integrations.drive import (
    _format_size,
    _has_non_photo,
    _make_unique_filename,
    _total_size_bytes,
)
from integrations.gmail import get_gmail_service, send_email_with_attachments
from ui.keyboard import build_settings_keyboard
from ui.messages import (
    SESSION_EXPIRED_TEXT,
    render_progress_bar,
    try_edit_message_text,
    try_edit_message_text_markup,
    try_edit_query_message,
)

ADD_MSG_IDLE_SECONDS = 5
UI_EXPIRED_TEXT = "新 UI 已生成，请在最新消息操作。"


def _build_main_ui(session_key: str, session_data: dict) -> tuple[str, InlineKeyboardMarkup]:
    files = session_data.get("files") or []
    settings = session_data.get("settings") or config.DEFAULT_SETTINGS.copy()

    file_names = [name for _, name in files]
    attach_list = "\n".join(file_names) if file_names else "暂无附件"
    total_bytes = _total_size_bytes(files)
    total_size_text = _format_size(total_bytes) if files else ""
    has_non_photo = _has_non_photo(file_names)
    auto_drive = total_bytes > (config.DRIVE_AUTO_SIZE_MB * 1024 * 1024) if files else False
    force_drive = settings.get("drive_upload") == "Google Drive"
    drive_mode = config.USE_DRIVE_SHARE or auto_drive or force_drive

    settings_text = (
        f"類型：{settings['type']}\n"
        f"優先度：{settings['priority']}\n"
        f"語言：{settings['language']}\n"
        f"发送方式：{settings.get('drive_upload', '普通')}"
    )
    fb_url_line = ""
    try:
        if session_data.get("fb_url"):
            fb_url_line = f"\n\nFB URL：\n{session_data.get('fb_url')}"
    except Exception:
        fb_url_line = ""

    remind_line = ""
    if not has_non_photo:
        if total_size_text:
            remind_line = f"\n\n⚠️ 尚未添加公关稿本体（非图片附件）。当前总大小：{total_size_text}"
        else:
            remind_line = "\n\n⚠️ 尚未添加公关稿本体（非图片附件）。"
    size_line = ""
    if drive_mode and files:
        size_line = (
            f"\n\n总大小：{total_size_text}\n已超过 {config.DRIVE_AUTO_SIZE_MB}MB，将改用 Drive 共享链接发送。"
        )

    ui_msg = f"附件列表：\n{attach_list}{remind_line}{size_line}\n\n---\n\n{settings_text}{fb_url_line}"

    buttons = [
        [
            InlineKeyboardButton("确认", callback_data=f"confirm_send|{session_key}"),
            InlineKeyboardButton(config.FB_URL_BUTTON_TEXT, callback_data=f"fb_url_menu|{session_key}"),
            InlineKeyboardButton("删除", callback_data=f"menu_delete_mode|{session_key}"),
        ],
        [
            InlineKeyboardButton("⚙️ 设置", callback_data=f"menu_settings|{session_key}"),
            InlineKeyboardButton("🧾 Logs", callback_data=f"menu_logs|{session_key}"),
            InlineKeyboardButton("🔄 刷新", callback_data=f"main_refresh|{session_key}"),
            InlineKeyboardButton("🛑 结束会话", callback_data=f"end_session|{session_key}"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    return ui_msg, reply_markup


async def _on_add_msg_idle(context: ContextTypes.DEFAULT_TYPE):
    data = getattr(context.job, "data", None) or {}
    session_key = data.get("session_key")
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    count = data.get("count")
    if not session_key or chat_id is None or message_id is None:
        return

    session_data = user_sessions.get(session_key)
    if not session_data:
        return
    if session_data.get("add_msg_id") != message_id:
        return

    text = f"✅ 已完成所有附件加载（{count}个）"
    await try_edit_message_text(
        context.application,
        chat_id=int(chat_id),
        message_id=int(message_id),
        text=text,
    )
    session_data["add_msg_done"] = True
    session_data["add_msg_done_job"] = None

    ui_chat_id = session_data.get("ui_chat_id")
    ui_message_id = session_data.get("ui_message_id")
    if ui_chat_id is not None and ui_message_id is not None:
        ui_msg, reply_markup = _build_main_ui(session_key, session_data)
        await try_edit_message_text_markup(
            context.application,
            int(ui_chat_id),
            int(ui_message_id),
            ui_msg,
            reply_markup=reply_markup,
        )
        try:
            log_event(
                "ui_auto_refresh",
                session_key=session_key,
                session_id=session_data.get("session_id"),
                update=None,
                extra={"reason": "add_msg_complete"},
            )
        except Exception:
            pass


def _schedule_add_msg_idle(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_key: str,
    chat_id: int,
    message_id: int,
    count: int,
):
    session_data = user_sessions.get(session_key)
    if not session_data or not message_id:
        return

    job = session_data.get("add_msg_done_job")
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass
        session_data["add_msg_done_job"] = None

    try:
        job = context.application.job_queue.run_once(
            _on_add_msg_idle,
            when=ADD_MSG_IDLE_SECONDS,
            data={
                "session_key": session_key,
                "chat_id": chat_id,
                "message_id": message_id,
                "count": count,
            },
            name=f"add_msg_idle:{session_key}",
        )
        session_data["add_msg_done_job"] = job
    except Exception:
        session_data["add_msg_done_job"] = None


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"

    if session_key not in user_sessions:
        user_sessions[session_key] = new_session_struct()
        log_event(
            "session_start",
            session_key=session_key,
            session_id=user_sessions[session_key].get("session_id"),
            update=update,
        )
        try:
            last_seen_fb_url.pop(session_key, None)
        except Exception:
            pass

    session_data = user_sessions[session_key]

    touch_session(context=context, session_key=session_key, user_id=user_id, chat_id=chat_id)

    os.makedirs("temp", exist_ok=True)
    file_id, file_name = None, None
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
    elif message.photo:
        photo_file = message.photo[-1]
        file_id = photo_file.file_id
        try:
            session_data["photo_seq"] = int(session_data.get("photo_seq") or 0) + 1
        except Exception:
            session_data["photo_seq"] = 1
        file_name = f"photo_{session_data['photo_seq']}.jpg"

    if file_id and file_name:
        existing_names = [name for _, name in session_data.get("files", [])]
        existing_names += [name for _, name in session_data.get("sending_snapshot", [])]
        file_name = _make_unique_filename(file_name, existing_names)
        file = await context.bot.get_file(file_id)
        file_path = f"temp/{file_name}"
        await file.download_to_drive(file_path)

        session_data["files"].append((file_path, file_name))

        try:
            session_data["add_msg_count"] = int(session_data.get("add_msg_count") or 0) + 1
        except Exception:
            session_data["add_msg_count"] = 1

        job = session_data.get("add_msg_done_job")
        if job is not None:
            try:
                job.schedule_removal()
            except Exception:
                pass
            session_data["add_msg_done_job"] = None

        if session_data.get("add_msg_done"):
            session_data["add_msg_done"] = False
            session_data["add_msg_id"] = None
            session_data["add_msg_ts"] = 0.0

        now_ts = time.time()
        should_update = False
        if not session_data.get("add_msg_id"):
            should_update = True
        elif (now_ts - float(session_data.get("add_msg_ts") or 0)) >= 1.0:
            should_update = True
        elif session_data["add_msg_count"] % 5 == 0:
            should_update = True

        if should_update:
            text = f"已添加: {file_name}\n当前累计: {session_data['add_msg_count']} 个"
            if session_data.get("add_msg_id"):
                await try_edit_message_text(
                    context.application,
                    chat_id=message.chat.id,
                    message_id=session_data["add_msg_id"],
                    text=text,
                )
            else:
                sent = await message.reply_text(text)
                session_data["add_msg_id"] = sent.message_id
            session_data["add_msg_ts"] = now_ts

        if session_data.get("add_msg_id"):
            _schedule_add_msg_idle(
                context,
                session_key=session_key,
                chat_id=message.chat.id,
                message_id=session_data["add_msg_id"],
                count=session_data["add_msg_count"],
            )

        try:
            sd = session_data or {}
            log_event(
                "file_added",
                session_key=session_key,
                session_id=sd.get("session_id"),
                update=update,
                extra={
                    "file_name": file_name,
                    "file_path": file_path,
                    "file_kind": "document"
                    if message.document
                    else ("photo" if message.photo else "unknown"),
                    "total_files": len(sd.get("files") or []),
                },
            )
        except Exception:
            pass


async def on_menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    current_settings = user_sessions[session_key]["settings"]
    context.user_data[f"temp_settings_{session_key}"] = current_settings.copy()

    log_event(
        "settings_open",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )

    await show_settings_menu(update, context, session_key, current_settings)


async def on_main_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    try:
        log_event(
            "ui_refresh_click",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
        )
    except Exception:
        pass
    await handle_mention(update, context)


async def show_settings_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_key: str, settings: dict
):
    query = update.callback_query
    reply_markup = build_settings_keyboard(session_key, settings)
    await query.edit_message_text("请选择需要的选项：", reply_markup=reply_markup)


async def on_set_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    temp_settings = context.user_data[f"temp_settings_{session_key}"]
    temp_settings[key] = value

    log_event(
        "settings_change",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
        extra={"key": key, "value": value},
    )

    await show_settings_menu(update, context, session_key, temp_settings)


async def on_settings_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"temp_settings_{session_key}"
    ].copy()

    del context.user_data[f"temp_settings_{session_key}"]

    log_event(
        "settings_confirm",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
        extra={"settings": user_sessions[session_key].get("settings")},
    )

    await handle_mention(update, context)


async def on_settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    original_settings = user_sessions[session_key]["settings"]
    temp_settings = context.user_data.get(f"temp_settings_{session_key}")

    if original_settings == temp_settings:
        del context.user_data[f"temp_settings_{session_key}"]
        await handle_mention(update, context)
    else:
        log_event(
            "settings_cancel_prompt",
            session_key=session_key,
            session_id=user_sessions[session_key].get("session_id"),
            update=update,
        )
        buttons = [
            [
                InlineKeyboardButton(
                    "是，放弃更改", callback_data=f"settings_cancel_confirm|{session_key}"
                ),
                InlineKeyboardButton(
                    "否，继续编辑", callback_data=f"menu_settings_back|{session_key}"
                ),
            ]
        ]
        await query.edit_message_text(
            "设置已更改，是否放弃并返回？", reply_markup=InlineKeyboardMarkup(buttons)
        )


async def on_settings_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    del context.user_data[f"temp_settings_{session_key}"]

    log_event(
        "settings_cancel_confirm",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )

    await handle_mention(update, context)


async def on_menu_settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    temp_settings = context.user_data[f"temp_settings_{session_key}"]

    log_event(
        "settings_back_to_edit",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )
    await show_settings_menu(update, context, session_key, temp_settings)


async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        message = update.message
        user_id = message.from_user.id
        chat_id = message.chat.id
    else:
        query = update.callback_query
        message = query.message
        user_id = query.from_user.id
        chat_id = message.chat.id

    session_key = f"{chat_id}_{user_id}"

    if session_key not in user_sessions:
        user_sessions[session_key] = new_session_struct()
        log_event(
            "session_start",
            session_key=session_key,
            session_id=user_sessions[session_key].get("session_id"),
            update=update,
        )

    session_data = user_sessions[session_key]
    files = session_data["files"]
    settings = session_data["settings"]

    if update.message:
        old_chat_id = session_data.get("ui_chat_id")
        old_message_id = session_data.get("ui_message_id")
        if old_chat_id is not None and old_message_id is not None:
            await try_edit_message_text_markup(
                context.application,
                int(old_chat_id),
                int(old_message_id),
                UI_EXPIRED_TEXT,
                reply_markup=None,
            )
            try:
                log_event(
                    "ui_expired",
                    session_key=session_key,
                    session_id=session_data.get("session_id"),
                    update=update,
                    extra={"old_message_id": old_message_id},
                )
            except Exception:
                pass

    if update.message:
        candidate_texts = []
        try:
            candidate_texts.append(update.message.text or "")
        except Exception:
            pass
        try:
            if update.message.reply_to_message and getattr(
                update.message.reply_to_message, "text", None
            ):
                candidate_texts.append(update.message.reply_to_message.text or "")
        except Exception:
            pass

        found_url = None
        for t in candidate_texts:
            u = _extract_first_url(t)
            if u:
                found_url = u
                break

        if not found_url:
            try:
                rec = last_seen_fb_url.get(session_key)
                if rec and rec.get("url") and rec.get("dt"):
                    now_dt = datetime.now(now_hk().tzinfo)
                    if (now_dt - rec["dt"]).total_seconds() <= config.FB_URL_RECENT_SECONDS:
                        found_url = rec["url"]
            except Exception:
                pass

        if found_url:
            norm = _normalize_fb_url(found_url)
            if _looks_like_facebook_url(norm):
                session_data["fb_url"] = norm
                session_data["awaiting_fb_url"] = False
                user_sessions[session_key] = session_data

                log_event(
                    "fb_url_detected",
                    session_key=session_key,
                    session_id=session_data.get("session_id"),
                    update=update,
                    extra={"fb_url": norm},
                )

                buttons = _build_fb_url_confirm_markup(session_key)
                sent = await message.reply_text(
                    _build_fb_url_confirm_text(norm, settings, detected=True),
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

    ui_msg, reply_markup = _build_main_ui(session_key, session_data)

    if update.callback_query:
        await try_edit_message_text_markup(
            context.application,
            message.chat.id,
            message.message_id,
            ui_msg,
            reply_markup=reply_markup,
        )
        touch_session(
            context=context,
            session_key=session_key,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message.message_id,
        )
    else:
        sent = await message.reply_text(ui_msg, reply_markup=reply_markup)
        touch_session(
            context=context,
            session_key=session_key,
            user_id=user_id,
            chat_id=chat_id,
            message_id=sent.message_id,
        )
        try:
            log_event(
                "ui_summoned",
                session_key=session_key,
                session_id=session_data.get("session_id"),
                update=update,
                extra={"new_message_id": sent.message_id},
            )
        except Exception:
            pass


async def on_menu_delete_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    session_data = user_sessions.get(
        session_key, {"files": [], "settings": config.DEFAULT_SETTINGS.copy()}
    )
    files = session_data["files"]

    try:
        log_event(
            "delete_menu_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={"file_count": len(files or [])},
        )
    except Exception:
        pass

    keyboard = []
    for index, (_, filename) in enumerate(files):
        display_name = filename
        max_len = 40
        if len(display_name) > max_len:
            display_name = display_name[: max_len - 1] + "…"

        btn_text = f"❌ {display_name}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    btn_text, callback_data=f"ask_del_one|{session_key}|{index}"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("🗑️ 全部删除", callback_data=f"ask_del_all|{session_key}"),
            InlineKeyboardButton("✅ 完成", callback_data=f"back_to_main|{session_key}"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = "点击红色 X 删除特定附件：" if files else "暂无附件可删除。"

    await try_edit_message_text_markup(
        context.application,
        query.message.chat.id,
        query.message.message_id,
        msg_text,
        reply_markup=reply_markup,
    )


async def on_ask_del_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, index_str = query.data.split("|")

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    index = int(index_str)

    session_data = user_sessions.get(
        session_key, {"files": [], "settings": config.DEFAULT_SETTINGS.copy()}
    )
    files = session_data["files"]

    if index >= len(files):
        await try_edit_message_text_markup(
            context.application,
            query.message.chat.id,
            query.message.message_id,
            "⚠️ 文件不存在或已被删除。",
            reply_markup=None,
        )
        return

    target_file_name = files[index][1]

    buttons = [
        [
            InlineKeyboardButton("是，删除", callback_data=f"do_del_one|{session_key}|{index}"),
            InlineKeyboardButton("否，返回", callback_data=f"menu_delete_mode|{session_key}"),
        ]
    ]
    await try_edit_message_text_markup(
        context.application,
        query.message.chat.id,
        query.message.message_id,
        f"确定要删除 {target_file_name} 吗？",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_do_del_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("已删除")
    _, session_key, index_str = query.data.split("|")

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    index = int(index_str)

    session_data = user_sessions.get(
        session_key, {"files": [], "settings": config.DEFAULT_SETTINGS.copy()}
    )
    files = session_data["files"]

    if index < len(files):
        target_file_name = files[index][1]
        file_path = files[index][0]
        try:
            os.remove(file_path)
        except Exception:
            pass
        files.pop(index)
        user_sessions[session_key]["files"] = files

        try:
            log_event(
                "file_deleted",
                session_key=session_key,
                session_id=(user_sessions.get(session_key) or {}).get("session_id"),
                update=update,
                extra={
                    "file_name": target_file_name,
                    "index": index,
                    "remaining_files": len(files or []),
                },
            )
        except Exception:
            pass

    await on_menu_delete_mode(update, context)


async def on_ask_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    session_data = user_sessions.get(
        session_key, {"files": [], "settings": config.DEFAULT_SETTINGS.copy()}
    )
    files = session_data["files"]

    try:
        log_event(
            "delete_all_prompt",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={"file_count": len(files or [])},
        )
    except Exception:
        pass

    if not files:
        await query.answer("列表已经是空的了", show_alert=True)
        return

    buttons = [
        [
            InlineKeyboardButton("⚠️ 确认全部删除", callback_data=f"do_del_all|{session_key}"),
            InlineKeyboardButton("取消", callback_data=f"menu_delete_mode|{session_key}"),
        ]
    ]
    await query.edit_message_text("⚠️ 确定要清空所有附件吗？此操作不可逆。", reply_markup=InlineKeyboardMarkup(buttons))


async def on_do_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_key = query.data.split("|")[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    await query.answer("所有附件已清空")
    await end_session(
        application=context.application,
        session_key=session_key,
        reason_text="🗑️ 已全部删除。会话结束。",
        reason_code="delete_all",
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )


async def on_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]
    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    await handle_mention(update, context)


async def on_confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    session_data = user_sessions.get(session_key)
    if not session_data or not session_data["files"]:
        await query.edit_message_text("⚠️ 没有附件，请先上传文件或图片。")
        return

    sending_snapshot = list(session_data.get("files") or [])
    session_data["sending_snapshot"] = sending_snapshot
    session_data["files"] = []
    session_data["sending"] = True

    files = sending_snapshot
    settings = session_data["settings"]
    message = query.message

    file_paths, file_names = zip(*files)
    total_bytes = _total_size_bytes(files)
    auto_drive = total_bytes > (config.DRIVE_AUTO_SIZE_MB * 1024 * 1024)
    force_drive = settings.get("drive_upload") == "Google Drive"
    drive_mode = config.USE_DRIVE_SHARE or auto_drive or force_drive

    total_units = (2 + (len(file_names) * 2) + 1) if drive_mode else 3
    done_units = 0

    progress_active = True
    progress_state = {"percent": 0, "status": "准备附件", "dirty": True}

    async def _progress_loop():
        last_text = ""
        while progress_active:
            if progress_state["dirty"]:
                text = f"进度: {render_progress_bar(progress_state['percent'])}\n当前步骤：{progress_state['status']}"
                if text != last_text:
                    await try_edit_query_message(query, text)
                    last_text = text
                progress_state["dirty"] = False
            await asyncio.sleep(5.0)

    progress_task = asyncio.create_task(_progress_loop())

    def _progress_update(status: str, inc: int = 0):
        nonlocal done_units
        nonlocal progress_state
        if not progress_active:
            return
        if inc:
            done_units = min(total_units, done_units + inc)
        percent = int(round((done_units / total_units) * 100)) if total_units else 0
        progress_state["percent"] = percent
        progress_state["status"] = status
        progress_state["dirty"] = True

    _progress_update("准备附件", 0)

    sender_info = {
        "name": (message.reply_to_message.from_user.first_name or "")
        + (f" {message.reply_to_message.from_user.last_name}" if message.reply_to_message.from_user.last_name else ""),
        "username": message.reply_to_message.from_user.username or "unknown",
        "chat_title": message.chat.title or "private",
        "date": message.date.astimezone(now_hk().tzinfo).strftime("%Y-%m-%d %H:%M:%S"),
    }

    gmail_service = get_gmail_service()

    log_event(
        "send_attempt",
        session_key=session_key,
        session_id=session_data.get("session_id"),
        update=update,
        extra={
            "file_names": list(file_names),
            "file_count": len(file_names),
            "settings": settings,
        },
    )
    if drive_mode:
        success, err, drive_folder_link = await send_drive_mode(
            update=update,
            query=query,
            session_key=session_key,
            session_data=session_data,
            file_paths=list(file_paths),
            file_names=list(file_names),
            settings=settings,
            sender_info=sender_info,
            message_date=message.date,
            progress_update=_progress_update,
        )
        if not success:
            return
    else:
        _progress_update("打包附件", 1)
        _progress_update("发送邮件", 1)
        success, err = await asyncio.to_thread(
            send_email_with_attachments,
            gmail_service,
            file_paths,
            sender_info,
            file_names,
            settings,
        )
        drive_folder_link = None

    if success:
        extra_link = ""
        if drive_mode and drive_folder_link:
            extra_link = f"\n\nDrive 文件夹：\n{drive_folder_link}"
        done_units = total_units
        _progress_update("发送完成", 0)
        progress_active = False
        try:
            progress_task.cancel()
        except Exception:
            pass
        for fp, _ in (sending_snapshot or []):
            try:
                os.remove(fp)
            except Exception:
                pass
        session_data["sending_snapshot"] = []
        session_data["sending"] = False

        if session_data.get("files"):
            await try_edit_query_message(
                query,
                f"✅ 本批已发送到 {config.TARGET_EMAIL}\n检测到新增附件，已保留在列表中，请继续发送。{extra_link}",
            )
            await handle_mention(update, context)
        else:
            await end_session(
                application=context.application,
                session_key=session_key,
                reason_text=f"✅ 文件已发送到 {config.TARGET_EMAIL}\n会话结束。{extra_link}",
                reason_code="send_success",
                user_id=query.from_user.id,
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
            )
    else:
        progress_active = False
        try:
            progress_task.cancel()
        except Exception:
            pass
        await query.edit_message_text("❌ 发送失败,请重试")
        if sending_snapshot:
            session_data["files"] = sending_snapshot + (session_data.get("files") or [])
        session_data["sending_snapshot"] = []
        session_data["sending"] = False
        log_event(
            "send_failed",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"error": err},
        )


async def on_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    await end_session(
        application=context.application,
        session_key=session_key,
        reason_text="🛑 会话已结束。",
        reason_code="manual_end",
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
