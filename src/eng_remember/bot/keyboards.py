"""Keyboards.

The main menu is a reply keyboard because it is the fastest path on mobile; the
per-card actions are inline, because they belong to one message and must not
linger as a sticky button.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from eng_remember.bot import texts
from eng_remember.bot.callbacks import (
    CardEditCB,
    CardEditStepCB,
    ClarifyCB,
    DeckCB,
    FileImportCB,
    ImportCB,
    LeechCB,
    LevelCB,
    PauseCB,
    PracticeCB,
    ReviewCB,
)
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck

__all__ = [
    "card_edit",
    "card_edit_step",
    "clarification",
    "deck_delete_confirm",
    "deck_manager",
    "deck_rename_cancel",
    "empty_keyboard",
    "file_import_confirm",
    "import_confirm",
    "leech_menu",
    "level_menu",
    "main_menu",
    "paused_cards",
    "practice_labels",
    "practice_offer",
    "practice_options",
    "review_prompt",
]

# Telegram caps a button's text at 64 bytes, and a label is measured in bytes:
# two Cyrillic characters take four. 60 leaves room for the numbering suffix.
OPTION_BYTE_LIMIT = 60
_ELLIPSIS = "…"


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=texts.BTN_ADD), KeyboardButton(text=texts.BTN_REVIEW)],
        [KeyboardButton(text=texts.BTN_PRACTICE), KeyboardButton(text=texts.BTN_DECKS)],
        [KeyboardButton(text=texts.BTN_STATS), KeyboardButton(text=texts.BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Слово или фраза — разберу сам",
    )


def import_confirm(count: int, *, token: str, offer_more: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Сохранить ({count})", callback_data=ImportCB(action="confirm", token=token)
    )
    if offer_more:
        builder.button(text="🔄 Ещё слова", callback_data=ImportCB(action="more", token=token))
    builder.button(text="❌ Отмена", callback_data=ImportCB(action="cancel", token=token))
    builder.adjust(1)
    return builder.as_markup()


def clarification(options: list[str]) -> InlineKeyboardMarkup:
    """Answer buttons for a question the model asked.

    The escape hatch is always last and always present: forcing a choice is a
    worse outcome than a slightly vaguer suggestion, and the model does not
    always offer a usable option.
    """
    builder = InlineKeyboardBuilder()
    for index, option in enumerate(options):
        builder.button(text=option, callback_data=ClarifyCB(action="option", index=index))
    builder.button(text="🤷 Реши сам", callback_data=ClarifyCB(action="skip"))
    builder.adjust(1)
    return builder.as_markup()


def file_import_confirm(*, token: str) -> InlineKeyboardMarkup:
    """Confirmation of a CSV batch: exactly the same yes/no shape as capture."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Сохранить", callback_data=FileImportCB(action="confirm", token=token))
    builder.button(text="❌ Отмена", callback_data=FileImportCB(action="cancel", token=token))
    builder.adjust(1)
    return builder.as_markup()


def empty_keyboard() -> InlineKeyboardMarkup:
    """An explicit empty markup: an edit without one keeps the old buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[])


def level_menu(current: str | None) -> InlineKeyboardMarkup:
    """All six CEFR levels, with the current one marked, plus a reset."""
    builder = InlineKeyboardBuilder()
    for level in texts.CEFR_LEVELS:
        mark = "• " if level == current else ""
        builder.button(text=f"{mark}{level}", callback_data=LevelCB(action="set", level=level))
    builder.button(text="Сбросить", callback_data=LevelCB(action="reset"))
    builder.adjust(3, 3, 1)
    return builder.as_markup()


def review_prompt(card_id: int, *, hint: bool = False) -> InlineKeyboardMarkup:
    """The affordances on the front: context in one tap, giving up honestly in one.

    The answer itself is typed, not tapped, so a rating button would invite
    grading without the recall the flow exists for. The hint only appears when
    the card actually has an example to show.
    """
    builder = InlineKeyboardBuilder()
    if hint:
        builder.button(
            text="💡 Подсказка",
            callback_data=ReviewCB(action="hint", card_id=card_id),
        )
    builder.button(
        text="🤷 Не знаю",
        callback_data=ReviewCB(action="dont_know", card_id=card_id),
    )
    builder.adjust(2 if hint else 1)
    return builder.as_markup()


def leech_menu(card_id: int) -> InlineKeyboardMarkup:
    """What to try about a word that keeps failing.

    Every tap on this menu ends it: the message is edited into the outcome, so
    the same card cannot collect three mnemonics from one offer.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🧠 Мнемоника",
        callback_data=LeechCB(action="mnemonic", card_id=card_id),
    )
    builder.button(
        text="✍️ Свежий пример",
        callback_data=LeechCB(action="example", card_id=card_id),
    )
    builder.button(
        text="⏸ Пауза",
        callback_data=LeechCB(action="pause", card_id=card_id),
    )
    builder.button(
        text="✅ Оставить",
        callback_data=LeechCB(action="leave", card_id=card_id),
    )
    builder.adjust(2, 2)
    return builder.as_markup()


def deck_manager(decks: Sequence[Deck]) -> InlineKeyboardMarkup:
    """One row per deck: rename on the left, delete on the right.

    The same cap as the list itself: a button for a deck the message does not
    show would be a row the learner cannot match to what it does.
    """
    builder = InlineKeyboardBuilder()
    for deck in list(decks)[: texts.PREVIEW_DECKS]:
        buttons = [
            InlineKeyboardButton(
                text=_truncate_utf8(f"✏️ {deck.name}", OPTION_BYTE_LIMIT),
                callback_data=DeckCB(action="rn", deck_id=deck.id).pack(),
            )
        ]
        if not deck.is_default:
            # The default deck cannot be deleted; a button that can only refuse
            # is a dead end, so it is not drawn at all.
            buttons.append(
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=DeckCB(action="dl", deck_id=deck.id).pack(),
                )
            )
        builder.row(*buttons)
    return builder.as_markup()


def deck_delete_confirm(deck_id: int) -> InlineKeyboardMarkup:
    """Deleting a deck takes its words with it: one deliberate tap, or none."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить", callback_data=DeckCB(action="ok", deck_id=deck_id))
    builder.button(text="❌ Отмена", callback_data=DeckCB(action="dc", deck_id=deck_id))
    builder.adjust(1)
    return builder.as_markup()


def deck_rename_cancel(deck_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=DeckCB(action="rc", deck_id=deck_id))
    return builder.as_markup()


def card_edit(card_id: int) -> InlineKeyboardMarkup:
    """The single action a graded card offers: fix what the answer revealed."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Правка", callback_data=CardEditCB(card_id=card_id))
    return builder.as_markup()


def card_edit_step(card_id: int) -> InlineKeyboardMarkup:
    """Skip, save now, or leave: a step must never be a dead end."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⏭ Пропустить", callback_data=CardEditStepCB(action="skip", card_id=card_id)
    )
    builder.button(text="✅ Готово", callback_data=CardEditStepCB(action="finish", card_id=card_id))
    builder.button(text="✖️ Отмена", callback_data=CardEditStepCB(action="cancel", card_id=card_id))
    builder.adjust(2, 1)
    return builder.as_markup()


def paused_cards(cards: Sequence[Card]) -> InlineKeyboardMarkup:
    """One button per paused word, labelled with the word itself."""
    builder = InlineKeyboardBuilder()
    for card in cards:
        builder.button(
            text=f"▶️ {card.term[:26]}",
            callback_data=PauseCB(action="resume", card_id=card.id),
        )
    builder.adjust(1)
    return builder.as_markup()


def practice_offer() -> InlineKeyboardMarkup:
    """The invitation on a review screen with nothing to do."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🏋️ Потренироваться", callback_data=PracticeCB(action="start"))
    return builder.as_markup()


def practice_options(options: Sequence[str], *, token: int, question: int) -> InlineKeyboardMarkup:
    """One button per translation; the payload carries indices, not text."""
    builder = InlineKeyboardBuilder()
    for index, label in enumerate(practice_labels(options)):
        builder.button(
            text=label,
            callback_data=PracticeCB(action="answer", token=token, question=question, option=index),
        )
    builder.adjust(1)
    return builder.as_markup()


def practice_labels(options: Sequence[str]) -> list[str]:
    """Button labels that fit Telegram's byte cap and stay distinguishable.

    Two translations that share a long prefix are cut to the same text, which
    would read as one answer offered twice. A repeated label (including one that
    happens to equal an already-numbered label) gets a number attached, on top
    of the same cut, so every final label is unique within the budget.
    """
    labels = [_truncate_utf8(option, OPTION_BYTE_LIMIT) for option in options]
    used: set[str] = set()
    result: list[str] = []
    for option, label in zip(options, labels, strict=True):
        candidate = label
        counter = 1
        while candidate in used:
            counter += 1
            suffix = f" ({counter})"
            budget = OPTION_BYTE_LIMIT - len(suffix.encode("utf-8"))
            candidate = _truncate_utf8(option, budget) + suffix
        used.add(candidate)
        result.append(candidate)
    return result


def _truncate_utf8(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes, never inside a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    ellipsis_bytes = len(_ELLIPSIS.encode("utf-8"))
    if limit < ellipsis_bytes:
        # No room for the ellipsis itself: cut to the raw budget, no marker.
        return encoded[:limit].decode("utf-8", errors="ignore")
    room = limit - ellipsis_bytes
    cut = encoded[:room].decode("utf-8", errors="ignore").rstrip()
    return cut + _ELLIPSIS
