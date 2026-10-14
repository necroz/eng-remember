"""Small helpers for updating messages from callbacks."""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

__all__ = ["callback_chat_id", "edit_message_or_send", "edit_or_send"]

log = structlog.get_logger(__name__)


async def edit_message_or_send(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int | None,
    text: str,
) -> None:
    """Replace a message by id, falling back to a new one.

    The front is remembered by id, not by an open callback, and it can be too
    old to edit by the time the answer is graded — then the result is simply
    sent as a new message instead of being lost.
    """
    if message_id is not None:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
            log.warning("edit_failed", error=str(exc), message_id=message_id)

    await bot.send_message(chat_id, text)


async def edit_or_send(
    query: CallbackQuery,
    text: str,
    *,
    bot: Bot,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Replace the text of the message a button belongs to.

    Two situations are handled rather than raised: the message may be too old to
    edit (or belong to another account), and the new text may be identical to the
    current one — both are normal when a user taps the same button twice.
    """
    message = query.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
            log.warning("edit_failed", error=str(exc), message_id=message.message_id)

    await bot.send_message(callback_chat_id(query), text, reply_markup=reply_markup)


def callback_chat_id(query: CallbackQuery) -> int:
    """The chat to reply in.

    The bot only serves private chats, where the chat id equals the user id, and
    aiogram models ``from_user`` as required — so this needs no fallback.
    """
    return query.from_user.id
