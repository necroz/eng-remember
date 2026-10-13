"""Keyboards.

The main menu is a reply keyboard because it is the fastest path on mobile; the
per-card actions are inline, because they belong to one message and must not
linger as a sticky button.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from eng_remember.bot import texts
from eng_remember.bot.callbacks import ClarifyCB, ImportCB, ReviewCB
from eng_remember.domain.enums import ReviewRating
from eng_remember.domain.srs import SchedulePreview
from eng_remember.utils.time import humanize_interval

__all__ = [
    "clarification",
    "import_confirm",
    "main_menu",
    "review_prompt",
    "review_rating",
]

_RATING_ORDER = (ReviewRating.AGAIN, ReviewRating.HARD, ReviewRating.GOOD, ReviewRating.EASY)


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


def review_prompt(card_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👀 Показать ответ", callback_data=ReviewCB(action="show", card_id=card_id))
    return builder.as_markup()


def review_rating(card_id: int, previews: list[SchedulePreview]) -> InlineKeyboardMarkup:
    """Four grades with the interval each one would produce.

    Showing the interval before the answer is what makes the buttons
    comprehensible: "Трудно" is abstract, "Трудно · 2 дн" is a decision.
    """
    by_rating = {preview.rating: preview for preview in previews}
    buttons: list[InlineKeyboardButton] = []
    for rating in _RATING_ORDER:
        preview = by_rating.get(rating)
        label = f"{rating.emoji} {rating.label}"
        if preview is not None:
            label += f" · {humanize_interval(preview.interval_seconds)}"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=ReviewCB(action="grade", card_id=card_id, rating=int(rating)).pack(),
            )
        )

    builder = InlineKeyboardBuilder()
    builder.row(*buttons[:2])
    builder.row(*buttons[2:])
    builder.row(
        InlineKeyboardButton(
            text="⏭ Позже", callback_data=ReviewCB(action="later", card_id=card_id).pack()
        ),
        InlineKeyboardButton(text="⏹ Закончить", callback_data=ReviewCB(action="stop").pack()),
    )
    return builder.as_markup()
