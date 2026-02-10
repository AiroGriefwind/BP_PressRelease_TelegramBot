#!/bin/bash

cd ~/BP_PressRelease_TelegramBot

# 停止正在运行的 bot
pkill -f bot.py

# 拉取最新代码
git pull origin main

# 自动安装新依赖（有就会自动补齐）
source ~/venv/bin/activate
pip install -r requirements.txt

# 重新启动 bot，后台运行，把日志保存到 bot.log
nohup python3 bot.py > bot.log 2>&1 &

echo "Bot updated and restarted at $(date)"
