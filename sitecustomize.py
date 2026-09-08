"""Runtime compatibility guard for the aiogram 3 Telegram bot.

aiogram's Message.edit_text() accepts InlineKeyboardMarkup, not
ReplyKeyboardMarkup. This module also provides a safe fallback for messages
that Telegram refuses to edit: a fresh message is sent and future edits of the
original Message object are transparently redirected to that fresh message.
Normal message.answer(..., reply_markup=menu()) behavior is unchanged.
"""

try:
    from aiogram.types import Message, ReplyKeyboardMarkup

    _original_message_edit_text = Message.edit_text
    _replacement_messages = {}

    async def _safe_edit_text(self, text=None, **kwargs):
        reply_markup = kwargs.get("reply_markup")
        if isinstance(reply_markup, ReplyKeyboardMarkup):
            kwargs.pop("reply_markup", None)

        # Once Telegram refuses to edit an original message, redirect all
        # subsequent progress/status edits to the fresh replacement message.
        target = _replacement_messages.get(id(self), self)
        try:
            return await _original_message_edit_text(target, text=text, **kwargs)
        except Exception as original_exc:
            try:
                replacement = await target.answer(
                    text or "",
                    reply_markup=reply_markup,
                )
                _replacement_messages[id(self)] = replacement
                # Keep memory bounded during very long-running workers.
                if len(_replacement_messages) > 1024:
                    oldest = next(iter(_replacement_messages))
                    _replacement_messages.pop(oldest, None)
                print(
                    f"[Voroa] Message edit fallback created a new status message: "
                    f"{type(original_exc).__name__}: {original_exc}",
                    flush=True,
                )
                return replacement
            except Exception:
                raise original_exc

    Message.edit_text = _safe_edit_text
except Exception as exc:
    print(
        f"[Voroa] aiogram edit_text compatibility hook unavailable: "
        f"{type(exc).__name__}: {exc}",
        flush=True,
    )
