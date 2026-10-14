"""Builders for Telegram objects and model answers.

Handlers are exercised through ``Dispatcher.feed_update`` with real aiogram
types, so the tests cover routing, filters and callback payload parsing rather
than a hand-rolled imitation of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.methods import (
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from openai.types.chat import ChatCompletion

from eng_remember.domain.enums import CardState
from eng_remember.domain.srs import CardSchedule
from eng_remember.services.llm.schema import (
    AnswerCheckResponse,
    ExamplesResponse,
    ExpansionResponse,
    MnemonicResponse,
    ParseResponse,
    TranslationsResponse,
)
from eng_remember.utils.time import utcnow

__all__ = [
    "TEST_CHAT_ID",
    "TEST_USER_ID",
    "ScriptedClient",
    "card_payload",
    "completion",
    "expansion_payload",
    "make_callback_update",
    "make_message_update",
    "message_from",
    "parse_payload",
    "schedule",
]

TEST_USER_ID = 100500
TEST_CHAT_ID = 100500
_FIXED_DATE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def telegram_user(user_id: int = TEST_USER_ID, first_name: str = "Tester") -> TelegramUser:
    return TelegramUser(id=user_id, is_bot=False, first_name=first_name, username="tester")


def message_from(
    text: str | None,
    *,
    message_id: int = 1,
    user_id: int = TEST_USER_ID,
    date: datetime | None = None,
) -> Message:
    return Message(
        message_id=message_id,
        date=date if date is not None else _FIXED_DATE,
        chat=Chat(id=user_id, type="private"),
        from_user=telegram_user(user_id),
        text=text,
    )


def make_message_update(
    text: str,
    *,
    update_id: int = 1,
    message_id: int = 1,
    user_id: int = TEST_USER_ID,
    date: datetime | None = None,
) -> Update:
    return Update(
        update_id=update_id,
        message=message_from(text, message_id=message_id, user_id=user_id, date=date),
    )


def make_callback_update(
    data: str,
    *,
    update_id: int = 2,
    message_id: int = 1,
    user_id: int = TEST_USER_ID,
    text: str = "card",
) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb-{update_id}",
            from_user=telegram_user(user_id),
            chat_instance="chat-instance",
            data=data,
            message=message_from(text, message_id=message_id, user_id=user_id),
        ),
    )


def completion(
    content: str,
    *,
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
) -> ChatCompletion:
    """A well-formed chat completion, as the OpenAI SDK would parse it."""
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def schedule(
    due_in_days: float = 0.0,
    *,
    state: CardState = CardState.LEARNING,
    reps: int = 0,
    lapses: int = 0,
    stability: float | None = None,
) -> CardSchedule:
    """A card memory state placed at a chosen point in time.

    ``reps=0`` means "never answered" (a new card); anything else puts the card
    into rotation with a due date relative to now.
    """
    now = utcnow()
    return CardSchedule(
        due_at=now + timedelta(days=due_in_days),
        state=state,
        step=None if state is CardState.REVIEW else 0,
        stability=stability if stability is not None else (5.0 if reps else None),
        difficulty=5.0 if reps else None,
        last_review_at=now - timedelta(days=1) if reps else None,
        reps=reps,
        lapses=lapses,
    )


def response_for(method: TelegramMethod[Any]) -> Any:
    """What the API would answer to this method, for the recording session.

    Only the two methods this bot actually cares about are modelled; everything
    else gets a bare ``True``, which is what most Telegram methods return.
    """
    if isinstance(method, SendMessage):
        return Message(
            message_id=len(str(method.text or "")) + 1,
            date=_FIXED_DATE,
            chat=Chat(id=TEST_CHAT_ID, type="private"),
            text=method.text,
        )
    if isinstance(method, EditMessageText):
        return Message(
            message_id=1,
            date=_FIXED_DATE,
            chat=Chat(id=TEST_CHAT_ID, type="private"),
            text=method.text,
        )
    return True


def card_payload(term: str, translation: str, **overrides: Any) -> dict[str, Any]:
    """One card as the model would return it."""
    payload: dict[str, Any] = {"term": term, "translation": translation}
    payload.update(overrides)
    return payload


def parse_payload(**overrides: Any) -> dict[str, Any]:
    """A ParseResponse body with everything defaulted."""
    payload: dict[str, Any] = {"intent": "add_cards", "cards": []}
    payload.update(overrides)
    return payload


def expansion_payload(
    cards: list[dict[str, Any]],
    *,
    topic: str = "",
) -> dict[str, Any]:
    return {"topic": topic, "cards": cards}


class ScriptedClient:
    """A ``JsonChatClient`` whose answers the test writes down in advance.

    Injected where the container would build a real OpenAI client, which makes
    the whole model-dependent path — intent routing, the clarification dialogue,
    word discovery — testable without a network or an API key. Answers are
    dispatched by the requested schema, so parse and expansion calls cannot be
    confused with each other.
    """

    def __init__(self) -> None:
        self.parse_queue: list[Any] = []
        self.expand_queue: list[Any] = []
        self.check_queue: list[Any] = []
        self.examples_queue: list[Any] = []
        self.mnemonic_queue: list[Any] = []
        self.translations_queue: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return "scripted-model"

    @property
    def available(self) -> bool:
        return True

    def push_parse(self, *responses: Any) -> ScriptedClient:
        self.parse_queue.extend(responses)
        return self

    def push_expand(self, *responses: Any) -> ScriptedClient:
        self.expand_queue.extend(responses)
        return self

    def push_check(self, *responses: Any) -> ScriptedClient:
        self.check_queue.extend(responses)
        return self

    def push_examples(self, *responses: Any) -> ScriptedClient:
        self.examples_queue.extend(responses)
        return self

    def push_mnemonic(self, *responses: Any) -> ScriptedClient:
        self.mnemonic_queue.extend(responses)
        return self

    def push_translations(self, *responses: Any) -> ScriptedClient:
        self.translations_queue.extend(responses)
        return self

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: Any,
        user_id: int | None = None,
    ) -> Any:
        self.calls.append(
            {
                "purpose": purpose,
                "system": system,
                "user": user,
                "schema": schema,
                "user_id": user_id,
            }
        )
        if schema is ParseResponse:
            queue = self.parse_queue
        elif schema is ExpansionResponse:
            queue = self.expand_queue
        elif schema is AnswerCheckResponse:
            queue = self.check_queue
        elif schema is ExamplesResponse:
            queue = self.examples_queue
        elif schema is MnemonicResponse:
            queue = self.mnemonic_queue
        elif schema is TranslationsResponse:
            queue = self.translations_queue
        else:
            raise AssertionError(f"no scripted queue for {schema.__name__}")
        if not queue:
            raise AssertionError(f"no scripted answer left for {schema.__name__}")
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return schema.model_validate(outcome)
        return outcome

    def last_prompt(self) -> str:
        return str(self.calls[-1]["user"]) if self.calls else ""
