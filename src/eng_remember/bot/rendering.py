"""Small helpers for updating messages from callbacks."""

from __future__ import annotations

import re

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

__all__ = [
    "TELEGRAM_TEXT_LIMIT",
    "callback_chat_id",
    "edit_message_or_send",
    "edit_or_send",
    "fit_text",
    "require_state",
]

log = structlog.get_logger(__name__)

# Telegram rejects a message longer than this, so every send goes through
# fit_text rather than trusting the renderer to stay small.
TELEGRAM_TEXT_LIMIT = 4096

_TRUNCATED = "\n… (сообщение сокращено)"
_TAG = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*>")
_ENTITY_TAIL = re.compile(r"&[a-zA-Z#][a-zA-Z0-9]*$")
_VOID_TAGS = frozenset({"br", "hr"})


def fit_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Make a rendered message fit into one Telegram message.

    The cut falls on a line boundary, never inside markup: a tag or an HTML
    entity split in half would turn the rest of the message into visible
    garbage. Tags left open by the cut are closed, so the suffix and everything
    before it stay valid HTML. An extremely long single line is still cut, just
    without the line-boundary courtesy.
    """
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return _TRUNCATED[: max(limit, 0)]

    budget = limit - len(_TRUNCATED)
    boundary = text.rfind("\n", 0, budget)
    end = boundary if boundary > 0 else budget

    while end > 0:
        prefix = text[:end]
        tag_start = _unclosed_tag_start(prefix)
        if tag_start is not None:
            previous = text.rfind("\n", 0, tag_start)
            end = previous if previous > 0 else tag_start
            continue

        entity = _ENTITY_TAIL.search(prefix)
        if entity is not None:
            end = entity.start()
            continue

        closing = _closing_tags(prefix)
        if len(prefix) + len(closing) <= budget:
            return prefix.rstrip() + closing + _TRUNCATED

        # The closing tags must fit too: move the cut to the previous boundary.
        previous = text.rfind("\n", 0, end)
        end = previous if previous > 0 else end - 1

    return _TRUNCATED[:limit]


def _unclosed_tag_start(prefix: str) -> int | None:
    """Where the last raw ``<`` begins when its tag is still open."""
    last_open = prefix.rfind("<")
    if last_open > prefix.rfind(">"):
        return last_open
    return None


def _closing_tags(prefix: str) -> str:
    """Close every tag the cut left open, innermost first."""
    stack: list[str] = []
    for match in _TAG.finditer(prefix):
        closing, name = match.group(1), match.group(2).lower()
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                del stack[stack.index(name) :]
        elif not match.group(0).rstrip().endswith("/>") and name not in _VOID_TAGS:
            stack.append(name)
    return "".join(f"</{name}>" for name in reversed(stack))


async def require_state(query: CallbackQuery, fsm: FSMContext, expected: State) -> bool:
    """Whether the flow that owns this button is still the active one.

    Every inline keyboard outlives the moment it belonged to: a tap on it can
    arrive while another flow is running, and acting on it would clear or
    overwrite that flow. A stale tap is answered here — an unanswered callback
    leaves the spinner turning forever — and the caller simply returns.

    The text is imported locally: ``texts`` imports this module, so a
    module-level import would be a cycle.
    """
    from eng_remember.bot import texts

    if await fsm.get_state() == expected.state:
        return True
    await query.answer(texts.stale_button(), show_alert=True)
    return False


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
    text = fit_text(text)
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
    text = fit_text(text)
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
