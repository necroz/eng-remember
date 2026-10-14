"""The practice quiz: multiple choice over the learner's own cards.

No database write happens anywhere in this flow — the questions are built once
at the start and carried in FSM storage, so a card answered mid-session cannot
change what the following questions ask, and FSRS is never touched.
"""

from __future__ import annotations

import secrets

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import keyboards, rendering, texts
from eng_remember.bot.callbacks import PracticeCB
from eng_remember.bot.rendering import callback_chat_id
from eng_remember.bot.states import (
    CORRECT_COUNT_KEY,
    PRACTICE_ANSWER_KEY,
    PRACTICE_TOKEN_KEY,
    QUESTION_INDEX_KEY,
    QUESTIONS_KEY,
    Practice,
)
from eng_remember.models.user import User
from eng_remember.services.container import Services
from eng_remember.services.practice import QuizQuestion

log = structlog.get_logger(__name__)

# Big enough that two sessions never share a token.
TOKEN_LIMIT = 10**9


def build_router() -> Router:
    """Create the router and register its handlers."""
    router = Router(name="practice")

    @router.message(Command("practice"))
    @router.message(F.text == texts.BTN_PRACTICE)
    async def start(
        message: Message, bot: Bot, user: User, services: Services, state: FSMContext
    ) -> None:
        await state.clear()
        await _begin(bot, message.chat.id, user, services, state)

    @router.callback_query(PracticeCB.filter(F.action == "start"))
    async def start_from_offer(
        query: CallbackQuery, bot: Bot, user: User, services: Services, state: FSMContext
    ) -> None:
        await query.answer()
        await state.clear()
        await _begin(bot, callback_chat_id(query), user, services, state)

    @router.callback_query(PracticeCB.filter(F.action == "answer"))
    async def answer(
        query: CallbackQuery,
        callback_data: PracticeCB,
        bot: Bot,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        if (
            await state.get_state() != Practice.awaiting_answer.state
            or data.get(PRACTICE_TOKEN_KEY) != callback_data.token
            or data.get(PRACTICE_ANSWER_KEY)
        ):
            await query.answer(texts.stale_button(), show_alert=True)
            return

        questions = _restore_questions(data.get(QUESTIONS_KEY))
        index = data.get(QUESTION_INDEX_KEY)
        if (
            not isinstance(index, int)
            or index != callback_data.question
            or not 0 <= index < len(questions)
        ):
            await query.answer(texts.stale_button(), show_alert=True)
            return

        question = questions[index]
        if not 0 <= callback_data.option < len(question.options):
            await query.answer(texts.stale_button(), show_alert=True)
            return

        # Set before the first await: a second tap arriving concurrently must
        # not count the same answer twice.
        await state.update_data({PRACTICE_ANSWER_KEY: True})

        correct = callback_data.option == question.correct_index
        count = int(data.get(CORRECT_COUNT_KEY, 0)) + int(correct)
        await state.update_data({CORRECT_COUNT_KEY: count})

        verdict = (
            texts.practice_correct(question.options[question.correct_index])
            if correct
            else texts.practice_wrong(question.options[question.correct_index])
        )
        await query.answer()
        await rendering.edit_or_send(query, verdict, bot=bot)

        next_index = index + 1
        if next_index >= len(questions):
            await state.clear()
            await bot.send_message(
                callback_chat_id(query),
                texts.practice_finished(count, len(questions)),
                reply_markup=keyboards.main_menu(),
            )
            return

        await state.update_data({QUESTION_INDEX_KEY: next_index, PRACTICE_ANSWER_KEY: False})
        await _send_question(
            bot,
            callback_chat_id(query),
            question=questions[next_index],
            token=callback_data.token,
            index=next_index,
        )

    return router


async def _begin(bot: Bot, chat_id: int, user: User, services: Services, state: FSMContext) -> None:
    questions = await services.practice.build_session(user)
    if not questions:
        await bot.send_message(
            chat_id, texts.practice_unavailable(), reply_markup=keyboards.main_menu()
        )
        return

    token = secrets.randbelow(TOKEN_LIMIT)
    await state.set_state(Practice.awaiting_answer)
    await state.update_data(
        {
            QUESTIONS_KEY: [_serialize(question) for question in questions],
            QUESTION_INDEX_KEY: 0,
            CORRECT_COUNT_KEY: 0,
            PRACTICE_TOKEN_KEY: token,
            PRACTICE_ANSWER_KEY: False,
        }
    )
    await _send_question(bot, chat_id, question=questions[0], token=token, index=0)


async def _send_question(
    bot: Bot, chat_id: int, *, question: QuizQuestion, token: int, index: int
) -> None:
    await bot.send_message(
        chat_id,
        texts.practice_question(question.term),
        reply_markup=keyboards.practice_options(question.options, token=token, question=index),
    )


def _serialize(question: QuizQuestion) -> dict[str, object]:
    """Plain data, not the dataclass: a Redis FSM storage must be possible."""
    return {
        "card_id": question.card_id,
        "term": question.term,
        "options": list(question.options),
        "correct": question.correct_index,
    }


def _restore_questions(raw: object) -> list[QuizQuestion]:
    """Rebuild questions from FSM storage, skipping anything malformed."""
    if not isinstance(raw, list):
        return []

    restored: list[QuizQuestion] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        card_id = item.get("card_id")
        term = item.get("term")
        options = item.get("options")
        correct = item.get("correct")
        if (
            not isinstance(card_id, int)
            or not isinstance(term, str)
            or not isinstance(options, list)
            or not isinstance(correct, int)
            or not all(isinstance(option, str) for option in options)
        ):
            continue
        restored.append(
            QuizQuestion(
                card_id=card_id,
                term=term,
                options=tuple(options),
                correct_index=correct,
            )
        )
    return restored
