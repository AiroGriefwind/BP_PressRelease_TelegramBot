#!/usr/bin/env python3
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config
from core.logging_ops import log_event
from integrations.ops_log_archive import format_upload_result, resolve_day_yyyymmdd, upload_ops_log_by_day


def _load_runtime_config(base_dir: str) -> None:
    cfg_path = os.path.join(base_dir, "config.json")
    if not os.path.exists(cfg_path):
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            config.apply_runtime_config(cfg)
    except Exception:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload local ops_log.jsonl to GCS bucket.")
    parser.add_argument(
        "--day",
        default="today",
        help="today | yesterday | YYYYMMDD",
    )
    args = parser.parse_args()

    _load_runtime_config(BASE_DIR)

    day = (args.day or "today").strip().lower()
    try:
        resolve_day_yyyymmdd(day)
    except Exception:
        print("invalid --day, use: today | yesterday | YYYYMMDD")
        return 2

    result = upload_ops_log_by_day(day)
    print(format_upload_result(result))
    try:
        log_event("opslog_archive_manual_vm", extra={"input_day": day, **result})
    except Exception:
        pass
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
