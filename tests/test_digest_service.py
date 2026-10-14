"""Window arithmetic for the digest and the heatmap."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.models.review import Review
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.digest import heatmap_days, local_midnight
from tests.factories import schedule


def test_local_midnight_before_the_spring_forward_is_winter_time() -> None:
    """Berlin switches at 02:00 on 2026-03-29; that day still starts at CET."""
    assert local_midnight("Europe/Berlin", date(2026, 3, 29)) == datetime(
        2026, 3, 28, 23, 0, tzinfo=UTC
    )


def test_local_midnight_after_the_spring_forward_is_summer_time() -> None:
    assert local_midnight("Europe/Berlin", date(2026, 3, 30)) == datetime(
        2026, 3, 29, 22, 0, tzinfo=UTC
    )


async def test_heatmap_cells_follow_the_local_calendar_across_a_dst_switch(
    services: Services,
    session: AsyncSession,
    user: User,
) -> None:
    """23:30 and 00:30 local around the switch land in different cells."""
    user.timezone = "Europe/Berlin"
    deck = await services.card_service.default_deck(user)
    card = await services.cards.add(
        user_id=user.id,
        deck_id=deck.id,
        draft=CardDraft(term="run", translation="бежать"),
        schedule=schedule(due_in_days=7, state=CardState.REVIEW, reps=1),
    )
    for reviewed_at in (
        datetime(2026, 3, 29, 21, 30, tzinfo=UTC),  # 23:30 CEST, still the 29th
        datetime(2026, 3, 29, 22, 30, tzinfo=UTC),  # 00:30 CEST, already the 30th
    ):
        await services.reviews.add(
            Review(
                card_id=card.id,
                user_id=user.id,
                rating=ReviewRating.GOOD,
                reviewed_at=reviewed_at,
                state_before=CardState.REVIEW,
                state_after=CardState.REVIEW,
            )
        )
    await session.commit()

    days = await heatmap_days(services.reviews, user, now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC))

    assert days[-2] == 1
    assert days[-1] == 1
