import json
import os
from typing import Optional

import config


def load_runtime_config_from_file(config_path: str = "config.json") -> bool:
    """
    从指定 json 文件加载运行时配置并应用到 config 模块。
    返回 True 表示读取并应用成功；否则返回 False。
    """
    path = (config_path or "").strip() or "config.json"
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return False
        config.apply_runtime_config(cfg)
        return True
    except Exception:
        return False
