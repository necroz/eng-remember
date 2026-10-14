"""One place that knows how the layers wire together.

Handlers ask for a :class:`Services` object and get repositories, the scheduler
and the LLM-facing services already connected to the current database session.
Tests build the same container around a session of their own, so there is no
second wiring path that can drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from eng_remember.config import Settings
from eng_remember.domain.srs import SrsScheduler
from eng_remember.repositories.cards import CardRepository
from eng_remember.repositories.decks import DeckRepository
from eng_remember.repositories.llm_calls import LlmCallRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.repositories.streaks import StreakRepository
from eng_remember.repositories.users import UserRepository
from eng_remember.services.cards import CardService
from eng_remember.services.discovery import DiscoveryService
from eng_remember.services.grading import AnswerGradingService
from eng_remember.services.llm.checker import AnswerChecker
from eng_remember.services.llm.explorer import WordExplorer
from eng_remember.services.llm.factory import build_llm_client
from eng_remember.services.llm.parser import MessageParser
from eng_remember.services.praise import PraiseService
from eng_remember.services.review import ReviewService
from eng_remember.services.streak import StreakService

__all__ = ["Services", "build_services"]


@dataclass(slots=True)
class Services:
    settings: Settings
    session: AsyncSession

    users: UserRepository
    decks: DeckRepository
    cards: CardRepository
    reviews: ReviewRepository
    llm_calls: LlmCallRepository
    streaks: StreakRepository

    scheduler: SrsScheduler
    card_service: CardService
    review_service: ReviewService
    answer_checker: AnswerChecker
    grading_service: AnswerGradingService
    streak_service: StreakService
    praise_service: PraiseService
    discovery_service: DiscoveryService

    parser: MessageParser
    explorer: WordExplorer

    async def flush(self) -> None:
        """Push pending changes to the database without ending the transaction."""
        await self.session.flush()

    async def commit(self) -> None:
        """Commit early, e.g. before telling the user the data was saved."""
        await self.session.commit()


def build_services(session: AsyncSession, settings: Settings) -> Services:
    users = UserRepository(session)
    decks = DeckRepository(session)
    cards = CardRepository(session)
    reviews = ReviewRepository(session)
    llm_calls = LlmCallRepository(session)
    streaks = StreakRepository(session)

    scheduler = SrsScheduler(settings.srs)

    # One client for every LLM-facing service, so a per-call audit sink and the
    # retry policy cannot differ between the parser and the explorer.
    llm = build_llm_client(session, settings)
    parser = MessageParser(llm=llm)
    explorer = WordExplorer(llm)

    streak_service = StreakService(streaks=streaks, settings=settings.streak)
    review_service = ReviewService(
        cards=cards,
        reviews=reviews,
        scheduler=scheduler,
        streak=streak_service,
        reviews_per_session=settings.bot.reviews_per_session,
        new_cards_per_day=settings.bot.new_cards_per_day,
    )
    answer_checker = AnswerChecker(llm)

    return Services(
        settings=settings,
        session=session,
        users=users,
        decks=decks,
        cards=cards,
        reviews=reviews,
        llm_calls=llm_calls,
        streaks=streaks,
        scheduler=scheduler,
        card_service=CardService(
            cards=cards,
            decks=decks,
            reviews=reviews,
            scheduler=scheduler,
            default_deck_name=settings.bot.default_deck_name,
        ),
        review_service=review_service,
        answer_checker=answer_checker,
        grading_service=AnswerGradingService(
            reviews=review_service,
            checker=answer_checker,
            easy_threshold_seconds=settings.review.easy_threshold_seconds,
        ),
        streak_service=streak_service,
        praise_service=PraiseService(),
        discovery_service=DiscoveryService(
            cards=cards,
            explorer=explorer,
            settings=settings.discovery,
        ),
        parser=parser,
        explorer=explorer,
    )
