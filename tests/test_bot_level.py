"""The /level command and the CEFR badge."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import LevelCB
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.factories import ScriptedClient, card_payload, parse_payload
from tests.test_bot_flow import send, tap


def button_data(recording: RecordingSession, needle: str) -> str:
    matches = [data for data in recording.last_button_data() if needle in data]
    assert matches, f"no button matching {needle!r} among {recording.last_button_data()}"
    return matches[0]


async def level_of(session_factory: async_sessionmaker[AsyncSession]) -> str | None:
    async with session_factory() as check:
        user = (await check.scalars(select(User))).one()
        return user.level


async def test_level_without_argument_shows_the_menu(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    user: User,
) -> None:
    await send(dispatcher, bot, "/level", update_id=100)

    assert "не указан" in recording.last_text()
    assert any("lv:set" in data for data in recording.last_button_data())


async def test_level_argument_sets_it(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await send(dispatcher, bot, "/level b1", update_id=100)

    assert await level_of(session_factory) == "B1"
    assert "B1" in recording.last_text()


async def test_level_argument_is_validated(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await send(dispatcher, bot, "/level xyz", update_id=100)

    assert await level_of(session_factory) is None
    assert "A1" in recording.last_text()


async def test_tapping_a_level_saves_it(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await send(dispatcher, bot, "/level", update_id=100)

    await tap(dispatcher, bot, LevelCB(action="set", level="B1").pack(), update_id=101)

    assert await level_of(session_factory) == "B1"
    assert "B1" in recording.last_text()


async def test_reset_clears_it(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await send(dispatcher, bot, "/level b2", update_id=100)
    await tap(dispatcher, bot, LevelCB(action="reset").pack(), update_id=101)

    assert await level_of(session_factory) is None
    assert "сброшен" in recording.last_text()


async def test_the_badge_appears_on_the_confirmation(
    llm_dispatcher: Dispatcher,
    llm: ScriptedClient,
    bot: Bot,
    recording: RecordingSession,
) -> None:
    llm.push_parse(
        parse_payload(
            cards=[card_payload("run", "бежать", cefr="B2", frequency="rare")],
        )
    )

    await send(llm_dispatcher, bot, "добавь run", update_id=100)

    assert "B2" in recording.last_text()
    assert "редкое" in recording.last_text()


async def test_the_badge_appears_in_the_review_result(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await services.card_service.add_draft(
        user,
        CardDraft(term="run", translation="бежать", cefr="B2", frequency="rare"),
    )
    await services.commit()
    await make_due(session_factory, "run")
    await send(dispatcher, bot, "/review", update_id=100)

    await send(dispatcher, bot, "бежать", update_id=101, date=utcnow())

    assert any("B2 · редкое" in text for text in recording.sent_texts())


async def test_a_card_without_levels_shows_no_badge(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))
    await services.commit()
    await make_due(session_factory, "run")
    await send(dispatcher, bot, "/review", update_id=100)

    await send(dispatcher, bot, "бежать", update_id=101, date=utcnow())

    assert not any(" · " in text and "B2" in text for text in recording.sent_texts())
