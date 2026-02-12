from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.logging_ops import log_event
from core.session import touch_session, user_sessions
from ui.messages import SESSION_EXPIRED_TEXT


HELP_ITEMS = [
    {
        "id": "send_pr",
        "title": "1) 傳送公關稿（附件上傳 → 確認傳送）",
        "purpose": "把公關稿正文與附件傳送到目標信箱，並完成本次會話。",
        "precondition": "已開啟主界面，或已先傳送附件後再 @Bot 喚起主界面。",
        "steps": [
            "向 Bot 傳送公關稿正文檔（非圖片）與需要的附件/圖片。",
            "如尚未出現主界面，可在傳送附件後 @Bot 喚起。",
            "等待 Bot 提示「已添加…當前累計…」。",
            "在主界面確認「附件列表」是否完整。",
            "如需補充或刪除附件，先完成附件調整。",
            "點擊「確認」開始傳送。",
            "觀察進度提示，等待「傳送完成」。",
            "若提示「會話結束」，表示本次已完成。",
        ],
        "notes": [
            "若僅上傳圖片，系統會提示「尚未添加公關稿本體（非圖片附件）」。",
            "若附件過大，會自動改用 Drive 共享連結傳送。",
            "傳送中途失敗會提示「傳送失敗」，可重試。",
        ],
    },
    {
        "id": "fb_url",
        "title": "2) FB URL 流程（輸入/確認/傳送）",
        "purpose": "將 Facebook 分享連結傳送到目標信箱。",
        "precondition": "已開啟主界面，或已先傳送 FB URL 再 @Bot 喚起主界面。",
        "steps": [
            "在主界面點擊「FB URL」。",
            "按提示傳送包含 FB 分享連結的訊息。",
            "若已先傳送 FB URL，可直接 @Bot 喚起主界面繼續操作。",
            "Bot 自動識別後，出現「✅ 傳送 FB URL / ✏️ 重新輸入」。",
            "如需修改連結，點擊「✏️ 重新輸入」。",
            "如需改動類型/語言，點「⚙️ 設定」。",
            "確認無誤後點擊「✅ 傳送 FB URL」。",
            "看到「會話結束」表示已完成。",
        ],
        "notes": [
            "僅支援 Facebook 相關連結（facebook.com / fb.com / fb.watch / fb.me）。",
            "若未偵測到連結，會提示「未偵測到 URL」。",
        ],
    },
    {
        "id": "delete",
        "title": "3) 附件刪除（單個/全部）",
        "purpose": "在傳送前調整附件列表，刪除不需要的檔案。",
        "precondition": "主界面「附件列表」裡已有檔案。",
        "steps": [
            "在主界面點擊「刪除」。",
            "看到每個附件前有「❌」按鈕。",
            "點擊某個「❌ 檔名」進入刪除確認。",
            "選擇「是，刪除」完成單個刪除。",
            "需要全刪時點擊「🗑️ 全部刪除」。",
            "在提示中確認「⚠️ 確認全部刪除」。",
            "刪除完成後點擊「✅ 完成」返回主界面。",
        ],
        "notes": [
            "「全部刪除」會清空附件並結束會話。",
            "如果檔案已不存在，會提示「檔案不存在或已被刪除」。",
        ],
    },
    {
        "id": "settings",
        "title": "4) AI 模式設定（類型/優先度/語言/傳送方式）",
        "purpose": "在傳送前調整 AI 處理類型、優先度、語言與傳送方式。",
        "precondition": "已開啟主界面。",
        "steps": [
            "點擊「⚙️ 設定」。",
            "在列表中選擇：類型、優先度、語言、傳送方式。",
            "目前選中項會顯示「✅」。",
            "確認無誤點擊「確認」。",
            "若不想儲存，點擊「取消」。",
            "如已修改，系統會提示是否放棄更改。",
            "返回主界面後查看設定是否已更新。",
        ],
        "notes": [
            "傳送方式為「Google Drive」時，附件將透過 Drive 共享連結傳送。",
            "FB URL 的設定僅支援「類型/語言」，入口在 FB URL 選單內。",
        ],
    },
    {
        "id": "session_ui",
        "title": "5) UI 會話操作（刷新/結束/返回）",
        "purpose": "管理目前會話狀態與界面內容。",
        "precondition": "已開啟主界面或功能子選單。",
        "steps": [
            "點擊「🔄 刷新」更新主界面。",
            "點擊「🛑 結束會話」主動結束目前會話。",
            "進入子選單後可用「⬅️ 返回主選單/返回」回到主界面。",
            "若看到「新 UI 已生成」，請到最新訊息繼續操作。",
            "若會話逾時，會提示「10分鐘無操作，會話自動結束」。",
        ],
        "notes": [
            "刷新會重新生成 UI，舊 UI 可能無法繼續操作。",
        ],
    },
    {
        "id": "logs",
        "title": "6) Logs 查看（篩選/關鍵字/翻頁/詳情）",
        "purpose": "查看近期傳送記錄、失敗原因與詳情。",
        "precondition": "已開啟主界面。",
        "steps": [
            "點擊「🧾 Logs」。",
            "選擇時間範圍：「1天 / 3天 / 7天」。",
            "選擇狀態：「全部 / 成功 / 失敗」。",
            "點擊「🔍 關鍵字」輸入關鍵字進行篩選。",
            "點擊「❌ 清除」可清空關鍵字。",
            "使用「⬅️ 上一頁 / ➡️ 下一頁」翻頁。",
            "點擊列表中的記錄按鈕查看詳情。",
            "點擊「🔄 刷新」拉取最新記錄。",
            "點擊「⬅️ 返回」回到主界面。",
        ],
        "notes": [
            "初次進入或快取過期時會自動刷新。",
            "關鍵字匹配範圍是「標題/Subject」。",
        ],
    },
]


def _build_help_list_text() -> str:
    return "📘 幫助列表\n請選擇要查看的功能："


def _build_help_list_markup(session_key: str) -> InlineKeyboardMarkup:
    keyboard = []
    for item in HELP_ITEMS:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"📗 {item['title']}", callback_data=f"help_detail|{session_key}|{item['id']}"
                )
            ]
        )
    keyboard.append(
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data=f"help_back_main|{session_key}")]
    )
    return InlineKeyboardMarkup(keyboard)


def _find_help_item(item_id: str):
    for item in HELP_ITEMS:
        if item["id"] == item_id:
            return item
    return None


def _build_detail_text(item: dict) -> str:
    step_prefix = {
        1: "1️⃣",
        2: "2️⃣",
        3: "3️⃣",
        4: "4️⃣",
        5: "5️⃣",
        6: "6️⃣",
        7: "7️⃣",
        8: "8️⃣",
        9: "9️⃣",
        10: "🔟",
    }
    steps_text = "\n".join(
        f"{step_prefix.get(idx, f'{idx}.')} {step}" for idx, step in enumerate(item["steps"], start=1)
    )
    notes_text = "\n".join(f"- {note}" for note in item["notes"])
    return (
        f"📘 {item['title']}\n"
        "━━━━━━━━━━\n"
        f"🎯 目的\n{item['purpose']}\n\n"
        f"📌 前置條件\n{item['precondition']}\n\n"
        f"🪜 操作步驟\n{steps_text}\n\n"
        f"⚠️ 常見問題/注意事項\n{notes_text}\n\n"
        "🚧 引導模式：後續開放。"
    )


def _build_detail_markup(session_key: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("⬅️ 返回幫助列表", callback_data=f"help_back_list|{session_key}")],
        [InlineKeyboardButton("⬅️ 返回主選單", callback_data=f"help_back_main|{session_key}")],
    ]
    return InlineKeyboardMarkup(buttons)


async def on_menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "help_menu_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
        )
    except Exception:
        pass
    await query.edit_message_text(
        _build_help_list_text(), reply_markup=_build_help_list_markup(session_key)
    )


async def on_help_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, item_id = query.data.split("|")

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

    item = _find_help_item(item_id)
    if not item:
        try:
            log_event(
                "help_detail_missing",
                session_key=session_key,
                session_id=(user_sessions.get(session_key) or {}).get("session_id"),
                update=update,
                extra={"item_id": item_id},
            )
        except Exception:
            pass
        await query.edit_message_text(
            "⚠️ 功能說明不存在，請返回幫助列表重試。",
            reply_markup=_build_help_list_markup(session_key),
        )
        return

    try:
        log_event(
            "help_detail_open",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
            extra={"item_id": item_id, "title": item.get("title")},
        )
    except Exception:
        pass

    await query.edit_message_text(
        _build_detail_text(item), reply_markup=_build_detail_markup(session_key)
    )


async def on_help_back_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "help_back_list_click",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
        )
    except Exception:
        pass
    await query.edit_message_text(
        _build_help_list_text(), reply_markup=_build_help_list_markup(session_key)
    )


async def on_help_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "help_back_main_click",
            session_key=session_key,
            session_id=(user_sessions.get(session_key) or {}).get("session_id"),
            update=update,
        )
    except Exception:
        pass

    from features.pr_processing import handle_mention

    await handle_mention(update, context)
