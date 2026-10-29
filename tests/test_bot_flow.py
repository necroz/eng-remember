"""End-to-end handler tests.

Updates are fed through the real dispatcher — middlewares, filters, routers and
FSM included — with only the Telegram transport replaced. What these tests
assert is what the user would actually receive.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot import texts
from eng_remember.bot.callbacks import ReviewCB
from eng_remember.domain.enums import CardDirection, ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.services.llm.schema import AnswerCheckResponse
from eng_remember.utils.time import utcnow
from tests.conftest import RecordingSession, make_due
from tests.factories import (
    TEST_USER_ID,
    ScriptedClient,
    card_payload,
    make_callback_update,
    make_message_update,
    parse_payload,
)


async def send(dispatcher: Dispatcher, bot: Bot, text: str, **kwargs: object) -> None:
    await dispatcher.feed_update(bot, make_message_update(text, **kwargs))  # type: ignore[arg-type]


async def tap(dispatcher: Dispatcher, bot: Bot, data: str, **kwargs: object) -> None:
    await dispatcher.feed_update(bot, make_callback_update(data, **kwargs))  # type: ignore[arg-type]


def button_with(recording: RecordingSession, needle: str) -> str:
    matches = [data for data in recording.last_button_data() if needle in data]
    assert matches, f"no button matching {needle!r} among {recording.last_button_data()}"
    return matches[0]


def result_text(recording: RecordingSession) -> str:
    """The text of the graded card among the sends and edits."""
    matches = [text for text in recording.sent_texts() if "Дальше через" in text]
    assert matches, recording.sent_texts()
    return matches[-1]


@pytest.fixture
async def card_id(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """A saved card whose first repetition has come.

    Created the way a user would — by confirming a parse — and then carried past
    its due date: a word added today is deliberately kept out of today's queue,
    and these tests are about what the next day looks like.
    """
    await send(dispatcher, bot, "ubiquitous — вездесущий")
    await tap(dispatcher, bot, button_with(recording, "im:confirm"))
    recording.clear()
    await make_due(session_factory, "ubiquitous")

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
        assert "иностранные слова" in recording.last_text()
        assert "Французский" in recording.last_text()

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

    async def test_saving_says_when_the_first_repetition_comes(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        """The learner is told not to look for the word today: it is not there yet."""
        await send(dispatcher, bot, "resilient - устойчивый")

        await tap(dispatcher, bot, button_with(recording, "im:confirm"))

        assert "Первое повторение — завтра" in recording.last_text()

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

    async def test_the_preview_names_the_card_language_and_how_to_fix_it(
        self,
        llm_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                cards=[card_payload("bonjour", "здравствуйте", language="fra")],
                language="fra",
            )
        )

        await send(llm_dispatcher, bot, "добавь bonjour — здравствуйте")

        sent = recording.last_text()
        assert "🇫🇷 Французский" in sent
        assert "Если язык определён неверно" in sent
        assert "французское слово hotel — отель" in sent

    async def test_a_fallback_preview_explains_script_based_language(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
    ) -> None:
        await send(dispatcher, bot, "bonjour - здравствуйте")

        sent = recording.last_text()
        assert "🇬🇧 Английский" in sent
        assert (
            "Без модели язык определяется по письменности; латиница считается английской." in sent
        )

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
        assert "Напишите иностранное слово" in sent
        assert "Не знаю такой команды" not in sent

    async def test_the_add_menu_button_reaches_its_own_handler(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, texts.BTN_ADD)

        assert "Напишите иностранное слово" in recording.last_text()


class TestReview:
    async def test_review_without_cards_says_so(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await send(dispatcher, bot, "/review")

        assert "нет слов" in recording.last_text()

    async def test_a_word_added_today_waits_for_tomorrow(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        """The queue is empty on the day of adding, and the screen says when to come back."""
        await send(dispatcher, bot, "ubiquitous — вездесущий", update_id=50)
        await tap(dispatcher, bot, button_with(recording, "im:confirm"), update_id=51)
        recording.clear()

        await send(dispatcher, bot, "/review", update_id=52)

        sent = recording.last_text()
        assert "всё повторено" in sent
        assert "Следующая карточка" in sent

    async def test_review_shows_the_front_with_a_single_button(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession, card_id: int
    ) -> None:
        await send(dispatcher, bot, "/review")

        sent = recording.last_text()
        assert "🇬🇧" in sent
        assert "Английский" in sent
        assert "переведите на русский" in sent
        assert "ubiquitous" in sent
        assert "вездесущий" not in sent
        assert recording.last_button_data() == [
            ReviewCB(action="dont_know", card_id=card_id).pack()
        ]

    async def test_a_fast_correct_answer_is_graded_easy(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await send(dispatcher, bot, "Вездесущий!", update_id=21, date=utcnow())

        async with session_factory() as check:
            review = (await check.scalars(select(Review))).one()
            assert review.rating is ReviewRating.EASY
            assert review.duration_ms is not None
            card = (
                await check.scalars(select(Card).where(Card.direction == CardDirection.FORWARD))
            ).one()
            assert card.reps == 1

        assert "✅ Верно" in result_text(recording)
        assert "Ваш ответ:" in result_text(recording)
        assert "вездесущий" in result_text(recording)
        assert "Повторение завершено" in recording.last_text()

    async def test_a_slow_correct_answer_is_graded_good(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await send(
            dispatcher, bot, "вездесущий", update_id=21, date=utcnow() + timedelta(seconds=10)
        )

        async with session_factory() as check:
            review = (await check.scalars(select(Review))).one()
            assert review.rating is ReviewRating.GOOD

    async def test_a_wrong_answer_without_a_model_is_again(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await send(dispatcher, bot, "неправильно", update_id=21, date=utcnow())

        async with session_factory() as check:
            review = (await check.scalars(select(Review))).one()
            assert review.rating is ReviewRating.AGAIN

        assert "❌ Неверно" in result_text(recording)
        assert "Ваш ответ:" in result_text(recording)

    async def test_a_command_does_not_cancel_the_card(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await send(dispatcher, bot, "/stats", update_id=21)
        await send(dispatcher, bot, "вездесущий", update_id=22, date=utcnow())

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Review.id))) == 1

    async def test_the_model_may_confirm_a_synonym(
        self,
        llm_dispatcher: Dispatcher,
        llm: ScriptedClient,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        saved_card: Card,
    ) -> None:
        """The card is seeded through the service: with the scripted model
        patched in globally, the capture flow would need its own scripted
        parse and the test is about the check, not about parsing."""
        llm.push_check({"verdict": "correct"})
        await make_due(session_factory)

        await send(llm_dispatcher, bot, "/review", update_id=20)
        await send(llm_dispatcher, bot, "жуткий", update_id=21, date=utcnow())

        async with session_factory() as check:
            review = (await check.scalars(select(Review))).one()
            assert review.rating is ReviewRating.EASY
        assert llm.calls[-1]["purpose"] == "check_answer"

    async def test_the_dont_know_button_is_again(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await tap(
            dispatcher,
            bot,
            ReviewCB(action="dont_know", card_id=card_id).pack(),
            update_id=21,
        )

        async with session_factory() as check:
            review = (await check.scalars(select(Review))).one()
            assert review.rating is ReviewRating.AGAIN

        assert "🤷 Не помню" in result_text(recording)
        assert "Ваш ответ" not in result_text(recording)

    async def test_a_message_sent_before_the_card_is_shown_is_ignored(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)
        await send(
            dispatcher,
            bot,
            "вездесущий",
            update_id=21,
            date=utcnow() - timedelta(minutes=1),
        )

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Review.id))) == 0

        await send(dispatcher, bot, "вездесущий", update_id=22, date=utcnow())

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Review.id))) == 1

    async def test_a_pasted_answer_is_shown_truncated(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        """A pasted paragraph must not overflow the revealed answer.

        The whole answer is used for grading; only the revealed line is capped.
        A translation is a few words, so the cap is far below Telegram's limit.
        """
        await send(dispatcher, bot, "/review", update_id=20)
        await send(dispatcher, bot, "а" * 300, update_id=21, date=utcnow())

        text = result_text(recording)
        assert "а" * 201 not in text
        assert "…" in text
        assert len(text) < 4096

    async def test_a_pause_during_the_check_wins(
        self,
        llm_dispatcher: Dispatcher,
        llm: ScriptedClient,
        monkeypatch: pytest.MonkeyPatch,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        saved_card: Card,
    ) -> None:
        """A multi-second model check must not hijack the state after a pause.

        «Добавить слова» ends the review for now; the in-flight answer is still
        graded — it was given — but its result must not overwrite the new flow.
        """
        await make_due(session_factory)
        gate = asyncio.Event()
        entered = asyncio.Event()
        original = llm.complete_json

        async def gated(
            *,
            purpose: str,
            system: str,
            user: str,
            schema: object,
            user_id: int | None = None,
        ) -> object:
            if schema is AnswerCheckResponse:
                entered.set()
                await gate.wait()
            return await original(
                purpose=purpose, system=system, user=user, schema=schema, user_id=user_id
            )

        monkeypatch.setattr(llm, "complete_json", gated)
        llm.push_check({"verdict": "correct"})
        llm.push_parse(parse_payload(cards=[card_payload("run", "бежать")]))

        await send(llm_dispatcher, bot, "/review", update_id=20)
        recording.clear()
        answer = asyncio.create_task(
            send(llm_dispatcher, bot, "жуткий", update_id=21, date=utcnow())
        )
        await asyncio.wait_for(entered.wait(), timeout=1)

        await send(llm_dispatcher, bot, texts.BTN_ADD, update_id=22)
        gate.set()
        await answer
        await send(llm_dispatcher, bot, "run - бежать", update_id=23)
        confirm_data = button_with(recording, "im:confirm")

        await tap(llm_dispatcher, bot, confirm_data, update_id=24)

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
        assert "run" in terms

    async def test_a_stale_tap_is_rejected(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        card_id: int,
    ) -> None:
        await tap(dispatcher, bot, ReviewCB(action="dont_know", card_id=card_id).pack())

        answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
        assert answers[-1].show_alert
        assert "устарела" in (answers[-1].text or "")

    async def test_a_legacy_button_from_history_is_rejected(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        await tap(dispatcher, bot, "rv:grade:5:3", update_id=99)

        answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
        assert answers[-1].show_alert
        assert "устарела" in (answers[-1].text or "")

    async def test_adding_words_pauses_review(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)

        await send(dispatcher, bot, texts.BTN_ADD, update_id=21)
        assert "Напишите иностранное слово" in recording.last_text()

        await send(dispatcher, bot, "run - бежать", update_id=22)
        assert any("im:confirm" in data for data in recording.last_button_data())

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Review.id))) == 0

        await send(dispatcher, bot, "/review", update_id=23)
        assert "ubiquitous" in recording.last_text()

    async def test_a_missing_card_is_reported(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=20)

        async with session_factory() as session:
            await session.execute(delete(Card).where(Card.id == card_id))
            await session.commit()

        await tap(
            dispatcher,
            bot,
            ReviewCB(action="dont_know", card_id=card_id).pack(),
            update_id=21,
        )

        answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
        assert answers[-1].show_alert
        assert "не доступна" in (answers[-1].text or "")


class TestEscaping:
    async def test_markup_in_a_word_cannot_break_the_message(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await send(dispatcher, bot, "<b>bold</b> - жирный")
        await tap(dispatcher, bot, button_with(recording, "im:confirm"))
        recording.clear()
        await make_due(session_factory)

        await send(dispatcher, bot, "/review")

        sent = recording.last_text()
        assert "&lt;b&gt;bold&lt;/b&gt;" in sent
        assert "<b>bold</b>" not in sent

    async def test_markup_in_the_answer_is_escaped(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        session_factory: async_sessionmaker[AsyncSession],
        card_id: int,
    ) -> None:
        await send(dispatcher, bot, "/review", update_id=60)
        await send(dispatcher, bot, "<b>bold</b>", update_id=61, date=utcnow())

        assert "&lt;b&gt;bold&lt;/b&gt;" in result_text(recording)
        assert "<b>bold</b>" not in result_text(recording)


class TestOtherCommands:
    async def test_stats_counts_the_collection(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession, card_id: int
    ) -> None:
        await send(dispatcher, bot, "/stats")

        # Counts are per card; a word is a pair of cards.
        assert "Всего слов: <b>2</b>" in recording.last_text()

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
