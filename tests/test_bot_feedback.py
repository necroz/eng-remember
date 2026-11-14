"""What the bot says when the message is not something it can use.

Silence reads as a broken bot: a photo, a voice note or an answer typed before
the card was on screen must all get a short explanation instead.
"""

from __future__ import annotations

from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, PhotoSize, Update
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.factories import TEST_USER_ID, telegram_user
from tests.test_bot_flow import send


def make_photo_update(*, update_id: int = 40) -> Update:
    """A private-chat message carrying a photo and no text."""
    return Update(
        update_id=update_id,
        message=Message(
            message_id=1,
            date=utcnow(),
            chat=Chat(id=TEST_USER_ID, type="private"),
            from_user=telegram_user(TEST_USER_ID),
            photo=[
                PhotoSize(file_id="photo-1", file_unique_id="photo-unique-1", width=90, height=90)
            ],
        ),
    )


async def test_photo_gets_reply(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    await dispatcher.feed_update(bot, make_photo_update())

    assert "текст и CSV" in recording.last_text()


async def test_early_answer_gets_feedback(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An answer typed before the card appeared is explained, not graded."""
    await make_due(session_factory)
    await send(dispatcher, bot, "/review", update_id=20)
    recording.clear()

    await send(
        dispatcher,
        bot,
        "вездесущий",
        update_id=21,
        date=utcnow() - timedelta(minutes=1),
    )

    assert "раньше карточки" in recording.last_text()
    async with session_factory() as check:
        assert await check.scalar(select(func.count(Review.id))) == 0


async def test_an_empty_answer_gets_feedback(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    saved_card: Card,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_due(session_factory)
    await send(dispatcher, bot, "/review", update_id=22)
    recording.clear()

    await send(dispatcher, bot, "   ", update_id=23, date=utcnow())

    assert "Пустой ответ" in recording.last_text()
    async with session_factory() as check:
        assert await check.scalar(select(func.count(Review.id))) == 0
