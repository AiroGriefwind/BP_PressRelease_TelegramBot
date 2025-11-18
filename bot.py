import os
import json
import pickle

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import Header
from base64 import urlsafe_b64encode

from zoneinfo import ZoneInfo

# 邮件目标
SCOPES = ['https://www.googleapis.com/auth/gmail.send']
TARGET_EMAIL = 'bp.filtermailbox@gmail.com'

# 保存每个用户添加的文件列表（支持群聊私聊）
user_sessions = {}

def get_gmail_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('gmail', 'v1', credentials=creds)

def send_email_with_attachments(service, file_paths, sender_info, file_names):
    message = MIMEMultipart()
    message['to'] = TARGET_EMAIL
    message['subject'] = "新稿件: " + ', '.join(file_names)
    body = f"""
    来自: {sender_info['name']} (@{sender_info['username']})
    群组: {sender_info['chat_title']}
    时间: {sender_info['date']}
    附件: {', '.join(file_names)}
    """
    message.attach(MIMEText(body, 'plain', 'utf-8'))

    for file_path, file_name in zip(file_paths, file_names):
        with open(file_path, 'rb') as f:
            part = MIMEApplication(f.read(), Name=file_name)
            filename_utf8 = str(Header(file_name, 'utf-8'))
            part.add_header('Content-Disposition',
                f'attachment; filename="{filename_utf8}"')
            message.attach(part)

    raw_message = urlsafe_b64encode(message.as_bytes()).decode()
    try:
        service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()
        return True
    except Exception as e:
        print(f"发送邮件失败: {e}")
        return False

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 可处理文档+图片
    message = update.message
    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"

    os.makedirs('temp', exist_ok=True)
    # 文件类型判断
    file_id, file_name = None, None
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
    elif message.photo:
        photo_file = message.photo[-1]  # 最大分辨率
        file_id = photo_file.file_id
        file_name = f"{file_id}.jpg"

    if file_id and file_name:
        file = await context.bot.get_file(file_id)
        file_path = f"temp/{file_name}"
        await file.download_to_drive(file_path)
        # 存储到 session
        files = user_sessions.get(session_key, [])
        files.append((file_path, file_name))
        user_sessions[session_key] = files
        await message.reply_text(f"已添加: {file_name}")

async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"
    # 判断是否@bot
    bot_username = f"@{context.bot.username}"
    text_or_caption = (message.text or "") + " " + (message.caption or "")
    is_mentioned = bot_username.lower() in text_or_caption.lower()

    files = user_sessions.get(session_key, [])
    if is_mentioned and files:
        # 构建发件人信息
        sender_info = {
            'name': (message.from_user.first_name or "") + (f" {message.from_user.last_name}" if message.from_user.last_name else ""),
            'username': message.from_user.username or "unknown",
            'chat_title': message.chat.title or "private",
            'date': message.date.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime('%Y-%m-%d %H:%M:%S')
        }
        file_paths, file_names = zip(*files)
        gmail_service = get_gmail_service()
        await message.reply_text("正在打包并发送所有附件...")
        success = send_email_with_attachments(gmail_service, file_paths, sender_info, file_names)
        if success:
            await message.reply_text(f"✅ 文件已发送到 {TARGET_EMAIL}")
        else:
            await message.reply_text("❌ 发送失败,请重试")
        # 清理session和临时文件
        for fp in file_paths:
            try: os.remove(fp)
            except: pass
        user_sessions[session_key] = []
    elif is_mentioned:
        await message.reply_text("⚠️ 没有收集到附件，请先上传文件或图片！")

def main():
    # 配置文件含 telegram_token
    with open('config.json', 'r') as f:
        config = json.load(f)
    BOT_TOKEN = config['telegram_token']
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'@'), handle_mention))

    print("Bot 已启动...")
    app.run_polling()

if __name__ == '__main__':
    main()
