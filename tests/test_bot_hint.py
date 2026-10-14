"""The context hint on the review front."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.domain.enums import ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.factories import make_callback_update, make_message_update


async def send(dispatcher: Dispatcher, bot: Bot, text: str, **kwargs: object) -> None:
    await dispatcher.feed_update(bot, make_message_update(text, **kwargs))  # type: ignore[arg-type]


async def tap(dispatcher: Dispatcher, bot: Bot, data: str, **kwargs: object) -> None:
    await dispatcher.feed_update(bot, make_callback_update(data, **kwargs))  # type: ignore[arg-type]


def button_with(recording: RecordingSession, needle: str) -> str:
    matches = [data for data in recording.last_button_data() if needle in data]
    assert matches, f"no button matching {needle!r} among {recording.last_button_data()}"
    return matches[0]


def result_text(recording: RecordingSession) -> str:
    matches = [text for text in recording.sent_texts() if "Дальше через" in text]
    assert matches, recording.sent_texts()
    return matches[-1]


def last_callback_answer(recording: RecordingSession) -> AnswerCallbackQuery:
    answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
    assert answers, recording.requests
    return answers[-1]


async def seed_due_card(
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
    draft: CardDraft,
) -> Card:
    card = await services.card_service.add_draft(user, draft)
    await services.commit()
    await make_due(session_factory, draft.term)
    return card


async def test_the_hint_button_appears_only_when_there_is_an_example(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_due_card(
        services,
        user,
        session_factory,
        CardDraft(term="run", translation="бежать", examples=("He runs every morning.",)),
    )

    await send(dispatcher, bot, "/review")

    assert any("rv:hint" in data for data in recording.last_button_data())


async def test_a_card_without_examples_gets_no_hint_button(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_due_card(
        services, user, session_factory, CardDraft(term="run", translation="бежать")
    )

    await send(dispatcher, bot, "/review")

    assert not any("rv:hint" in data for data in recording.last_button_data())


async def test_the_hint_shows_a_cloze_and_caps_a_fast_answer_at_good(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_due_card(
        services,
        user,
        session_factory,
        CardDraft(term="run", translation="бежать", examples=("He runs every morning.",)),
    )
    await send(dispatcher, bot, "/review", update_id=20)

    await tap(dispatcher, bot, button_with(recording, "rv:hint"), update_id=21)

    assert "He ____ every morning." in recording.last_text()

    await send(dispatcher, bot, "бежать", update_id=22, date=utcnow())

    async with session_factory() as check:
        review = (await check.scalars(select(Review))).one()
        assert review.rating is ReviewRating.GOOD
    assert "💡 подсказка" in result_text(recording)


async def test_a_repeated_hint_tap_is_a_quiet_no_op(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_due_card(
        services,
        user,
        session_factory,
        CardDraft(term="run", translation="бежать", examples=("He runs every morning.",)),
    )
    await send(dispatcher, bot, "/review", update_id=20)
    hint = button_with(recording, "rv:hint")

    await tap(dispatcher, bot, hint, update_id=21)
    first = recording.last_text()
    await tap(dispatcher, bot, hint, update_id=22)

    assert recording.last_text() == first
    assert not last_callback_answer(recording).show_alert


async def test_a_stale_hint_tap_is_rejected_like_the_skip_button(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_due_card(
        services,
        user,
        session_factory,
        CardDraft(term="run", translation="бежать", examples=("He runs every morning.",)),
    )
    await send(dispatcher, bot, "/review", update_id=20)
    hint = button_with(recording, "rv:hint")
    await send(dispatcher, bot, "бежать", update_id=21, date=utcnow())

    await tap(dispatcher, bot, hint, update_id=22)

    assert last_callback_answer(recording).show_alert
