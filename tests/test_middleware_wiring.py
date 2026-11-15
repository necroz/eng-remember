"""One LLM transport per process, one per update is not enough.

The transport owns the provider connection and the lessons learned from
rejected parameters (a model that refuses ``reasoning_effort`` is remembered
once). Built per update, every message would relearn them. ``build_dispatcher``
takes it once and ``DbSessionMiddleware`` hands it to ``build_services`` — this
test pins that seam down, because the fake client in ``conftest`` accepts a
``transport`` argument and then drops it, so nothing else would notice.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eng_remember.bot.dispatcher import build_dispatcher
from eng_remember.config import Settings
from eng_remember.services.container import Services, build_services
from eng_remember.services.llm.client import LlmTransport
from tests.factories import make_message_update


async def test_the_process_transport_reaches_every_request(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = LlmTransport(settings.llm)
    seen: list[object] = []
    real_build = build_services

    def spy(
        session: AsyncSession,
        config: Settings,
        *,
        llm_transport: LlmTransport | None = None,
        backfill: Any = None,
    ) -> Services:
        seen.append(llm_transport)
        return real_build(session, config, llm_transport=llm_transport, backfill=backfill)

    monkeypatch.setattr("eng_remember.bot.middlewares.db.build_services", spy)
    dispatcher = build_dispatcher(settings, session_factory, llm_transport=transport)

    await dispatcher.feed_update(bot, make_message_update("/start", update_id=1))
    await dispatcher.feed_update(bot, make_message_update("/start", update_id=2))

    assert len(seen) == 2
    assert all(item is transport for item in seen)
