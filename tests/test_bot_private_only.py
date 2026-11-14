"""Only private chats are served.

A bot added to a group would otherwise answer there, and every stranger in that
group would get a user row the moment the bot is mentioned. The dispatcher
filters the chat type out before any middleware opens a session, so a group
update costs nothing.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.models.user import User
from tests.conftest import RecordingSession
from tests.factories import make_callback_update, make_message_update


async def send(
    dispatcher: Dispatcher,
    bot: Bot,
    text: str,
    *,
    chat_type: str = "private",
    update_id: int = 1,
) -> None:
    await dispatcher.feed_update(
        bot,
        make_message_update(text, chat_type=chat_type, update_id=update_id),
    )


async def tap(
    dispatcher: Dispatcher,
    bot: Bot,
    data: str,
    *,
    chat_type: str = "private",
    update_id: int = 2,
) -> None:
    await dispatcher.feed_update(
        bot,
        make_callback_update(data, chat_type=chat_type, update_id=update_id),
    )


async def user_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as check:
        return int(await check.scalar(select(func.count(User.id))) or 0)


class TestPrivateOnly:
    async def test_group_messages_are_ignored(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/start", chat_type="group", update_id=10)

        assert await user_count(session_factory) == 0
        assert recording.sent_texts() == []

    async def test_a_private_update_still_works(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/start", update_id=11)

        assert await user_count(session_factory) == 1
        assert "Привет" in recording.last_text()

    async def test_group_button_presses_are_ignored(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await tap(dispatcher, bot, "im:confirm", chat_type="supergroup", update_id=12)

        assert await user_count(session_factory) == 0
        assert not [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
        assert recording.sent_texts() == []

    async def test_a_channel_post_is_ignored(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "run - бежать", chat_type="channel", update_id=13)

        assert await user_count(session_factory) == 0
        assert recording.sent_texts() == []
