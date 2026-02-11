import base64
import html
import json
import os
import pickle
import re
from datetime import datetime
from typing import List

from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from base64 import urlsafe_b64encode

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from core.time_utils import now_hk
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


def get_google_creds():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", config.SCOPES
            )
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)
    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_creds())


def send_email_with_attachments(service, file_paths, sender_info, file_names, settings):
    message = MIMEMultipart()
    message["to"] = config.TARGET_EMAIL
    subject_title = _pick_attachment_title(list(file_names))
    subject = "新稿件: " + subject_title
    if len(subject) > config.MAX_SUBJECT_LEN:
        subject = subject[: config.MAX_SUBJECT_LEN - 3] + "..."
    message["subject"] = subject

    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}
附件: {', '.join(file_names)}
"""
    message.attach(MIMEText(body, "plain", "utf-8"))

    for file_path, file_name in zip(file_paths, file_names):
        try:
            with open(file_path, "rb") as f:
                part = MIMEApplication(f.read())
                part.add_header(
                    "Content-Disposition", "attachment", filename=(Header(file_name, "utf-8").encode())
                )
                message.attach(part)
        except Exception as e:
            print(f"无法读取文件: {file_name}, {e}")

    raw_message = urlsafe_b64encode(message.as_bytes()).decode()
    try:
        service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        return True, None
    except Exception as e:
        print(f"发送邮件失败: {e}")
        return False, str(e)


def send_email_with_drive_links(
    service,
    sender_info,
    file_items,
    settings,
    *,
    folder_link: str,
    title: str,
    attachment_paths: List[str],
    attachment_names: List[str],
):
    message = MIMEMultipart()
    message["to"] = config.TARGET_EMAIL
    subject_title = title or _pick_attachment_title([x.get("name") for x in file_items])
    subject = "新稿件(Drive): " + subject_title
    if len(subject) > config.MAX_SUBJECT_LEN:
        subject = subject[: config.MAX_SUBJECT_LEN - 3] + "..."
    message["subject"] = subject

    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}
"""
    message.attach(MIMEText(body, "plain", "utf-8"))

    for file_path, file_name in zip(attachment_paths, attachment_names):
        try:
            with open(file_path, "rb") as f:
                part = MIMEApplication(f.read())
                part.add_header(
                    "Content-Disposition", "attachment", filename=(Header(file_name, "utf-8").encode())
                )
                message.attach(part)
        except Exception as e:
            print(f"无法读取文件: {file_name}, {e}")

    drive_links_payload = {
        "title": subject_title,
        "folder_link": folder_link,
        "files": [
            {"name": it.get("name"), "url": it.get("link")} for it in (file_items or [])
        ],
        "generated_at": now_hk().isoformat(timespec="seconds"),
    }
    drive_links_bytes = json.dumps(
        drive_links_payload, ensure_ascii=False, indent=2
    ).encode("utf-8")
    drive_links_part = MIMEApplication(drive_links_bytes)
    drive_links_part.add_header(
        "Content-Disposition", "attachment", filename=("drive_links.json")
    )
    message.attach(drive_links_part)

    raw_message = urlsafe_b64encode(message.as_bytes()).decode()
    try:
        service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        return True, None
    except Exception as e:
        print(f"发送邮件失败: {e}")
        return False, str(e)


def send_email_with_fb_url(service, fb_url: str, sender_info: dict, settings: dict):
    message = MIMEMultipart()
    message["to"] = config.TARGET_EMAIL
    message["subject"] = f"[FB URL]: {fb_url}"

    body = f"""URL: {fb_url}

来自: {sender_info.get('name')} (@{sender_info.get('username')})
群组: {sender_info.get('chat_title')}
时间: {sender_info.get('date')}
類型：{settings.get('type')}
語言：{settings.get('language')}
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


def ensure_logs_cache():
    if os.path.exists(config.LOGS_CACHE_PATH):
        return
    with open(config.LOGS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def read_logs_cache() -> List[dict]:
    ensure_logs_cache()
    try:
        with open(config.LOGS_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


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
    with open(config.LOGS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)


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
    body = payload.get("body") or {}
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
    q = f"(subject:SUCCESS OR subject:ERROR) newer_than:{days}d"

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

        payload = detail.get("payload") or {}
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
        ts = datetime.fromtimestamp(internal_ms / 1000, now_hk().tzinfo).isoformat(
            timespec="seconds"
        )

        title = original_subject or subject
        short_title = (title or "")[:8]

        out.append(
            {
                "id": mid,
                "ts": ts,
                "status": status,
                "error_code": error_code,
                "title": title,
                "short_title": short_title,
                "subject": subject,
                "gmail_id": gmail_id,
                "original_subject": original_subject,
            }
        )

    upsert_logs_cache(out)
    return len(out)
