# Ops Log 自动归档到 Firebase Storage（GCS）说明

## 1. 功能概览

- 每天 `00:00`（默认香港时区）自动上传“前一天”日志：
  - 本地：`logs/YYYYMMDD/ops_log.jsonl`
  - 远端：`gs://telegram_file-to-email_bot_logs/bp_logs/YYYYMMDD/ops_log.jsonl`
- 支持两种手动触发：
  - VM 命令：`python scripts/upload_ops_log.py --day ...`
  - Telegram 命令：`/opslog_push ...` 或 `/opslog_push_today`

## 2. 配置项（config.json）

已新增以下字段：

- `ops_log_archive_enabled`: 是否启用自动/手动上传逻辑。
- `ops_log_archive_bucket`: bucket 名称。
- `ops_log_archive_prefix`: 对象前缀（默认 `bp_logs`）。
- `ops_log_archive_timezone`: 时区（默认 `Asia/Hong_Kong`）。
- `ops_log_archive_credentials_json`: 可选，Service Account JSON 文件绝对路径；为空时使用 ADC。
- `ops_log_archive_admin_user_ids`: Telegram 手动命令管理员白名单（数组，空数组表示不限制）。

## 3. VM 手动触发（紧急拉取）

在项目根目录执行：

```bash
python3 scripts/upload_ops_log.py --day today
python3 scripts/upload_ops_log.py --day yesterday
python3 scripts/upload_ops_log.py --day 20260302
```

返回码：

- `0`：上传成功
- `1`：上传失败（凭证/权限/文件不存在等）
- `2`：参数错误

## 4. Telegram 手动触发

- 上传今天：`/opslog_push_today`
- 上传指定日期：
  - `/opslog_push today`
  - `/opslog_push yesterday`
  - `/opslog_push 20260302`

若配置了 `ops_log_archive_admin_user_ids`，只有白名单用户可执行。

## 5. 自动任务

Bot 启动后会注册每日任务，在 `00:00` 自动执行上传“昨天日志”。

## 6. 凭证与权限检查

至少满足以下之一：

1) VM 已配置 ADC（如 `gcloud auth application-default login`）；  
2) 在 `ops_log_archive_credentials_json` 填入 service account JSON 路径。

并确保账号有 bucket 写权限（例如 `Storage Object Creator`）。

## 7. 常见问题

- 报 `file_not_found`：当天（或指定日期）本地日志还没生成。
- 报 `bucket_not_configured`：未配置 `ops_log_archive_bucket`。
- 报权限相关错误：检查 service account/ADC 以及 bucket IAM。
