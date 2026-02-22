import asyncio

from telegram import Update

import config
from core.logging_ops import log_event
from core.time_utils import now_hk
from integrations.drive import _is_photo_name, get_drive_service, upload_files_to_drive
from integrations.gmail import get_gmail_service, send_email_with_drive_links


async def send_drive_mode(
    *,
    update: Update,
    query,
    session_key: str,
    session_data: dict,
    file_paths: list,
    file_names: list,
    settings: dict,
    sender_info: dict,
    pr_body_text: str,
    message_date,
    progress_update,
):
    _progress_update = progress_update
    _progress_update("準備上傳到 Drive", 0)
    drive_service = get_drive_service()
    dt = message_date.astimezone(now_hk().tzinfo)
    log_event(
        "drive_upload_attempt",
        session_key=session_key,
        session_id=session_data.get("session_id"),
        update=update,
        extra={"file_count": len(file_names)},
    )
    ok, err, file_items = await asyncio.to_thread(
        upload_files_to_drive,
        drive_service,
        list(file_paths),
        list(file_names),
        folder_id=config.DRIVE_FOLDER_ID,
        root_folder_name=config.DRIVE_ROOT_FOLDER_NAME,
        date_dt=dt,
        progress_cb=_progress_update,
    )
    if not ok:
        await query.edit_message_text(f"❌ Drive 上傳失敗，請重試。\n原因：{err}")
        log_event(
            "drive_upload_failed",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"error": err},
        )
        return False, err, None
    log_event(
        "drive_upload_success",
        session_key=session_key,
        session_id=session_data.get("session_id"),
        update=update,
        extra={
            "file_count": len((file_items or {}).get("items") or []),
            "folder_id": (file_items or {}).get("folder_id"),
            "folder_title": (file_items or {}).get("title"),
        },
    )
    _progress_update("生成 Drive 連結 JSON", 0)
    _progress_update("傳送郵件", 1)
    non_photo_files = [
        (fp, fn) for fp, fn in zip(file_paths, file_names) if not _is_photo_name(fn)
    ]
    attach_paths = [fp for fp, _ in non_photo_files]
    attach_names = [fn for _, fn in non_photo_files]
    success, err = await asyncio.to_thread(
        send_email_with_drive_links,
        get_gmail_service(),
        sender_info,
        (file_items or {}).get("items") or [],
        settings,
        pr_body_text,
        folder_link=(file_items or {}).get("folder_link"),
        title=(file_items or {}).get("title") or "",
        attachment_paths=attach_paths,
        attachment_names=attach_names,
    )
    drive_folder_link = (file_items or {}).get("folder_link")
    return success, err, drive_folder_link
