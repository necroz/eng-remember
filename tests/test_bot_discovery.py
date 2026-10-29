"""The word-discovery and clarification flows, driven through the dispatcher."""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.callbacks import ClarifyCB, ImportCB
from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.services.llm.schema import ExpansionResponse, ParseResponse
from tests.conftest import RecordingSession
from tests.factories import ScriptedClient, card_payload, expansion_payload, parse_payload
from tests.test_bot_flow import button_with, send, tap

PARAGRAPH = (
    "The committee spent three weeks negotiating the terms of the settlement. "
    "A preliminary agreement was reached, but the reluctance of both sides to "
    "compromise remained obvious throughout the deliberations."
)


@pytest.fixture
def discovery_dispatcher(llm_dispatcher: Dispatcher) -> Dispatcher:
    return llm_dispatcher


class TestExpansionByTopic:
    async def test_suggestions_are_shown_before_anything_is_saved(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=2))
        llm.push_expand(
            expansion_payload(
                [card_payload("fever", "жар"), card_payload("cough", "кашель")],
                topic="медицина",
            )
        )

        await send(discovery_dispatcher, bot, "ещё 2 слова про медицину")

        sent = recording.last_text()
        assert "медицина" in sent
        assert "fever" in sent and "cough" in sent
        assert any("im:confirm" in data for data in recording.last_button_data())

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Card.id))) == 0

    async def test_confirming_saves_the_suggestions(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=2))
        llm.push_expand(
            expansion_payload(
                [card_payload("fever", "жар"), card_payload("cough", "кашель")],
                topic="медицина",
            )
        )
        await send(discovery_dispatcher, bot, "ещё 2 слова про медицину")

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
            assert terms == {"fever", "cough"}

    async def test_the_topic_is_remembered_for_next_time(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ "More words" without a subject has to land somewhere sensible."""
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))
        await send(discovery_dispatcher, bot, "ещё слова про медицину")
        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            deck = (await check.scalars(select(Deck))).one()
            assert deck.topic == "медицина"

    async def test_another_round_excludes_what_is_on_screen(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))
        llm.push_expand(expansion_payload([card_payload("cough", "кашель")], topic="медицина"))
        await send(discovery_dispatcher, bot, "ещё слово про медицину")

        await tap(discovery_dispatcher, bot, button_with(recording, "im:more"))

        sent = recording.last_text()
        assert "cough" in sent
        assert "fever" not in sent
        async with session_factory() as check:
            assert await check.scalar(select(func.count(Card.id))) == 0

    async def test_the_deck_topic_is_used_when_the_request_names_none(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))
        await send(discovery_dispatcher, bot, "ещё слова")
        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))
        recording.clear()

        llm.push_parse(parse_payload(intent="expand"))
        llm.push_expand(expansion_payload([card_payload("cough", "кашель")], topic="медицина"))

        await send(discovery_dispatcher, bot, "подбери ещё")

        assert "медицина" in llm.last_prompt()


class TestExpansionFromText:
    async def test_a_pasted_paragraph_is_used_as_the_source(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", count=3))
        llm.push_expand(
            expansion_payload(
                [
                    card_payload("settlement", "урегулирование"),
                    card_payload("ubiquitous", "вездесущий"),
                ]
            )
        )

        await send(discovery_dispatcher, bot, PARAGRAPH)

        assert "only" in llm.last_prompt().lower()
        assert "settlement" in recording.last_text()
        # The word that is not in the paragraph never reaches the user.
        assert "ubiquitous" not in recording.last_text()
        assert "нет в тексте" in recording.last_text()

    async def test_a_short_message_is_not_treated_as_source_text(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        llm: ScriptedClient,
        recording: RecordingSession,
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=2))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))

        await send(discovery_dispatcher, bot, "ещё слова про медицину")

        assert "<source_text>" not in llm.last_prompt()


class TestAWordAndWordsAroundIt:
    """One message can ask for a word to save and for words from its domain.

    The failure this covers: "arrangement как во фразе ... И добавь ещё пару
    слов на эту тему" saved the word and silently dropped the second request.
    """

    async def test_the_word_and_the_found_words_are_one_batch(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[
                    card_payload(
                        "arrangement",
                        "упорядоченный набор",
                        example=(
                            "The graded evaluation criteria is an arrangement of "
                            "descriptions sorted from 0 points upwards."
                        ),
                        tags=["assessment"],
                    )
                ],
                topic="оценка по критериям",
                count=2,
                more_words=True,
            )
        )
        llm.push_expand(
            expansion_payload(
                [card_payload("criterion", "критерий"), card_payload("score", "балл")],
                topic="оценка по критериям",
            )
        )

        await send(
            discovery_dispatcher,
            bot,
            "arrangement как во фразе The graded evaluation criteria is an arrangement "
            "of descriptions sorted from 0 points upwards. И добавь ещё пару слов на эту тему",
        )

        sent = recording.last_text()
        assert "arrangement" in sent
        assert "criterion" in sent and "score" in sent
        assert "оценка по критериям" in sent
        # One batch, one save button — not two confirmations that can drift apart.
        assert recording.last_button_data().count("im:confirm") == 1
        assert "im:more" in recording.last_button_data()
        # The learner's word seeds the search instead of coming back as a find.
        assert "arrangement" in llm.calls[-1]["user"]

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
            deck = (await check.scalars(select(Deck))).one()
        assert terms == {"arrangement", "criterion", "score"}
        # And the domain is remembered, so "ещё слова" works without repeating it.
        assert deck.topic == "оценка по критериям"

    async def test_a_question_about_the_sense_is_asked_and_both_halves_still_run(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        """The learner's own words survive the detour through a question."""
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("arrangement", "расположение")],
                clarification={
                    "question": "О каком смысле arrangement речь?",
                    "options": ["оценка по критериям", "договорённость"],
                },
            )
        )
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("arrangement", "упорядоченный набор")],
                topic="оценка по критериям",
                count=2,
                more_words=True,
            )
        )
        llm.push_expand(
            expansion_payload([card_payload("criterion", "критерий")], topic="оценка по критериям")
        )

        await send(discovery_dispatcher, bot, "добавь arrangement и ещё пару слов на эту тему")

        assert "О каком смысле" in recording.last_text()

        await tap(
            discovery_dispatcher,
            bot,
            ClarifyCB(action="option", index=0).pack(),
            update_id=95,
        )

        sent = recording.last_text()
        assert "arrangement" in sent
        assert "criterion" in sent
        # The answer reached the model together with the original request.
        clarified = llm.calls[-2]["user"]
        assert "О каком смысле" in clarified
        assert "оценка по критериям" in clarified

    async def test_a_word_named_as_the_basis_is_saved_too(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An expand request that names its starting word still saves that word."""
        llm.push_parse(
            parse_payload(
                intent="expand",
                cards=[card_payload("arrangement", "расположение")],
                topic="оценка",
                count=1,
            )
        )
        llm.push_expand(expansion_payload([card_payload("criterion", "критерий")], topic="оценка"))

        await send(discovery_dispatcher, bot, "подбери ещё слова как arrangement")

        assert "arrangement" in recording.last_text()
        assert "criterion" in recording.last_text()

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
        assert terms == {"arrangement", "criterion"}

    async def test_another_round_replaces_only_the_found_half(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Asking for a different selection must not withdraw the learner's word."""
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("arrangement", "расположение")],
                topic="оценка",
                count=1,
                more_words=True,
            )
        )
        llm.push_expand(expansion_payload([card_payload("criterion", "критерий")], topic="оценка"))
        llm.push_expand(expansion_payload([card_payload("score", "балл")], topic="оценка"))
        await send(discovery_dispatcher, bot, "arrangement и ещё слово на эту тему")

        await tap(discovery_dispatcher, bot, button_with(recording, "im:more"))

        sent = recording.last_text()
        assert "arrangement" in sent
        assert "score" in sent
        assert "criterion" not in sent
        prompt = llm.last_prompt()
        assert "arrangement" in prompt  # still the seed
        assert "criterion" not in prompt  # the rejected round is not named again

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
        assert terms == {"arrangement", "score"}

    async def test_a_failed_search_still_offers_the_word(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        """Half a request done honestly beats a word lost to the other half."""
        from eng_remember.errors import LlmResponseError

        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("arrangement", "расположение")],
                topic="оценка",
                count=2,
                more_words=True,
            )
        )
        llm.push_expand(LlmResponseError("provider is down"))

        await send(discovery_dispatcher, bot, "arrangement и ещё пару слов про оценку")

        sent = recording.last_text()
        assert "arrangement" in sent
        assert "Дополнительные слова" in sent

    async def test_an_empty_search_says_so_under_the_word(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("arrangement", "расположение")],
                topic="оценка",
                count=2,
                more_words=True,
            )
        )
        llm.push_expand(expansion_payload([]))

        await send(discovery_dispatcher, bot, "arrangement и ещё пару слов про оценку")

        sent = recording.last_text()
        assert "arrangement" in sent
        assert "не нашлось" in sent

    async def test_a_request_with_no_readable_domain_is_asked_about(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No topic and no context: ask, do not guess words for a wrong sense.

        "arrangement" alone is договорённость, расположение or аранжировка, and a
        batch built on the wrong one would be worse than a question.
        """
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("arrangement", "расположение")],
                more_words=True,
            )
        )

        await send(discovery_dispatcher, bot, "добавь arrangement и ещё пару слов на эту тему")

        sent = recording.last_text()
        assert "arrangement" in sent
        assert "не понял тему" in sent
        # No search was attempted at all: there was nothing to search by.
        assert [call["purpose"] for call in llm.calls] == ["parse_message"]

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            terms = set((await check.scalars(select(Card.term))).all())
        assert terms == {"arrangement"}


class TestDiscoveryLanguage:
    async def test_a_french_request_keeps_its_language_across_more_and_confirm(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("bonjour", "здравствуйте", language="fra")],
                topic="приветствия",
                count=1,
                more_words=True,
            )
        )
        llm.push_expand(
            expansion_payload(
                [card_payload("merci", "спасибо", language="eng")], topic="приветствия"
            )
        )
        llm.push_expand(
            expansion_payload(
                [card_payload("au revoir", "до свидания", language="eng")],
                topic="приветствия",
            )
        )

        await send(discovery_dispatcher, bot, "добавь bonjour и ещё слово")

        assert "Target language: French (fra)." in llm.last_prompt()
        assert "bonjour" in recording.last_text()

        await tap(discovery_dispatcher, bot, button_with(recording, "im:more"))

        assert "Target language: French (fra)." in llm.last_prompt()
        assert "au revoir" in recording.last_text()

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            cards = (await check.scalars(select(Card))).all()
        assert {card.term: card.language for card in cards} == {
            "bonjour": "fra",
            "au revoir": "fra",
        }

    async def test_skipping_a_clarification_keeps_the_language(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="expand",
                topic="приветствия",
                count=1,
                language="fra",
                clarification={"question": "Уточнить тему?", "options": ["приветствия"]},
            )
        )
        await send(discovery_dispatcher, bot, "хочу ещё слов", update_id=700)

        llm.push_expand(
            expansion_payload(
                [card_payload("bonjour", "здравствуйте", language="eng")],
                topic="приветствия",
            )
        )
        await tap(
            discovery_dispatcher,
            bot,
            ClarifyCB(action="skip").pack(),
            update_id=701,
        )

        assert "Target language: French (fra)." in llm.last_prompt()
        assert "bonjour" in recording.last_text()

    async def test_a_mixed_batch_keeps_each_cards_own_language(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                language="fra",
                cards=[card_payload("hello", "привет", language="eng")],
                topic="приветствия",
                count=1,
                more_words=True,
            )
        )
        llm.push_expand(
            expansion_payload(
                [card_payload("bonjour", "здравствуйте", language="eng")],
                topic="приветствия",
            )
        )

        await send(discovery_dispatcher, bot, "добавь hello и ещё слово", update_id=710)

        assert "Target language: French (fra)." in llm.last_prompt()
        assert "hello" in recording.last_text()
        assert "bonjour" in recording.last_text()

        await tap(
            discovery_dispatcher,
            bot,
            button_with(recording, "im:confirm"),
            update_id=711,
        )

        async with session_factory() as check:
            cards = (await check.scalars(select(Card))).all()
        assert {card.term: card.language for card in cards} == {
            "hello": "eng",
            "bonjour": "fra",
        }


class TestDiscoveryGuardrails:
    async def test_without_a_model_the_user_is_told(
        self, dispatcher: Dispatcher, bot: Bot, recording: RecordingSession
    ) -> None:
        """Without a model the rule-based parser cannot read this request."""
        await send(dispatcher, bot, "ещё 10 слов про медицину")

        assert recording.last_text()

    async def test_nothing_to_expand_from_asks_for_context(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        """No topic, no text, no remembered subject: ask, do not invent."""
        llm.push_parse(parse_payload(intent="expand"))

        await send(discovery_dispatcher, bot, "хочу ещё слов")

        assert "нужно понимать контекст" in recording.last_text()
        assert llm.calls[-1]["schema"] is ParseResponse

    async def test_an_empty_suggestion_is_reported_honestly(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина"))
        llm.push_expand(expansion_payload([]))

        await send(discovery_dispatcher, bot, "ещё слов про медицину")

        assert "Ничего нового" in recording.last_text()

    async def test_a_failing_model_degrades_to_a_message(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        from eng_remember.errors import LlmResponseError

        llm.push_parse(parse_payload(intent="expand", topic="медицина"))
        llm.push_expand(LlmResponseError("provider is down"))

        await send(discovery_dispatcher, bot, "ещё слов про медицину")

        assert "нужна подключённая модель" in recording.last_text()

    async def test_an_exhausted_quota_gets_its_own_message(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        """A spent budget is not a missing model, and must not be reported as one."""
        from eng_remember.errors import LlmQuotaExceededError

        llm.push_parse(parse_payload(intent="expand", topic="медицина"))
        llm.push_expand(LlmQuotaExceededError("calls_per_day limit reached: 60 used, limit 60"))

        await send(discovery_dispatcher, bot, "ещё слов про медицину")

        sent = recording.last_text()
        assert "Лимит" in sent
        assert "нужна подключённая модель" not in sent

    async def test_an_exhausted_global_budget_gets_a_busy_message(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        from eng_remember.bot import texts
        from eng_remember.errors import LlmGlobalBudgetExceededError

        llm.push_parse(parse_payload(intent="expand", topic="медицина"))
        llm.push_expand(LlmGlobalBudgetExceededError("global token budget reached"))

        await send(discovery_dispatcher, bot, "ещё слов про медицину")

        assert texts.model_busy() in recording.last_text()


class TestClarification:
    async def test_a_vague_request_is_met_with_a_question(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="expand",
                clarification={"question": "О чём именно?", "options": ["бизнес", "медицина"]},
            )
        )

        await send(discovery_dispatcher, bot, "хочу ещё слов")

        assert "О чём именно?" in recording.last_text()
        assert [d for d in recording.last_button_data() if d.startswith("cl:option")] == [
            "cl:option:0",
            "cl:option:1",
        ]

    async def test_tapping_an_option_answers_the_question(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="expand",
                clarification={"question": "О чём именно?", "options": ["бизнес", "медицина"]},
            )
        )
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))
        await send(discovery_dispatcher, bot, "хочу ещё слов")

        await tap(
            discovery_dispatcher,
            bot,
            ClarifyCB(action="option", index=1).pack(),
            update_id=50,
        )

        # The clarified call carries the original request, the question and the answer.
        clarified = llm.calls[-2]["user"]
        assert "хочу ещё слов" in clarified
        assert "О чём именно?" in clarified
        assert "медицина" in clarified
        assert "fever" in recording.last_text()

    async def test_an_option_answer_keeps_the_stored_language(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="expand",
                topic="приветствия",
                count=1,
                language="fra",
                clarification={"question": "Уточнить тему?", "options": ["приветствия"]},
            )
        )
        llm.push_parse(parse_payload(intent="expand", topic="приветствия", count=1))
        llm.push_expand(
            expansion_payload(
                [card_payload("bonjour", "здравствуйте", language="eng")],
                topic="приветствия",
            )
        )
        await send(discovery_dispatcher, bot, "хочу ещё слов", update_id=55)

        await tap(
            discovery_dispatcher,
            bot,
            ClarifyCB(action="option", index=0).pack(),
            update_id=56,
        )

        assert "Target language: French (fra)." in llm.last_prompt()
        assert "bonjour" in recording.last_text()

    async def test_a_typed_answer_keeps_the_stored_language(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="expand",
                topic="приветствия",
                count=1,
                language="fra",
                clarification={"question": "Уточнить тему?", "options": ["приветствия"]},
            )
        )
        llm.push_parse(parse_payload(intent="expand", topic="приветствия", count=1))
        llm.push_expand(
            expansion_payload(
                [card_payload("bonjour", "здравствуйте", language="eng")],
                topic="приветствия",
            )
        )
        await send(discovery_dispatcher, bot, "хочу ещё слов", update_id=57)

        await send(discovery_dispatcher, bot, "приветствия", update_id=58)

        assert "Target language: French (fra)." in llm.last_prompt()
        assert "bonjour" in recording.last_text()

    async def test_a_typed_answer_also_resolves_the_question(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", clarification={"question": "О чём именно?"}))
        llm.push_parse(parse_payload(intent="expand", topic="бизнес", count=1))
        llm.push_expand(expansion_payload([card_payload("revenue", "выручка")], topic="бизнес"))
        await send(discovery_dispatcher, bot, "хочу ещё слов")

        await send(discovery_dispatcher, bot, "про бизнес", update_id=51)

        assert "revenue" in recording.last_text()

    async def test_skip_falls_back_to_the_deck_topic(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))
        await send(discovery_dispatcher, bot, "ещё слова про медицину")
        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        llm.push_parse(parse_payload(intent="expand", clarification={"question": "О чём именно?"}))
        await send(discovery_dispatcher, bot, "хочу ещё слов", update_id=60)
        llm.push_expand(expansion_payload([card_payload("cough", "кашель")], topic="медицина"))

        await tap(discovery_dispatcher, bot, ClarifyCB(action="skip").pack(), update_id=61)

        assert "медицина" in llm.last_prompt()
        assert "cough" in recording.last_text()

    async def test_skip_without_any_context_asks_instead_of_guessing(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", clarification={"question": "О чём именно?"}))
        await send(discovery_dispatcher, bot, "хочу ещё слов")

        await tap(discovery_dispatcher, bot, ClarifyCB(action="skip").pack(), update_id=70)

        assert "нужно понимать контекст" in recording.last_text()
        assert not llm.expand_queue

    async def test_the_questions_do_not_go_on_forever(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        """A model that keeps asking must not trap the conversation."""
        asking = parse_payload(intent="expand", clarification={"question": "О чём именно?"})
        llm.push_parse(asking, asking, asking, asking)

        await send(discovery_dispatcher, bot, "хочу ещё слов", update_id=80)
        await send(discovery_dispatcher, bot, "ну не знаю", update_id=81)
        await send(discovery_dispatcher, bot, "всё ещё не знаю", update_id=82)

        # Two clarifications, then the flow gives up on asking and asks for
        # context in plain words instead of looping.
        assert llm.parse_queue or "нужно понимать контекст" in recording.last_text()

    async def test_a_stale_option_button_is_rejected(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
    ) -> None:
        from aiogram.methods import AnswerCallbackQuery

        await tap(discovery_dispatcher, bot, ClarifyCB(action="option", index=7).pack())

        answers = [m for m in recording.requests if isinstance(m, AnswerCallbackQuery)]
        assert answers[-1].show_alert


class TestMoreButtonWithoutSuggestions:
    async def test_cancelling_a_suggestion_batch_saves_nothing(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=2))
        llm.push_expand(
            expansion_payload(
                [card_payload("fever", "жар"), card_payload("cough", "кашель")],
                topic="медицина",
            )
        )
        await send(discovery_dispatcher, bot, "ещё слова про медицину")

        await tap(discovery_dispatcher, bot, ImportCB(action="cancel").pack())

        async with session_factory() as check:
            assert await check.scalar(select(func.count(Card.id))) == 0


class TestSchemaDispatch:
    async def test_parse_and_expansion_use_different_schemas(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        llm: ScriptedClient,
        recording: RecordingSession,
    ) -> None:
        """The script dispatches by schema, so a mixed-up call would be caught."""
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))

        await send(discovery_dispatcher, bot, "ещё слово про медицину")

        purposes = [call["purpose"] for call in llm.calls]
        assert purposes == ["parse_message", "expand_words"]
        assert llm.calls[-1]["schema"] is ExpansionResponse


class TestTopicIsLearnedFromPlainAdditions:
    async def test_tags_from_a_manual_addition_become_the_deck_topic(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Otherwise "more words" only works after a discovery request."""
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[
                    card_payload("revenue", "выручка", tags=["business"]),
                    card_payload("invoice", "счёт", tags=["business", "money"]),
                ],
            )
        )
        await send(discovery_dispatcher, bot, "revenue - выручка, invoice - счёт")

        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"))

        async with session_factory() as check:
            deck = (await check.scalars(select(Deck))).one()
            assert deck.topic == "business"

    async def test_a_later_request_without_a_topic_uses_what_was_learned(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        llm.push_parse(
            parse_payload(
                intent="add_cards",
                cards=[card_payload("revenue", "выручка", tags=["business"])],
            )
        )
        await send(discovery_dispatcher, bot, "revenue - выручка", update_id=900)
        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"), update_id=901)

        llm.push_parse(parse_payload(intent="expand"))
        llm.push_expand(expansion_payload([card_payload("invoice", "счёт")], topic="business"))

        await send(discovery_dispatcher, bot, "ещё слова", update_id=902)

        assert "business" in llm.last_prompt()

    async def test_cards_without_tags_leave_the_topic_alone(
        self,
        discovery_dispatcher: Dispatcher,
        bot: Bot,
        recording: RecordingSession,
        llm: ScriptedClient,
    ) -> None:
        llm.push_parse(parse_payload(intent="expand", topic="медицина", count=1))
        llm.push_expand(expansion_payload([card_payload("fever", "жар")], topic="медицина"))
        await send(discovery_dispatcher, bot, "ещё слово про медицину", update_id=910)
        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"), update_id=911)

        llm.push_parse(parse_payload(intent="add_cards", cards=[card_payload("run", "бежать")]))
        await send(discovery_dispatcher, bot, "run - бежать", update_id=912)
        await tap(discovery_dispatcher, bot, button_with(recording, "im:confirm"), update_id=913)

        # The deck still says "медицина": untagged cards must not erase it.
        llm.push_parse(parse_payload(intent="expand"))
        await send(discovery_dispatcher, bot, "ещё слова", update_id=914)
        assert "медицина" in llm.last_prompt()
