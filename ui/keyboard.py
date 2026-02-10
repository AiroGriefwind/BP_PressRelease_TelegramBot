from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config


def build_settings_keyboard(session_key: str, settings: dict) -> InlineKeyboardMarkup:
    keyboard = []

    for key, options in config.SETTINGS_OPTIONS.items():
        row = []
        for option in options:
            text = option
            if settings.get(key) == option:
                text = f"✅ {option}"
            callback = f"set_option|{session_key}|{key}|{option}"
            row.append(InlineKeyboardButton(text, callback_data=callback))
        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton("确认", callback_data=f"settings_confirm|{session_key}"),
            InlineKeyboardButton("取消", callback_data=f"settings_cancel|{session_key}"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)
