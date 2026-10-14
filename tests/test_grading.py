from sqlalchemy import select

from eng_remember.domain.answers import Verdict
from eng_remember.domain.enums import ReviewRating
from eng_remember.errors import LlmQuotaExceededError, LlmResponseError
from eng_remember.models.card import Card
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.grading import AnswerGradingService
from eng_remember.services.llm.checker import AnswerChecker
from tests.factories import ScriptedClient


def build(
    services: Services, client: ScriptedClient | None, threshold: float = 5.0
) -> AnswerGradingService:
    return AnswerGradingService(
        reviews=services.review_service,
        checker=AnswerChecker(llm=client),
        easy_threshold_seconds=threshold,
    )


async def test_a_matching_answer_is_correct_without_the_model(
    services: Services, user: User, saved_card: Card
) -> None:
    client = ScriptedClient()
    outcome = await build(services, client).grade(
        user, saved_card, typed="Вездесущий!", duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.CORRECT
    assert outcome.rating is ReviewRating.EASY
    assert client.calls == []
    await services.commit()
    review = (await services.session.scalars(select(Review))).one()
    assert review.rating is ReviewRating.EASY
    assert review.duration_ms == 1000


async def test_an_escalated_synonym_is_correct(
    services: Services, user: User, saved_card: Card
) -> None:
    client = ScriptedClient().push_check({"verdict": "correct"})
    outcome = await build(services, client).grade(
        user, saved_card, typed="жуткий", duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.CORRECT
    assert outcome.rating is ReviewRating.EASY
    assert client.calls[-1]["purpose"] == "check_answer"


async def test_a_near_miss_is_hard(services: Services, user: User, saved_card: Card) -> None:
    client = ScriptedClient().push_check({"verdict": "almost"})
    outcome = await build(services, client).grade(
        user, saved_card, typed="вездесущный", duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.ALMOST
    assert outcome.rating is ReviewRating.HARD


async def test_a_slow_correct_answer_is_good(
    services: Services, user: User, saved_card: Card
) -> None:
    outcome = await build(services, None).grade(
        user, saved_card, typed="вездесущий", duration_seconds=10.0
    )

    assert outcome.rating is ReviewRating.GOOD


async def test_a_hint_caps_a_fast_correct_answer_at_good(
    services: Services, user: User, saved_card: Card
) -> None:
    outcome = await build(services, None).grade(
        user, saved_card, typed="вездесущий", duration_seconds=1.0, hint_used=True
    )

    assert outcome.verdict is Verdict.CORRECT
    assert outcome.rating is ReviewRating.GOOD


async def test_a_model_failure_falls_back_to_wrong(
    services: Services, user: User, saved_card: Card
) -> None:
    client = ScriptedClient().push_check(LlmResponseError("boom"))
    outcome = await build(services, client).grade(
        user, saved_card, typed="жуть", duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.WRONG
    assert outcome.rating is ReviewRating.AGAIN


async def test_a_spent_quota_falls_back_to_wrong(
    services: Services, user: User, saved_card: Card
) -> None:
    client = ScriptedClient().push_check(LlmQuotaExceededError("calls_per_hour limit reached"))
    outcome = await build(services, client).grade(
        user, saved_card, typed="жуть", duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.WRONG
    assert outcome.rating is ReviewRating.AGAIN


async def test_without_a_model_an_unmatched_answer_is_wrong(
    services: Services, user: User, saved_card: Card
) -> None:
    outcome = await build(services, None).grade(
        user, saved_card, typed="жуть", duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.WRONG
    assert outcome.rating is ReviewRating.AGAIN


async def test_a_skip_is_again_without_a_check(
    services: Services, user: User, saved_card: Card
) -> None:
    client = ScriptedClient()
    outcome = await build(services, client).grade(
        user, saved_card, typed=None, duration_seconds=1.0
    )

    assert outcome.verdict is Verdict.WRONG
    assert outcome.rating is ReviewRating.AGAIN
    assert outcome.typed is None
    assert client.calls == []


async def test_a_reverse_match_is_correct_without_the_model(services: Services, user: User) -> None:
    forward = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать")
    )
    twin = await services.cards.find_twin(forward)
    assert twin is not None

    outcome = await build(services, None).grade(user, twin, typed="Run!", duration_seconds=1.0)

    assert outcome.verdict is Verdict.CORRECT
    assert outcome.rating is ReviewRating.EASY


async def test_a_reverse_synonym_escalates_to_the_model(services: Services, user: User) -> None:
    forward = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать")
    )
    twin = await services.cards.find_twin(forward)
    assert twin is not None
    client = ScriptedClient().push_check({"verdict": "almost"})

    outcome = await build(services, client).grade(user, twin, typed="jog", duration_seconds=1.0)

    assert outcome.verdict is Verdict.ALMOST
    assert outcome.rating is ReviewRating.HARD
    assert "<expected_term>run</expected_term>" in client.calls[-1]["user"]
