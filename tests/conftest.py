"""Shared fixtures.

The database is a throwaway SQLite file per test rather than ``:memory:``: a
file gives every session its own connection, which matters because the bot
middleware opens a session of its own while the test holds another one.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import eng_remember.models  # noqa: F401  — registers tables on Base.metadata
from eng_remember.bot.dispatcher import build_dispatcher
from eng_remember.config import BotSettings, ReminderSettings, Settings
from eng_remember.db import Base, create_engine, create_session_factory
from eng_remember.models.card import Card
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.repositories.users import TelegramProfile, UserRepository
from eng_remember.services.container import Services, build_services
from eng_remember.utils.time import utcnow
from tests.factories import TEST_USER_ID, ScriptedClient, response_for

TEST_TOKEN = "42:TEST-TOKEN"


async def make_due(session_factory: async_sessionmaker[AsyncSession], *terms: str) -> None:
    """Bring saved cards to the moment their repetition is due.

    A word is added with its first repetition scheduled for the learner's next
    day, so today's queue is empty by design. A test that wants a card in front of
    the user has to let that day arrive — moving the due date into the past is
    that day arriving, without sleeping until midnight. With no terms every card
    is moved, which is what a whole-collection assertion wants.
    """
    stmt = update(Card).values(due_at=utcnow() - timedelta(minutes=1))
    if terms:
        stmt = stmt.where(Card.term.in_(terms))
    async with session_factory() as session:
        await session.execute(stmt)
        await session.commit()


class RecordingSession(BaseSession):
    """A bot session that answers locally and remembers what was asked.

    This is the seam that makes handler tests possible without network access:
    aiogram hands every outgoing method to ``make_request``, so recording there
    captures exactly what the user would have seen.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        # Chats the API refuses to deliver to, for exercising the blocked-user
        # path without a real Telegram.
        self.forbidden: set[int] = set()

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(
                method=method, message="Forbidden: bot was blocked by the user"
            )
        self.requests.append(method)
        return response_for(method)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    # -- assertions helpers ------------------------------------------------

    @property
    def outbound(self) -> list[SendMessage | EditMessageText]:
        """Everything that produced visible text, edits included.

        Reviewing a card edits the message in place, so counting only
        ``SendMessage`` would miss half of what the user actually sees.
        """
        return [
            method for method in self.requests if isinstance(method, (SendMessage, EditMessageText))
        ]

    def sent_texts(self) -> list[str]:
        return [method.text or "" for method in self.outbound]

    def last_text(self) -> str:
        return self.outbound[-1].text or ""

    def last_buttons(self) -> list[InlineKeyboardButton]:
        """Flattened inline buttons of the most recent outbound message."""
        markup = self.outbound[-1].reply_markup
        if not isinstance(markup, InlineKeyboardMarkup):
            return []
        return [button for row in markup.inline_keyboard for button in row]

    def last_button_data(self) -> list[str]:
        return [button.callback_data or "" for button in self.last_buttons()]

    def clear(self) -> None:
        self.requests.clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at a scratch database, with no LLM key configured.

    The weekly digest is off here on purpose: a test suite that depends on the
    real weekday would go red once a week. Digest tests switch it back on.

    The inbound guards are tested directly; handler tests raise their limits so
    a long scenario does not trip a production-shaped minute budget.
    """
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        bot_token=SecretStr(TEST_TOKEN),
        log_json=False,
        log_level="WARNING",
        bot=BotSettings(
            messages_per_minute=1000,
            callbacks_per_minute=1000,
            duplicate_message_ttl_seconds=0,
        ),
        reminders=ReminderSettings(digest_enabled=False),
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def services(session: AsyncSession, settings: Settings) -> Services:
    return build_services(session, settings)


@pytest.fixture
async def user(session: AsyncSession) -> User:
    repository = UserRepository(session)
    user, _ = await repository.get_or_create(
        TelegramProfile(telegram_id=TEST_USER_ID, first_name="Tester", username="tester")
    )
    await session.commit()
    return user


@pytest.fixture
async def saved_card(services: Services, user: User) -> Card:
    """A committed card, created through the service rather than the bot.

    Reward tests are about what happens after an answer, so setting the card up
    through the chat would only add noise to them.
    """
    card = await services.card_service.add_draft(
        user, CardDraft(term="ubiquitous", translation="вездесущий")
    )
    await services.commit()
    return card


@pytest.fixture
def recording() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(recording: RecordingSession) -> Bot:
    return Bot(
        token=TEST_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=recording,
    )


@pytest.fixture
def dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    return build_dispatcher(settings, session_factory)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> ScriptedClient:
    """Replace the container's LLM client with a scripted one.

    Patched at the container rather than at the factory so every service built
    for a request — parser and explorer alike — shares the same script, which is
    what the multi-step flows need.
    """
    client = ScriptedClient()

    def fake_build(session: AsyncSession, settings: Settings) -> ScriptedClient:
        del session, settings
        return client

    monkeypatch.setattr("eng_remember.services.container.build_llm_client", fake_build)
    return client


@pytest.fixture
async def llm_services(
    llm: ScriptedClient,
    session: AsyncSession,
    settings: Settings,
) -> Services:
    """Services wired to the scripted model.

    Separate from ``services`` so that tests which exercise the rule-based path
    keep getting the real container: making every test carry a scripted client
    would quietly change what those tests prove.
    """
    del llm
    return build_services(session, settings)


@pytest.fixture
def llm_dispatcher(
    llm: ScriptedClient,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    """A dispatcher whose handlers see the scripted model."""
    del llm
    return build_dispatcher(settings, session_factory)
