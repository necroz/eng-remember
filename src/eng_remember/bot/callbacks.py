"""Typed callback payloads.

Inline button payloads are built through these factories rather than by string
concatenation, so a renamed field is a type error instead of a silent mismatch
at runtime. All payloads stay far below Telegram's 64-byte limit.
"""

from __future__ import annotations

from typing import Literal

from aiogram.filters.callback_data import CallbackData

__all__ = [
    "CardEditAction",
    "CardEditCB",
    "CardEditStepCB",
    "ClarifyAction",
    "ClarifyCB",
    "DeckAction",
    "DeckCB",
    "FileImportAction",
    "FileImportCB",
    "ImportAction",
    "ImportCB",
    "LeechAction",
    "LeechCB",
    "LevelAction",
    "LevelCB",
    "PauseAction",
    "PauseCB",
    "PracticeAction",
    "PracticeCB",
    "ReviewAction",
    "ReviewCB",
]

ReviewAction = Literal["dont_know", "hint"]
ImportAction = Literal["confirm", "cancel", "more"]
ClarifyAction = Literal["option", "skip"]
LeechAction = Literal["mnemonic", "example", "pause", "leave"]
PauseAction = Literal["resume"]
PracticeAction = Literal["start", "answer"]
FileImportAction = Literal["confirm", "cancel"]
LevelAction = Literal["set", "reset"]
DeckAction = Literal["rn", "dl", "ok", "dc", "rc"]
CardEditAction = Literal["skip", "finish", "cancel"]


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


class PauseCB(CallbackData, prefix="ps"):
    """Returning a paused word to rotation from the /paused list."""

    action: PauseAction
    card_id: int = 0


class PracticeCB(CallbackData, prefix="pr"):
    """A quiz answer, or the start offer on an empty review screen.

    The session token is what makes stale buttons safe: two sessions number
    their questions identically, so without it an old question-2 button would
    be accepted as the current question 2.
    """

    action: PracticeAction
    token: int = 0
    question: int = 0
    option: int = 0


class FileImportCB(CallbackData, prefix="fi"):
    """Confirmation of a CSV import waiting in FSM storage."""

    action: FileImportAction


class LevelCB(CallbackData, prefix="lv"):
    """Choosing the learner's CEFR level from the /level menu."""

    action: LevelAction
    level: str = ""


class DeckCB(CallbackData, prefix="dk"):
    """A row of the /decks list or the decision it asked for.

    The deck id travels in the payload, so a button left in the chat history
    addresses the deck it was drawn for. A deck that has been deleted since then
    is reported instead of acting on whatever now answers to that id.

    ``rn`` starts a rename, ``dl`` asks whether to delete, ``ok`` deletes after
    that question, ``dc`` abandons the deletion and ``rc`` the rename — the two
    cancellations stay apart because they abandon different flows.
    """

    action: DeckAction
    deck_id: int = 0


class CardEditCB(CallbackData, prefix="ce"):
    """The «Правка» button on a graded card.

    The card id travels in the payload: the result message stays in the chat
    history, and editing must address the card that was answered rather than
    whatever the review is showing by the time it is tapped.
    """

    card_id: int


class CardEditStepCB(CallbackData, prefix="cx"):
    """One step of the card-edit dialogue.

    A prefix of its own, because the entry button carries nothing but the card:
    ``ce:<card_id>`` is what the learner's message shows. ``skip`` advances to
    the next question without changing the field, ``finish`` saves what has been
    typed so far, and ``cancel`` abandons the edit.
    """

    action: CardEditAction
    card_id: int = 0
