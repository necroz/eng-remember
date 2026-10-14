"""The practice quiz service: questions built from the learner's own cards."""

from __future__ import annotations

import random

from eng_remember.config import Settings
from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.practice import PracticeService
from tests.factories import schedule


async def seed_cards(
    services: Services,
    user: User,
    *pairs: tuple[str, str, str | None],
) -> list[Card]:
    """Reviewed cards, created through the repository so no review is logged."""
    deck = await services.card_service.default_deck(user)
    cards: list[Card] = []
    for term, translation, part_of_speech in pairs:
        card = await services.cards.add(
            user_id=user.id,
            deck_id=deck.id,
            draft=CardDraft(term=term, translation=translation, part_of_speech=part_of_speech),
            schedule=schedule(due_in_days=7, state=CardState.REVIEW, reps=1),
            source="manual",
        )
        cards.append(card)
    await services.commit()
    return cards


def build(services: Services, settings: Settings) -> PracticeService:
    return PracticeService(cards=services.cards, settings=settings, rng=random.Random(42))


async def test_build_session_returns_four_unique_options_with_exact_answer(
    services: Services, user: User, settings: Settings
) -> None:
    cards = await seed_cards(
        services,
        user,
        ("one", "первый", "noun"),
        ("two", "второй", "noun"),
        ("three", "третий", "noun"),
        ("four", "четвертый", "noun"),
        ("five", "пятый", "noun"),
    )
    translations = {card.id: card.translation for card in cards}

    questions = await build(services, settings).build_session(user)

    assert len(questions) == 5
    for question in questions:
        assert len(question.options) == 4
        assert len(set(question.options)) == 4
        assert question.options[question.correct_index] == translations[question.card_id]


async def test_new_and_suspended_cards_are_excluded(
    services: Services, user: User, settings: Settings
) -> None:
    seen = await seed_cards(
        services,
        user,
        ("one", "первый", "noun"),
        ("two", "второй", "noun"),
        ("three", "третий", "noun"),
        ("four", "четвертый", "noun"),
    )
    deck = await services.card_service.default_deck(user)
    new_card = await services.cards.add(
        user_id=user.id,
        deck_id=deck.id,
        draft=CardDraft(term="five", translation="пятый", part_of_speech="noun"),
        schedule=schedule(due_in_days=7),
        source="manual",
    )
    suspended = await services.cards.add(
        user_id=user.id,
        deck_id=deck.id,
        draft=CardDraft(term="six", translation="шестой", part_of_speech="noun"),
        schedule=schedule(due_in_days=7, state=CardState.REVIEW, reps=1),
        source="manual",
    )
    await services.cards.set_suspended(suspended, suspended=True)
    await services.commit()

    questions = await build(services, settings).build_session(user)

    assert {question.card_id for question in questions} == {card.id for card in seen}
    assert new_card.id not in {question.card_id for question in questions}


async def test_same_part_of_speech_is_preferred(
    services: Services, user: User, settings: Settings
) -> None:
    cards = await seed_cards(
        services,
        user,
        ("run", "бежать", "verb"),
        ("jump", "прыгать", "verb"),
        ("swim", "плавать", "verb"),
        ("fly", "летать", "verb"),
        ("house", "дом", "noun"),
    )
    card_by_id = {card.id: card for card in cards}
    verb_translations = {"бежать", "прыгать", "плавать", "летать"}

    questions = await build(services, settings).build_session(user)

    for question in questions:
        if card_by_id[question.card_id].part_of_speech == "verb":
            assert set(question.options) <= verb_translations


async def test_variant_listings_are_excluded_from_distractors(
    services: Services, user: User, settings: Settings
) -> None:
    """The quiz answer key must agree with what grading accepts.

    «ужасный» is a valid answer for «ужасный, кошмарный» in review, so it must
    never sit next to it as a distractor in practice.
    """
    cards = await seed_cards(
        services,
        user,
        ("awful", "ужасный, кошмарный", "adjective"),
        ("terrible", "ужасный", "adjective"),
        ("great", "отличный", "adjective"),
        ("good", "хороший", "adjective"),
        ("fine", "прекрасный", "adjective"),
    )
    card_by_id = {card.id: card for card in cards}

    questions = await build(services, settings).build_session(user)

    awful = next(q for q in questions if card_by_id[q.card_id].term == "awful")
    assert "ужасный" not in awful.options
    terrible = next(q for q in questions if card_by_id[q.card_id].term == "terrible")
    assert "ужасный, кошмарный" not in terrible.options


async def test_a_word_pair_yields_one_question(
    services: Services, user: User, settings: Settings
) -> None:
    """Practice works on forward cards: the reverse twin is not a second word."""
    forward = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать")
    )
    twin = await services.cards.find_twin(forward)
    assert twin is not None
    await services.review_service.answer(user, forward, ReviewRating.GOOD)
    await services.review_service.answer(user, twin, ReviewRating.GOOD)
    await seed_cards(
        services,
        user,
        ("swim", "плавать", "verb"),
        ("fly", "летать", "verb"),
        ("go", "идти", "verb"),
    )

    questions = await build(services, settings).build_session(user)

    run_questions = [question for question in questions if question.term == "run"]
    assert len(run_questions) == 1


async def test_duplicate_translations_never_appear_twice(
    services: Services, user: User, settings: Settings
) -> None:
    cards = await seed_cards(
        services,
        user,
        ("run", "бежать", "verb"),
        ("dash", "бежать", "verb"),
        ("swim", "плавать", "verb"),
        ("fly", "летать", "verb"),
        ("go", "идти", "verb"),
    )
    card_by_id = {card.id: card for card in cards}

    questions = await build(services, settings).build_session(user)

    run = next(question for question in questions if card_by_id[question.card_id].term == "run")
    assert len(set(run.options)) == 4
    assert "бежать" not in [
        option for option in run.options if option != run.options[run.correct_index]
    ]


async def test_too_few_cards_returns_empty_and_unavailable(
    services: Services, user: User, settings: Settings
) -> None:
    await seed_cards(
        services,
        user,
        ("one", "первый", "noun"),
        ("two", "второй", "noun"),
        ("three", "третий", "noun"),
    )
    practice = build(services, settings)

    assert await practice.build_session(user) == []
    assert await practice.available(user) is False


async def test_the_session_is_capped_by_the_configured_size(
    services: Services, user: User, settings: Settings
) -> None:
    await seed_cards(
        services,
        user,
        *[(f"t{i}", f"перевод{i}", "noun") for i in range(12)],
    )

    questions = await build(services, settings).build_session(user)

    assert len(questions) == settings.bot.practice_size == 10
