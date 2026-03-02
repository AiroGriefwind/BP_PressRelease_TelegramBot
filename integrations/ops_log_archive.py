import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from google.cloud import storage

import config


def _get_tz() -> ZoneInfo:
    tz_name = (config.OPS_LOG_ARCHIVE_TIMEZONE or "Asia/Hong_Kong").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Hong_Kong")


def _normalize_prefix(prefix: str) -> str:
    return (prefix or "").strip().strip("/")


def resolve_day_yyyymmdd(day: str = "today") -> str:
    token = (day or "today").strip().lower()
    tz = _get_tz()
    now = datetime.now(tz)
    if token == "today":
        return now.strftime("%Y%m%d")
    if token == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    if len(token) == 8 and token.isdigit():
        return token
    raise ValueError("day must be one of: today, yesterday, YYYYMMDD")


def build_local_log_path(day_yyyymmdd: str) -> str:
    return os.path.join(config.OPS_LOG_DIR, day_yyyymmdd, "ops_log.jsonl")


def build_object_path(day_yyyymmdd: str) -> str:
    prefix = _normalize_prefix(config.OPS_LOG_ARCHIVE_PREFIX)
    if prefix:
        return f"{prefix}/{day_yyyymmdd}/ops_log.jsonl"
    return f"{day_yyyymmdd}/ops_log.jsonl"


def _build_storage_client() -> storage.Client:
    sa_json = (config.OPS_LOG_ARCHIVE_CREDENTIALS_JSON or "").strip()
    if sa_json:
        return storage.Client.from_service_account_json(sa_json)
    return storage.Client()


def upload_ops_log_by_day(day: str = "today") -> Dict[str, Any]:
    day_yyyymmdd = resolve_day_yyyymmdd(day)
    local_path = build_local_log_path(day_yyyymmdd)
    object_path = build_object_path(day_yyyymmdd)
    bucket_name = (config.OPS_LOG_ARCHIVE_BUCKET or "").strip()

    if not config.OPS_LOG_ARCHIVE_ENABLED:
        return {
            "ok": False,
            "reason": "archive_disabled",
            "day": day_yyyymmdd,
            "local_path": local_path,
            "object_path": object_path,
            "bucket": bucket_name,
            "message": "ops log archive is disabled",
        }

    if not bucket_name:
        return {
            "ok": False,
            "reason": "bucket_not_configured",
            "day": day_yyyymmdd,
            "local_path": local_path,
            "object_path": object_path,
            "bucket": bucket_name,
            "message": "OPS_LOG_ARCHIVE_BUCKET is empty",
        }

    if not os.path.exists(local_path):
        return {
            "ok": False,
            "reason": "file_not_found",
            "day": day_yyyymmdd,
            "local_path": local_path,
            "object_path": object_path,
            "bucket": bucket_name,
            "message": "ops log file not found",
        }

    try:
        file_size = os.path.getsize(local_path)
    except Exception:
        file_size = None

    try:
        client = _build_storage_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_path)
        blob.upload_from_filename(local_path, content_type="application/jsonl")
        blob.reload()
        return {
            "ok": True,
            "reason": "uploaded",
            "day": day_yyyymmdd,
            "local_path": local_path,
            "object_path": object_path,
            "bucket": bucket_name,
            "size_bytes": file_size,
            "gcs_uri": f"gs://{bucket_name}/{object_path}",
            "generation": getattr(blob, "generation", None),
            "updated": getattr(blob, "updated", None).isoformat()
            if getattr(blob, "updated", None)
            else None,
        }
    except Exception as e:
        return {
            "ok": False,
            "reason": "upload_error",
            "day": day_yyyymmdd,
            "local_path": local_path,
            "object_path": object_path,
            "bucket": bucket_name,
            "size_bytes": file_size,
            "error": str(e),
        }


def format_upload_result(result: Dict[str, Any]) -> str:
    if result.get("ok"):
        return (
            f"上传成功: {result.get('day')} -> "
            f"{result.get('gcs_uri')} (size={result.get('size_bytes')})"
        )
    return (
        f"上传失败: reason={result.get('reason')} day={result.get('day')} "
        f"bucket={result.get('bucket')} path={result.get('object_path')} "
        f"error={result.get('error') or result.get('message')}"
    )


def is_archive_admin(user_id: Optional[int]) -> bool:
    admins = config.OPS_LOG_ARCHIVE_ADMIN_USER_IDS or []
    if not admins:
        return True
    try:
        uid = int(user_id) if user_id is not None else None
    except Exception:
        return False
    return uid in admins
