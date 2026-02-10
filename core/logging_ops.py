import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from telegram import Update

from config import OPS_LOG_DIR
from core.time_utils import now_hk

_ops_log_lock = threading.Lock()


def _append_ops_log(record: Dict[str, Any]):
    """
    追加写入一条操作日志（JSONL）。
    注意：不要在这里抛异常影响主流程。
    """
    try:
        # 按日分目录：logs/YYYYMMDD/ops_log.jsonl
        ts = record.get("ts")
        try:
            dt = datetime.fromisoformat(ts) if ts else now_hk()
        except Exception:
            dt = now_hk()
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
            "ts": now_hk().isoformat(timespec="seconds"),
            "event": event,
            "session_key": session_key,
            "session_id": session_id,
            **actor,
            "extra": extra or {},
        }
        _append_ops_log(record)
    except Exception:
        return
