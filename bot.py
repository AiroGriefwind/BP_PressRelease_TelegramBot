import json

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

import config
from features.fb_url import (
    handle_text,
    on_fb_menu_settings_back,
    on_fb_set_option,
    on_fb_settings_cancel,
    on_fb_settings_cancel_confirm,
    on_fb_settings_confirm,
    on_fb_url_menu,
    on_fb_url_reset,
    on_fb_url_send,
    on_fb_url_settings,
)
from features.logs_ui import (
    on_log_detail,
    on_logs_back,
    on_logs_days,
    on_logs_keyword,
    on_logs_keyword_clear,
    on_logs_mode,
    on_logs_page,
    on_logs_refresh,
    on_menu_logs,
)
from features.help_ui import on_help_back_list, on_help_back_main, on_help_detail, on_menu_help
from features.pr_processing import (
    handle_file,
    handle_mention,
    on_main_refresh,
    on_ask_del_all,
    on_ask_del_one,
    on_back_to_main,
    on_confirm_send,
    on_do_del_all,
    on_do_del_one,
    on_end_session,
    on_menu_delete_mode,
    on_menu_settings,
    on_menu_settings_back,
    on_set_option,
    on_settings_cancel,
    on_settings_cancel_confirm,
    on_settings_confirm,
)


def main():
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    bot_token = cfg["telegram_token"]

    config.apply_runtime_config(cfg)

    app = ApplicationBuilder().token(bot_token).build()

    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"@"), handle_mention))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(on_confirm_send, pattern=r"^confirm_send\|"))
    app.add_handler(CallbackQueryHandler(on_menu_delete_mode, pattern=r"^menu_delete_mode\|"))
    app.add_handler(CallbackQueryHandler(on_ask_del_one, pattern=r"^ask_del_one\|"))
    app.add_handler(CallbackQueryHandler(on_do_del_one, pattern=r"^do_del_one\|"))
    app.add_handler(CallbackQueryHandler(on_ask_del_all, pattern=r"^ask_del_all\|"))
    app.add_handler(CallbackQueryHandler(on_do_del_all, pattern=r"^do_del_all\|"))
    app.add_handler(CallbackQueryHandler(on_back_to_main, pattern=r"^back_to_main\|"))
    app.add_handler(CallbackQueryHandler(on_menu_settings, pattern=r"^menu_settings\|"))
    app.add_handler(CallbackQueryHandler(on_set_option, pattern=r"^set_option\|"))
    app.add_handler(CallbackQueryHandler(on_settings_confirm, pattern=r"^settings_confirm\|"))
    app.add_handler(CallbackQueryHandler(on_settings_cancel, pattern=r"^settings_cancel\|"))
    app.add_handler(
        CallbackQueryHandler(on_settings_cancel_confirm, pattern=r"^settings_cancel_confirm\|")
    )
    app.add_handler(CallbackQueryHandler(on_menu_settings_back, pattern=r"^menu_settings_back\|"))
    app.add_handler(CallbackQueryHandler(on_end_session, pattern=r"^end_session\|"))
    app.add_handler(CallbackQueryHandler(on_main_refresh, pattern=r"^main_refresh\|"))
    app.add_handler(CallbackQueryHandler(on_fb_url_menu, pattern=r"^fb_url_menu\|"))
    app.add_handler(CallbackQueryHandler(on_fb_url_reset, pattern=r"^fb_url_reset\|"))
    app.add_handler(CallbackQueryHandler(on_fb_url_send, pattern=r"^fb_url_send\|"))
    app.add_handler(CallbackQueryHandler(on_fb_url_settings, pattern=r"^fb_url_settings\|"))
    app.add_handler(CallbackQueryHandler(on_fb_set_option, pattern=r"^fb_set_option\|"))
    app.add_handler(CallbackQueryHandler(on_fb_settings_confirm, pattern=r"^fb_settings_confirm\|"))
    app.add_handler(CallbackQueryHandler(on_fb_settings_cancel, pattern=r"^fb_settings_cancel\|"))
    app.add_handler(
        CallbackQueryHandler(
            on_fb_settings_cancel_confirm, pattern=r"^fb_settings_cancel_confirm\|"
        )
    )
    app.add_handler(
        CallbackQueryHandler(on_fb_menu_settings_back, pattern=r"^fb_menu_settings_back\|")
    )
    app.add_handler(CallbackQueryHandler(on_menu_logs, pattern=r"^menu_logs\|"))
    app.add_handler(CallbackQueryHandler(on_menu_help, pattern=r"^menu_help\|"))
    app.add_handler(CallbackQueryHandler(on_help_detail, pattern=r"^help_detail\|"))
    app.add_handler(CallbackQueryHandler(on_help_back_list, pattern=r"^help_back_list\|"))
    app.add_handler(CallbackQueryHandler(on_help_back_main, pattern=r"^help_back_main\|"))
    app.add_handler(CallbackQueryHandler(on_logs_days, pattern=r"^logs_days\|"))
    app.add_handler(CallbackQueryHandler(on_logs_mode, pattern=r"^logs_mode\|"))
    app.add_handler(CallbackQueryHandler(on_logs_keyword, pattern=r"^logs_keyword\|"))
    app.add_handler(CallbackQueryHandler(on_logs_keyword_clear, pattern=r"^logs_keyword_clear\|"))
    app.add_handler(CallbackQueryHandler(on_logs_page, pattern=r"^logs_page\|"))
    app.add_handler(CallbackQueryHandler(on_logs_refresh, pattern=r"^logs_refresh\|"))
    app.add_handler(CallbackQueryHandler(on_log_detail, pattern=r"^log_detail\|"))
    app.add_handler(CallbackQueryHandler(on_logs_back, pattern=r"^logs_back\|"))

    print("Bot 已启动...")
    app.run_polling()


if __name__ == "__main__":
    main()
