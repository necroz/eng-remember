"""User preferences that change scheduling behaviour.

Named ``preferences`` rather than ``settings`` so the module never shadows the
``Settings`` configuration object that handlers receive as a parameter.
"""

from __future__ import annotations

from datetime import time
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import keyboards, rendering, texts
from eng_remember.bot.callbacks import LevelCB
from eng_remember.config import ReminderSettings
from eng_remember.models.user import User
from eng_remember.services.container import Services
from eng_remember.utils.time import humanize_clock, parse_clock

REMIND_USAGE = (
    "<b>Напоминания</b>\n\n"
    "<code>/remind</code> — показать настройки\n"
    "<code>/remind on</code> · <code>/remind off</code> — включить или выключить\n"
    "<code>/remind 8:30</code> — время утреннего напоминания\n"
    "<code>/remind evening 20:00</code> — время вечернего предупреждения\n"
    "<code>/remind evening off</code> — выключить вечернее\n\n"
    "<i>Время указывается в вашем часовом поясе.</i>"
)


def build_router() -> Router:
    """Create the router and register its handlers.

    A factory rather than a module-level singleton: aiogram refuses to
    attach one Router to two Dispatchers, which would make a second
    dispatcher (a test harness, a second bot) impossible.
    """
    router = Router(name="preferences")

    MIN_NEW_CARDS = 0
    MAX_NEW_CARDS = 200

    @router.message(Command("timezone", "tz"))
    async def set_timezone(
        message: Message,
        command: CommandObject,
        user: User,
        services: Services,
    ) -> None:
        argument = (command.args or "").strip()
        if not argument:
            await message.answer(
                f"Часовой пояс: <code>{escape(user.timezone)}</code>\n"
                "Изменить: <code>/tz Europe/Moscow</code>"
            )
            return

        try:
            ZoneInfo(argument)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer(texts.unknown_timezone(argument))
            return

        user.timezone = argument
        await services.commit()
        # The daily new-card budget is measured from the user's midnight, so this
        # value is not cosmetic: it moves where the day boundary falls.
        await message.answer(
            f"Готово, теперь день считается по <code>{escape(argument)}</code>.",
            reply_markup=keyboards.main_menu(),
        )

    @router.message(Command("limit"))
    async def set_new_cards_limit(
        message: Message,
        command: CommandObject,
        user: User,
        services: Services,
    ) -> None:
        argument = (command.args or "").strip()
        if not argument:
            await message.answer(
                f"Новых слов в день: <b>{user.new_cards_per_day}</b>\nИзменить: <code>/limit 30</code>"
            )
            return

        if not argument.isdigit() or not MIN_NEW_CARDS <= int(argument) <= MAX_NEW_CARDS:
            await message.answer(f"Нужно число от {MIN_NEW_CARDS} до {MAX_NEW_CARDS}.")
            return

        user.new_cards_per_day = int(argument)
        await services.commit()
        await message.answer(
            f"Хорошо, не больше {user.new_cards_per_day} новых слов в день.",
            reply_markup=keyboards.main_menu(),
        )

    def _effective(user: User, settings: ReminderSettings) -> tuple[time, time | None]:
        morning = user.reminder_time or settings.default_time
        evening = None
        if user.evening_reminder:
            evening = user.evening_reminder_time or settings.default_streak_time
        return morning, evening

    async def _show_reminders(message: Message, user: User, services: Services) -> None:
        settings = services.settings.reminders
        morning, evening = _effective(user, settings)

        status = "🔔 включены" if user.reminders_enabled else "🔕 выключены"

        lines = [
            "⏰ <b>Напоминания</b>",
            "",
            f"Состояние: <b>{status}</b>",
            f"Утреннее: <code>{humanize_clock(morning)}</code>",
        ]
        if evening is None:
            lines.append("Вечернее: выключено")
        else:
            lines.append(f"Вечернее: <code>{humanize_clock(evening)}</code>")
        lines += [
            "",
            f"<i>Время в вашем поясе: {escape(user.timezone)}.</i>",
            REMIND_USAGE,
        ]
        await message.answer("\n".join(lines), reply_markup=keyboards.main_menu())

    @router.message(Command("remind", "reminders"))
    async def reminders(
        message: Message,
        command: CommandObject,
        user: User,
        services: Services,
    ) -> None:
        argument = (command.args or "").strip().lower()

        if not argument:
            await _show_reminders(message, user, services)
            return

        if argument in {"on", "off"}:
            user.reminders_enabled = argument == "on"
            await services.commit()
            await _show_reminders(message, user, services)
            return

        if argument.startswith("evening"):
            rest = argument.removeprefix("evening").strip()
            if rest in {"on", "off"}:
                user.evening_reminder = rest == "on"
                await services.commit()
                await _show_reminders(message, user, services)
                return
            parsed_evening = parse_clock(rest) if rest else None
            if parsed_evening is None:
                await message.answer(REMIND_USAGE)
                return
            user.evening_reminder = True
            user.evening_reminder_time = parsed_evening
            await services.commit()
            await _show_reminders(message, user, services)
            return

        parsed = parse_clock(argument)
        if parsed is None:
            await message.answer(REMIND_USAGE)
            return
        user.reminder_time = parsed
        await services.commit()
        await _show_reminders(message, user, services)

    @router.message(Command("level"))
    async def set_level(
        message: Message,
        command: CommandObject,
        user: User,
        services: Services,
    ) -> None:
        argument = (command.args or "").strip().upper()
        if not argument:
            await message.answer(
                texts.level_menu(user.level),
                reply_markup=keyboards.level_menu(user.level),
            )
            return
        if argument not in texts.CEFR_LEVELS:
            await message.answer(
                texts.level_invalid(argument),
                reply_markup=keyboards.level_menu(user.level),
            )
            return
        user.level = argument
        await services.commit()
        await message.answer(
            texts.level_set(argument),
            reply_markup=keyboards.level_menu(argument),
        )

    @router.callback_query(LevelCB.filter(F.action == "set"))
    async def set_level_from_button(
        query: CallbackQuery,
        callback_data: LevelCB,
        bot: Bot,
        user: User,
        services: Services,
    ) -> None:
        if callback_data.level not in texts.CEFR_LEVELS:
            await query.answer(texts.stale_button(), show_alert=True)
            return
        user.level = callback_data.level
        await services.commit()
        await query.answer()
        await rendering.edit_or_send(
            query,
            texts.level_set(callback_data.level),
            bot=bot,
            reply_markup=keyboards.level_menu(callback_data.level),
        )

    @router.callback_query(LevelCB.filter(F.action == "reset"))
    async def reset_level(
        query: CallbackQuery,
        bot: Bot,
        user: User,
        services: Services,
    ) -> None:
        user.level = None
        await services.commit()
        await query.answer()
        await rendering.edit_or_send(
            query,
            texts.level_reset(),
            bot=bot,
            reply_markup=keyboards.level_menu(None),
        )

    @router.message(Command("settings"))
    @router.message(F.text == texts.BTN_HELP)
    async def show_settings(message: Message, user: User, services: Services) -> None:
        morning, evening = _effective(user, services.settings.reminders)
        reminder_state = "🔔 включены" if user.reminders_enabled else "🔕 выключены"
        evening_text = "выключено" if evening is None else humanize_clock(evening)
        await message.answer(
            "⚙️ <b>Настройки</b>\n\n"
            f"Часовой пояс: <code>{escape(user.timezone)}</code> — сменить: "
            "<code>/tz Europe/Moscow</code>\n"
            f"Новых слов в день: <b>{user.new_cards_per_day}</b> — сменить: <code>/limit 30</code>\n"
            f"Напоминания: <b>{reminder_state}</b> — "
            f"утро <code>{humanize_clock(morning)}</code>, вечер <code>{evening_text}</code>\n\n"
            "<b>Команды</b>\n"
            "/review — начать повторение\n"
            "/add — добавить слова\n"
            "/decks — колоды\n"
            "/stats — статистика\n"
            "/level — уровень английского\n"
            "/remind — напоминания\n"
            "/cancel — отменить текущее действие",
            reply_markup=keyboards.main_menu(),
        )

    return router
