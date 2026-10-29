"""The enrichment service and the background backfill worker."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.config import Settings
from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.errors import LlmResponseError
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.enrichment import EnrichmentService
from eng_remember.services.enrichment_worker import _enrich_card
from eng_remember.services.llm.enricher import CardEnricher
from tests.factories import ScriptedClient


class FakeEnricher(CardEnricher):
    """A CardEnricher whose answers the test writes down in advance."""

    def __init__(self, *, examples: list[str] | None = None, mnemonic: str = "мнемоника") -> None:
        super().__init__(llm=None)
        self._answers = list(examples or [])
        self._mnemonic = mnemonic
        self.calls: list[tuple[str, str, int, tuple[str, ...], int | None]] = []
        self.languages: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def examples(
        self,
        *,
        term: str,
        translation: str,
        count: int,
        existing: Sequence[str],
        user_id: int | None = None,
        language: str = DEFAULT_LANGUAGE,
    ) -> list[str]:
        self.calls.append(("examples", term, count, tuple(existing), user_id))
        self.languages.append(language)
        return self._answers[:count]

    async def mnemonic(
        self,
        *,
        term: str,
        translation: str,
        user_id: int | None = None,
        language: str = DEFAULT_LANGUAGE,
    ) -> str:
        self.calls.append(("mnemonic", term, 0, (), user_id))
        self.languages.append(language)
        return self._mnemonic


@pytest.fixture
def enricher() -> FakeEnricher:
    return FakeEnricher(examples=["New one.", "New two."])


@pytest.fixture
def enrichment(services: Services, enricher: FakeEnricher, settings: Settings) -> EnrichmentService:
    return EnrichmentService(cards=services.cards, enricher=enricher, settings=settings)


async def test_enrich_embeds_the_existing_example_first(
    services: Services, user: User, enrichment: EnrichmentService, enricher: FakeEnricher
) -> None:
    card = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать", example="My own sentence.")
    )

    added = await enrichment.enrich_examples(card=card, user_id=user.id)

    assert added == ["New one.", "New two."]
    assert card.examples == ["My own sentence.", "New one.", "New two."]
    assert card.example == "My own sentence."
    assert enricher.calls == [("examples", "run", 2, ("My own sentence.",), user.id)]


async def test_enrichment_passes_the_card_language_to_the_enricher(
    services: Services, user: User, enrichment: EnrichmentService, enricher: FakeEnricher
) -> None:
    card = await services.card_service.add_draft(
        user, CardDraft(term="bonjour", translation="здравствуйте", language="fra")
    )

    await enrichment.enrich_examples(card=card, user_id=user.id)
    await enrichment.add_mnemonic(card=card, user_id=user.id)

    assert enricher.languages == ["fra", "fra"]


async def test_enrich_does_nothing_when_the_target_is_already_met(
    services: Services, user: User, enrichment: EnrichmentService, enricher: FakeEnricher
) -> None:
    card = await services.card_service.add_draft(
        user,
        CardDraft(term="run", translation="бежать", examples=("One.", "Two.", "Three.")),
    )

    assert await enrichment.enrich_examples(card=card, user_id=user.id) == []
    assert enricher.calls == []


async def test_the_backfill_worker_skips_full_and_vanished_cards(
    services: Services,
    user: User,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    card = await services.card_service.add_draft(
        user,
        CardDraft(term="run", translation="бежать", examples=("One.", "Two.", "Three.")),
    )
    await services.commit()

    assert await _enrich_card(session_factory, settings, user.id, card.id) is False
    assert await _enrich_card(session_factory, settings, user.id, 999_999) is False


async def test_the_backfill_worker_writes_generated_examples(
    llm: ScriptedClient,
    services: Services,
    user: User,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm.push_examples({"examples": ["He runs fast."]})
    card = await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))
    await services.commit()

    assert await _enrich_card(session_factory, settings, user.id, card.id) is True

    async with session_factory() as check:
        reloaded = await check.get(Card, card.id)
    assert reloaded is not None
    assert reloaded.examples == ["He runs fast."]


async def test_the_backfill_worker_survives_enricher_errors(
    llm: ScriptedClient,
    services: Services,
    user: User,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm.push_examples(RuntimeError("boom"))
    card = await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))
    await services.commit()

    assert await _enrich_card(session_factory, settings, user.id, card.id) is False


async def test_add_example_appends_one(services: Services, user: User, settings: Settings) -> None:
    service = EnrichmentService(
        cards=services.cards, enricher=FakeEnricher(examples=["Fresh."]), settings=settings
    )
    card = await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))

    added = await service.add_example(card=card, user_id=user.id)

    assert added == "Fresh."
    assert card.examples == ["Fresh."]
    assert card.example == "Fresh."


async def test_add_example_raises_when_the_model_returns_nothing(
    services: Services, user: User, settings: Settings
) -> None:
    service = EnrichmentService(
        cards=services.cards, enricher=FakeEnricher(examples=[]), settings=settings
    )
    card = await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))

    with pytest.raises(LlmResponseError):
        await service.add_example(card=card, user_id=user.id)


async def test_mnemonic_joins_notes_or_sets_them(
    services: Services, user: User, settings: Settings
) -> None:
    service = EnrichmentService(
        cards=services.cards, enricher=FakeEnricher(mnemonic="мнемоника"), settings=settings
    )
    with_notes = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать", notes="про предлог")
    )
    without_notes = await services.card_service.add_draft(
        user, CardDraft(term="jump", translation="прыгать")
    )

    assert await service.add_mnemonic(card=with_notes, user_id=user.id) == "мнемоника"
    assert with_notes.notes == "про предлог · мнемоника"

    await service.add_mnemonic(card=without_notes, user_id=user.id)
    assert without_notes.notes == "мнемоника"
