"""Keyboards.

The main menu is a reply keyboard because it is the fastest path on mobile; the
per-card actions are inline, because they belong to one message and must not
linger as a sticky button.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from eng_remember.bot import texts
from eng_remember.bot.callbacks import ClarifyCB, ImportCB, LeechCB, ReviewCB

__all__ = [
    "clarification",
    "import_confirm",
    "leech_menu",
    "main_menu",
    "review_prompt",
]


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=texts.BTN_ADD), KeyboardButton(text=texts.BTN_REVIEW)],
        [KeyboardButton(text=texts.BTN_DECKS), KeyboardButton(text=texts.BTN_STATS)],
        [KeyboardButton(text=texts.BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Слово или фраза — разберу сам",
    )


def import_confirm(count: int, *, offer_more: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Сохранить ({count})", callback_data=ImportCB(action="confirm"))
    if offer_more:
        builder.button(text="🔄 Ещё слова", callback_data=ImportCB(action="more"))
    builder.button(text="❌ Отмена", callback_data=ImportCB(action="cancel"))
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
