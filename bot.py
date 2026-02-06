import os
import json
import pickle
import uuid
import threading
import urllib.parse

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import Header
from base64 import urlsafe_b64encode

from zoneinfo import ZoneInfo

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackQueryHandler, Application
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler

# 处理Logs邮件.
import base64
import html
import re
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from telegram.error import BadRequest
from telegram.ext import Job

# 日志缓存文件路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_CACHE_PATH = os.path.join(BASE_DIR, "logs_cache.json")
OPS_LOG_DIR = os.path.join(BASE_DIR, "logs")
_ops_log_lock = threading.Lock()

LOGS_PER_PAGE = 8

ERROR_TEXT = {
    100: "沒有找到附件",
    101: "附件內容讀取失敗",
    102: "附件可能是純圖片類型",
    200: "敏感詞",
    300: "AI 處理失敗，通用 AI pipeline 失敗",
    301: "Gemini 處理達到限額",
    400: "SEO 信息提取失敗",
    500: "插入 WP 草稿箱失敗",
    501: "插入 WP 草稿箱部分成功：文字 OK，圖片失敗",
    900: "未知的異常，兜底",
}


# 邮件目标
SCOPES = [
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/drive.file",
]

TARGET_EMAIL = 'bp.filtermailbox@gmail.com'
USE_DRIVE_SHARE = False
DRIVE_FOLDER_ID = None
DRIVE_ROOT_FOLDER_NAME = "大批量图片"
DRIVE_AUTO_SIZE_MB = 25

# Gmail subject 避免过长
MAX_SUBJECT_LEN = 160

# --- FB URL（Facebook 分享链接）相关常量/辅助 ---
FB_URL_BUTTON_TEXT = "FB URL"
FB_URL_RECENT_SECONDS = 10 * 60  # “上一条URL”兜底：只取最近10分钟

# 记录每个 (chat_id, user_id) 最近一次出现的 FB URL，支持“先发URL，再单独@bot”的兜底体验
# key: session_key = f"{chat_id}_{user_id}"
last_seen_fb_url: Dict[str, Dict[str, Any]] = {}

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

# 可选设置项
SETTINGS_OPTIONS = {
    'type': ['全文不改', '只改標題'],
    'priority': ['普通', '緊急'],
    'language': ['中文', '英文']
}

# 默认设置
DEFAULT_SETTINGS = {
    'type': '全文不改',
    'priority': '普通',
    'language': '中文'
}

# 保存每个用户添加的文件列表（支持群聊私聊）
user_sessions = {}

# 会话超时（无操作）自动结束：10分钟
SESSION_TIMEOUT_SECONDS = 10 * 60
session_timeout_jobs: Dict[str, Job] = {}


def _append_ops_log(record: Dict[str, Any]):
    """
    追加写入一条操作日志（JSONL）。
    注意：不要在这里抛异常影响主流程。
    """
    try:
        # 按日分目录：logs/YYYYMMDD/ops_log.jsonl
        ts = record.get("ts")
        try:
            dt = datetime.fromisoformat(ts) if ts else _now_hk()
        except Exception:
            dt = _now_hk()
        day = dt.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y%m%d")
        day_dir = os.path.join(OPS_LOG_DIR, day)
        os.makedirs(day_dir, exist_ok=True)
        daily_path = os.path.join(day_dir, "ops_log.jsonl")

        line = json.dumps(record, ensure_ascii=False)
        with _ops_log_lock:
            with open(daily_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return


def _extract_actor_from_update(update: Optional[Update] = None) -> Dict[str, Any]:
    if not update:
        return {}
    try:
        if update.callback_query:
            u = update.callback_query.from_user
            chat = update.callback_query.message.chat if update.callback_query.message else None
            msg = update.callback_query.message
        else:
            u = update.effective_user
            chat = update.effective_chat
            msg = update.effective_message

        return {
            "user_id": getattr(u, "id", None),
            "username": getattr(u, "username", None),
            "first_name": getattr(u, "first_name", None),
            "last_name": getattr(u, "last_name", None),
            "chat_id": getattr(chat, "id", None) if chat else None,
            "chat_title": getattr(chat, "title", None) if chat else None,
            "message_id": getattr(msg, "message_id", None) if msg else None,
        }
    except Exception:
        return {}


def log_event(
    event: str,
    *,
    session_key: Optional[str] = None,
    session_id: Optional[str] = None,
    update: Optional[Update] = None,
    extra: Optional[Dict[str, Any]] = None,
):
    """
    写一条操作日志：谁在何处做了什么。
    - event: 事件名（稳定字段，便于检索）
    - extra: 事件细节（可扩展）
    """
    try:
        actor = _extract_actor_from_update(update)
        record = {
            "ts": _now_hk().isoformat(timespec="seconds"),
            "event": event,
            "session_key": session_key,
            "session_id": session_id,
            **actor,
            "extra": extra or {},
        }
        _append_ops_log(record)
    except Exception:
        return


def _new_session_struct() -> Dict[str, Any]:
    return {
        "files": [],
        "settings": DEFAULT_SETTINGS.copy(),
        "session_id": uuid.uuid4().hex,
        "created_ts": _now_hk().isoformat(timespec="seconds"),
        # FB URL 流程
        "fb_url": None,
        "awaiting_fb_url": False,
    }


def _safe_del(d: dict, k: str):
    try:
        if d is not None and k in d:
            del d[k]
    except Exception:
        pass


def _cleanup_session_userdata(application: Application, user_id: int, session_key: str):
    """
    清理 application.user_data 里与 session_key 相关的临时键（设置、Logs 视图等）。
    """
    try:
        ud = application.user_data.get(user_id)
        if not isinstance(ud, dict):
            return
        _safe_del(ud, f"temp_settings_{session_key}")
        _safe_del(ud, f"logs_view_{session_key}")
    except Exception:
        pass


async def _try_edit_message_text(
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
):
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except BadRequest:
        # 可能被用户删了 / 已不可编辑，直接忽略
        return
    except Exception:
        return

async def _try_edit_message_text_markup(
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    disable_web_page_preview: bool = False,
):
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except BadRequest:
        return
    except Exception:
        return


async def end_session(
    *,
    application: Application,
    session_key: str,
    reason_text: str,
    reason_code: str = "unknown",
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
):
    """
    统一的“会话结束”入口：
    - 取消超时 Job
    - 删除临时文件
    - 清理 user_sessions + user_data 的临时键
    - 尝试把 UI 消息更新为结束文案（如能定位到 message）
    """
    # 1) 取消超时 Job
    job = session_timeout_jobs.pop(session_key, None)
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass

    # 2) 删除临时文件 & 清 session（并写结束日志）
    session_data = user_sessions.get(session_key)
    if session_data and isinstance(session_data, dict):
        # 结束日志：尽量在删除前保留快照
        try:
            log_event(
                "session_end",
                session_key=session_key,
                session_id=session_data.get("session_id"),
                update=None,
                extra={
                    "reason_code": reason_code,
                    "reason_text": reason_text,
                    "file_count": len(session_data.get("files") or []),
                    "settings": session_data.get("settings"),
                    "fb_url": session_data.get("fb_url"),
                    "created_ts": session_data.get("created_ts"),
                    "last_touch_ts": session_data.get("last_touch_ts"),
                },
            )
        except Exception:
            pass

        files = session_data.get("files") or []
        for fp, _ in files:
            try:
                os.remove(fp)
            except Exception:
                pass

        # 清理 user_data 里的 session 相关临时键
        if user_id is not None:
            _cleanup_session_userdata(application, user_id=user_id, session_key=session_key)

        try:
            del user_sessions[session_key]
        except Exception:
            pass

    # 3) 尝试更新 UI（优先显式 chat_id/message_id，其次用 session_data 里记录的 ui_*）
    final_chat_id = chat_id
    final_message_id = message_id
    if (final_chat_id is None or final_message_id is None) and session_data:
        final_chat_id = final_chat_id or session_data.get("ui_chat_id")
        final_message_id = final_message_id or session_data.get("ui_message_id")

    if final_chat_id is not None and final_message_id is not None:
        await _try_edit_message_text(application, int(final_chat_id), int(final_message_id), reason_text)


async def _on_session_timeout(context: ContextTypes.DEFAULT_TYPE):
    data = getattr(context.job, "data", None) or {}
    session_key = data.get("session_key")
    if not session_key:
        return

    # 已经结束就不重复处理
    if session_key not in user_sessions:
        session_timeout_jobs.pop(session_key, None)
        return

    await end_session(
        application=context.application,
        session_key=session_key,
        reason_text="⏱️ 10分钟无操作，会话自动结束。",
        reason_code="timeout",
        user_id=data.get("user_id"),
        chat_id=data.get("chat_id"),
        message_id=data.get("message_id"),
    )


def touch_session(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    session_key: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
):
    """
    记录一次“交互”，并重置 10分钟超时 Job。
    如果能拿到 chat_id/message_id，会同步写入 session_data 方便超时后更新 UI。
    """
    if session_key not in user_sessions:
        return

    sd = user_sessions.get(session_key, {})
    sd["last_touch_ts"] = _now_hk().isoformat(timespec="seconds")
    if chat_id is not None:
        sd["ui_chat_id"] = int(chat_id)
    if message_id is not None:
        sd["ui_message_id"] = int(message_id)
    user_sessions[session_key] = sd

    # 重置超时 Job
    old = session_timeout_jobs.pop(session_key, None)
    if old is not None:
        try:
            old.schedule_removal()
        except Exception:
            pass

    try:
        job = context.application.job_queue.run_once(
            _on_session_timeout,
            when=SESSION_TIMEOUT_SECONDS,
            data={
                "session_key": session_key,
                "user_id": user_id,
                "chat_id": chat_id,
                "message_id": message_id,
            },
            name=f"session_timeout:{session_key}",
        )
        session_timeout_jobs[session_key] = job
    except Exception:
        # 没有 job_queue 或者调度失败就忽略（不影响主流程）
        pass

def get_google_creds():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

def get_gmail_service():
    return build('gmail', 'v1', credentials=get_google_creds())

def get_drive_service():
    return build('drive', 'v3', credentials=get_google_creds())

def _format_gapi_error(e: Exception) -> str:
    if isinstance(e, HttpError):
        try:
            payload = json.loads(e.content.decode("utf-8", errors="ignore"))
            msg = payload.get("error", {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        return f"HTTP {getattr(e.resp, 'status', 'unknown')}: {msg}"
    return str(e)

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
    resp = service.files().list(q=q, fields="files(id,name)").execute()
    files = resp.get("files", []) or []
    if files:
        return files[0].get("id")
    created = service.files().create(
        body={
            "name": safe_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    return created.get("id")

def upload_files_to_drive(
    service,
    file_paths,
    file_names,
    folder_id=None,
    root_folder_name: str = DRIVE_ROOT_FOLDER_NAME,
    date_dt: Optional[datetime] = None,
):
    # 目录结构：根/大批量图片/YYYY/MMDD/文章标题
    dt = date_dt or _now_hk()
    year = dt.strftime("%Y")
    mmdd = dt.strftime("%m%d")
    title = _pick_attachment_title(file_names)

    try:
        base_parent = folder_id or "root"
        root_id = ensure_drive_folder(service, root_folder_name, base_parent)
        year_id = ensure_drive_folder(service, year, root_id)
        day_id = ensure_drive_folder(service, mmdd, year_id)
        title_id = ensure_drive_folder(service, title, day_id)

        # 设文件夹为任何人可读
        service.permissions().create(
            fileId=title_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
    except Exception as e:
        return False, _format_gapi_error(e), None

    items = []
    for file_path, file_name in zip(file_paths, file_names):
        try:
            metadata = {"name": file_name, "parents": [title_id]}
            media = MediaFileUpload(file_path, resumable=True)
            created = service.files().create(
                body=metadata,
                media_body=media,
                fields="id,webViewLink",
            ).execute()
            file_id = created.get("id")
            if not file_id:
                return False, f"上传失败：未返回 fileId ({file_name})", None
            # 设为任何人可读（双保险）
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
            link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            items.append({"name": file_name, "id": file_id, "link": link})
        except Exception as e:
            return False, _format_gapi_error(e), None
    folder_link = f"https://drive.google.com/drive/folders/{title_id}"
    return True, None, {"items": items, "folder_link": folder_link, "folder_id": title_id, "title": title}
    items = []
    for file_path, file_name in zip(file_paths, file_names):
        try:
            metadata = {"name": file_name}
            if folder_id:
                metadata["parents"] = [folder_id]
            media = MediaFileUpload(file_path, resumable=True)
            created = service.files().create(
                body=metadata,
                media_body=media,
                fields="id,webViewLink",
            ).execute()
            file_id = created.get("id")
            if not file_id:
                return False, f"上传失败：未返回 fileId ({file_name})", None
            # 设为任何人可读
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
            link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            items.append({"name": file_name, "id": file_id, "link": link})
        except Exception as e:
            return False, _format_gapi_error(e), None
    return True, None, items

def send_email_with_attachments(service, file_paths, sender_info, file_names, settings):
    message = MIMEMultipart()
    message['to'] = TARGET_EMAIL
    subject_title = _pick_attachment_title(list(file_names))
    subject = f"新稿件: " + subject_title
    if len(subject) > MAX_SUBJECT_LEN:
        subject = subject[:MAX_SUBJECT_LEN - 3] + "..."
    message['subject'] = subject
    
    # 新的 body 格式
    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}
附件: {', '.join(file_names)}
"""
    message.attach(MIMEText(body, 'plain', 'utf-8'))

    # ... (后面的附件逻辑不变)
    for file_path, file_name in zip(file_paths, file_names):
        with open(file_path, 'rb') as f:
            part = MIMEApplication(f.read(), Name=file_name)
            filename_utf8 = str(Header(file_name, 'utf-8'))
            part.add_header('Content-Disposition',
                            f'attachment; filename="{filename_utf8}"')
            message.attach(part)
    
    raw_message = urlsafe_b64encode(message.as_bytes()).decode()
    try:
        service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()
        return True, None
    except Exception as e:
        print(f"发送邮件失败: {e}")
        return False, str(e)

def send_email_with_drive_links(service, sender_info, file_items, settings, folder_link=None, title: str = ""):
    message = MIMEMultipart()
    message["to"] = TARGET_EMAIL
    subject_title = title or "附件"
    subject = "新稿件(Drive): " + subject_title
    if len(subject) > MAX_SUBJECT_LEN:
        subject = subject[:MAX_SUBJECT_LEN - 3] + "..."
    message["subject"] = subject

    links = "\n".join([f"- {x.get('name')}: {x.get('link')}" for x in file_items])
    folder_line = f"\n文件夹链接: {folder_link}\n" if folder_link else ""
    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}

Drive 共享链接:{folder_line}
{links}
"""
    message.attach(MIMEText(body, "plain", "utf-8"))
    raw_message = urlsafe_b64encode(message.as_bytes()).decode()
    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw_message},
        ).execute()
        return True, None
    except Exception as e:
        print(f"发送 Drive 链接邮件失败: {e}")
        return False, str(e)

def send_email_with_fb_url(service, fb_url: str, sender_info: dict):
    """
    发送“FB URL”到目标邮箱（不带附件）。
    """
    message = MIMEMultipart()
    message["to"] = TARGET_EMAIL
    message["subject"] = f"[FB URL]: {fb_url}"

    body = f"""URL: {fb_url}

来自: {sender_info.get('name')} (@{sender_info.get('username')})
群组: {sender_info.get('chat_title')}
时间: {sender_info.get('date')}
""".strip()

    message.attach(MIMEText(body, "plain", "utf-8"))
    raw_message = urlsafe_b64encode(message.as_bytes()).decode()
    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw_message},
        ).execute()
        return True, None
    except Exception as e:
        print(f"发送 FB URL 邮件失败: {e}")
        return False, str(e)

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
            "date": (dt.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S") if dt else _now_hk().strftime("%Y-%m-%d %H:%M:%S")),
        }
    except Exception:
        return {
            "name": "unknown",
            "username": "unknown",
            "chat_title": "unknown",
            "date": _now_hk().strftime("%Y-%m-%d %H:%M:%S"),
        }

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"

    # 如果是新session，创建完整结构
    if session_key not in user_sessions:
        user_sessions[session_key] = _new_session_struct()
        log_event("session_start", session_key=session_key, session_id=user_sessions[session_key].get("session_id"), update=update)

    # 任何文件上传也算一次交互，重置会话超时
    touch_session(context=context, session_key=session_key, user_id=user_id, chat_id=chat_id)

    os.makedirs('temp', exist_ok=True)
    file_id, file_name = None, None
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
    elif message.photo:
        photo_file = message.photo[-1]
        file_id = photo_file.file_id
        # 对于直接发送的图片，使用时间戳 + 唯一ID，避免同秒/重复名覆盖
        timestamp = message.date.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime('%Y%m%d_%H%M%S')
        suffix = getattr(photo_file, "file_unique_id", "") or file_id[-6:]
        file_name = f"photo_{timestamp}_{suffix}.jpg"

    if file_id and file_name:
        existing_names = [name for _, name in user_sessions[session_key].get('files', [])]
        file_name = _make_unique_filename(file_name, existing_names)
        file = await context.bot.get_file(file_id)
        file_path = f"temp/{file_name}"
        await file.download_to_drive(file_path)
        
        # 存储文件
        user_sessions[session_key]['files'].append((file_path, file_name))
        await message.reply_text(f"已添加: {file_name}")

        # 上传日志
        try:
            sd = user_sessions.get(session_key) or {}
            log_event(
                "file_added",
                session_key=session_key,
                session_id=sd.get("session_id"),
                update=update,
                extra={
                    "file_name": file_name,
                    "file_path": file_path,
                    "file_kind": "document" if message.document else ("photo" if message.photo else "unknown"),
                    "total_files": len(sd.get("files") or []),
                },
            )
        except Exception:
            pass

# --- 进入设置菜单 ---
async def on_menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    # 将当前设置存入临时的 user_data，用于“取消”功能
    current_settings = user_sessions[session_key]['settings']
    context.user_data[f'temp_settings_{session_key}'] = current_settings.copy()

    log_event(
        "settings_open",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )

    await show_settings_menu(update, context, session_key, current_settings)

# --- 辅助函数：渲染设置菜单 ---
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, session_key: str, settings: dict):
    query = update.callback_query
    keyboard = []
    
    # 动态生成三行设置按钮
    for key, options in SETTINGS_OPTIONS.items():
        row = []
        for option in options:
            text = option
            # 高亮当前选项
            if settings.get(key) == option:
                text = f"✅ {option}"
            
            # callback_data 包含要修改的键和值
            callback = f"set_option|{session_key}|{key}|{option}"
            row.append(InlineKeyboardButton(text, callback_data=callback))
        keyboard.append(row)

    # 底部确认和取消按钮
    keyboard.append([
        InlineKeyboardButton("确认", callback_data=f"settings_confirm|{session_key}"),
        InlineKeyboardButton("取消", callback_data=f"settings_cancel|{session_key}")
    ])
    
    await query.edit_message_text("请选择需要的选项：", reply_markup=InlineKeyboardMarkup(keyboard))

# --- 点击选项按钮 ---
async def on_set_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, key, value = query.data.split('|')

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    
    # 修改临时设置
    temp_settings = context.user_data[f'temp_settings_{session_key}']
    temp_settings[key] = value

    log_event(
        "settings_change",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
        extra={"key": key, "value": value},
    )

    # 重新渲染菜单以提供反馈
    await show_settings_menu(update, context, session_key, temp_settings)

# --- 点击“确认”保存设置 ---
async def on_settings_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    # 将临时设置保存回主 session
    user_sessions[session_key]['settings'] = context.user_data[f'temp_settings_{session_key}'].copy()
    
    # 清理临时数据
    del context.user_data[f'temp_settings_{session_key}']

    log_event(
        "settings_confirm",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
        extra={"settings": user_sessions[session_key].get("settings")},
    )

    # 返回主菜单
    await handle_mention(update, context)

# --- 点击“取消” ---
async def on_settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    
    original_settings = user_sessions[session_key]['settings']
    temp_settings = context.user_data.get(f'temp_settings_{session_key}')

    # 如果设置没变，直接返回
    if original_settings == temp_settings:
        del context.user_data[f'temp_settings_{session_key}']
        await handle_mention(update, context)
    else:
        # 如果变了，弹出确认放弃的提示
        log_event(
            "settings_cancel_prompt",
            session_key=session_key,
            session_id=user_sessions[session_key].get("session_id"),
            update=update,
        )
        buttons = [[
            InlineKeyboardButton("是，放弃更改", callback_data=f"settings_cancel_confirm|{session_key}"),
            InlineKeyboardButton("否，继续编辑", callback_data=f"menu_settings_back|{session_key}")
        ]]
        await query.edit_message_text("设置已更改，是否放弃并返回？", reply_markup=InlineKeyboardMarkup(buttons))

# --- 确认放弃更改 ---
async def on_settings_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    
    # 清理临时数据，不保存
    del context.user_data[f'temp_settings_{session_key}']
    
    log_event(
        "settings_cancel_confirm",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )

    # 返回主菜单
    await handle_mention(update, context)

# --- 从“放弃更改”页面返回设置菜单 ---
async def on_menu_settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    temp_settings = context.user_data[f'temp_settings_{session_key}']

    log_event(
        "settings_back_to_edit",
        session_key=session_key,
        session_id=user_sessions[session_key].get("session_id"),
        update=update,
    )
    await show_settings_menu(update, context, session_key, temp_settings)

async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        # ... (和之前一样的代码来获取 session_key)
        message = update.message
        user_id = message.from_user.id
        chat_id = message.chat.id
    else:
        # ... (和之前一样的代码来获取 session_key)
        query = update.callback_query
        message = query.message
        user_id = query.from_user.id
        chat_id = message.chat.id

    session_key = f"{chat_id}_{user_id}"
    
    # 确保session存在
    if session_key not in user_sessions:
        user_sessions[session_key] = _new_session_struct()
        log_event("session_start", session_key=session_key, session_id=user_sessions[session_key].get("session_id"), update=update)
        
    session_data = user_sessions[session_key]
    files = session_data['files']
    settings = session_data['settings']

    # --- 可选增强：若用户在“@bot”文本或其 reply 的消息里带了 FB URL，直接进入确认发送界面 ---
    if update.message:
        candidate_texts = []
        try:
            candidate_texts.append(update.message.text or "")
        except Exception:
            pass
        try:
            if update.message.reply_to_message and getattr(update.message.reply_to_message, "text", None):
                candidate_texts.append(update.message.reply_to_message.text or "")
        except Exception:
            pass

        found_url = None
        for t in candidate_texts:
            u = _extract_first_url(t)
            if u:
                found_url = u
                break

        # 兜底：如果这条 @ 消息里没有URL，也没有 reply_to，尝试使用同一用户最近发过的 FB URL
        if not found_url:
            try:
                rec = last_seen_fb_url.get(session_key)
                if rec and rec.get("url") and rec.get("dt"):
                    now_dt = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                    if (now_dt - rec["dt"]).total_seconds() <= FB_URL_RECENT_SECONDS:
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

                buttons = [[
                    InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}"),
                    InlineKeyboardButton("✏️ 重新输入", callback_data=f"fb_url_menu|{session_key}"),
                ], [
                    InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}"),
                ]]
                sent = await message.reply_text(
                    f"已检测到 FB URL：\n{norm}\n\n是否发送到 {TARGET_EMAIL}？",
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
                return
    
    file_names = [name for _, name in files]
    attach_list = "\n".join(file_names) if file_names else "暂无附件"
    total_bytes = _total_size_bytes(files)
    total_size_text = _format_size(total_bytes) if files else ""
    auto_drive = total_bytes > (DRIVE_AUTO_SIZE_MB * 1024 * 1024) if files else False
    drive_mode = USE_DRIVE_SHARE or auto_drive

    # 构建带设置的UI消息
    settings_text = (
        f"類型：{settings['type']}\n"
        f"優先度：{settings['priority']}\n"
        f"語言：{settings['language']}"
    )
    fb_url_line = ""
    try:
        if session_data.get("fb_url"):
            fb_url_line = f"\n\nFB URL：\n{session_data.get('fb_url')}"
    except Exception:
        fb_url_line = ""

    size_line = ""
    if drive_mode and files:
        size_line = f"\n\n总大小：{total_size_text}\n已超过 {DRIVE_AUTO_SIZE_MB}MB，将改用 Drive 共享链接发送。"
    ui_msg = f"附件列表：\n{attach_list}{size_line}\n\n---\n\n{settings_text}{fb_url_line}"

    # 构建按钮 （确认，FB URL，删除，设置，Logs）
    buttons = [
    [
        InlineKeyboardButton("确认", callback_data=f"confirm_send|{session_key}"),
        InlineKeyboardButton(FB_URL_BUTTON_TEXT, callback_data=f"fb_url_menu|{session_key}"),
        InlineKeyboardButton("删除", callback_data=f"menu_delete_mode|{session_key}"),
    ],
    [
        InlineKeyboardButton("⚙️ 设置", callback_data=f"menu_settings|{session_key}"),
        InlineKeyboardButton("🧾 Logs", callback_data=f"menu_logs|{session_key}"),
        InlineKeyboardButton("🛑 结束会话", callback_data=f"end_session|{session_key}"),
    ]
    ]

    reply_markup = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await message.edit_text(ui_msg, reply_markup=reply_markup)
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


async def on_fb_url_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    进入 FB URL 输入流程：提示用户发送 URL 文本。
    如果 session 已有 fb_url，则展示确认发送界面（不必重新输入）。
    """
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
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
        buttons = [[
            InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}"),
            InlineKeyboardButton("✏️ 重新输入", callback_data=f"fb_url_reset|{session_key}"),
        ], [
            InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}"),
        ]]
        await query.edit_message_text(
            f"当前 FB URL：\n{fb_url}\n\n是否发送到 {TARGET_EMAIL}？",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
        return

    # 没有 URL：进入输入状态
    sd["awaiting_fb_url"] = True
    sd["fb_url"] = None
    user_sessions[session_key] = sd

    log_event(
        "fb_url_input_start",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
    )

    buttons = [[InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}")]]
    await query.edit_message_text(
        "请发送一条消息，内容为 Facebook 分享链接（URL）。\n我只会读取你“下一条”消息来作为 FB URL。",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def on_fb_url_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
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
        "好的，请重新发送一条 Facebook 分享链接（URL）。\n我只会读取你“下一条”消息。",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def on_fb_url_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
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
        await query.edit_message_text("⚠️ 尚未输入 FB URL，请先点 FB URL 并发送链接。")
        return

    await query.edit_message_text("正在发送 FB URL... 请稍后。", disable_web_page_preview=True)

    sender_info = _build_sender_info_from_message(query.message, fallback_user=query.from_user)
    gmail_service = get_gmail_service()

    log_event(
        "fb_url_send_attempt",
        session_key=session_key,
        session_id=sd.get("session_id"),
        update=update,
        extra={"fb_url": fb_url},
    )

    success, err = send_email_with_fb_url(gmail_service, fb_url, sender_info)

    if success:
        log_event(
            "fb_url_send_success",
            session_key=session_key,
            session_id=sd.get("session_id"),
            update=update,
        )

        # 和“附件发送成功”一致：直接结束会话
        await end_session(
            application=context.application,
            session_key=session_key,
            reason_text=f"✅ FB URL 已发送到 {TARGET_EMAIL}\n会话结束。",
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
                    "dt": datetime.now(ZoneInfo("Asia/Hong_Kong")),
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
        buttons = [[
            InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}"),
            InlineKeyboardButton("✏️ 重新输入", callback_data=f"fb_url_reset|{session_key}"),
        ], [
            InlineKeyboardButton("⬅️ 返回主菜单", callback_data=f"back_to_main|{session_key}"),
        ]]
        await _try_edit_message_text_markup(
            context.application,
            int(ui_chat_id),
            int(ui_message_id),
            f"已收到 FB URL：\n{norm}\n\n是否发送到 {TARGET_EMAIL}？",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
    else:
        # 没有 UI message 可编辑就直接发一条确认消息
        buttons = [[InlineKeyboardButton("✅ 发送 FB URL", callback_data=f"fb_url_send|{session_key}")]]
        sent = await message.reply_text(
            f"已收到 FB URL：\n{norm}\n\n是否发送到 {TARGET_EMAIL}？",
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

# 删除模式菜单逻辑 (列出所有文件带X)
async def on_menu_delete_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

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

    # 构建文件按钮列表，每个文件一行，格式：[ ❌ 文件名  ]
    keyboard = []
    for index, (_, filename) in enumerate(files):
        # 显示名可选：太长时截断一下，避免把按钮撑太宽
        display_name = filename
        max_len = 40
        if len(display_name) > max_len:
            display_name = display_name[:max_len - 1] + "…"

        # 把红叉放到前面：❌ filename
        btn_text = f"❌ {display_name}"
        keyboard.append([
            InlineKeyboardButton(
                btn_text,
                callback_data=f"ask_del_one|{session_key}|{index}"
            )
        ])

    # 底部功能键
    keyboard.append([
        InlineKeyboardButton("🗑️ 全部删除", callback_data=f"ask_del_all|{session_key}"),
        InlineKeyboardButton("✅ 完成", callback_data=f"back_to_main|{session_key}")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    # 如果没有文件，提示文字稍微变一下
    msg_text = "点击红色 X 删除特定附件：" if files else "暂无附件可删除。"
    
    await _try_edit_message_text_markup(
        context.application,
        query.message.chat.id,
        query.message.message_id,
        msg_text,
        reply_markup=reply_markup,
    )

async def on_ask_del_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, index_str = query.data.split('|')

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    index = int(index_str)
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

    if index >= len(files):
        await _try_edit_message_text_markup(
            context.application,
            query.message.chat.id,
            query.message.message_id,
            "⚠️ 文件不存在或已被删除。",
            reply_markup=None,
        )
        # 这里可以加个逻辑自动跳回菜单，或者让用户重新发令
        return

    target_file_name = files[index][1]

    # 确认菜单
    buttons = [
        [
            InlineKeyboardButton("是，删除", callback_data=f"do_del_one|{session_key}|{index}"),
            InlineKeyboardButton("否，返回", callback_data=f"menu_delete_mode|{session_key}")
        ]
    ]
    await _try_edit_message_text_markup(
        context.application,
        query.message.chat.id,
        query.message.message_id,
        f"确定要删除 {target_file_name} 吗？",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# 单个文件删除：确认与执行
async def on_do_del_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("已删除")
    _, session_key, index_str = query.data.split('|')

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    index = int(index_str)
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

    if index < len(files):
        target_file_name = files[index][1]
        # 删除物理文件
        file_path = files[index][0]
        try:
            os.remove(file_path)
        except Exception:
            pass
        # 从列表中移除
        files.pop(index)
        user_sessions[session_key]['files'] = files  # 仅更新文件列表

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

    # 删除后，直接刷新回“删除模式菜单”
    await on_menu_delete_mode(update, context)

async def on_ask_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

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
            InlineKeyboardButton("取消", callback_data=f"menu_delete_mode|{session_key}")
        ]
    ]
    await query.edit_message_text("⚠️ 确定要清空所有附件吗？此操作不可逆。", reply_markup=InlineKeyboardMarkup(buttons))

# 全部删除：确认与执行
async def on_do_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_key = query.data.split('|')[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']
    
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

# 返回主菜单
async def on_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    # 直接复用 handle_mention 的逻辑来重新渲染主界面
    await handle_mention(update, context)

# “确认”按钮的事件回调
async def on_confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    
    session_data = user_sessions.get(session_key)
    if not session_data or not session_data['files']:
        await query.edit_message_text("⚠️ 没有附件，请先上传文件或图片。")
        return

    files = session_data['files']
    settings = session_data['settings']
    message = query.message # 需要用 message 对象获取发件人信息

    await query.edit_message_text("正在打包并发送所有附件... 请稍后。")

    # 构建发件人信息
    sender_info = {
        'name': (message.reply_to_message.from_user.first_name or "") + (f" {message.reply_to_message.from_user.last_name}" if message.reply_to_message.from_user.last_name else ""),
        'username': message.reply_to_message.from_user.username or "unknown",
        'chat_title': message.chat.title or "private",
        'date': message.date.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime('%Y-%m-%d %H:%M:%S')
    }
    
    file_paths, file_names = zip(*files)
    gmail_service = get_gmail_service()
    total_bytes = _total_size_bytes(files)
    auto_drive = total_bytes > (DRIVE_AUTO_SIZE_MB * 1024 * 1024)
    drive_mode = USE_DRIVE_SHARE or auto_drive

    # 把 settings 传给发送函数
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
        drive_service = get_drive_service()
        dt = message.date.astimezone(ZoneInfo("Asia/Hong_Kong"))
        log_event(
            "drive_upload_attempt",
            session_key=session_key,
            session_id=session_data.get("session_id"),
            update=update,
            extra={"file_count": len(file_names)},
        )
        ok, err, file_items = upload_files_to_drive(
            drive_service,
            list(file_paths),
            list(file_names),
            folder_id=DRIVE_FOLDER_ID,
            root_folder_name=DRIVE_ROOT_FOLDER_NAME,
            date_dt=dt,
        )
        if not ok:
            await query.edit_message_text(f"❌ Drive 上传失败，请重试。\n原因：{err}")
            log_event(
                "drive_upload_failed",
                session_key=session_key,
                session_id=session_data.get("session_id"),
                update=update,
                extra={"error": err},
            )
            return
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
        success, err = send_email_with_drive_links(
            gmail_service,
            sender_info,
            (file_items or {}).get("items") or [],
            settings,
            folder_link=(file_items or {}).get("folder_link"),
            title=(file_items or {}).get("title") or "",
        )
        drive_folder_link = (file_items or {}).get("folder_link")
    else:
        success, err = send_email_with_attachments(gmail_service, file_paths, sender_info, file_names, settings)
    
    if success:
        extra_link = ""
        if drive_mode and drive_folder_link:
            extra_link = f"\n\nDrive 文件夹：\n{drive_folder_link}"
        await end_session(
            application=context.application,
            session_key=session_key,
            reason_text=f"✅ 文件已发送到 {TARGET_EMAIL}\n会话结束。{extra_link}",
            reason_code="send_success",
            user_id=query.from_user.id,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
        )
    else:
        await query.edit_message_text("❌ 发送失败,请重试")
        # 失败不结束会话，让用户可以重试/调整
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

# --- 以下为 Logs 邮件处理相关辅助函数 ---
def _now_hk() -> datetime:
    return datetime.now(ZoneInfo("Asia/Hong_Kong"))

def ensure_logs_cache():
    if os.path.exists(LOGS_CACHE_PATH):
        return
    with open(LOGS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def read_logs_cache() -> List[Dict[str, Any]]:
    ensure_logs_cache()
    try:
        with open(LOGS_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _filter_logs(logs: List[Dict[str, Any]], days: int, mode: str) -> List[Dict[str, Any]]:
    cutoff = _now_hk() - timedelta(days=days)
    out = []
    for x in logs:
        try:
            ts = datetime.fromisoformat(x.get("ts", ""))
        except Exception:
            continue
        if ts < cutoff:
            continue
        st = (x.get("status") or "").upper()
        if mode == "SUCCESS" and st != "SUCCESS":
            continue
        if mode == "ERROR" and st != "ERROR":
            continue
        out.append(x)
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out

def _get_logs_view(context: ContextTypes.DEFAULT_TYPE, session_key: str) -> Dict[str, Any]:
    key = f"logs_view_{session_key}"
    if key not in context.user_data:
        context.user_data[key] = {"days": 1, "mode": "ALL", "page": 0}
    return context.user_data[key]

# --- Logs 菜单及交互逻辑 ---
async def show_logs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, session_key: str):
    query = update.callback_query
    view = _get_logs_view(context, session_key)
    days, mode, page = view["days"], view["mode"], view["page"]

    logs = read_logs_cache()
    filtered = _filter_logs(logs, days=days, mode=mode)

    succ = sum(1 for x in filtered if (x.get("status") or "").upper() == "SUCCESS")
    fail = sum(1 for x in filtered if (x.get("status") or "").upper() == "ERROR")

    total = len(filtered)
    start = page * LOGS_PER_PAGE
    end = start + LOGS_PER_PAGE
    items = filtered[start:end]

    text = (
        f"🧾 Logs（最近{days}天 / {mode}）\n"
        f"成功: {succ}  失败: {fail}  总计: {total}\n"
        f"页: {page + 1} / {max(1, (total + LOGS_PER_PAGE - 1)//LOGS_PER_PAGE)}"
    )

    keyboard = []
    for x in items:
        st = (x.get("status") or "").upper()
        # 按钮文本：✅ or ❌ + 截断标题
        prefix = "✅" if st == "SUCCESS" else "❌"
        short_title = (x.get("title") or "")[:8]
        btn_text = f"{prefix} {short_title}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"log_detail|{session_key}|{x.get('id')}")])

    # 筛选：天数
    keyboard.append([
        InlineKeyboardButton("1天", callback_data=f"logs_days|{session_key}|1"),
        InlineKeyboardButton("3天", callback_data=f"logs_days|{session_key}|3"),
        InlineKeyboardButton("7天", callback_data=f"logs_days|{session_key}|7"),
    ])
    # 筛选：状态
    keyboard.append([
        InlineKeyboardButton("全部", callback_data=f"logs_mode|{session_key}|ALL"),
        InlineKeyboardButton("成功", callback_data=f"logs_mode|{session_key}|SUCCESS"),
        InlineKeyboardButton("失败", callback_data=f"logs_mode|{session_key}|ERROR"),
    ])
    # 翻页 + 刷新 + 返回
    keyboard.append([
        InlineKeyboardButton("⬅️ 上一页", callback_data=f"logs_page|{session_key}|-1"),
        InlineKeyboardButton("➡️ 下一页", callback_data=f"logs_page|{session_key}|1"),
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 刷新", callback_data=f"logs_refresh|{session_key}"),
        InlineKeyboardButton("⬅️ 返回", callback_data=f"logs_back|{session_key}"),
    ])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def on_menu_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    _get_logs_view(context, session_key)  # init
    await show_logs_menu(update, context, session_key)

async def on_logs_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, days = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    view = _get_logs_view(context, session_key)
    view["days"] = int(days)
    view["page"] = 0
    await show_logs_menu(update, context, session_key)

async def on_logs_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, mode = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    view = _get_logs_view(context, session_key)
    view["mode"] = mode
    view["page"] = 0
    await show_logs_menu(update, context, session_key)

async def on_logs_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, delta = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    view = _get_logs_view(context, session_key)
    logs = _filter_logs(read_logs_cache(), days=view["days"], mode=view["mode"])
    max_page = max(0, (len(logs) - 1) // LOGS_PER_PAGE)
    view["page"] = min(max(0, view["page"] + int(delta)), max_page)
    await show_logs_menu(update, context, session_key)

async def on_logs_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer("刷新中...", cache_time=0)
    except BadRequest:
        # 回调过期就忽略，不要让整个刷新流程炸掉
        pass

    session_key = query.data.split('|')[1]

    if session_key not in user_sessions:
        try:
            await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        except Exception:
            pass
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    days = _get_logs_view(context, session_key)["days"]

    try:
        await asyncio.to_thread(fetch_logs_from_gmail, days=days, max_results=200)
    except Exception as e:
        try:
            await query.answer(f"拉取失败: {e}", show_alert=True)
        except BadRequest:
            pass

    await show_logs_menu(update, context, session_key)

async def on_log_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, log_id = query.data.split("|")

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )

    logs = read_logs_cache()
    x = next((r for r in logs if str(r.get("id")) == str(log_id)), None)
    if not x:
        await query.edit_message_text("⚠️ 记录不存在或已过期。")
        return

    st = (x.get("status") or "").upper()
    code = x.get("error_code")
    err = ERROR_TEXT.get(int(code), "") if code is not None else ""
    ts = x.get("ts", "")
    subject = x.get("subject", "")
    title = x.get("title", "")

    text = (
        f"🧾 Log 详情\n"
        f"时间: {ts}\n"
        f"状态: {st}\n"
        f"错误码: {code or '-'} {f'({err})' if err else ''}\n"
        f"标题: {title}\n\n"
        f"Subject:\n{subject}"
    )
    keyboard = [[InlineKeyboardButton("⬅️ 返回列表", callback_data=f"menu_logs|{session_key}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def on_logs_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split("|")[1]

    if session_key not in user_sessions:
        await query.edit_message_text("⚠️ 会话已结束，请重新@我开始。")
        return

    touch_session(
        context=context,
        session_key=session_key,
        user_id=query.from_user.id,
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
    )
    await handle_mention(update, context)  # 回到你的主菜单渲染


# --- Logs 邮件处理相关辅助函数实现 ---
def _safe_header(headers: list, name: str) -> str:
    for h in headers or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""

def _parse_status_error_from_subject(subject: str):
    s = (subject or "").upper()
    if "SUCCESS" in s:
        return "SUCCESS", None

    m = re.search(r"ERROR\s*(\d+)", s)
    if m:
        return "ERROR", int(m.group(1))

    if "ERROR" in s:
        return "ERROR", None

    return "UNKNOWN", None


def _extract_fields_from_text(text: str):
    gmail_id = None
    original_subject = None

    m1 = re.search(r"Gmail ID\s*:\s*([0-9a-fA-F]+)", text or "")
    if m1:
        gmail_id = m1.group(1).strip()

    m2 = re.search(r"Original Subject\s*:\s*([^\r\n]+)", text or "")
    if m2:
        original_subject = m2.group(1).strip()

    return gmail_id, original_subject


def upsert_logs_cache(items: list):
    ensure_logs_cache()
    existing = read_logs_cache()
    by_key = {}
    for x in existing:
        k = x.get("gmail_id") or x.get("id")
        if k:
            by_key[k] = x
    for it in items:
        k = it.get("gmail_id") or it.get("id")
        if k:
            by_key[k] = it

    merged = list(by_key.values())
    merged.sort(key=lambda r: r.get("ts", ""), reverse=True)
    with open(LOGS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

def _b64url_decode(data: str) -> str:
    if not data:
        return ""
    # Gmail 是 base64url
    raw = base64.urlsafe_b64decode(data + "===")
    return raw.decode("utf-8", errors="ignore")

def _extract_text_from_payload(payload: dict) -> str:
    if not payload:
        return ""
    mime = (payload.get("mimeType") or "").lower()
    body = (payload.get("body") or {})
    data = body.get("data")

    # 直接是 text/plain
    if mime == "text/plain" and data:
        return _b64url_decode(data)

    # multipart 递归找 text/plain
    for part in payload.get("parts") or []:
        t = _extract_text_from_payload(part)
        if t:
            return t

    # 兜底：如果只有 text/html，就解出来（只用于搜字段，不做完整渲染）
    if mime == "text/html" and data:
        return html.unescape(_b64url_decode(data))

    return ""

def fetch_logs_from_gmail(days: int = 1, max_results: int = 200) -> int:
    service = get_gmail_service()

    # 只抓 Subject 含 SUCCESS/ERROR 的邮件，避免 (SUCCESS OR ERROR) 误命中正文
    q = f'(subject:SUCCESS OR subject:ERROR) newer_than:{days}d'

    # 初步测试输出
    print("q =", q)
    resp = service.users().messages().list(userId="me", q=q, maxResults=100).execute()
    print("resultSizeEstimate =", resp.get("resultSizeEstimate"))
    print("messages len =", len(resp.get("messages", []) or []))


    # 1) 先分页 list 拿到 message id 列表
    msgs = []
    page_token = None
    while True:
        remaining = max_results - len(msgs)
        if remaining <= 0:
            break

        resp = service.users().messages().list(
            userId="me",
            q=q,
            maxResults=min(100, remaining),
            pageToken=page_token,
            # ⚠️ 不要写 labelIds=["INBOX"]，否则归档/不在收件箱的 logs 会抓不到
        ).execute()

        batch = resp.get("messages", []) or []
        msgs.extend(batch)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # 2) 对每封邮件 get(full) 解析字段
    out = []
    for m in msgs:
        mid = m.get("id")
        if not mid:
            continue

        detail = service.users().messages().get(
            userId="me",
            id=mid,
            format="full",
        ).execute()

        payload = (detail.get("payload") or {})
        headers = payload.get("headers") or []
        subject = _safe_header(headers, "Subject")

        status, error_code = _parse_status_error_from_subject(subject)
        if status not in ("SUCCESS", "ERROR"):
            continue

        snippet = detail.get("snippet") or ""
        body_text = _extract_text_from_payload(payload) or ""

        gmail_id, original_subject = _extract_fields_from_text(body_text)
        if not original_subject:
            gmail_id2, original_subject2 = _extract_fields_from_text(snippet)
            gmail_id = gmail_id or gmail_id2
            original_subject = original_subject2

        internal_ms = int(detail.get("internalDate", "0") or "0")
        ts = datetime.fromtimestamp(
            internal_ms / 1000,
            ZoneInfo("Asia/Hong_Kong")
        ).isoformat(timespec="seconds")

        title = original_subject or subject
        short_title = (title or "")[:8]

        out.append({
            "id": mid,
            "ts": ts,
            "status": status,
            "error_code": error_code,
            "title": title,
            "short_title": short_title,
            "subject": subject,
            "gmail_id": gmail_id,
            "original_subject": original_subject,
        })

    upsert_logs_cache(out)
    return len(out)



def main():
    # 配置文件含 telegram_token
    with open('config.json', 'r', encoding="utf-8") as f:
        config = json.load(f)
    BOT_TOKEN = config['telegram_token']

    # 可选：从 config.json 覆盖目标邮箱
    global TARGET_EMAIL
    global USE_DRIVE_SHARE, DRIVE_FOLDER_ID, DRIVE_ROOT_FOLDER_NAME
    try:
        if isinstance(config, dict) and config.get("target_email"):
            TARGET_EMAIL = str(config.get("target_email")).strip()
        if isinstance(config, dict) and config.get("use_drive_share") is not None:
            USE_DRIVE_SHARE = bool(config.get("use_drive_share"))
        if isinstance(config, dict) and config.get("drive_folder_id"):
            DRIVE_FOLDER_ID = str(config.get("drive_folder_id")).strip() or None
        if isinstance(config, dict) and config.get("drive_root_folder_name"):
            DRIVE_ROOT_FOLDER_NAME = str(config.get("drive_root_folder_name")).strip() or DRIVE_ROOT_FOLDER_NAME
    except Exception:
        pass

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 消息处理器
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'@'), handle_mention))
    # 兜底文本（只在 awaiting_fb_url 时处理）
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Callback 处理器
    # 1. 发送确认
    app.add_handler(CallbackQueryHandler(on_confirm_send, pattern=r"^confirm_send\|"))
    
    # 2. 进入删除模式菜单
    app.add_handler(CallbackQueryHandler(on_menu_delete_mode, pattern=r"^menu_delete_mode\|"))
    
    # 3. 单个文件删除流程
    app.add_handler(CallbackQueryHandler(on_ask_del_one, pattern=r"^ask_del_one\|"))
    app.add_handler(CallbackQueryHandler(on_do_del_one, pattern=r"^do_del_one\|"))
    
    # 4. 全部删除流程
    app.add_handler(CallbackQueryHandler(on_ask_del_all, pattern=r"^ask_del_all\|"))
    app.add_handler(CallbackQueryHandler(on_do_del_all, pattern=r"^do_del_all\|"))
    
    # 5. 返回主菜单
    app.add_handler(CallbackQueryHandler(on_back_to_main, pattern=r"^back_to_main\|"))

    # 6. 设置流程
    app.add_handler(CallbackQueryHandler(on_menu_settings, pattern=r"^menu_settings\|"))
    app.add_handler(CallbackQueryHandler(on_set_option, pattern=r"^set_option\|"))
    app.add_handler(CallbackQueryHandler(on_settings_confirm, pattern=r"^settings_confirm\|"))
    app.add_handler(CallbackQueryHandler(on_settings_cancel, pattern=r"^settings_cancel\|"))
    app.add_handler(CallbackQueryHandler(on_settings_cancel_confirm, pattern=r"^settings_cancel_confirm\|"))
    app.add_handler(CallbackQueryHandler(on_menu_settings_back, pattern=r"^menu_settings_back\|"))

    # 6.5 结束会话
    app.add_handler(CallbackQueryHandler(on_end_session, pattern=r"^end_session\|"))

    # 6.6 FB URL 流程
    app.add_handler(CallbackQueryHandler(on_fb_url_menu, pattern=r"^fb_url_menu\|"))
    app.add_handler(CallbackQueryHandler(on_fb_url_reset, pattern=r"^fb_url_reset\|"))
    app.add_handler(CallbackQueryHandler(on_fb_url_send, pattern=r"^fb_url_send\|"))

    # 7. Logs 菜单及交互逻辑
    app.add_handler(CallbackQueryHandler(on_menu_logs, pattern=r"^menu_logs\|"))
    app.add_handler(CallbackQueryHandler(on_logs_days, pattern=r"^logs_days\|"))
    app.add_handler(CallbackQueryHandler(on_logs_mode, pattern=r"^logs_mode\|"))
    app.add_handler(CallbackQueryHandler(on_logs_page, pattern=r"^logs_page\|"))
    app.add_handler(CallbackQueryHandler(on_logs_refresh, pattern=r"^logs_refresh\|"))
    app.add_handler(CallbackQueryHandler(on_log_detail, pattern=r"^log_detail\|"))
    app.add_handler(CallbackQueryHandler(on_logs_back, pattern=r"^logs_back\|"))


    print("Bot 已启动...")
    app.run_polling()

if __name__ == '__main__':
    main()
