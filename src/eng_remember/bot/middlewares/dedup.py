"""Ignore an identical plain-text message repeated while the first is handled.

Double taps, client retries and impatience should not buy two model calls. The
key is the learner plus whitespace/case-normalised text; commands are exempt so
``/review`` and ``/stats`` keep working when pressed again.

The guard is deliberately narrow. Text typed into a flow that reads typed input
(a review answer, a clarification answer) is never a duplicate: the same text
twice is exactly what repetition looks like. Menu reply buttons are exempt too —
pressing "Статистика" again is a request, not a retry. Button-only states keep
the guard: nothing there consumes a typed message except the capture catch-all,
which would cost another model call. And a key is released as soon as the
handler has dealt with the message without leaving an FSM flow behind, so a
refusal such as ``cannot_parse`` can be retried immediately.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from eng_remember.bot import texts
from eng_remember.bot.states import is_text_input_state

__all__ = ["DuplicateMessageMiddleware"]

log = structlog.get_logger(__name__)

# Reply-keyboard labels rendered by :func:`keyboards.main_menu` — pressing the
# same button twice must run the menu action both times.
_MENU_BUTTONS = frozenset(" ".join(button.split()).casefold() for button in texts.MAIN_MENU_BUTTONS)


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


class DuplicateMessageMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._seen: dict[tuple[int, str], float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = data.get("event_from_user")
        if (
            self._ttl <= 0
            or from_user is None
            or from_user.is_bot
            or not isinstance(event, Message)
        ):
            return await handler(event, data)

        text = event.text
        if not text or text.startswith("/"):
            return await handler(event, data)

        normalised = _normalise(text)
        if normalised in _MENU_BUTTONS or is_text_input_state(data.get("raw_state")):
            # A live text flow owns the next message (an answer is often
            # repeated), and a menu button is a fresh request every time.
            return await handler(event, data)

        key = (from_user.id, normalised)
        now = self._clock()
        self._prune(now)
        seen_at = self._seen.get(key)
        if seen_at is not None and now - seen_at < self._ttl:
            log.info("duplicate_message_ignored", user_id=from_user.id)
            await event.answer(texts.duplicate_message())
            return None
        self._seen.pop(key, None)

        self._seen[key] = now
        try:
            result = await handler(event, data)
        except Exception:
            # A failed request did not become a card; let the learner retry the
            # same text immediately instead of treating it as a duplicate.
            self._seen.pop(key, None)
            raise

        state = data.get("state")
        keeps_flow = state is not None and await state.get_state() is not None
        if not keeps_flow:
            # The handler answered or refused without parking anything in the
            # FSM, so the next identical text is a new attempt, not a duplicate.
            self._seen.pop(key, None)
        return result

    def _prune(self, now: float) -> None:
        """Drop expired keys on every message, so the map cannot grow forever."""
        cutoff = now - self._ttl
        for key, seen_at in list(self._seen.items()):
            if seen_at <= cutoff:
                del self._seen[key]
