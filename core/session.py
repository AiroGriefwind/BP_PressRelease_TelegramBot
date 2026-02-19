import os
import uuid
from typing import Any, Dict, Optional

from telegram.ext import Application, ContextTypes
from telegram.ext import Job

import config
from core.logging_ops import log_event
from core.time_utils import now_hk
from ui.messages import try_edit_message_text

# 保存每个用户添加的文件列表（支持群聊私聊）
user_sessions: Dict[str, Dict[str, Any]] = {}

# 会话超时（无操作）自动结束：10分钟
session_timeout_jobs: Dict[str, Job] = {}

# 记录每个 (chat_id, user_id) 最近一次出现的 FB URL
# key: session_key = f"{chat_id}_{user_id}"
last_seen_fb_url: Dict[str, Dict[str, Any]] = {}


def new_session_struct() -> Dict[str, Any]:
    return {
        "files": [],
        "settings": config.DEFAULT_SETTINGS.copy(),
        "session_id": uuid.uuid4().hex,
        "created_ts": now_hk().isoformat(timespec="seconds"),
        # FB URL 流程
        "fb_url": None,
        "awaiting_fb_url": False,
        # 大批量图片发送辅助
        "photo_seq": 0,
        "sending": False,
        "sending_snapshot": [],
        # 附件回执 UI（避免群聊刷屏/触发频控）
        "add_msg_id": None,
        "add_msg_ts": 0.0,
        "add_msg_count": 0,
        "add_msg_done": False,
        "add_msg_done_job": None,
        "add_msg_done_task": None,
        # 长文本公关稿确认流程
        "awaiting_pr_confirm": False,
        "pending_pr_text": None,
        "pending_pr_meta": None,
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
        _safe_del(ud, f"temp_fb_settings_{session_key}")
        _safe_del(ud, f"logs_view_{session_key}")
    except Exception:
        pass


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
        # 取消附件回执的延迟完成提示
        job = session_data.get("add_msg_done_job")
        if job is not None:
            try:
                job.schedule_removal()
            except Exception:
                pass
        task = session_data.get("add_msg_done_task")
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass

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
        await try_edit_message_text(
            application,
            int(final_chat_id),
            int(final_message_id),
            reason_text,
        )

    # 清除上一条 FB URL 兜底记录，避免影响下一会话
    try:
        last_seen_fb_url.pop(session_key, None)
    except Exception:
        pass


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
        reason_text="⏱️ 10分鐘無操作，會話自動結束。",
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
    sd["last_touch_ts"] = now_hk().isoformat(timespec="seconds")
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
            when=config.SESSION_TIMEOUT_SECONDS,
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
