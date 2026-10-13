"""End-to-end handler tests.

Updates are fed through the real dispatcher — middlewares, filters, routers and
FSM included — with only the Telegram transport replaced. What these tests
assert is what the user would actually receive.
"""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot import texts
from eng_remember.bot.callbacks import ReviewCB
from eng_remember.domain.enums import ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession
from tests.factories import TEST_USER_ID, ScriptedClient, make_callback_update, make_message_update


async def send(dispatcher: Dispatcher, bot: Bot, text: str, **kwargs: object) -> None:
    await dispatcher.feed_update(bot, make_message_update(text, **kwargs))  # type: ignore[arg-type]


async def tap(dispatcher: Dispatcher, bot: Bot, data: str, **kwargs: object) -> None:
    await dispatcher.feed_update(bot, make_callback_update(data, **kwargs))  # type: ignore[arg-type]


def button_with(recording: RecordingSession, needle: str) -> str:
    matches = [data for data in recording.last_button_data() if needle in data]
    assert matches, f"no button matching {needle!r} among {recording.last_button_data()}"
    return matches[0]


@pytest.fixture
async def card_id(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """A saved card, created the way a user would: by confirming a parse."""
    await send(dispatcher, bot, "ubiquitous — вездесущий")
    await tap(dispatcher, bot, button_with(recording, "im:confirm"))
    recording.clear()

    async with session_factory() as check:
        created = await check.scalar(select(Card.id))
    assert created is not None
    return int(created)


class TestStart:
    async def test_start_creates_the_user_and_a_default_deck(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/start")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            deck = (await check.scalars(select(Deck))).one()
            assert user.telegram_id == TEST_USER_ID
            assert deck.user_id == user.id
            assert deck.is_default

        assert "Привет" in recording.last_text()

    async def test_start_again_greets_a_returning_user(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/start")
        recording.clear()

        await send(dispatcher, bot, "/start")

        assert "С возвращением" in recording.last_text()

    async def test_help_lists_the_menu(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/help")

        assert texts.BTN_REVIEW in recording.last_text()


class TestCapturingWords:
    async def test_free_text_is_shown_for_confirmation_before_saving(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "ubiquitous — вездесущий")

        assert "ubiquitous" in recording.last_text()
        assert any("im:confirm" in data for data in recording.last_button_data())

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Card.id))) == 0

    async def test_confirming_saves_the_cards(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "resilient - устойчивый\nthorough - тщательный")

        await tap(dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
            assert terms == {"resilient", "thorough"}

    async def test_a_quota_refusal_falls_back_to_the_rules_parser(
        self,
        llm_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A spent model budget must not stop the learner from adding words."""
        from eng_remember.errors import LlmQuotaExceededError

        llm.push_parse(LlmQuotaExceededError("calls_per_hour limit reached: 60 used, limit 60"))

        await send(llm_dispatcher, bot, "run — бежать")

        assert "Разобрано без модели" in recording.last_text()

        await tap(llm_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
            assert terms == {"run"}

    async def test_a_second_message_replaces_the_pending_batch(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Only the most recent preview may be confirmable.

        Otherwise a user who typed twice and then tapped "save" would get a
        batch they had already moved on from.
        """
        await send(dispatcher, bot, "run - бежать")
        await send(dispatcher, bot, "jump - прыгать")

        await tap(dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
            assert terms == {"jump"}

    async def test_cancelling_saves_nothing(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "ubiquitous — вездесущий")

        await tap(dispatcher, bot, button_with(recording, "im:cancel"))

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Card.id))) == 0
        assert "Отменил" in recording.last_text()

    async def test_unparseable_text_is_answered_with_an_example(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "как дела?")

        assert "Не разобрал" in recording.last_text()

    async def test_an_unknown_command_is_answered_rather_than_ignored(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/nonexistent")

        assert "Не знаю такой команды" in recording.last_text()

    async def test_add_explains_the_expected_format(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        """Guards the handler order: the catch-all must not swallow /add."""
        await send(dispatcher, bot, "/add")

        sent = recording.last_text()
        assert "Напишите слово" in sent
        assert "Не знаю такой команды" not in sent

    async def test_the_add_menu_button_reaches_its_own_handler(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, texts.BTN_ADD)

        assert "Напишите слово" in recording.last_text()


class TestReview:
    async def test_review_without_cards_says_so(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/review")

        assert "нет слов" in recording.last_text()

    async def test_review_shows_the_front_of_a_card(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession, card_id: int
    ) -> None:
        await send(dispatcher, bot, "/review")

        assert "ubiquitous" in recording.last_text()
        assert "вездесущий" not in recording.last_text()
        assert ReviewCB(action="show", card_id=card_id).pack() in recording.last_button_data()

    async def test_showing_the_answer_offers_four_grades(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession, card_id: int
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=10)

        await tap(dispatcher, bot, ReviewCB(action="show", card_id=card_id).pack(), update_id=11)

        grades = [data for data in recording.last_button_data() if "rv:grade" in data]
        labels = [
            button.text
            for button in recording.last_buttons()
            if "rv:grade" in (button.callback_data or "")
        ]
        assert len(grades) == 4
        assert "вездесущий" in recording.last_text()
        # Each grade advertises the interval it would produce.
        assert all("мин" in label or "дн" in label for label in labels)

    async def test_grading_records_a_review_and_finishes_the_session(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await tap(dispatcher, bot, ReviewCB(action="show", card_id=card_id).pack(), update_id=21)

        await tap(
            dispatcher,
            bot,
            ReviewCB(action="grade", card_id=card_id, rating=int(ReviewRating.GOOD)).pack(),
            update_id=22,
        )

        async with session_factory() as check:
            review = (await check.scalars(select(Review))).one()
            assert review.rating is ReviewRating.GOOD
            assert review.duration_ms is not None
            card = (await check.scalars(select(Card))).one()
            assert card.reps == 1
            assert card.last_review_at is not None

        assert "Повторение завершено" in recording.last_text()

    async def test_a_missing_card_is_reported_not_crashed(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await tap(dispatcher, bot, ReviewCB(action="show", card_id=4242).pack())

        answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
        assert answers[-1].show_alert
        assert "не доступна" in (answers[-1].text or "")

    async def test_later_postpones_without_grading(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=30)

        await tap(dispatcher, bot, ReviewCB(action="later", card_id=card_id).pack(), update_id=31)

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Review.id))) == 0
            card = (await check.scalars(select(Card))).one()
            assert card.reps == 0
            assert card.due_at > utcnow()

    async def test_stopping_the_session_reports_the_count(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession, card_id: int
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=40)

        await tap(dispatcher, bot, ReviewCB(action="stop").pack(), update_id=41)

        assert "Повторение завершено" in recording.last_text()


class TestEscaping:
    async def test_markup_in_a_word_cannot_break_the_message(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "<b>bold</b> - жирный")
        await tap(dispatcher, bot, button_with(recording, "im:confirm"))
        recording.clear()

        await send(dispatcher, bot, "/review")

        sent = recording.last_text()
        assert "&lt;b&gt;bold&lt;/b&gt;" in sent
        assert "<b>bold</b>" not in sent


class TestOtherCommands:
    async def test_stats_counts_the_collection(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession, card_id: int
    ) -> None:
        await send(dispatcher, bot, "/stats")

        assert "Всего слов: <b>1</b>" in recording.last_text()

    async def test_decks_lists_the_default_deck(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/decks")

        assert "Основная колода" in recording.last_text()

    async def test_timezone_can_be_changed(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/tz Europe/Moscow")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert user.timezone == "Europe/Moscow"

    async def test_an_invalid_timezone_is_rejected(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/tz Mars/Olympus")

        assert "Не знаю такой пояс" in recording.last_text()
        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert user.timezone == "UTC"

    async def test_an_invalid_timezone_is_echoed_back_escaped(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        """The rejected argument is user input and must not be parsed as markup."""
        await send(dispatcher, bot, "/tz <b>Москва</b>")

        sent = recording.last_text()
        assert "&lt;b&gt;" in sent
        assert "<b>Москва</b>" not in sent

    async def test_the_daily_limit_is_validated(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/limit not-a-number")

        assert "Нужно число" in recording.last_text()

    async def test_the_daily_limit_can_be_changed(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/limit 5")

        async with session_factory() as check:
            user = (await check.scalars(select(User))).one()
            assert user.new_cards_per_day == 5


class TestBlockedUser:
    async def test_a_blocked_user_gets_no_replies(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "/start")
        async with session_factory() as setup:
            user = (await setup.scalars(select(User))).one()
            user.is_blocked = True
            await setup.commit()
        recording.clear()

        await send(dispatcher, bot, "run - бежать")

        assert recording.sent_texts() == []
