"""Runtime compatibility guard for the aiogram 3 Telegram bot.

aiogram's Message.edit_text() accepts InlineKeyboardMarkup, not
ReplyKeyboardMarkup. Older bot handler paths in this project sometimes passed
the persistent reply keyboard to edit_text(), which raises a Pydantic
ValidationError and breaks the callback handler. This startup hook safely
strips ReplyKeyboardMarkup from edit_text() calls before the application is
loaded, while leaving normal message.answer(..., reply_markup=menu()) behavior
unchanged.
"""

try:
    from aiogram.types import Message, ReplyKeyboardMarkup

    _original_message_edit_text = Message.edit_text

    async def _safe_edit_text(self, text=None, **kwargs):
        reply_markup = kwargs.get("reply_markup")
        if isinstance(reply_markup, ReplyKeyboardMarkup):
            kwargs.pop("reply_markup", None)
        return await _original_message_edit_text(self, text=text, **kwargs)

    Message.edit_text = _safe_edit_text
except Exception as exc:
    print(f"[Voroa] aiogram edit_text compatibility hook unavailable: {type(exc).__name__}: {exc}", flush=True)
