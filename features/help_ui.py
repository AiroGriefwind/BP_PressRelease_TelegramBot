from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.logging_ops import log_event
from core.session import touch_session, user_sessions
from ui.messages import SESSION_EXPIRED_TEXT


HELP_ITEMS = [
    {
        "id": "send_pr",
        "title": "1) 如何傳送公關稿？",
        "purpose": "如何傳送公關稿？",
        "steps": [
            "先開啟主界面；若尚未出現，請先傳送附件後再 @Bot 喚起。",
            "向 Bot 傳送公關稿正文檔（非圖片）與需要的附件/圖片。",
            "等待 Bot 提示「已添加…當前累計…」。",
            "在主界面確認「附件列表」是否完整。",
            "如需補充或刪除附件，先完成附件調整。",
            "點擊「確認」開始傳送。",
            "看到「傳送完成」與「會話結束」即完成。",
        ],
    },
    {
        "id": "send_pr_long_text",
        "title": "2) 如何傳送長文本公關稿？",
        "purpose": "如何傳送長文本公關稿？",
        "steps": [
            "直接在群組傳送一則長文本（可含「新聞稿」或「公關稿」標記）。",
            "若命中明確標記，Bot 會自動提示「已偵測…正在轉換 DOCX」。",
            "若沒有明確標記但內容夠長，Bot 會詢問是否當作公關稿。",
            "點擊「✅ 是」後，文本會轉成 DOCX 並加入附件列表。",
            "之後照常上傳圖片/附件，並在主界面按「確認」傳送。",
            "如判斷錯誤可點「❌ 不是，忽略」，不會加入附件列表。",
        ],
    },
    {
        "id": "fb_url",
        "title": "3) 如何傳送 FB URL？",
        "purpose": "如何傳送 FB URL？",
        "steps": [
            "先開啟主界面；若你已先傳送 FB URL，可直接 @Bot 喚起後繼續。",
            "在主界面點擊「FB URL」。",
            "按提示傳送包含 FB 分享連結的訊息。",
            "Bot 自動識別後，出現「✅ 傳送 FB URL / ✏️ 重新輸入」。",
            "如需修改連結，點擊「✏️ 重新輸入」。",
            "如需改動類型/語言，點「⚙️ 設定」。",
            "確認無誤後點擊「✅ 傳送 FB URL」。",
            "看到「會話結束」表示已完成。",
        ],
    },
    {
        "id": "delete",
        "title": "4) 如何刪除附件？",
        "purpose": "如何刪除附件？",
        "steps": [
            "先確認主界面「附件列表」裡已有檔案。",
            "在主界面點擊「刪除」。",
            "看到每個附件前有「❌」按鈕。",
            "點擊某個「❌ 檔名」進入刪除確認。",
            "選擇「是，刪除」完成單個刪除。",
            "需要全刪時點擊「🗑️ 全部刪除」。",
            "在提示中確認「⚠️ 確認全部刪除」。",
            "刪除完成後點擊「✅ 完成」返回主界面。",
        ],
    },
    {
        "id": "settings",
        "title": "5) 如何調整 AI 設定？",
        "purpose": "如何調整 AI 設定？",
        "steps": [
            "先開啟主界面。",
            "點擊「⚙️ 設定」。",
            "在列表中選擇：類型、優先度、語言、傳送方式。",
            "目前選中項會顯示「✅」。",
            "確認無誤點擊「確認」。",
            "若不想儲存，點擊「取消」。",
            "如已修改，系統會提示是否放棄更改。",
            "返回主界面後查看設定是否已更新。",
        ],
    },
    {
        "id": "session_ui",
        "title": "6) 如何管理會話（刷新/結束/返回）？",
        "purpose": "如何管理會話（刷新/結束/返回）？",
        "steps": [
            "先開啟主界面或功能子選單。",
            "點擊「🔄 刷新」更新主界面。",
            "點擊「🛑 結束會話」主動結束目前會話。",
            "進入子選單後可用「⬅️ 返回主選單/返回」回到主界面。",
            "若看到「新 UI 已生成」，請到最新訊息繼續操作。",
            "若會話逾時，會提示「10分鐘無操作，會話自動結束」。",
        ],
    },
    {
        "id": "logs",
        "title": "7) 如何查看 Logs？",
        "purpose": "如何查看 Logs？",
        "steps": [
            "先開啟主界面。",
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
    return (
        f"📘 {item['purpose']}\n"
        "━━━━━━━━━━\n"
        "✅ 這樣做就對了\n\n"
        f"🪜 操作步驟\n{steps_text}\n\n"
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
