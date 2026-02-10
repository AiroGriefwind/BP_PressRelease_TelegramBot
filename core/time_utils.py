from datetime import datetime
from zoneinfo import ZoneInfo


def now_hk() -> datetime:
    return datetime.now(ZoneInfo("Asia/Hong_Kong"))
