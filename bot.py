import json
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from base64 import urlsafe_b64encode
import pickle
from email.mime.application import MIMEApplication
from email.header import Header
from zoneinfo import ZoneInfo

# Gmail API 设置
SCOPES = ['https://www.googleapis.com/auth/gmail.send']
TARGET_EMAIL = 'bp.filtermailbox@gmail.com'

def get_gmail_service():
    """获取 Gmail API 服务"""
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

def send_email_with_attachment(service, file_path, sender_info, file_name):
    """发送带附件的邮件"""
    message = MIMEMultipart()
    message['to'] = TARGET_EMAIL
    message['subject'] = f'新稿件: {file_name}'
    
    # 邮件正文
    body = f"""
    来自: {sender_info['name']} (@{sender_info['username']})
    群组: {sender_info['chat_title']}
    时间: {sender_info['date']}
    
    附件: {file_name}
    """
    message.attach(MIMEText(body, 'plain', 'utf-8'))
    
    # 添加附件
    with open(file_path, 'rb') as f:
        part = MIMEApplication(f.read(), Name=file_name)
        filename_utf8 = str(Header(file_name, 'utf-8'))
        part.add_header(
            'Content-Disposition', 
            f'attachment; filename="{filename_utf8}"'
        )
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

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文档消息"""
    message = update.message
    
    # 检查是否在群组中
    if message.chat.type not in ['group', 'supergroup']:
        return
    
    # 检查是否 @ 了 bot（同时支持文本与媒体 caption）
    bot_username = f"@{context.bot.username}"
    text_or_caption = (message.text or "") + " " + (message.caption or "")
    is_mentioned = bot_username.lower() in text_or_caption.lower()
    if not is_mentioned:
        return

    
    # 检查是否有文档附件
    if not message.document:
        await message.reply_text("请发送 PDF 或 DOC 文件")
        return
    
    # 检查文件类型
    file_name = message.document.file_name
    if not (file_name.lower().endswith('.pdf') or 
            file_name.lower().endswith('.doc') or 
            file_name.lower().endswith('.docx')):
        await message.reply_text("只支持 PDF 和 DOC/DOCX 文件")
        return
    
    try:
        # 下载文件
        await message.reply_text("正在处理文件...")
        file = await context.bot.get_file(message.document.file_id)
        file_path = f'temp/{file_name}'
        os.makedirs('temp', exist_ok=True)
        await file.download_to_drive(file_path)
        
        # 获取发送者信息
        local_time = message.date.astimezone(ZoneInfo("Asia/Hong_Kong"))
        sender_info = {
            'name': message.from_user.first_name + (f' {message.from_user.last_name}' if message.from_user.last_name else ''),
            'username': message.from_user.username or 'unknown',
            'chat_title': message.chat.title,
            'date': message.date.strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # 发送邮件
        gmail_service = get_gmail_service()
        success = send_email_with_attachment(
            gmail_service, 
            file_path, 
            sender_info, 
            file_name
        )
        
        if success:
            await message.reply_text(f"✅ 文件已发送到 {TARGET_EMAIL}")
        else:
            await message.reply_text("❌ 发送失败,请重试")
        
        # 清理临时文件
        os.remove(file_path)
        
    except Exception as e:
        await message.reply_text(f"处理失败: {str(e)}")
        print(f"错误: {e}")

def main():
    # 替换为你的 Bot Token
    with open('config.json', 'r') as f:
        config = json.load(f)
    BOT_TOKEN = config['telegram_token']
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 添加文档处理器
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.GROUPS,
        handle_document
    ))
    
    print("Bot 已启动...")
    app.run_polling()

if __name__ == '__main__':
    main()
