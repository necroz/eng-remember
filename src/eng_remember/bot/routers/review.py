"""The review loop.

Flow: show the front of a due card → the user asks for the answer → four grade
buttons, each labelled with the interval it would produce → the answer is graded
and the next card is sent as a new message, so the previous result stays in the
chat history instead of being overwritten.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import keyboards, texts
from eng_remember.bot.callbacks import ReviewCB
from eng_remember.bot.rendering import callback_chat_id, edit_or_send
from eng_remember.bot.states import SHOWN_AT_KEY, SHOWN_CARD_KEY
from eng_remember.domain.enums import ReviewRating
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.services.container import Services
from eng_remember.utils.time import ensure_utc, utcnow


def build_router() -> Router:
    """Create the router and register its handlers.

    A factory rather than a module-level singleton: aiogram refuses to
    attach one Router to two Dispatchers, which would make a second
    dispatcher (a test harness, a second bot) impossible.
    """
    router = Router(name="review")

    POSTPONE_MINUTES = 10

    @router.message(Command("review"))
    @router.message(F.text == texts.BTN_REVIEW)
    async def start_review(
        message: Message,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        await state.clear()
        await state.update_data(answered=0)

        queue = await services.review_service.build_queue(user)
        if not queue.cards:
            next_due = await services.cards.next_due_at(user.id, now=utcnow())
            await message.answer(texts.nothing_due(next_due), reply_markup=keyboards.main_menu())
            return

        await _send_front(bot, message.chat.id, queue.cards[0], state)

    @router.callback_query(ReviewCB.filter(F.action == "show"))
    async def show_answer(
        query: CallbackQuery,
        callback_data: ReviewCB,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        card = await services.cards.get(callback_data.card_id, user_id=user.id)
        if card is None:
            await query.answer("Карточка больше не доступна", show_alert=True)
            return

        previews = await services.review_service.preview(card)
        await state.update_data({SHOWN_CARD_KEY: card.id, SHOWN_AT_KEY: utcnow().isoformat()})
        await edit_or_send(
            query,
            texts.card_back(card, previews),
            bot=bot,
            reply_markup=keyboards.review_rating(card.id, previews),
        )
        await query.answer()

    @router.callback_query(ReviewCB.filter(F.action == "grade"))
    async def grade_card(
        query: CallbackQuery,
        callback_data: ReviewCB,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        chat_id = callback_chat_id(query)
        card = await services.cards.get(callback_data.card_id, user_id=user.id)
        if card is None:
            await query.answer("Карточка больше не доступна", show_alert=True)
            return

        rating = ReviewRating(callback_data.rating)
        answer = await services.review_service.answer(
            user,
            card,
            rating,
            duration_ms=await _time_on_card(state, card),
        )
        # Praise is chosen after the answer because reaching a round number
        # changes which bank it comes from.
        pick = services.praise_service.pick(
            user,
            rating=rating,
            milestone=answer.streak.milestone if answer.streak else None,
        )
        await services.commit()

        answered = int((await state.get_data()).get("answered", 0)) + 1
        await state.update_data(answered=answered)

        streak = answer.streak.state if answer.streak else None
        await edit_or_send(
            query,
            texts.answer_accepted(
                texts.praise_line(
                    pick.text,
                    term=card.term,
                    interval=answer.interval_text,
                    streak=streak.current if streak else 0,
                ),
                answer.interval_text,
                streak=streak.current if streak else 0,
                notes=texts.streak_notes(answer.streak),
            ),
            bot=bot,
        )
        await query.answer()

        next_card = await services.review_service.get_next(user)
        if next_card is None:
            await state.clear()
            await bot.send_message(
                chat_id,
                texts.review_finished(answered),
                reply_markup=keyboards.main_menu(),
            )
            return

        await _send_front(bot, chat_id, next_card, state)

    @router.callback_query(ReviewCB.filter(F.action == "later"))
    async def postpone_card(
        query: CallbackQuery,
        callback_data: ReviewCB,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        chat_id = callback_chat_id(query)
        card = await services.cards.get(callback_data.card_id, user_id=user.id)
        if card is None:
            await query.answer("Карточка больше не доступна", show_alert=True)
            return

        await services.review_service.postpone(card, minutes=POSTPONE_MINUTES)
        await services.commit()
        await query.answer(f"Верну через {POSTPONE_MINUTES} мин")

        next_card = await services.review_service.get_next(user)
        if next_card is None:
            # A real timestamp, not None: the queue is empty, but the collection
            # is not — the card just postponed comes back in ten minutes.
            next_due = await services.cards.next_due_at(user.id, now=utcnow())
            await edit_or_send(query, texts.nothing_due(next_due), bot=bot)
            return
        await _send_front(bot, chat_id, next_card, state)

    @router.callback_query(ReviewCB.filter(F.action == "stop"))
    async def stop_review(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
        answered = int((await state.get_data()).get("answered", 0))
        await state.clear()
        await edit_or_send(query, texts.review_finished(answered), bot=bot)
        await query.answer()

    async def _send_front(bot: Bot, chat_id: int, card: Card, state: FSMContext) -> None:
        await state.update_data({SHOWN_CARD_KEY: card.id, SHOWN_AT_KEY: utcnow().isoformat()})
        await bot.send_message(
            chat_id,
            texts.card_front(card),
            reply_markup=keyboards.review_prompt(card.id),
        )

    async def _time_on_card(state: FSMContext, card: Card) -> int | None:
        """Milliseconds between showing the answer and grading it.

        Saved because FSRS can use review duration when its parameters are fitted
        later. It is missing whenever the flow started in an earlier session, which
        is fine — the column is nullable.
        """
        data = await state.get_data()
        if data.get(SHOWN_CARD_KEY) != card.id:
            return None
        shown_at = _parse_iso(data.get(SHOWN_AT_KEY))
        if shown_at is None:
            return None
        return max(0, int((utcnow() - shown_at).total_seconds() * 1000))

    def _parse_iso(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None

    return router
