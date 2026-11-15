"""A blocked account comes back as soon as Telegram delivers an update.

The reminder loop marks an account blocked when a send fails with "bot was
blocked". Receiving any update from that account is proof the learner unblocked
it and wrote to the bot, so the stale flag is cleared and the update handled
normally instead of being silently dropped.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import ReviewCB
from eng_remember.models.user import User
from tests.conftest import RecordingSession, make_due
from tests.helpers import send, tap


def confirm_button(recording: RecordingSession) -> str:
    matches = [data for data in recording.last_button_data() if "im:confirm" in data]
    assert matches, recording.last_button_data()
    return matches[0]


async def mark_blocked(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """What the reminder loop leaves behind after a forbidden send."""
    async with session_factory() as setup:
        user = (await setup.scalars(select(User))).one()
        user.is_blocked = True
        await setup.commit()


async def is_blocked(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    async with session_factory() as check:
        user = (await check.scalars(select(User))).one()
        return user.is_blocked


class TestUnblockOnContact:
    async def test_blocked_user_is_restored_on_any_message(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/start")
        await mark_blocked(session_factory)
        recording.clear()

        await send(dispatcher, bot, "run - бежать", update_id=10)

        assert recording.sent_texts(), "a restored user must get an answer"
        assert not await is_blocked(session_factory)

    async def test_a_button_press_also_restores_the_account(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/start")
        await mark_blocked(session_factory)
        recording.clear()

        await tap(dispatcher, bot, ReviewCB(action="dont_know", card_id=1).pack())

        assert not await is_blocked(session_factory)

    async def test_the_full_cycle_block_start_then_review(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole way back: a card saved before the block is reviewable again."""
        await send(dispatcher, bot, "ubiquitous — вездесущий", update_id=20)
        await tap(dispatcher, bot, confirm_button(recording), update_id=21)
        await make_due(session_factory, "ubiquitous")
        await mark_blocked(session_factory)

        await send(dispatcher, bot, "/start", update_id=22)
        assert "С возвращением" in recording.last_text()
        recording.clear()

        await send(dispatcher, bot, "/review", update_id=23)

        assert "ubiquitous" in recording.last_text()
        assert not await is_blocked(session_factory)


@pytest.mark.parametrize("command", ["/start", "/review"])
async def test_commands_of_a_blocked_user_are_never_silently_dropped(
    command: str,
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await send(dispatcher, bot, "/start")
    await mark_blocked(session_factory)
    recording.clear()

    await send(dispatcher, bot, command, update_id=30)

    assert recording.sent_texts()
