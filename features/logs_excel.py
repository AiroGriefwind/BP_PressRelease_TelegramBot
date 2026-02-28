import asyncio
import os
import re
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.logging_ops import log_event
from core.session import end_session, touch_session, user_sessions
from core.time_utils import now_hk
from integrations.gmail import fetch_rthk_emails_for_excel
from ui.messages import SESSION_EXPIRED_TEXT


def parse_rthk_json_data(json_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从JSON附件数据解析为Excel行数据"""
    rows: List[Dict[str, Any]] = []
    if not json_data or not isinstance(json_data, list):
        return rows

    for category in json_data:
        target = category.get("target", "")
        data_list = category.get("data", [])
        if not isinstance(data_list, list):
            continue

        for item in data_list:
            rows.append(
                {
                    "Target": target,
                    "Original Subject": item.get("title", ""),
                    "Post ID": item.get("post_id", ""),
                    "WP Title": item.get("wp_title", ""),
                    "url": item.get("url", ""),
                    "time text": item.get("time_text", ""),
                    "新聞內文": item.get("body", ""),
                    "改寫後內文": item.get("wp_body", ""),
                }
            )

    return rows


def generate_rthk_excel(items: List[Dict[str, Any]]) -> tuple[str, int]:
    os.makedirs("temp", exist_ok=True)
    filename = f"RTHK News改寫狀況一覽_{now_hk().strftime('%m%d_%H%M%S')}.xlsx"
    output_path = os.path.join("temp", filename)

    wb = Workbook()
    ws = wb.active
    ws.title = "RTHK Logs"

    headers = [
        "Target",
        "Original Subject",
        "Post ID",
        "WP Title",
        "url",
        "time text",
        "新聞內文",
        "改寫後內文",
    ]
    ws.append(headers)

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for item in items:
        ws.append([item.get(h, "") for h in headers])

    # 设置列宽
    ws.column_dimensions["A"].width = 14  # Target
    ws.column_dimensions["B"].width = 40  # Original Subject
    ws.column_dimensions["C"].width = 16  # Post ID
    ws.column_dimensions["D"].width = 52  # WP Title
    ws.column_dimensions["E"].width = 52  # url
    ws.column_dimensions["F"].width = 24  # time text
    ws.column_dimensions["G"].width = 60  # 新聞內文
    ws.column_dimensions["H"].width = 60  # 改寫後內文

    # 设置文本换行：WP Title, url, 新聞內文, 改寫後內文
    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=4).alignment = Alignment(wrap_text=True, vertical="top")  # WP Title
        ws.cell(row=row_idx, column=5).alignment = Alignment(wrap_text=True, vertical="top")  # url
        ws.cell(row=row_idx, column=7).alignment = Alignment(wrap_text=True, vertical="top")  # 新聞內文
        ws.cell(row=row_idx, column=8).alignment = Alignment(wrap_text=True, vertical="top")  # 改寫後內文

    wb.save(output_path)
    return output_path, len(items)


async def on_excel_export_rthk(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    session_data = user_sessions.get(session_key) or {}

    try:
        log_event(
            "logs_excel_rthk_click",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
        )
    except Exception:
        pass

    await query.edit_message_text("正在匯出 RTHK Logs（最近24小時，HKT）...\n請稍候。")

    output_path = ""
    try:
        log_event(
            "logs_excel_gmail_fetch_start",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "hours": 24},
        )

        emails = await asyncio.to_thread(fetch_rthk_emails_for_excel, 24, 500)
        log_event(
            "logs_excel_gmail_fetch_done",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "email_count": len(emails)},
        )

        all_items: List[Dict[str, Any]] = []
        for one in emails:
            json_data = one.get("json_data")
            all_items.extend(parse_rthk_json_data(json_data))

        log_event(
            "logs_excel_parse_done",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "item_count": len(all_items)},
        )

        output_path, row_count = await asyncio.to_thread(generate_rthk_excel, all_items)
        file_size = os.path.getsize(output_path)
        log_event(
            "logs_excel_generate_done",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "row_count": row_count, "path": output_path, "size": file_size},
        )

        with open(output_path, "rb") as fp:
            sent_message = await context.bot.send_document(
                chat_id=query.message.chat.id,
                document=fp,
                filename=os.path.basename(output_path),
                caption=f"RTHK Logs 導出（最近24小時 HKT），共 {row_count} 條。",
            )

        send_ok = bool(sent_message and getattr(sent_message, "document", None))
        log_event(
            "logs_excel_send_done",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "send_ok": send_ok, "chat_id": query.message.chat.id},
        )
        log_event(
            "logs_excel_send_verify",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "verified": send_ok},
        )

        if not send_ok:
            buttons = [[InlineKeyboardButton("⬅️ 返回", callback_data=f"logs_excel_export|{session_key}")]]
            await query.edit_message_text(
                "⚠️ Excel已生成，但發送驗證未通過，請重試。",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        log_event(
            "logs_excel_end_session",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK"},
        )
        await end_session(
            application=context.application,
            session_key=session_key,
            reason_text="excel已生成，會話結束。",
            reason_code="logs_excel_export_done",
            user_id=query.from_user.id,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
        )
    except Exception as e:
        log_event(
            "logs_excel_export_failed",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"source": "RTHK", "error": str(e)},
        )
        buttons = [[InlineKeyboardButton("⬅️ 返回", callback_data=f"logs_excel_export|{session_key}")]]
        await query.edit_message_text(
            f"❌ Excel導出失敗：{e}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    finally:
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
