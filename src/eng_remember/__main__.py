"""Process entry point: ``python -m eng_remember`` / ``eng-remember``."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from datetime import date, datetime, timedelta

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
from eng_remember.repositories.llm_calls import LlmCallRepository
from eng_remember.services.enrichment import BackfillQueue
from eng_remember.services.enrichment_worker import enrichment_worker
from eng_remember.services.llm.budget import BudgetAlert, global_budget
from eng_remember.services.llm.client import LlmTransport
from eng_remember.services.reminders import Notifier, build_reminder_service
from eng_remember.utils.time import ensure_utc, local_date, utcnow

log = structlog.get_logger(__name__)

# One heartbeat an hour at the default ten-minute tick: often enough that a
# monitor notices a wedged process, rare enough not to drown the log.
_HEARTBEAT_EVERY_TICKS = 6

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
    # One connection owner for the whole process: every update's client is a
    # thin view over it, and what a provider rejects is learned only once.
    # The standby route gets its own owner and lessons.
    llm_transport = LlmTransport(settings.llm) if settings.llm.primary_configured else None
    llm_fallback_transport = (
        LlmTransport(settings.llm.fallback_copy()) if settings.llm.fallback_configured else None
    )
    # One backfill queue as well: review handlers put cards in, the worker
    # below takes them out.
    backfill = BackfillQueue()
    dispatcher = build_dispatcher(
        settings,
        session_factory,
        llm_transport=llm_transport,
        llm_fallback_transport=llm_fallback_transport,
        backfill=backfill,
    )

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
            enrichment_worker(
                session_factory,
                settings,
                stopping,
                queue=backfill,
                transport=llm_transport,
                fallback_transport=llm_fallback_transport,
            ),
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
        # Nested finallys so a failure in one close still runs the others: a
        # hung bot session must not leave the HTTP client and the pool open.
        try:
            await bot.session.close()
        finally:
            try:
                if llm_transport is not None:
                    await llm_transport.aclose()
                if llm_fallback_transport is not None:
                    await llm_fallback_transport.aclose()
            finally:
                await engine.dispose()
                log.info("bot_stopped")


async def reminder_loop(
    *,
    bot: Bot,
    session_factory: SessionFactory,
    settings: Settings,
    stopping: asyncio.Event,
    notifier: Notifier | None = None,
) -> None:
    """Send reminders every ``REMINDERS__TICK_SECONDS`` until asked to stop.

    The first tick runs immediately, which is what gives the catch-up behaviour:
    a bot that was down at the learner's 08:00 still sends the morning reminder
    when it comes up at 09:00, and nothing is sent at 03:00 because a timer
    drifted.

    Each tick gets its own session, and the service commits after every learner
    it messages — not once per batch or per tick. A crash or a stop request
    halfway through a large tick therefore keeps the watermarks of everyone
    already delivered, so the next tick skips them instead of messaging them
    twice; at most the one delivery that was in flight repeats.

    ``notifier`` exists for tests and for a caller that already has a delivery
    channel; production omits it and gets a :class:`BotNotifier`.
    """
    delivery = (
        notifier
        if notifier is not None
        else BotNotifier(bot, admin_chat_id=settings.bot.admin_chat_id)
    )
    interval = settings.reminders.tick_seconds
    pruned_on: date | None = None
    ticks = 0

    while not stopping.is_set():
        # Retention is a daily housekeeping job, not a per-tick one: the audit
        # table changes slowly and a delete on every ten-minute tick would be
        # pure overhead. The helper owns the UTC-day boundary so the cron entry
        # point prunes on exactly the same rule.
        pruned_on = await prune_llm_calls_if_due(session_factory, settings, pruned_on)

        started = time.monotonic()
        try:
            async with session_factory() as session:
                service = build_reminder_service(session, settings, delivery)
                run = await service.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failing tick must not kill the loop: the next one may well work,
            # and reminders are not worth taking the bot down for. The operator
            # hears about it in the admin chat, not only in the log stream.
            log.exception("reminder_tick_crashed")
            await _alert_tick_crash(delivery, exc)
        else:
            log.info(
                "reminder_tick_finished",
                duration_ms=int((time.monotonic() - started) * 1000),
                users_processed=run.candidates,
                sent=run.sent_count,
                unreachable=len(run.unreachable),
                failed=len(run.failed),
            )

        ticks += 1
        if ticks % _HEARTBEAT_EVERY_TICKS == 0:
            log.info("bot_heartbeat", tick=ticks, **_pool_stats(session_factory))

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval)


async def _alert_tick_crash(notifier: Notifier, error: Exception) -> None:
    """Put a crashed tick in front of a human, without failing the loop again."""
    try:
        await notifier.send_alert(
            f"Тик напоминаний упал: {type(error).__name__}: {error}. Бот продолжает работу."
        )
    except Exception as exc:
        log.warning("reminder_tick_alert_failed", error=str(exc))


def _pool_stats(session_factory: SessionFactory) -> dict[str, int]:
    """Connection-pool occupancy for the heartbeat, best effort.

    SQLite uses pools that do not implement ``size()``; a heartbeat without the
    numbers is still a heartbeat, so a missing implementation is not an error.
    """
    engine = session_factory.kw.get("bind")
    pool = getattr(engine, "pool", None)
    if pool is None:
        return {}
    try:
        return {"pool_checked_out": pool.checkedout(), "pool_size": pool.size()}
    except NotImplementedError:
        return {}


async def prune_llm_calls(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int | None:
    """Delete LLM audit rows past the retention window.

    Returns the number removed, or ``None`` when retention is disabled. It runs
    in its own session and commits immediately, so the delete never holds the
    reminder tick's transaction open.
    """
    days = settings.llm_call_retention_days
    if days <= 0:
        return None
    moment = ensure_utc(now) or utcnow()
    cutoff = moment - timedelta(days=days)
    async with session_factory() as session:
        removed = await LlmCallRepository(session).delete_older_than(cutoff)
        await session.commit()
    # The cron entry point has no memory of earlier runs and prunes on each
    # invocation; "nothing to delete" is the normal case and must not fill its
    # log with one event every ten minutes.
    if removed:
        log.info("llm_calls_pruned", removed=removed, retention_days=days)
    else:
        log.debug("llm_calls_pruned", removed=0, retention_days=days)
    return removed


async def prune_llm_calls_if_due(
    session_factory: SessionFactory,
    settings: Settings,
    last_run: date | None = None,
    *,
    now: datetime | None = None,
) -> date | None:
    """Run the audit prune unless ``last_run`` says it already ran today.

    Returns the UTC day the prune last ran within this process. Shared by the
    in-process loop and the cron entry point so the two cannot disagree about
    the boundary, and so a deployment that has no loop still gets the cleanup.
    The UTC date — not a monotonic timer — survives a restart and matches the
    timezone the audit column stores. A one-shot caller has no stamp to carry
    between runs and passes ``None``: it prunes on every invocation, which is
    safe because the DELETE is idempotent and indexed.

    A failed cleanup is logged and the stamp is not advanced: the next tick
    retries rather than skipping a whole day. ``CancelledError`` still
    propagates, so shutdown is never delayed by housekeeping.
    """
    today = local_date("UTC", now=now)
    if settings.llm_call_retention_days <= 0 or last_run == today:
        return last_run
    try:
        await prune_llm_calls(session_factory, settings, now=now)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A failed cleanup must not stop reminders; the caller's next turn retries.
        log.warning("llm_calls_prune_failed", error=str(exc))
        return last_run
    return today


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
