"""``/start``, ``/help`` and the main menu entry point."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
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
    router = Router(name="start")

    @router.message(CommandStart())
    async def cmd_start(
        message: Message,
        user: User,
        services: Services,
        state: FSMContext,
        is_new_user: bool,
    ) -> None:
        await state.clear()
        deck = await services.card_service.default_deck(user)

        if is_new_user:
            await message.answer(
                texts.welcome_new(user.first_name, deck.name),
                reply_markup=keyboards.main_menu(),
            )
            return

        stats = await services.card_service.stats(user)
        streak = await services.streak_service.current(user)
        await services.commit()
        await message.answer(
            texts.welcome_back(user.first_name, stats, streak.state),
            reply_markup=keyboards.main_menu(),
        )

    @router.message(Command("help"))
    @router.message(Command("menu"))
    async def cmd_help(message: Message, user: User, services: Services) -> None:
        deck = await services.card_service.default_deck(user)
        await message.answer(
            texts.welcome_new(user.first_name, deck.name),
            reply_markup=keyboards.main_menu(),
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменил.", reply_markup=keyboards.main_menu())

    return router
