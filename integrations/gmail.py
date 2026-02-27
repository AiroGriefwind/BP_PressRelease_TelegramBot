import base64
import html
import json
import os
import pickle
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from base64 import urlsafe_b64encode

import markdown
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


def _is_meta_header_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if s in {"即時發放", "即时发放"}:
        return True
    if re.fullmatch(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", s):
        return True
    if re.fullmatch(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", s):
        return True
    return False


def _pick_title_from_pr_body(pr_body_text: str) -> str:
    lines = [(ln or "").strip() for ln in (pr_body_text or "").splitlines()]
    filtered = [ln for ln in lines if not _is_meta_header_line(ln)]
    if not filtered:
        return ""

    for ln in filtered:
        m = re.search(r"\*([^*\n]{2,200})\*", ln)
        if m:
            return f"*{m.group(1).strip()}*"
    for ln in filtered:
        if ln:
            return ln
    return ""


def _pick_subject_title(file_names: List[str], pr_body_text: str) -> str:
    # 如果有长信息，优先使用公关稿的标题
    body_title = _pick_title_from_pr_body(pr_body_text)
    if body_title:
        return body_title
    # 否则使用附件标题
    attachment_title = _pick_attachment_title(file_names)
    return attachment_title


def _render_pr_body_markdown_html(pr_body_text: str, pr_body_html: Optional[str] = None) -> str:
    if (pr_body_html or "").strip():
        rich = (pr_body_html or "").strip()
        # Telegram entities 转出的 HTML 常含换行符但不含块级标签；
        # 这里显式换成 <br>，避免在邮件客户端被折叠成单段。
        if not re.search(r"</?(p|div|li|ul|ol|h[1-6]|blockquote|br)\b", rich, flags=re.I):
            rich = rich.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>\n")
        # PR 文案里常用 *xxx* 表示"加粗重点"，即使 Telegram HTML 也可能有未转换的 *xxx*，需要转换为 <b>xxx</b>
        # 使用负向前后查找确保不会匹配已经是 **xxx** 的情况（避免匹配 <b>xxx</b> 因为 HTML 中不应该有 * 在标签内）
        rich = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<b>\1</b>", rich)
        return rich
    raw = (pr_body_text or "").strip()
    if not raw or raw == "無":
        return "<p>無</p>"
    # PR 文案里常用 *xxx* 表示“加粗重点”，这里统一转成 Markdown 粗体。
    raw = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"**\1**", raw)
    # Escape raw HTML from user input but preserve Markdown syntax.
    safe_markdown = html.escape(raw, quote=False)
    return markdown.markdown(
        safe_markdown,
        extensions=["extra", "sane_lists", "nl2br"],
    )


def _build_email_html(
    *,
    sender_info: dict,
    settings: dict,
    pr_body_text: str,
    pr_body_html: Optional[str] = None,
    attachments_text: Optional[str] = None,
) -> str:
    meta_html = (
        f"<p><strong>来自:</strong> {html.escape(sender_info.get('name', ''))} "
        f"(@{html.escape(sender_info.get('username', ''))})</p>"
        f"<p><strong>群组:</strong> {html.escape(sender_info.get('chat_title', ''))}</p>"
        f"<p><strong>时间:</strong> {html.escape(sender_info.get('date', ''))}</p>"
        f"<p><strong>類型：</strong>{html.escape(settings.get('type', ''))}</p>"
        f"<p><strong>優先度：</strong>{html.escape(settings.get('priority', ''))}</p>"
        f"<p><strong>語言：</strong>{html.escape(settings.get('language', ''))}</p>"
        f"<p><strong>target:</strong> 來稿</p>"
    )
    if attachments_text is not None:
        meta_html += f"<p><strong>附件:</strong> {html.escape(attachments_text)}</p>"

    rendered_pr_body_html = _render_pr_body_markdown_html(pr_body_text, pr_body_html)
    return (
        '<div style="font-family: Arial, sans-serif; font-size: 14px; color: #111;">'
        f"{meta_html}"
        "<hr style='border:none;border-top:1px solid #ddd;margin:12px 0;'>"
        "<p><strong>公關稿正文：</strong></p>"
        "<div style='line-height:1.7;font-size:14px;'>"
        f"{rendered_pr_body_html}"
        "</div>"
        "</div>"
    )


def _attach_plain_and_html_body(message: MIMEMultipart, plain_body: str, html_body: str):
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    message.attach(alt)


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


def send_email_with_attachments(
    service,
    file_paths,
    sender_info,
    file_names,
    settings,
    pr_body_text: str = "",
    pr_body_html: Optional[str] = None,
):
    message = MIMEMultipart()
    message["to"] = config.TARGET_EMAIL
    subject_title = _pick_subject_title(list(file_names), pr_body_text)
    subject = "新稿件: " + subject_title
    if len(subject) > config.MAX_SUBJECT_LEN:
        subject = subject[: config.MAX_SUBJECT_LEN - 3] + "..."
    message["subject"] = subject

    pr_body_value = (pr_body_text or "").strip() or "無"
    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}
target: 來稿
附件: {', '.join(file_names)}

公關稿正文：{pr_body_value}
"""
    html_body = _build_email_html(
        sender_info=sender_info,
        settings=settings,
        pr_body_text=pr_body_value,
        pr_body_html=pr_body_html,
        attachments_text=", ".join(file_names),
    )
    _attach_plain_and_html_body(message, body, html_body)

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
    pr_body_text: str = "",
    pr_body_html: Optional[str] = None,
    *,
    folder_link: str,
    title: str,
    attachment_paths: List[str],
    attachment_names: List[str],
):
    message = MIMEMultipart()
    message["to"] = config.TARGET_EMAIL
    # 如果有长信息，优先使用公关稿标题；否则使用非图片附件的名字作为标题
    pr_body_title = _pick_title_from_pr_body(pr_body_text)
    if pr_body_title:
        subject_title = pr_body_title
    else:
        # 没有长信息时，使用非图片附件的名字作为标题（不使用Drive文件夹标题）
        # attachment_names 包含非图片附件，优先使用它们
        attachment_title = _pick_attachment_title(attachment_names)
        if attachment_title != "untitled":
            subject_title = attachment_title
        else:
            # 如果没有非图片附件，使用所有文件中的第一个非图片文件名
            all_file_names = [x.get("name") for x in file_items]
            attachment_title = _pick_attachment_title(all_file_names)
            subject_title = attachment_title
    subject = "新稿件(Drive): " + subject_title
    if len(subject) > config.MAX_SUBJECT_LEN:
        subject = subject[: config.MAX_SUBJECT_LEN - 3] + "..."
    message["subject"] = subject

    pr_body_value = (pr_body_text or "").strip() or "無"
    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}
target: 來稿

公關稿正文：{pr_body_value}
"""
    html_body = _build_email_html(
        sender_info=sender_info,
        settings=settings,
        pr_body_text=pr_body_value,
        pr_body_html=pr_body_html,
    )
    _attach_plain_and_html_body(message, body, html_body)

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
    # 如果同时触发大批量模式和长信息模式，使用不同的JSON文件名
    has_long_msg = pr_body_value != "無"
    json_filename = "long_msg_drive_links.json" if has_long_msg else "drive_links.json"
    drive_links_part.add_header(
        "Content-Disposition", "attachment", filename=(json_filename)
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
target: 來稿
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


def get_logs_cache_info() -> dict:
    ensure_logs_cache()
    try:
        mtime = os.path.getmtime(config.LOGS_CACHE_PATH)
        dt = datetime.fromtimestamp(mtime, now_hk().tzinfo)
        age_seconds = max(0, int((now_hk() - dt).total_seconds()))
        return {
            "last_refresh_ts": dt.isoformat(timespec="seconds"),
            "age_seconds": age_seconds,
            "ttl_seconds": config.LOGS_CACHE_TTL_SECONDS,
            "stale": age_seconds > config.LOGS_CACHE_TTL_SECONDS,
        }
    except Exception:
        return {
            "last_refresh_ts": None,
            "age_seconds": None,
            "ttl_seconds": config.LOGS_CACHE_TTL_SECONDS,
            "stale": True,
        }


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


def fetch_rthk_emails_for_excel(hours: int = 24, max_results: int = 500) -> List[Dict[str, Any]]:
    service = get_gmail_service()
    subject_keyword = "RTHK Batch"
    preferred_label_name = "公關稿ai-logs"
    q = 'subject:"RTHK Batch" newer_than:1d'

    def _norm_label_name(name: str) -> str:
        # 统一：大小写不敏感 + 去空白 + 去连字符，兼容 "AI Logs" / "ai-logs" 等差异
        return re.sub(r"[\s\-_]+", "", (name or "").strip().lower())

    label_id = None
    target_norm = _norm_label_name(preferred_label_name)
    try:
        labels_resp = service.users().labels().list(userId="me").execute()
        labels = labels_resp.get("labels", []) or []

        # 1) 优先精确名（忽略大小写）
        for lb in labels:
            lb_name = (lb.get("name") or "").strip()
            if lb_name.lower() == preferred_label_name.lower():
                label_id = lb.get("id")
                break

        # 2) 再做规范化模糊匹配（兼容空格/连字符）
        if not label_id:
            for lb in labels:
                lb_name = lb.get("name") or ""
                if _norm_label_name(lb_name) == target_norm:
                    label_id = lb.get("id")
                    break
        # 3) 最后做“包含式”匹配，兼容名称有前后缀
        if not label_id:
            for lb in labels:
                lb_name = lb.get("name") or ""
                n = _norm_label_name(lb_name)
                if target_norm in n or n in target_norm:
                    label_id = lb.get("id")
                    break
    except Exception:
        label_id = None

    msgs = []
    page_token = None
    while True:
        remaining = max_results - len(msgs)
        if remaining <= 0:
            break
        # 先按 subject 拉候选，再在 get(full) 里按 labelId 严格过滤。
        # 这样保留 label 条件，同时规避 list 接口对 label 查询的兼容问题。
        resp = service.users().messages().list(
            userId="me",
            q=q,
            maxResults=min(100, remaining),
            pageToken=page_token,
        ).execute()
        msgs.extend(resp.get("messages", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    cutoff = now_hk() - timedelta(hours=hours)
    out: List[Dict[str, Any]] = []
    for msg in msgs:
        mid = msg.get("id")
        if not mid:
            continue
        detail = service.users().messages().get(userId="me", id=mid, format="full").execute()
        if label_id:
            msg_label_ids = detail.get("labelIds", []) or []
            if label_id not in msg_label_ids:
                continue
        payload = detail.get("payload") or {}
        subject = _safe_header(payload.get("headers") or [], "Subject").strip()
        if subject_keyword.lower() not in subject.lower():
            continue

        internal_ms = int(detail.get("internalDate", "0") or "0")
        ts_dt = datetime.fromtimestamp(internal_ms / 1000, now_hk().tzinfo)
        if ts_dt < cutoff:
            continue

        out.append(
            {
                "id": mid,
                "subject": subject,
                "ts": ts_dt.isoformat(timespec="seconds"),
                "body_text": _extract_text_from_payload(payload) or "",
            }
        )

    out.sort(key=lambda x: x.get("ts", ""))
    return out
