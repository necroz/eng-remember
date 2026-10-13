"""Delivering reminders through Telegram.

The only place that knows how a reminder reaches a chat. It also owns the text,
so the reminder service deals in facts — how many cards, how long the streak —
and never in wording.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from eng_remember.bot import keyboards, texts
from eng_remember.domain.reminders import ReminderKind
from eng_remember.errors import RecipientUnreachable
from eng_remember.services.reminders import ReminderPlan

__all__ = ["BotNotifier"]

log = structlog.get_logger(__name__)


class BotNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_reminder(self, plan: ReminderPlan) -> None:
        if plan.kind is ReminderKind.STREAK:
            body = texts.streak_reminder(
                streak_days=plan.streak_days,
                due_now=plan.due_now,
                new_available=plan.new_available,
            )
        else:
            body = texts.queue_reminder(
                due_now=plan.due_now,
                new_available=plan.new_available,
                streak_days=plan.streak_days,
            )

        try:
            await self._bot.send_message(
                plan.user.telegram_id,
                body,
                reply_markup=keyboards.main_menu(),
            )
        except TelegramForbiddenError as exc:
            # The bot was blocked, which is final until the user writes again.
            raise RecipientUnreachable(str(exc)) from exc
        except TelegramBadRequest as exc:
            # "chat not found" and "user is deactivated" land here and are just
            # as final: retrying a deleted chat is pointless.
            raise RecipientUnreachable(str(exc)) from exc
