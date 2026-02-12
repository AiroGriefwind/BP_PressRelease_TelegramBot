from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config


def build_settings_keyboard(
    session_key: str,
    settings: dict,
    options: dict = None,
    *,
    set_option_prefix: str = "set_option",
    confirm_prefix: str = "settings_confirm",
    cancel_prefix: str = "settings_cancel",
) -> InlineKeyboardMarkup:
    keyboard = []

    option_map = options or config.SETTINGS_OPTIONS
    for key, options in option_map.items():
        row = []
        for option in options:
            text = option
            if settings.get(key) == option:
                text = f"✅ {option}"
            callback = f"{set_option_prefix}|{session_key}|{key}|{option}"
            row.append(InlineKeyboardButton(text, callback_data=callback))
        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton("確認", callback_data=f"{confirm_prefix}|{session_key}"),
            InlineKeyboardButton("取消", callback_data=f"{cancel_prefix}|{session_key}"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)
