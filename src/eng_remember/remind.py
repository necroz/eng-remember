"""One reminder tick as a command, for operators who prefer cron.

``python -m eng_remember.remind`` — send whatever is due right now and exit.

The bot container runs this same pass on a timer; this entry point exists for the
deployments that would rather keep scheduling outside the application, and for
answering "why did/didn't I get a reminder?" by running it by hand.

Reminders are idempotent per day, so running this next to the in-process loop is
safe but pointless — one of the two should be switched off. With several bot
replicas, switch the loop off and drive this from cron instead: otherwise every
replica would send the same reminder.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# The prune helper lives next to the loop so both entry points share one rule for
# the UTC-day boundary. Importing it does not start anything: ``__main__`` runs
# only under its own guard.
from eng_remember.__main__ import prune_llm_calls_if_due
from eng_remember.bot.notifier import BotNotifier
from eng_remember.config import get_settings, require_bot_token
from eng_remember.db.engine import create_engine, create_session_factory
from eng_remember.errors import ConfigurationError
from eng_remember.logging_setup import configure_logging
from eng_remember.services.reminders import build_reminder_service

log = structlog.get_logger(__name__)


async def main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    bot = Bot(
        token=require_bot_token(settings),
        default=DefaultBotProperties(parse_mode=ParseMode(settings.bot.parse_mode)),
    )

    # No LLM transport here: a reminder tick does not call the model, and a
    # client that nobody uses would only need closing.
    try:
        # The documented multi-replica deployment runs with the in-process loop
        # switched off, so this pass is the only place the daily audit prune can
        # happen. A one-shot process has no memory of the previous run, so it
        # checks the day afresh on every invocation; the delete is idempotent
        # and indexed, so a no-op costs one query.
        await prune_llm_calls_if_due(session_factory, settings)

        async with session_factory() as session:
            service = build_reminder_service(session, settings, BotNotifier(bot))
            # The service commits each delivery itself; there is no batch left
            # to flush here.
            outcome = await service.run()
    finally:
        # The engine must be disposed even if closing the bot session fails.
        try:
            await bot.session.close()
        finally:
            await engine.dispose()

    print(
        f"candidates={outcome.candidates} sent={outcome.sent_count} "
        f"unreachable={len(outcome.unreachable)} failed={len(outcome.failed)}"
    )
    return 0


def run() -> None:
    try:
        raise SystemExit(asyncio.run(main()))
    except ConfigurationError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    run()
