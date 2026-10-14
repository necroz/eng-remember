"""The review loop.

Flow: show the front of a due card → the learner types the translation (or taps
«Не знаю») → the answer is checked and graded, and the card with the verdict is
revealed in place → the next card is sent as a new message, so the previous
result stays in the chat history instead of being overwritten.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import keyboards, rendering, texts
from eng_remember.bot.callbacks import ReviewCB
from eng_remember.bot.rendering import callback_chat_id
from eng_remember.bot.states import (
    ANSWERED_KEY,
    ANSWERING_KEY,
    FRONT_MESSAGE_KEY,
    SESSION_KEY,
    SHOWN_AT_KEY,
    SHOWN_CARD_KEY,
    Review,
)
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.services.container import Services
from eng_remember.utils.time import ensure_utc, utcnow

log = structlog.get_logger(__name__)

# Telegram timestamps carry whole seconds while the stored moment keeps
# microseconds, so an answer sent right after the front can look up to a second
# older than it. Anything beyond that was typed before this card appeared.
CLOCK_TOLERANCE = timedelta(seconds=1)


def build_router() -> Router:
    """Create the router and register its handlers.

    A factory rather than a module-level singleton: aiogram refuses to
    attach one Router to two Dispatchers, which would make a second
    dispatcher (a test harness, a second bot) impossible.
    """
    router = Router(name="review")

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
        await state.update_data({ANSWERED_KEY: 0, SESSION_KEY: utcnow().isoformat()})

        queue = await services.review_service.build_queue(user)
        if not queue.cards:
            next_due = await services.cards.next_due_at(user.id, now=utcnow())
            await message.answer(texts.nothing_due(next_due), reply_markup=keyboards.main_menu())
            return

        await _send_front(bot, message.chat.id, queue.cards[0], state)

    @router.message(Review.awaiting_answer, Command("add"))
    @router.message(Review.awaiting_answer, F.text == texts.BTN_ADD)
    async def pause_for_add(_message: Message, state: FSMContext) -> None:
        """Adding words is leaving the review, not answering it.

        The state is dropped so the next typed message cannot be mistaken for an
        answer to a card that is no longer on screen, and the event is passed on
        to the capture router. The card stays due: «Повторять» brings it back.
        """
        await state.clear()
        raise SkipHandler

    @router.message(
        Review.awaiting_answer,
        F.text,
        ~F.text.startswith("/"),
        ~F.text.in_(texts.MAIN_MENU_BUTTONS),
    )
    async def answer_with_text(
        message: Message,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        data = await state.get_data()
        card_id = data.get(SHOWN_CARD_KEY)
        if not isinstance(card_id, int):
            return
        if data.get(ANSWERING_KEY) is not None:
            await message.answer(texts.already_checking())
            return

        sent_at = ensure_utc(message.date)
        shown_at = _parse_iso(data.get(SHOWN_AT_KEY))
        if sent_at is not None and shown_at is not None and sent_at + CLOCK_TOLERANCE < shown_at:
            # Typed while the previous card was still on screen: that answer was
            # already recorded, and grading this text against the new card would
            # corrupt both schedules.
            return

        await state.update_data({ANSWERING_KEY: card_id})
        card = await services.cards.get(card_id, user_id=user.id)
        if card is None:
            await message.answer(texts.card_unavailable())
            await _advance(bot, message.chat.id, user, services, state)
            return

        await _complete(
            bot=bot,
            chat_id=message.chat.id,
            user=user,
            services=services,
            state=state,
            card=card,
            typed=text,
            duration_seconds=_seconds_between(shown_at, sent_at),
        )

    @router.callback_query(ReviewCB.filter(F.action == "dont_know"))
    async def dont_know(
        query: CallbackQuery,
        callback_data: ReviewCB,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        if (
            await state.get_state() != Review.awaiting_answer.state
            or data.get(SHOWN_CARD_KEY) != callback_data.card_id
            or data.get(ANSWERING_KEY) is not None
        ):
            await query.answer(texts.stale_button(), show_alert=True)
            return

        shown_at = _parse_iso(data.get(SHOWN_AT_KEY))
        await state.update_data({ANSWERING_KEY: callback_data.card_id})

        card = await services.cards.get(callback_data.card_id, user_id=user.id)
        if card is None:
            await query.answer(texts.card_unavailable(), show_alert=True)
            await _advance(bot, callback_chat_id(query), user, services, state)
            return

        await query.answer()
        await _complete(
            bot=bot,
            chat_id=callback_chat_id(query),
            user=user,
            services=services,
            state=state,
            card=card,
            typed=None,
            duration_seconds=_seconds_between(shown_at, utcnow()),
        )

    # Registered last and matched by the raw prefix: buttons left in the chat
    # history by the previous flow no longer parse into ReviewCB, and an
    # unanswered spinner is exactly the silence the rest of the bot avoids.
    @router.callback_query(F.data.startswith("rv:"))
    async def legacy_button(query: CallbackQuery) -> None:
        await query.answer(texts.stale_button(), show_alert=True)

    async def _send_front(bot: Bot, chat_id: int, card: Card, state: FSMContext) -> None:
        await state.set_state(Review.awaiting_answer)
        await state.update_data(
            {
                SHOWN_CARD_KEY: card.id,
                SHOWN_AT_KEY: utcnow().isoformat(),
                ANSWERING_KEY: None,
            }
        )
        sent = await bot.send_message(
            chat_id,
            texts.card_front(card),
            reply_markup=keyboards.review_prompt(card.id),
        )
        await state.update_data({FRONT_MESSAGE_KEY: sent.message_id})

    async def _complete(
        *,
        bot: Bot,
        chat_id: int,
        user: User,
        services: Services,
        state: FSMContext,
        card: Card,
        typed: str | None,
        duration_seconds: float | None,
    ) -> None:
        """Grade the answer, reveal the card and move on."""
        before = await state.get_data()
        outcome = await services.grading_service.grade(
            user, card, typed=typed, duration_seconds=duration_seconds
        )

        # While the model was checking, the learner may have paused the review,
        # restarted it or confirmed an import. The answer itself was given and is
        # recorded; re-arming this session over whatever exists now would scatter
        # a flow that started meanwhile.
        data = await state.get_data()
        if data.get(SESSION_KEY) != before.get(SESSION_KEY) or data.get(ANSWERING_KEY) != card.id:
            log.warning("answer_abandoned", user_id=user.id, card_id=card.id)
            return

        # Praise is chosen after the answer because reaching a round number
        # changes which bank it comes from.
        pick = services.praise_service.pick(
            user,
            rating=outcome.rating,
            milestone=outcome.answer.streak.milestone if outcome.answer.streak else None,
        )
        await services.commit()

        await state.update_data({ANSWERED_KEY: int(data.get(ANSWERED_KEY, 0)) + 1})

        streak = outcome.answer.streak.state if outcome.answer.streak else None
        result = texts.answer_result(
            card,
            verdict=outcome.verdict,
            typed=outcome.typed,
            praise=texts.praise_line(
                pick.text,
                term=card.term,
                interval=outcome.answer.interval_text,
                streak=streak.current if streak else 0,
            ),
            interval_text=outcome.answer.interval_text,
            streak=streak.current if streak else 0,
            notes=texts.streak_notes(outcome.answer.streak),
        )
        front_message = data.get(FRONT_MESSAGE_KEY)
        await rendering.edit_message_or_send(
            bot,
            chat_id=chat_id,
            message_id=front_message if isinstance(front_message, int) else None,
            text=result,
        )
        await _advance(bot, chat_id, user, services, state)

    async def _advance(
        bot: Bot, chat_id: int, user: User, services: Services, state: FSMContext
    ) -> None:
        next_card = await services.review_service.get_next(user)
        if next_card is None:
            answered = int((await state.get_data()).get(ANSWERED_KEY, 0))
            await state.clear()
            await bot.send_message(
                chat_id,
                texts.review_finished(answered),
                reply_markup=keyboards.main_menu(),
            )
            return
        await _send_front(bot, chat_id, next_card, state)

    def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
        if start is None or end is None:
            return None
        return max(0.0, (end - start).total_seconds())

    def _parse_iso(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None

    return router
