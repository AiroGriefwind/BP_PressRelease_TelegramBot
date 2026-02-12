import json
import os
import time
from datetime import datetime
from typing import List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import config
from core.time_utils import now_hk
from integrations.gmail import get_google_creds


def get_drive_service():
    return build("drive", "v3", credentials=get_google_creds())


def _format_gapi_error(e: Exception) -> str:
    if isinstance(e, HttpError):
        try:
            payload = json.loads(e.content.decode("utf-8", errors="ignore"))
            msg = payload.get("error", {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        return f"HTTP {getattr(e.resp, 'status', 'unknown')}: {msg}"
    return str(e)


def _is_retryable_gapi_error(e: Exception) -> bool:
    if isinstance(e, HttpError):
        status = getattr(e.resp, "status", None)
        if status in (429, 500, 502, 503, 504):
            return True
        try:
            payload = json.loads(e.content.decode("utf-8", errors="ignore"))
            msg = (payload.get("error", {}).get("message") or "").lower()
            if "transient" in msg or "backend error" in msg:
                return True
        except Exception:
            pass
    return False


def _execute_with_retry(fn, *, max_attempts: int = 4, base_sleep: float = 1.0):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt >= max_attempts or not _is_retryable_gapi_error(e):
                raise
            time.sleep(base_sleep * (2 ** (attempt - 1)))
    if last_error:
        raise last_error


def _escape_drive_query_value(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def _sanitize_drive_folder_name(name: str, max_len: int = 80) -> str:
    safe = (name or "").replace("/", "_").replace("\\", "_").strip()
    if not safe:
        safe = "untitled"
    if len(safe) > max_len:
        safe = safe[:max_len]
    return safe


def _is_photo_name(name: str) -> bool:
    n = (name or "").lower()
    image_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff")
    return n.endswith(image_exts)


def _has_non_photo(file_names: List[str]) -> bool:
    return any((n and not _is_photo_name(n)) for n in (file_names or []))


def _pick_attachment_title(file_names: List[str]) -> str:
    for n in file_names:
        if not _is_photo_name(n):
            base = os.path.splitext(n)[0]
            return _sanitize_drive_folder_name(base)
    if file_names:
        base = os.path.splitext(file_names[0])[0]
        return _sanitize_drive_folder_name(base)
    return "untitled"


def _total_size_bytes(files: List[tuple]) -> int:
    total = 0
    for fp, _ in files:
        try:
            total += os.path.getsize(fp)
        except Exception:
            pass
    return total


def _format_size(bytes_size: int) -> str:
    if bytes_size < 1024:
        return f"{bytes_size} B"
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    return f"{bytes_size / (1024 * 1024):.2f} MB"


def _make_unique_filename(file_name: str, existing_names: List[str]) -> str:
    if file_name not in existing_names:
        return file_name
    base, ext = os.path.splitext(file_name)
    i = 2
    while True:
        candidate = f"{base} ({i}){ext}"
        if candidate not in existing_names:
            return candidate
        i += 1


def ensure_drive_folder(service, name: str, parent_id: str) -> str:
    safe_name = _sanitize_drive_folder_name(name)
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{_escape_drive_query_value(safe_name)}' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    resp = _execute_with_retry(
        lambda: service.files().list(q=q, fields="files(id,name)").execute()
    )
    files = resp.get("files", []) or []
    if files:
        return files[0].get("id")
    created = _execute_with_retry(
        lambda: service.files().create(
            body={
                "name": safe_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id",
        ).execute()
    )
    return created.get("id")


def upload_files_to_drive(
    service,
    file_paths,
    file_names,
    folder_id=None,
    root_folder_name: str = config.DRIVE_ROOT_FOLDER_NAME,
    date_dt: Optional[datetime] = None,
    progress_cb=None,
):
    # 目录结构：根/大批量图片/YYYY/MMDD/文章标题
    dt = date_dt or now_hk()
    year = dt.strftime("%Y")
    mmdd = dt.strftime("%m%d")
    title = _pick_attachment_title(file_names)

    try:
        if progress_cb:
            progress_cb("查找/建立 Drive 資料夾", 0)
        base_parent = folder_id or "root"
        root_id = ensure_drive_folder(service, root_folder_name, base_parent)
        year_id = ensure_drive_folder(service, year, root_id)
        day_id = ensure_drive_folder(service, mmdd, year_id)
        title_id = ensure_drive_folder(service, title, day_id)

        # 设文件夹为任何人可读
        if progress_cb:
            progress_cb("設定 Drive 資料夾權限", 0)
        _execute_with_retry(
            lambda: service.permissions().create(
                fileId=title_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
        )
        if progress_cb:
            progress_cb("Drive 資料夾已就緒", 2)
    except Exception as e:
        return False, _format_gapi_error(e), None

    items = []
    for file_path, file_name in zip(file_paths, file_names):
        try:
            metadata = {"name": file_name, "parents": [title_id]}
            media = MediaFileUpload(file_path, resumable=True)
            if progress_cb:
                progress_cb(f'正在上傳“{file_name}”....', 0)
            created = _execute_with_retry(
                lambda: service.files().create(
                    body=metadata,
                    media_body=media,
                    fields="id,webViewLink",
                ).execute()
            )
            file_id = created.get("id")
            if not file_id:
                return False, f"上傳失敗：未返回 fileId ({file_name})", None
            if progress_cb:
                progress_cb(f'上傳完成，設定檔案權限“{file_name}”', 1)
            # 设为任何人可读（双保险）
            _execute_with_retry(
                lambda: service.permissions().create(
                    fileId=file_id,
                    body={"type": "anyone", "role": "reader"},
                ).execute()
            )
            if progress_cb:
                progress_cb(f'檔案權限已設定“{file_name}”', 1)
            link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            items.append({"name": file_name, "id": file_id, "link": link})
        except Exception as e:
            return False, _format_gapi_error(e), None
    folder_link = f"https://drive.google.com/drive/folders/{title_id}"
    return True, None, {"items": items, "folder_link": folder_link, "folder_id": title_id, "title": title}
