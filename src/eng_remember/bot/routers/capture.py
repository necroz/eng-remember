"""The main input path: free text in, cards out.

Four ways a request can go:

* the user names words, and the parser turns them into cards;
* the user asks for *more* words — on a topic, or inside a text they pasted —
  and the explorer finds them;
* the user does both in one message — names a word to save and asks for words
  from the same domain, often attaching a phrase to pin the sense — and the
  result is offered as a single confirmable batch;
* the request is too vague to act on, and the bot asks one question first.

Everything a user types that is not a command or a menu button lands here. The
result is always shown for confirmation, and only an explicit tap writes to the
database — a misparse must never cost the user a wrong card they did not see
coming.

This router is registered last: routers are matched in order and the first match
wins, so specific buttons and commands get their chance first.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from eng_remember.bot import keyboards, texts
from eng_remember.bot.callbacks import ClarifyCB, ImportCB
from eng_remember.bot.rendering import callback_chat_id, edit_or_send
from eng_remember.bot.states import (
    CONTEXT_KEY,
    COUNT_KEY,
    DRAFT_KEY,
    ENGINE_KEY,
    LANGUAGE_KEY,
    OPTIONS_KEY,
    ORIGINAL_KEY,
    OWN_KEY,
    QUESTION_KEY,
    ROUND_KEY,
    TOPIC_KEY,
    AddWords,
)
from eng_remember.config import DiscoverySettings
from eng_remember.domain.languages import DEFAULT_LANGUAGE
from eng_remember.errors import (
    LlmError,
    LlmGlobalBudgetExceededError,
    LlmQuotaExceededError,
    ParseError,
)
from eng_remember.models.deck import Deck
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from eng_remember.services.discovery import DiscoveryResult
from eng_remember.services.llm.schema import Intent, ParseResponse

log = structlog.get_logger(__name__)


def build_router() -> Router:
    """Create the router and register its handlers.

    A factory rather than a module-level singleton: aiogram refuses to attach one
    Router to two Dispatchers, which would make a second dispatcher (a test
    harness, a second bot) impossible.
    """
    router = Router(name="capture")

    async def handle_request(
        *,
        text: str,
        bot: Bot,
        chat_id: int,
        user: User,
        services: Services,
        state: FSMContext,
        clarify: tuple[str, str, int] | None = None,
    ) -> None:
        """Parse ``text`` and route it to the right follow-up.

        ``clarify`` carries the question the user is answering, if any, so the
        parser can see the original request and the missing detail together.
        """
        try:
            if clarify is None:
                outcome = await services.parser.parse(text, user_id=user.id)
                round_number = 0
            else:
                original, question, round_number = clarify
                outcome = await services.parser.parse_clarified(
                    original=original,
                    question=question,
                    answer=text,
                    user_id=user.id,
                )
        except ParseError as exc:
            log.info("parse_failed", user_id=user.id, error=str(exc))
            await bot.send_message(chat_id, texts.cannot_parse(_llm_available(services)))
            return

        response = outcome.response
        settings = services.settings.discovery

        if response.clarification is not None and round_number < settings.max_clarification_rounds:
            await _ask_clarification(
                bot=bot,
                chat_id=chat_id,
                state=state,
                response=response,
                original=text if clarify is None else clarify[0],
                round_number=round_number,
            )
            return

        await state.clear()

        if response.intent is Intent.EXPLAIN and response.answer:
            await bot.send_message(
                chat_id,
                texts.explain(response.answer),
                reply_markup=keyboards.main_menu(),
            )
            return

        own = response.to_drafts()

        # One message can carry two requests: a word to save and a call for
        # words from the same domain ("добавь arrangement ... и ещё пару слов
        # на эту тему"). Running only the first was silently dropping half of
        # what the learner asked for.
        if response.intent is Intent.EXPAND or response.more_words:
            await run_discovery(
                bot=bot,
                chat_id=chat_id,
                user=user,
                services=services,
                state=state,
                response=response,
                source_text=text,
                settings=settings,
                own=own,
                engine=outcome.engine,
            )
            return

        if not own:
            await bot.send_message(chat_id, texts.cannot_parse(_llm_available(services)))
            return
        await _present(
            bot=bot,
            chat_id=chat_id,
            state=state,
            own=own,
            engine=outcome.engine,
            language=response.language,
        )

    async def run_discovery(
        *,
        bot: Bot,
        chat_id: int,
        user: User,
        services: Services,
        state: FSMContext,
        response: ParseResponse,
        source_text: str,
        settings: DiscoverySettings,
        exclude: tuple[str, ...] = (),
        own: Sequence[CardDraft] = (),
        engine: str = "",
        language: str | None = None,
    ) -> None:
        """Look for words the user does not have yet, then offer them.

        ``own`` are the words the learner named in this message. When there are
        any, they join the batch — the request was "add this word and find more
        like it" — and a search that cannot run degrades to a note under them
        instead of replacing the whole answer with an error.

        Whatever ``own`` holds also decides where the found words come from: a
        topic has to be read out of the request or away from its context, so a
        message that names no domain gets a question rather than a guess.
        """
        target = language or response.language
        own = list(own)
        seeds = [draft.term for draft in own]

        if not services.discovery_service.available:
            if own:
                await _present(
                    bot=bot,
                    chat_id=chat_id,
                    state=state,
                    own=own,
                    engine=engine,
                    note=texts.more_unavailable(),
                    language=target,
                )
            else:
                await bot.send_message(chat_id, texts.discovery_unavailable())
            return

        deck = await services.card_service.default_deck(user)
        # Words the learner typed make the message a request, not source
        # material: a phrase that came with them is context for the topic, and
        # mining it as text would throw away every word it does not contain.
        topic, context = _discovery_inputs(
            response,
            source_text if not own else "",
            deck,
            settings,
            allow_deck_topic=not own,
        )
        if topic is None and context is None:
            if own:
                await _present(
                    bot=bot,
                    chat_id=chat_id,
                    state=state,
                    own=own,
                    engine=engine,
                    note=texts.more_needs_topic(),
                    language=target,
                )
            else:
                await bot.send_message(chat_id, texts.discovery_needs_context())
            return

        try:
            result = await services.discovery_service.expand(
                user,
                topic=topic,
                context_text=context,
                seeds=seeds,
                count=response.count,
                exclude=exclude,
                language=target,
                user_id=user.id,
            )
        except LlmGlobalBudgetExceededError as exc:
            # The bot-wide budget is spent, not this learner's own one. The
            # manual path still works, so keep their own words and say why the
            # extra discovery did not run.
            log.info("discovery_global_budget_exceeded", user_id=user.id, error=str(exc))
            if own:
                await _present(
                    bot=bot,
                    chat_id=chat_id,
                    state=state,
                    own=own,
                    engine=engine,
                    note=texts.more_unavailable(),
                    language=target,
                )
            else:
                await bot.send_message(chat_id, texts.model_busy())
            return
        except LlmQuotaExceededError as exc:
            # A budget refusal is not a broken model; saying "connect a model"
            # here would send the learner looking for the wrong problem.
            log.info("discovery_quota_exceeded", user_id=user.id, error=str(exc))
            if own:
                await _present(
                    bot=bot,
                    chat_id=chat_id,
                    state=state,
                    own=own,
                    engine=engine,
                    note=texts.more_quota_exceeded(),
                    language=target,
                )
            else:
                await bot.send_message(chat_id, texts.quota_exceeded())
            return
        except LlmError as exc:
            log.warning("discovery_failed", user_id=user.id, error=str(exc))
            if own:
                await _present(
                    bot=bot,
                    chat_id=chat_id,
                    state=state,
                    own=own,
                    engine=engine,
                    note=texts.more_unavailable(),
                    language=target,
                )
            else:
                await bot.send_message(chat_id, texts.discovery_unavailable())
            return

        # Persist the audit rows before anything else can fail: a discovery that
        # returned nothing useful is exactly the call worth having a record of.
        await services.commit()

        if not result.drafts:
            if own:
                await _present(
                    bot=bot,
                    chat_id=chat_id,
                    state=state,
                    own=own,
                    engine=engine,
                    note=texts.more_found_nothing(),
                    language=target,
                )
            else:
                await bot.send_message(
                    chat_id, texts.discovery_empty(context_used=context is not None)
                )
            return

        await _present(
            bot=bot,
            chat_id=chat_id,
            state=state,
            own=own,
            engine=engine or result.engine,
            found=result,
            topic=result.topic,
            count=result.requested,
            context=context,
            deck_name=deck.name,
            language=target,
        )

    async def _ask_clarification(
        *,
        bot: Bot,
        chat_id: int,
        state: FSMContext,
        response: ParseResponse,
        original: str,
        round_number: int,
    ) -> None:
        clarification = response.clarification
        if clarification is None:  # pragma: no cover - guarded by the caller
            return

        await state.set_state(AddWords.clarifying)
        await state.update_data(
            {
                ORIGINAL_KEY: original,
                QUESTION_KEY: clarification.question,
                OPTIONS_KEY: clarification.options,
                ROUND_KEY: round_number,
                # Kept so "decide for me" still knows how many words were asked
                # for and what the topic was before the question interrupted.
                TOPIC_KEY: response.topic,
                COUNT_KEY: response.count,
                LANGUAGE_KEY: response.language,
            }
        )
        await bot.send_message(
            chat_id,
            texts.clarification(clarification.question),
            reply_markup=keyboards.clarification(clarification.options),
        )

    async def _present(
        *,
        bot: Bot,
        chat_id: int,
        state: FSMContext,
        own: list[CardDraft],
        engine: str,
        found: DiscoveryResult | None = None,
        note: str | None = None,
        topic: str | None = None,
        count: int | None = None,
        context: str | None = None,
        deck_name: str | None = None,
        language: str | None = None,
    ) -> None:
        """Show a batch for confirmation and remember it for the save button.

        The state keeps the learner's own words and the found ones apart, so
        "Ещё слова" can replace the second half without touching the first.
        What the button advertises is exactly what confirm will save.
        """
        details = list(found.drafts) if found is not None else []
        batch = [*own, *details]
        if not batch:  # pragma: no cover - callers never offer an empty batch
            await bot.send_message(chat_id, texts.nothing_to_save())
            return

        target = language or (found.language if found is not None else DEFAULT_LANGUAGE)
        await state.set_state(AddWords.confirming)
        await state.update_data(
            {
                DRAFT_KEY: [asdict(draft) for draft in details],
                OWN_KEY: [asdict(draft) for draft in own],
                ENGINE_KEY: engine,
                TOPIC_KEY: topic,
                COUNT_KEY: count,
                CONTEXT_KEY: context,
                LANGUAGE_KEY: target,
            }
        )

        if own:
            preview = texts.import_preview(own, engine, found=found, note=note)
        elif found is not None:
            preview = texts.discovery_preview(
                found,
                context_used=context is not None,
                deck_name=deck_name or "",
            )
        else:  # pragma: no cover - guarded by the batch check above
            preview = texts.import_preview([], engine, note=note)

        await bot.send_message(
            chat_id,
            preview,
            reply_markup=keyboards.import_confirm(len(batch), offer_more=found is not None),
        )

    @router.message(Command("add"))
    @router.message(F.text == texts.BTN_ADD)
    async def prompt_add(message: Message) -> None:
        await message.answer(
            "Напишите иностранное слово или сразу несколько.\n\n"
            "Примеры:\n"
            "• <code>bonjour — здравствуйте</code>\n"
            "• <code>书 — книга</code>\n"
            "• <code>resilient, thorough, ubiquitous</code>\n"
            "• <code>ещё 10 слов про медицину</code>\n"
            "• пришлите абзац текста — найду в нём слова\n\n"
            "Если язык не назван, латиница считается английской. Указать язык "
            "можно словами: «французское слово hotel — отель».\n\n"
            "Можно совместить: <code>arrangement как во фразе … и ещё пару слов "
            "на эту тему</code> — добавлю слово и подберу слова того же домена."
        )

    @router.message(F.text, ~F.text.startswith("/"))
    async def capture(
        message: Message,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        text = message.text or ""
        chat_id = message.chat.id

        if await state.get_state() == AddWords.clarifying.state:
            data = await state.get_data()
            await handle_request(
                text=text,
                bot=bot,
                chat_id=chat_id,
                user=user,
                services=services,
                state=state,
                clarify=(
                    str(data.get(ORIGINAL_KEY) or ""),
                    str(data.get(QUESTION_KEY) or ""),
                    int(data.get(ROUND_KEY, 0)) + 1,
                ),
            )
            return

        await handle_request(
            text=text,
            bot=bot,
            chat_id=chat_id,
            user=user,
            services=services,
            state=state,
        )

    @router.callback_query(ClarifyCB.filter(F.action == "option"))
    async def clarify_with_option(
        query: CallbackQuery,
        callback_data: ClarifyCB,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        options = data.get(OPTIONS_KEY) or []
        if not isinstance(options, list) or not 0 <= callback_data.index < len(options):
            await query.answer("Вариант устарел — напишите ответ словами", show_alert=True)
            return

        answer = str(options[callback_data.index])
        await edit_or_send(query, texts.clarification_answered(answer), bot=bot)
        await query.answer()
        await handle_request(
            text=answer,
            bot=bot,
            chat_id=callback_chat_id(query),
            user=user,
            services=services,
            state=state,
            clarify=(
                str(data.get(ORIGINAL_KEY) or ""),
                str(data.get(QUESTION_KEY) or ""),
                int(data.get(ROUND_KEY, 0)) + 1,
            ),
        )

    @router.callback_query(ClarifyCB.filter(F.action == "skip"))
    async def clarify_without_answer(
        query: CallbackQuery,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        """Proceed with whatever context is already available.

        Falls back to the deck's own topic, which is why the deck remembers it:
        the user's earlier request is often enough context for this one.
        """
        data = await state.get_data()
        await state.clear()
        await edit_or_send(query, texts.clarification_skipped(), bot=bot)
        await query.answer()

        language = data.get(LANGUAGE_KEY)
        await run_discovery(
            bot=bot,
            chat_id=callback_chat_id(query),
            user=user,
            services=services,
            state=state,
            response=ParseResponse(
                intent=Intent.EXPAND,
                topic=data.get(TOPIC_KEY) or None,
                count=data.get(COUNT_KEY) or None,
                language=language if isinstance(language, str) else DEFAULT_LANGUAGE,
            ),
            source_text="",
            settings=services.settings.discovery,
        )

    @router.callback_query(ImportCB.filter(F.action == "confirm"))
    async def confirm_import(
        query: CallbackQuery,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        await state.clear()

        # The learner's own words and what the search found are one batch again
        # at save time: they were offered together, so they are saved together.
        raw = [*(data.get(OWN_KEY) or []), *(data.get(DRAFT_KEY) or [])]
        if not raw:
            await edit_or_send(query, texts.nothing_to_save(), bot=bot)
            return

        drafts = [CardDraft(**item) for item in raw]
        deck = await services.card_service.default_deck(user)
        outcome = await services.card_service.add_drafts(
            user,
            drafts,
            deck=deck,
            source="llm" if data.get(ENGINE_KEY, "rules") != "rules" else "rules",
        )
        # Remember the context so "ещё слова" works without repeating it.
        topic = data.get(TOPIC_KEY)
        if not (isinstance(topic, str) and topic.strip()):
            topic = _topic_from_tags(drafts)
        if isinstance(topic, str) and topic.strip():
            await services.decks.set_topic(deck, topic.strip())

        # Commit before confirming: the user must not be told "saved" and then
        # have the transaction fail on the way out of the middleware.
        await services.commit()

        await edit_or_send(query, texts.import_result(outcome, deck.name), bot=bot)
        await query.answer(f"Добавлено: {len(outcome.added)}")

    @router.callback_query(ImportCB.filter(F.action == "more"))
    async def more_suggestions(
        query: CallbackQuery,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        """Another round from the same topic, without saving this one.

        Only the found half of the batch is replaced: a word the learner typed
        themselves stays on the button, because they asked for it to be saved
        and did not withdraw it by asking to look further.
        """
        data = await state.get_data()
        found_raw = data.get(DRAFT_KEY) or []
        own_raw = data.get(OWN_KEY) or []
        topic = data.get(TOPIC_KEY)
        context = data.get(CONTEXT_KEY)
        language = data.get(LANGUAGE_KEY)

        await edit_or_send(query, texts.looking_for_more(), bot=bot)
        await query.answer()
        await run_discovery(
            bot=bot,
            chat_id=callback_chat_id(query),
            user=user,
            services=services,
            state=state,
            response=ParseResponse(
                intent=Intent.EXPAND,
                topic=topic if isinstance(topic, str) else None,
                count=data.get(COUNT_KEY) if isinstance(data.get(COUNT_KEY), int) else None,
                language=language if isinstance(language, str) else DEFAULT_LANGUAGE,
            ),
            source_text=context if isinstance(context, str) else "",
            settings=services.settings.discovery,
            exclude=tuple(
                str(item.get("term", "")) for item in found_raw if isinstance(item, dict)
            ),
            own=[CardDraft(**item) for item in own_raw if isinstance(item, dict)],
            engine=str(data.get(ENGINE_KEY) or ""),
        )

    @router.callback_query(ImportCB.filter(F.action == "cancel"))
    async def cancel_import(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
        await state.clear()
        await edit_or_send(query, texts.import_cancelled(), bot=bot)
        await query.answer()

    # Registered after every specific handler in this router: aiogram matches
    # handlers in registration order, so a catch-all placed earlier would swallow
    # /add and the menu buttons before their own handlers ever saw them.
    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        """Last chance for a command no other router claimed.

        Without this the user gets silence, which reads as a broken bot.
        """
        await message.answer(
            texts.unknown_command(),
            reply_markup=keyboards.main_menu(),
        )

    return router


def _topic_from_tags(drafts: Sequence[CardDraft]) -> str | None:
    """Derive the deck's subject from the tags the model attached to the cards.

    Without this, "more words" would only work after a discovery request: someone
    who types words by hand would have no remembered subject at all, and would be
    asked for context they had effectively already given.
    """
    counts: Counter[str] = Counter(tag for draft in drafts for tag in draft.tags)
    if not counts:
        return None
    tag, _ = counts.most_common(1)[0]
    return tag


def _llm_available(services: Services) -> bool:
    return services.parser.engine != "rules"


def _discovery_inputs(
    response: ParseResponse,
    source_text: str,
    deck: Deck,
    settings: DiscoverySettings,
    *,
    allow_deck_topic: bool = True,
) -> tuple[str | None, str | None]:
    """Decide what to expand from: a topic, a pasted text, or the deck's own.

    A long message is treated as source material rather than as a topic — that is
    what "find ten words in this paragraph" looks like from here. The deck's
    remembered topic is the last resort, and it is deliberately not mixed with a
    fresh text: an old subject would pull the suggestions away from what the user
    just pasted.

    ``allow_deck_topic=False`` is for a message that names words of its own: the
    learner gave the domain's starting point right there, and an unrelated
    remembered subject is worse than admitting the domain was not understood.
    """
    text = source_text.strip()
    context = text if len(text) >= settings.context_text_min_length else None
    topic = (response.topic or "").strip() or None

    if topic is None and context is None and allow_deck_topic:
        topic = (deck.topic or "").strip() or None
    return topic, context
