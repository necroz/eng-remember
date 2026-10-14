"""Word discovery: what the model proposes, and what survives the checks."""

from __future__ import annotations

import pytest

from eng_remember.config import DiscoverySettings
from eng_remember.errors import LlmNotConfiguredError
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.discovery import DiscoveryService, appears_in_text
from eng_remember.services.llm.explorer import WordExplorer
from eng_remember.services.llm.prompts import build_expansion_prompt
from tests.factories import ScriptedClient, card_payload, expansion_payload

PARAGRAPH = (
    "The committee spent three weeks negotiating the terms of the settlement. "
    "A preliminary agreement was reached, but the reluctance of both sides to "
    "compromise on liability remained obvious throughout the deliberations."
)


@pytest.fixture
def discovery(llm_services: Services) -> DiscoveryService:
    return llm_services.discovery_service


class TestAppearsInText:
    @pytest.mark.parametrize(
        "term",
        ["committee", "settlement", "reluctance", "negotiating", "liability"],
    )
    def test_words_in_the_text_are_accepted(self, term: str) -> None:
        assert appears_in_text(term, PARAGRAPH)

    def test_a_lemma_is_accepted_for_an_inflected_form(self) -> None:
        """The text says "negotiating"; the dictionary form is the useful card."""
        assert appears_in_text("negotiate", PARAGRAPH)

    def test_the_other_direction_works_too(self) -> None:
        assert appears_in_text("deliberating", PARAGRAPH)

    @pytest.mark.parametrize("term", ["ubiquitous", "elephant", "spaceship"])
    def test_words_absent_from_the_text_are_rejected(self, term: str) -> None:
        assert not appears_in_text(term, PARAGRAPH)

    def test_case_does_not_matter(self) -> None:
        assert appears_in_text("COMMITTEE", PARAGRAPH.lower())

    def test_a_collocation_needs_every_part(self) -> None:
        assert appears_in_text("terms of the settlement", PARAGRAPH)
        assert not appears_in_text("terms of the spaceship", PARAGRAPH)

    def test_short_words_still_match(self) -> None:
        text = "He had to go home and eat."
        assert appears_in_text("go", text)
        assert appears_in_text("eat", text)
        assert not appears_in_text("fly", text)

    def test_empty_input_is_not_a_match(self) -> None:
        assert not appears_in_text("", PARAGRAPH)
        assert not appears_in_text("run", "")
        assert not appears_in_text("run", "нет латинских слов")


class TestDiscovery:
    async def test_suggestions_are_returned(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(
            expansion_payload(
                [card_payload("fever", "жар"), card_payload("cough", "кашель")],
                topic="медицина",
            )
        )

        result = await discovery.expand(user, topic="медицина", count=2)

        assert [draft.term for draft in result.drafts] == ["fever", "cough"]
        assert result.topic == "медицина"
        assert result.found == 2
        assert result.short_by == 0

    async def test_the_count_is_clamped_to_the_configured_maximum(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(expansion_payload([card_payload("a", "а")]))

        result = await discovery.expand(user, topic="медицина", count=9999)

        assert result.requested == discovery._settings.max_count

    async def test_asking_for_nothing_gives_the_default(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(expansion_payload([card_payload("a", "а")]))

        result = await discovery.expand(user, topic="медицина", count=None)

        assert result.requested == discovery._settings.default_count

    async def test_words_already_in_the_collection_are_dropped(
        self, discovery: DiscoveryService, llm_services: Services, user: User, llm: ScriptedClient
    ) -> None:
        await llm_services.card_service.add_draft(user, CardDraft(term="fever", translation="жар"))
        await llm_services.commit()
        llm.push_expand(
            expansion_payload(
                [card_payload("fever", "жар"), card_payload("cough", "кашель")],
                topic="медицина",
            )
        )

        result = await discovery.expand(user, topic="медицина", count=2)

        assert [draft.term for draft in result.drafts] == ["cough"]
        assert result.dropped_known == 1

    async def test_seed_words_are_dropped(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(
            expansion_payload([card_payload("fever", "жар"), card_payload("cough", "кашель")])
        )

        result = await discovery.expand(user, topic="медицина", seeds=["fever"], count=2)

        assert [draft.term for draft in result.drafts] == ["cough"]

    async def test_explicit_exclusions_are_dropped_without_being_mentioned(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        """The "more" button: rejected words must not anchor the next answer."""
        llm.push_expand(
            expansion_payload([card_payload("fever", "жар"), card_payload("cough", "кашель")])
        )

        result = await discovery.expand(user, topic="медицина", count=2, exclude=["fever"])

        assert [draft.term for draft in result.drafts] == ["cough"]
        assert "fever" not in llm.last_prompt()

    async def test_repeats_inside_one_answer_are_dropped(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(
            expansion_payload(
                [
                    card_payload("fever", "жар"),
                    card_payload("Fever", "лихорадка"),
                    card_payload("cough", "кашель"),
                ]
            )
        )

        result = await discovery.expand(user, topic="медицина", count=3)

        assert [draft.term for draft in result.drafts] == ["fever", "cough"]
        assert result.dropped_repeated == 1

    async def test_the_result_is_cut_to_the_requested_count(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(expansion_payload([card_payload(f"word{i}", "слово") for i in range(20)]))

        result = await discovery.expand(user, topic="медицина", count=5)

        assert result.found == 5

    async def test_more_than_asked_is_requested_from_the_model(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        """Both filters only ever remove, so a margin is needed."""
        llm.push_expand(expansion_payload([card_payload("a", "а")]))

        await discovery.expand(user, topic="медицина", count=10)

        assert "Find 14 English words." in llm.last_prompt()

    async def test_words_not_in_the_text_are_dropped(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(
            expansion_payload(
                [
                    card_payload("settlement", "урегулирование"),
                    card_payload("ubiquitous", "вездесущий"),
                    card_payload("reluctance", "нежелание"),
                ]
            )
        )

        result = await discovery.expand(user, context_text=PARAGRAPH, count=3)

        assert [draft.term for draft in result.drafts] == ["settlement", "reluctance"]
        assert result.dropped_out_of_text == 1
        assert result.short_by == 1

    async def test_the_text_reaches_the_prompt(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(expansion_payload([]))

        await discovery.expand(user, context_text=PARAGRAPH, count=3)

        prompt = llm.last_prompt()
        assert "settlement" in prompt
        assert "only" in prompt.lower()

    async def test_known_words_are_named_in_the_prompt(
        self, discovery: DiscoveryService, llm_services: Services, user: User, llm: ScriptedClient
    ) -> None:
        await llm_services.card_service.add_draft(user, CardDraft(term="fever", translation="жар"))
        await llm_services.commit()
        llm.push_expand(expansion_payload([]))

        await discovery.expand(user, topic="медицина", count=3)

        assert "fever" in llm.last_prompt()

    async def test_an_empty_answer_is_not_an_error(
        self, discovery: DiscoveryService, user: User, llm: ScriptedClient
    ) -> None:
        llm.push_expand(expansion_payload([]))

        result = await discovery.expand(user, topic="медицина", count=10)

        assert result.drafts == []
        assert result.short_by == 10

    async def test_without_a_model_discovery_refuses(self, services: Services, user: User) -> None:
        """Refusing beats returning an empty list: the messages differ."""
        service = DiscoveryService(
            cards=services.cards,
            explorer=WordExplorer(None),
            settings=DiscoverySettings(),
        )

        assert not service.available
        with pytest.raises(LlmNotConfiguredError):
            await service.expand(user, topic="медицина")

    def test_count_clamping_is_explicit(self) -> None:
        service = DiscoveryService(
            cards=None,  # type: ignore[arg-type]
            explorer=WordExplorer(None),
            settings=DiscoverySettings(default_count=10, max_count=30),
        )

        assert service.resolve_count(None) == 10
        assert service.resolve_count(0) == 1
        assert service.resolve_count(-5) == 1
        assert service.resolve_count(30) == 30
        assert service.resolve_count(500) == 30


class TestLevelGuidance:
    def test_the_topic_prompt_names_the_learners_level(self) -> None:
        prompt = build_expansion_prompt(
            topic="медицина",
            context_text=None,
            seeds=[],
            count=5,
            known=[],
            level="A2",
        )

        assert "Learner level: A2" in prompt
        assert "far above" in prompt

    def test_the_default_level_keeps_the_previous_behaviour(self) -> None:
        prompt = build_expansion_prompt(
            topic="медицина",
            context_text=None,
            seeds=[],
            count=5,
            known=[],
        )

        assert "Learner level: B1-B2" in prompt

    def test_a_source_text_does_not_get_level_guidance(self) -> None:
        prompt = build_expansion_prompt(
            topic="",
            context_text=PARAGRAPH,
            seeds=[],
            count=5,
            known=[],
            level="A2",
        )

        assert "Learner level" not in prompt

    async def test_the_learners_level_reaches_the_model(
        self, llm_services: Services, llm: ScriptedClient, user: User
    ) -> None:
        user.level = "B1"
        await llm_services.commit()
        llm.push_expand(expansion_payload([card_payload("run", "бежать")], topic="движение"))

        await llm_services.discovery_service.expand(user, topic="движение", count=1)

        assert "Learner level: B1" in llm.calls[-1]["user"]
