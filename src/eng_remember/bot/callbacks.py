"""Typed callback payloads.

Inline button payloads are built through these factories rather than by string
concatenation, so a renamed field is a type error instead of a silent mismatch
at runtime. All payloads stay far below Telegram's 64-byte limit.
"""

from __future__ import annotations

from typing import Literal

from aiogram.filters.callback_data import CallbackData

__all__ = [
    "ClarifyAction",
    "ClarifyCB",
    "ImportAction",
    "ImportCB",
    "LeechAction",
    "LeechCB",
    "ReviewAction",
    "ReviewCB",
]

ReviewAction = Literal["dont_know", "hint"]
ImportAction = Literal["confirm", "cancel", "more"]
ClarifyAction = Literal["option", "skip"]
LeechAction = Literal["mnemonic", "example", "pause", "leave"]


class ReviewCB(CallbackData, prefix="rv"):
    """Everything the review card buttons need.

    Buttons per card: «Не знаю» and, when the card has an example, «Подсказка».
    The card id travels in the payload instead of being kept in FSM storage, so
    a stale tap is detected by the state check rather than acting on whatever
    card happens to be current.
    """

    action: ReviewAction
    card_id: int = 0


class ImportCB(CallbackData, prefix="im"):
    """What to do with the batch waiting in FSM storage.

    ``more`` asks for another round of suggestions from the same topic, which is
    the natural next tap after "these ten are not quite it".
    """

    action: ImportAction


class ClarifyCB(CallbackData, prefix="cl"):
    """An answer to the clarification the bot asked for.

    The option text itself is not in the payload: it stays in FSM storage, so a
    long option cannot overflow the callback limit and a stale tap is detected by
    the index bounds check.
    """

    action: ClarifyAction
    index: int = 0


class LeechCB(CallbackData, prefix="lc"):
    """What to do about a word that keeps failing.

    The card id travels in the payload: the menu message outlives the review
    session, and treating a word must not depend on it still being on screen.
    """

    action: LeechAction
    card_id: int = 0
