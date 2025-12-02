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

# 可选设置项
SETTINGS_OPTIONS = {
    'type': ['全文不改', '只改標題'],
    'priority': ['普通', '緊急'],
    'language': ['中文', '英文']
}

# 默认设置
DEFAULT_SETTINGS = {
    'type': '全文不改',
    'priority': '普通',
    'language': '中文'
}

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

def send_email_with_attachments(service, file_paths, sender_info, file_names, settings):
    message = MIMEMultipart()
    message['to'] = TARGET_EMAIL
    message['subject'] = f"新稿件: " + ', '.join(file_names)
    
    # 新的 body 格式
    body = f"""
来自: {sender_info['name']} (@{sender_info['username']})
群组: {sender_info['chat_title']}
时间: {sender_info['date']}
類型：{settings['type']}
優先度：{settings['priority']}
語言：{settings['language']}
附件: {', '.join(file_names)}
"""
    message.attach(MIMEText(body, 'plain', 'utf-8'))

    # ... (后面的附件逻辑不变)
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
    message = update.message
    user_id = message.from_user.id
    chat_id = message.chat.id
    session_key = f"{chat_id}_{user_id}"

    # 如果是新session，创建完整结构
    if session_key not in user_sessions:
        user_sessions[session_key] = {
            'files': [],
            'settings': DEFAULT_SETTINGS.copy()
        }

    os.makedirs('temp', exist_ok=True)
    file_id, file_name = None, None
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
    elif message.photo:
        photo_file = message.photo[-1]
        file_id = photo_file.file_id
        file_name = f"{file_id}.jpg"

    if file_id and file_name:
        file = await context.bot.get_file(file_id)
        file_path = f"temp/{file_name}"
        await file.download_to_drive(file_path)
        
        # 存储文件
        user_sessions[session_key]['files'].append((file_path, file_name))
        await message.reply_text(f"已添加: {file_name}")

# --- 进入设置菜单 ---
async def on_menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    # 将当前设置存入临时的 user_data，用于“取消”功能
    current_settings = user_sessions[session_key]['settings']
    context.user_data[f'temp_settings_{session_key}'] = current_settings.copy()

    await show_settings_menu(update, context, session_key, current_settings)

# --- 辅助函数：渲染设置菜单 ---
async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, session_key: str, settings: dict):
    query = update.callback_query
    keyboard = []
    
    # 动态生成三行设置按钮
    for key, options in SETTINGS_OPTIONS.items():
        row = []
        for option in options:
            text = option
            # 高亮当前选项
            if settings.get(key) == option:
                text = f"✅ {option}"
            
            # callback_data 包含要修改的键和值
            callback = f"set_option|{session_key}|{key}|{option}"
            row.append(InlineKeyboardButton(text, callback_data=callback))
        keyboard.append(row)

    # 底部确认和取消按钮
    keyboard.append([
        InlineKeyboardButton("确认", callback_data=f"settings_confirm|{session_key}"),
        InlineKeyboardButton("取消", callback_data=f"settings_cancel|{session_key}")
    ])
    
    await query.edit_message_text("请选择需要的选项：", reply_markup=InlineKeyboardMarkup(keyboard))

# --- 点击选项按钮 ---
async def on_set_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, session_key, key, value = query.data.split('|')
    
    # 修改临时设置
    temp_settings = context.user_data[f'temp_settings_{session_key}']
    temp_settings[key] = value

    # 重新渲染菜单以提供反馈
    await show_settings_menu(update, context, session_key, temp_settings)

# --- 点击“确认”保存设置 ---
async def on_settings_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]

    # 将临时设置保存回主 session
    user_sessions[session_key]['settings'] = context.user_data[f'temp_settings_{session_key}'].copy()
    
    # 清理临时数据
    del context.user_data[f'temp_settings_{session_key}']

    # 返回主菜单
    await handle_mention(update, context)

# --- 点击“取消” ---
async def on_settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    
    original_settings = user_sessions[session_key]['settings']
    temp_settings = context.user_data.get(f'temp_settings_{session_key}')

    # 如果设置没变，直接返回
    if original_settings == temp_settings:
        del context.user_data[f'temp_settings_{session_key}']
        await handle_mention(update, context)
    else:
        # 如果变了，弹出确认放弃的提示
        buttons = [[
            InlineKeyboardButton("是，放弃更改", callback_data=f"settings_cancel_confirm|{session_key}"),
            InlineKeyboardButton("否，继续编辑", callback_data=f"menu_settings_back|{session_key}")
        ]]
        await query.edit_message_text("设置已更改，是否放弃并返回？", reply_markup=InlineKeyboardMarkup(buttons))

# --- 确认放弃更改 ---
async def on_settings_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    
    # 清理临时数据，不保存
    del context.user_data[f'temp_settings_{session_key}']
    
    # 返回主菜单
    await handle_mention(update, context)

# --- 从“放弃更改”页面返回设置菜单 ---
async def on_menu_settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    temp_settings = context.user_data[f'temp_settings_{session_key}']
    await show_settings_menu(update, context, session_key, temp_settings)

async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        # ... (和之前一样的代码来获取 session_key)
        message = update.message
        user_id = message.from_user.id
        chat_id = message.chat.id
    else:
        # ... (和之前一样的代码来获取 session_key)
        query = update.callback_query
        message = query.message
        user_id = query.from_user.id
        chat_id = message.chat.id

    session_key = f"{chat_id}_{user_id}"
    
    # 确保session存在
    if session_key not in user_sessions:
        user_sessions[session_key] = {
            'files': [],
            'settings': DEFAULT_SETTINGS.copy()
        }
        
    session_data = user_sessions[session_key]
    files = session_data['files']
    settings = session_data['settings']
    
    file_names = [name for _, name in files]
    attach_list = "\n".join(file_names) if file_names else "暂无附件"

    # 构建带设置的UI消息
    settings_text = (
        f"類型：{settings['type']}\n"
        f"優先度：{settings['priority']}\n"
        f"語言：{settings['language']}"
    )
    ui_msg = f"附件列表：\n{attach_list}\n\n---\n\n{settings_text}"

    # 主菜单按钮：确认 | 删除 | 设置
    buttons = [[
        InlineKeyboardButton("确认", callback_data=f"confirm_send|{session_key}"),
        InlineKeyboardButton("删除", callback_data=f"menu_delete_mode|{session_key}"),
        InlineKeyboardButton("⚙️ 设置", callback_data=f"menu_settings|{session_key}")
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await message.edit_text(ui_msg, reply_markup=reply_markup)
    else:
        await message.reply_text(ui_msg, reply_markup=reply_markup)

# 删除模式菜单逻辑 (列出所有文件带X)
async def on_menu_delete_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

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
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

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
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']

    if index < len(files):
        # 删除物理文件
        file_path = files[index][0]
        try:
            os.remove(file_path)
        except Exception:
            pass
        # 从列表中移除
        files.pop(index)
        user_sessions[session_key]['files'] = files  # 仅更新文件列表

    # 删除后，直接刷新回“删除模式菜单”
    await on_menu_delete_mode(update, context)

async def on_ask_del_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session_key = query.data.split('|')[1]
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']
    
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
    
    session_data = user_sessions.get(session_key, {'files': [], 'settings': DEFAULT_SETTINGS.copy()})
    files = session_data['files']
    
    for fp, _ in files:
        try: os.remove(fp)
        except: pass
    
    user_sessions[session_key]['files'] = []
    
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
    
    session_data = user_sessions.get(session_key)
    if not session_data or not session_data['files']:
        await query.edit_message_text("⚠️ 没有附件，请先上传文件或图片。")
        return

    files = session_data['files']
    settings = session_data['settings']
    message = query.message # 需要用 message 对象获取发件人信息

    await query.edit_message_text("正在打包并发送所有附件... 请稍后。")

    # 构建发件人信息
    sender_info = {
        'name': (message.reply_to_message.from_user.first_name or "") + (f" {message.reply_to_message.from_user.last_name}" if message.reply_to_message.from_user.last_name else ""),
        'username': message.reply_to_message.from_user.username or "unknown",
        'chat_title': message.chat.title or "private",
        'date': message.date.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime('%Y-%m-%d %H:%M:%S')
    }
    
    file_paths, file_names = zip(*files)
    gmail_service = get_gmail_service()

    # 把 settings 传给发送函数
    success = send_email_with_attachments(gmail_service, file_paths, sender_info, file_names, settings)
    
    if success:
        await query.edit_message_text(f"✅ 文件已发送到 {TARGET_EMAIL}")
    else:
        await query.edit_message_text("❌ 发送失败,请重试")
    
    # 清理session和临时文件
    for fp in file_paths:
        try: os.remove(fp)
        except: pass
    del user_sessions[session_key]


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

    # 6. 设置流程
    app.add_handler(CallbackQueryHandler(on_menu_settings, pattern=r"^menu_settings\|"))
    app.add_handler(CallbackQueryHandler(on_set_option, pattern=r"^set_option\|"))
    app.add_handler(CallbackQueryHandler(on_settings_confirm, pattern=r"^settings_confirm\|"))
    app.add_handler(CallbackQueryHandler(on_settings_cancel, pattern=r"^settings_cancel\|"))
    app.add_handler(CallbackQueryHandler(on_settings_cancel_confirm, pattern=r"^settings_cancel_confirm\|"))
    app.add_handler(CallbackQueryHandler(on_menu_settings_back, pattern=r"^menu_settings_back\|"))

    print("Bot 已启动...")
    app.run_polling()

if __name__ == '__main__':
    main()
