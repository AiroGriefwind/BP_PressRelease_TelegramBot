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

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackQueryHandler, Application
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler


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
    # 兼容 Message 和 CallbackQuery（用于“完成”按钮返回主菜单）
    if update.message:
        message = update.message
        user_id = message.from_user.id
        chat_id = message.chat.id
    else:
        query = update.callback_query
        message = query.message
        user_id = query.from_user.id
        chat_id = message.chat.id  # CallbackQuery 的 message 也有 chat 对象

    session_key = f"{chat_id}_{user_id}"
    files = user_sessions.get(session_key, [])

    # 仅展示文件名列表（纯文本）
    file_names = [name for _, name in files]
    attach_list = "\n".join(file_names) if file_names else "暂无附件"

    # 主菜单按钮：确认发送 | 进入删除模式
    buttons = [[
        InlineKeyboardButton("确认", callback_data=f"confirm_send|{session_key}"),
        InlineKeyboardButton("删除", callback_data=f"menu_delete_mode|{session_key}")
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)

    ui_msg = f"附件列表：\n{attach_list}"

    # 如果是回调（点击“完成”返回），用 edit_text；如果是新消息，用 reply_text
    if update.callback_query:
        await message.edit_text(ui_msg, reply_markup=reply_markup)
    else:
        await message.reply_text(ui_msg, reply_markup=reply_markup)



# 删除模式菜单逻辑 (列出所有文件带X)
async def on_menu_delete_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    files = user_sessions.get(session_key, [])

    # 构建文件按钮列表，每个文件一行，格式：[ ❌ 文件名  ]
    keyboard = []
    for index, (_, filename) in enumerate(files):
        # 显示名可选：太长时截断一下，避免把按钮撑太宽
        display_name = filename
        max_len = 40
        if len(display_name) > max_len:
            display_name = display_name[:max_len - 1] + "…"

        # 把红叉放到前面：❌ filename
        btn_text = f"❌ {display_name}"
        keyboard.append([
            InlineKeyboardButton(
                btn_text,
                callback_data=f"ask_del_one|{session_key}|{index}"
            )
        ])

    # 底部功能键
    keyboard.append([
        InlineKeyboardButton("🗑️ 全部删除", callback_data=f"ask_del_all|{session_key}"),
        InlineKeyboardButton("✅ 完成", callback_data=f"back_to_main|{session_key}")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    # 如果没有文件，提示文字稍微变一下
    msg_text = "点击红色 X 删除特定附件：" if files else "暂无附件可删除。"
    
    await query.edit_message_text(msg_text, reply_markup=reply_markup)

async def on_ask_del_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, index_str = query.data.split('|')
    index = int(index_str)
    files = user_sessions.get(session_key, [])

    if index >= len(files):
        await query.edit_message_text("⚠️ 文件不存在或已被删除。", reply_markup=None)
        # 这里可以加个逻辑自动跳回菜单，或者让用户重新发令
        return

    target_file_name = files[index][1]

    # 确认菜单
    buttons = [
        [
            InlineKeyboardButton("是，删除", callback_data=f"do_del_one|{session_key}|{index}"),
            InlineKeyboardButton("否，返回", callback_data=f"menu_delete_mode|{session_key}")
        ]
    ]
    await query.edit_message_text(f"确定要删除 {target_file_name} 吗？", reply_markup=InlineKeyboardMarkup(buttons))

# 单个文件删除：确认与执行
async def on_do_del_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("已删除")
    _, session_key, index_str = query.data.split('|')
    index = int(index_str)
    
    files = user_sessions.get(session_key, [])
    if index < len(files):
        # 删除物理文件
        file_path = files[index][0]
        try:
            os.remove(file_path)
        except Exception:
            pass
        # 从列表中移除
        files.pop(index)
        user_sessions[session_key] = files

    # 删除后，直接刷新回“删除模式菜单”
    await on_menu_delete_mode(update, context)

async def on_ask_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    
    files = user_sessions.get(session_key, [])
    if not files:
         await query.answer("列表已经是空的了", show_alert=True)
         return

    buttons = [
        [
            InlineKeyboardButton("⚠️ 确认全部删除", callback_data=f"do_del_all|{session_key}"),
            InlineKeyboardButton("取消", callback_data=f"menu_delete_mode|{session_key}")
        ]
    ]
    await query.edit_message_text("⚠️ 确定要清空所有附件吗？此操作不可逆。", reply_markup=InlineKeyboardMarkup(buttons))

# 全部删除：确认与执行
async def on_do_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_key = query.data.split('|')[1]
    
    files = user_sessions.get(session_key, [])
    for fp, _ in files:
        try: os.remove(fp)
        except: pass
    
    user_sessions[session_key] = []
    
    await query.answer("所有附件已清空")
    await query.edit_message_text("🗑️ 已全部删除。会话结束。")
    # 此时不再显示任何按钮，流程结束

# 返回主菜单
async def on_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # 直接复用 handle_mention 的逻辑来重新渲染主界面
    await handle_mention(update, context)

# “确认”按钮的事件回调
async def on_confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    files = user_sessions.get(session_key, [])

    if not files:
        await query.edit_message_text("⚠️ 没有附件，请先上传文件或图片。")
        return

    # 发邮件
    await query.edit_message_text("正在打包并发送所有附件... 请稍后。")
    sender_info = {"name": "xxx", "username": "xxx", "chat_title": "xxx", "date": "xxx"}  # 填写适用信息
    file_paths, file_names = zip(*files)
    gmail_service = get_gmail_service()
    success = send_email_with_attachments(gmail_service, file_paths, sender_info, file_names)
    if success:
        await query.edit_message_text(f"✅ 文件已发送到 {TARGET_EMAIL}")
    else:
        await query.edit_message_text("❌ 发送失败,请重试")
    # 清理
    for fp in file_paths:
        try: os.remove(fp)
        except: pass
    user_sessions[session_key] = []


def main():
    # 配置文件含 telegram_token
    with open('config.json', 'r') as f:
        config = json.load(f)
    BOT_TOKEN = config['telegram_token']
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 消息处理器
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'@'), handle_mention))

    # Callback 处理器
    # 1. 发送确认
    app.add_handler(CallbackQueryHandler(on_confirm_send, pattern=r"^confirm_send\|"))
    
    # 2. 进入删除模式菜单
    app.add_handler(CallbackQueryHandler(on_menu_delete_mode, pattern=r"^menu_delete_mode\|"))
    
    # 3. 单个文件删除流程
    app.add_handler(CallbackQueryHandler(on_ask_del_one, pattern=r"^ask_del_one\|"))
    app.add_handler(CallbackQueryHandler(on_do_del_one, pattern=r"^do_del_one\|"))
    
    # 4. 全部删除流程
    app.add_handler(CallbackQueryHandler(on_ask_del_all, pattern=r"^ask_del_all\|"))
    app.add_handler(CallbackQueryHandler(on_do_del_all, pattern=r"^do_del_all\|"))
    
    # 5. 返回主菜单
    app.add_handler(CallbackQueryHandler(on_back_to_main, pattern=r"^back_to_main\|"))

    print("Bot 已启动...")
    app.run_polling()

if __name__ == '__main__':
    main()
