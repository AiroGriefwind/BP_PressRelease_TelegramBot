import asyncio
import os
import re
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.logging_ops import log_event
from core.session import end_session, touch_session, user_sessions
from core.time_utils import now_hk
from integrations.gmail import fetch_rthk_emails_for_excel
from ui.messages import SESSION_EXPIRED_TEXT


def parse_rthk_email_body(body_text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    current_target = ""
    current_item: Dict[str, Any] | None = None

    for raw_line in (body_text or "").splitlines():
        line = (raw_line or "").strip()
        if not line:
            continue

        if line.lower().startswith("target:"):
            current_target = line.split(":", 1)[1].strip()
            continue

        m_item = re.match(r"^Item\s*No\.?\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if m_item:
            if current_item:
                rows.append(current_item)
            current_item = {
                "Target": current_target,
                "Item No.": m_item.group(1).strip(),
                "Original Subject": "",
                "Post ID": "",
                "WP Title": "",
                "url": "",
                "time text": "",
            }
            continue

        if not current_item:
            continue

        for key in ("Original Subject", "Post ID", "WP Title", "url", "time text"):
            if line.lower().startswith(f"{key.lower()}:"):
                current_item[key] = line.split(":", 1)[1].strip()
                break

    if current_item:
        rows.append(current_item)
    return rows


def generate_rthk_excel(items: List[Dict[str, Any]]) -> tuple[str, int]:
    os.makedirs("temp", exist_ok=True)
    filename = f"RTHK News改寫狀況一覽_{now_hk().strftime('%m%d_%H%M%S')}.xlsx"
    output_path = os.path.join("temp", filename)

    wb = Workbook()
    ws = wb.active
    ws.title = "RTHK Logs"

    headers = ["Target", "Item No.", "Original Subject", "Post ID", "WP Title", "url", "time text"]
    ws.append(headers)

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for item in items:
        ws.append([item.get(h, "") for h in headers])

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 52
    ws.column_dimensions["F"].width = 52
    ws.column_dimensions["G"].width = 24

    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=5).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=row_idx, column=6).alignment = Alignment(wrap_text=True, vertical="top")

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
            all_items.extend(parse_rthk_email_body(one.get("body_text", "")))

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
