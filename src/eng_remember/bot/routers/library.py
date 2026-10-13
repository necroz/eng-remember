"""Read-only views: decks and statistics."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from eng_remember.bot import keyboards, texts
from eng_remember.models.user import User
from eng_remember.services.container import Services


def build_router() -> Router:
    """Create the router and register its handlers.

    A factory rather than a module-level singleton: aiogram refuses to
    attach one Router to two Dispatchers, which would make a second
    dispatcher (a test harness, a second bot) impossible.
    """
    router = Router(name="library")

    @router.message(Command("decks"))
    @router.message(F.text == texts.BTN_DECKS)
    async def list_decks(message: Message, user: User, services: Services) -> None:
        decks = await services.decks.list_for_user(user.id)
        counts = await services.cards.count_by_deck(user.id)
        rows = [(deck.name, counts.get(deck.id, 0)) for deck in decks]
        await message.answer(
            texts.deck_list(rows, sum(counts.values())),
            reply_markup=keyboards.main_menu(),
        )

    @router.message(Command("stats"))
    @router.message(F.text == texts.BTN_STATS)
    async def show_stats(message: Message, user: User, services: Services) -> None:
        stats = await services.card_service.stats(user)
        # Read through the service, not the row: opening statistics after a gap
        # must settle the streak — freezes spent, streak broken — before it is
        # shown, or the number contradicts what the user knows.
        streak = await services.streak_service.current(user)
        await services.commit()
        await message.answer(
            texts.stats(
                stats,
                user.timezone,
                services.settings.srs.desired_retention,
                streak=streak.state,
            ),
            reply_markup=keyboards.main_menu(),
        )

    return router
