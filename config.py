import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 日志缓存文件路径
LOGS_CACHE_PATH = os.path.join(BASE_DIR, "logs_cache.json")
OPS_LOG_DIR = os.path.join(BASE_DIR, "logs")

LOGS_PER_PAGE = 8
LOGS_CACHE_TTL_SECONDS = 5 * 60

ERROR_TEXT = {
    100: "沒有找到附件",
    101: "附件內容讀取失敗",
    102: "附件可能是純圖片類型",
    103: "內容獲取失敗 / 缺少必要字段",
    104: "無效數據",
    105: "URL 缺失",
    106: "配置缺失 files 字段",
    107: "文件下載失敗",
    200: "敏感詞",
    300: "AI 處理失敗，通用 AI pipeline 失敗",
    301: "Gemini 處理達到限額",
    400: "SEO 信息提取失敗",
    500: "插入 WP 草稿箱失敗",
    501: "插入 WP 草稿箱部分成功：文字 OK，圖片失敗",
    600: "無法創建任務狀態",
    900: "未知的異常，兜底",
}

# 邮件目标
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

TARGET_EMAIL = "bp.filtermailbox@gmail.com"
USE_DRIVE_SHARE = False
DRIVE_FOLDER_ID = None
DRIVE_ROOT_FOLDER_NAME = "大批量图片"
DRIVE_AUTO_SIZE_MB = 25

# Gmail subject 避免过长
MAX_SUBJECT_LEN = 160

# --- FB URL（Facebook 分享链接）相关常量/辅助 ---
FB_URL_BUTTON_TEXT = "FB URL"
FB_URL_RECENT_SECONDS = 10 * 60  # “上一条URL”兜底：只取最近10分钟

# 可选设置项
SETTINGS_OPTIONS = {
    "type": ["全文不改", "只改標題", "全文改寫"],
    "priority": ["普通", "緊急"],
    "language": ["中文", "英文"],
    "drive_upload": ["普通", "Google Drive"],
}

# 默认设置
DEFAULT_SETTINGS = {
    "type": "全文不改",
    "priority": "普通",
    "language": "中文",
    "drive_upload": "普通",
}

# 会话超时（无操作）自动结束：10分钟
SESSION_TIMEOUT_SECONDS = 10 * 60


def apply_runtime_config(config: dict) -> None:
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
            DRIVE_ROOT_FOLDER_NAME = (
                str(config.get("drive_root_folder_name")).strip()
                or DRIVE_ROOT_FOLDER_NAME
            )
    except Exception:
        pass
