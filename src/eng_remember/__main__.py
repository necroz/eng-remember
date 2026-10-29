"""Process entry point: ``python -m eng_remember`` / ``eng-remember``."""

from __future__ import annotations

import asyncio
import contextlib
import sys

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from eng_remember.bot import texts
from eng_remember.bot.dispatcher import build_dispatcher
from eng_remember.bot.notifier import BotNotifier
from eng_remember.config import Settings, get_settings, require_bot_token
from eng_remember.db.engine import SessionFactory, create_engine, create_session_factory
from eng_remember.errors import ConfigurationError
from eng_remember.logging_setup import configure_logging
from eng_remember.services.enrichment_worker import enrichment_worker
from eng_remember.services.llm.budget import BudgetAlert, global_budget
from eng_remember.services.reminders import build_reminder_service

log = structlog.get_logger(__name__)

COMMANDS = [
    BotCommand(command="review", description="Повторить слова"),
    BotCommand(command="practice", description="Тренировка"),
    BotCommand(command="add", description="Добавить слова"),
    BotCommand(command="decks", description="Колоды"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="paused", description="Слова на паузе"),
    BotCommand(command="export", description="Выгрузить слова в CSV"),
    BotCommand(command="import", description="Загрузить слова из CSV"),
    BotCommand(command="limit", description="Сколько новых слов в день"),
    BotCommand(command="level", description="Мой уровень изучаемого языка"),
    BotCommand(command="tz", description="Часовой пояс"),
    BotCommand(command="remind", description="Напоминания"),
    BotCommand(command="cancel", description="Отменить действие"),
]


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    dispatcher = build_dispatcher(settings, session_factory)

    bot = Bot(
        token=require_bot_token(settings),
        default=DefaultBotProperties(parse_mode=ParseMode(settings.bot.parse_mode)),
    )

    async def notify_budget(alert: BudgetAlert) -> None:
        chat_id = settings.bot.admin_chat_id
        if chat_id is None:
            return
        await bot.send_message(
            chat_id,
            texts.budget_alert(alert.level, alert.used, alert.limit),
        )

    global_budget.set_notifier(notify_budget)

    log.info(
        "bot_starting",
        llm_enabled=settings.llm.is_configured,
        llm_model=settings.llm.model if settings.llm.is_configured else None,
        database=settings.database_url.split("@")[-1],
        reminders=settings.reminders.enabled,
        tasks_concurrency_limit=settings.bot.tasks_concurrency_limit,
    )

    stopping = asyncio.Event()
    reminders = (
        asyncio.create_task(
            reminder_loop(
                bot=bot,
                session_factory=session_factory,
                settings=settings,
                stopping=stopping,
            ),
            name="reminder-loop",
        )
        if settings.reminders.enabled
        else None
    )
    # Cards earn their examples in the background; with no model there is
    # nothing to generate, so the worker is not started at all.
    enrichment = (
        asyncio.create_task(
            enrichment_worker(session_factory, settings, stopping),
            name="enrichment-worker",
        )
        if settings.llm.is_configured
        else None
    )

    try:
        await bot.set_my_commands(COMMANDS)
        if settings.bot.drop_pending_updates:
            await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            tasks_concurrency_limit=settings.bot.tasks_concurrency_limit,
        )
    finally:
        stopping.set()
        for task in (reminders, enrichment):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await bot.session.close()
        await engine.dispose()
        log.info("bot_stopped")


async def reminder_loop(
    *,
    bot: Bot,
    session_factory: SessionFactory,
    settings: Settings,
    stopping: asyncio.Event,
) -> None:
    """Send reminders every ``REMINDERS__TICK_SECONDS`` until asked to stop.

    The first tick runs immediately, which is what gives the catch-up behaviour:
    a bot that was down at the learner's 08:00 still sends the morning reminder
    when it comes up at 09:00, and nothing is sent at 03:00 because a timer
    drifted.

    Each tick gets its own session and commits at the end, so a long-running
    process never accumulates one huge transaction, and a crash mid-tick leaves
    the already-sent watermarks in place rather than repeating them.
    """
    notifier = BotNotifier(bot)
    interval = settings.reminders.tick_seconds

    while not stopping.is_set():
        try:
            async with session_factory() as session:
                service = build_reminder_service(session, settings, notifier)
                await service.run()
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failing tick must not kill the loop: the next one may well work,
            # and reminders are not worth taking the bot down for.
            log.exception("reminder_tick_crashed")

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval)


def run() -> None:
    """Synchronous wrapper used by the console script."""
    try:
        with contextlib.suppress(KeyboardInterrupt, SystemExit):
            asyncio.run(main())
    except ConfigurationError as exc:
        # A missing token is a setup mistake, not a crash: print the fix rather
        # than a traceback through the event loop.
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    run()
