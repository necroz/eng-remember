"""Read-only views: decks and statistics, plus the paused-words list."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import keyboards, rendering, texts
from eng_remember.bot.callbacks import PauseCB
from eng_remember.models.user import User
from eng_remember.services.container import Services
from eng_remember.services.digest import heatmap_days


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
        heatmap = await heatmap_days(services.reviews, user)
        await services.commit()
        await message.answer(
            texts.stats(
                stats,
                user.timezone,
                services.settings.srs.desired_retention,
                streak=streak.state,
                heatmap=heatmap,
            ),
            reply_markup=keyboards.main_menu(),
        )

    @router.message(Command("paused"))
    async def list_paused(message: Message, user: User, services: Services) -> None:
        cards = await services.cards.list_suspended(user.id)
        total = await services.cards.count_suspended(user.id)
        if not cards:
            await message.answer(texts.paused_empty(), reply_markup=keyboards.main_menu())
            return
        await message.answer(
            texts.paused_list(cards, total),
            reply_markup=keyboards.paused_cards(cards),
        )

    @router.callback_query(PauseCB.filter(F.action == "resume"))
    async def resume_paused(
        query: CallbackQuery,
        callback_data: PauseCB,
        bot: Bot,
        user: User,
        services: Services,
    ) -> None:
        card = await services.card_service.suspend(user, callback_data.card_id, suspended=False)
        if card is None:
            await query.answer(texts.card_unavailable(), show_alert=True)
            return

        await services.commit()
        await query.answer()
        cards = await services.cards.list_suspended(user.id)
        total = await services.cards.count_suspended(user.id)
        if not cards:
            await rendering.edit_or_send(query, texts.paused_empty(), bot=bot)
            return
        await rendering.edit_or_send(
            query,
            texts.paused_list(cards, total),
            bot=bot,
            reply_markup=keyboards.paused_cards(cards),
        )

    return router
