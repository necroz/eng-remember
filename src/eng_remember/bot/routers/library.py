"""Read-only views: decks and statistics, plus the paused-words list.

The decks command also owns the two ways a collection changes shape: renaming
and deleting a deck. Both take the deck id from the button, never from anything
the learner typed, so a stale button cannot reach a deck it was not drawn for.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from eng_remember.bot import keyboards, rendering, texts
from eng_remember.bot.callbacks import DeckCB, PauseCB
from eng_remember.bot.rendering import callback_chat_id, require_state
from eng_remember.bot.states import DECK_ID_KEY, DeckRename
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

    async def deck_overview(user: User, services: Services) -> tuple[str, InlineKeyboardMarkup]:
        """The list and the buttons that act on exactly what it shows."""
        decks = await services.decks.list_for_user(user.id)
        counts = await services.cards.count_by_deck(user.id)
        rows = [(deck.name, counts.get(deck.id, 0)) for deck in decks]
        return texts.deck_list(rows, sum(counts.values())), keyboards.deck_manager(decks)

    @router.message(Command("decks"))
    @router.message(F.text == texts.BTN_DECKS)
    async def list_decks(
        message: Message, user: User, services: Services, state: FSMContext
    ) -> None:
        # Tapping the menu button is leaving the rename dialogue, not answering
        # it: the name typed next would otherwise land on a deck the learner had
        # already walked away from.
        if await state.get_state() == DeckRename.awaiting_name.state:
            await state.clear()
        text, markup = await deck_overview(user, services)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(DeckCB.filter(F.action == "dl"))
    async def ask_before_deleting(
        query: CallbackQuery,
        callback_data: DeckCB,
        bot: Bot,
        user: User,
        services: Services,
    ) -> None:
        deck = await services.decks.get(callback_data.deck_id, user_id=user.id)
        if deck is None:
            await query.answer(texts.deck_unavailable(), show_alert=True)
            return
        if deck.is_default:
            await query.answer(texts.deck_default_undeletable(), show_alert=True)
            return

        counts = await services.cards.count_by_deck(user.id)
        await query.answer()
        await bot.send_message(
            callback_chat_id(query),
            texts.deck_delete_confirm(deck.name, counts.get(deck.id, 0)),
            reply_markup=keyboards.deck_delete_confirm(deck.id),
        )

    @router.callback_query(DeckCB.filter(F.action == "ok"))
    async def delete_deck(
        query: CallbackQuery,
        callback_data: DeckCB,
        bot: Bot,
        user: User,
        services: Services,
    ) -> None:
        deck = await services.decks.get(callback_data.deck_id, user_id=user.id)
        if deck is None:
            await query.answer(texts.deck_unavailable(), show_alert=True)
            return
        # Checked again rather than trusted from the question: the default flag
        # can change between drawing the button and confirming it.
        if deck.is_default:
            await query.answer(texts.deck_default_undeletable(), show_alert=True)
            return

        name = deck.name
        await services.decks.delete(deck)
        await services.commit()
        await query.answer(texts.deck_deleted(name))

        text, markup = await deck_overview(user, services)
        await rendering.edit_or_send(query, text, bot=bot, reply_markup=markup)

    @router.callback_query(DeckCB.filter(F.action == "dc"))
    async def keep_the_deck(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        await rendering.edit_or_send(query, texts.deck_delete_cancelled(), bot=bot)

    @router.callback_query(DeckCB.filter(F.action == "rn"))
    async def ask_for_a_new_name(
        query: CallbackQuery,
        callback_data: DeckCB,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        deck = await services.decks.get(callback_data.deck_id, user_id=user.id)
        if deck is None:
            await query.answer(texts.deck_unavailable(), show_alert=True)
            return

        await state.set_state(DeckRename.awaiting_name)
        await state.update_data({DECK_ID_KEY: deck.id})
        await query.answer()
        await bot.send_message(
            callback_chat_id(query),
            texts.deck_rename_prompt(deck.name),
            reply_markup=keyboards.deck_rename_cancel(deck.id),
        )

    @router.message(
        DeckRename.awaiting_name,
        F.text,
        ~F.text.startswith("/"),
        ~F.text.in_(texts.MAIN_MENU_BUTTONS),
    )
    async def rename_deck(
        message: Message,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        deck_id = data.get(DECK_ID_KEY)
        if not isinstance(deck_id, int):
            # Nothing to rename: the dialogue lost its subject. Dropping the
            # state is the honest answer; the text falls to the capture router.
            await state.clear()
            return

        deck = await services.decks.get(deck_id, user_id=user.id)
        if deck is None:
            await state.clear()
            await message.answer(texts.deck_unavailable())
            return

        name = (message.text or "").strip()
        if not name or len(name) > texts.DECK_NAME_LIMIT:
            await message.answer(texts.deck_name_invalid())
            return
        existing = await services.decks.get_by_name(user.id, name)
        if existing is not None and existing.id != deck.id:
            await message.answer(texts.deck_name_taken(name))
            return

        old_name = deck.name
        await services.decks.rename(deck, name)
        await services.commit()
        await state.clear()

        text, markup = await deck_overview(user, services)
        await message.answer(f"{texts.deck_renamed(old_name, name)}\n\n{text}", reply_markup=markup)

    @router.callback_query(DeckCB.filter(F.action == "rc"))
    async def cancel_rename(
        query: CallbackQuery,
        bot: Bot,
        state: FSMContext,
    ) -> None:
        if not await require_state(query, state, DeckRename.awaiting_name):
            return
        await state.clear()
        await query.answer()
        await rendering.edit_or_send(query, texts.deck_rename_cancelled(), bot=bot)

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
