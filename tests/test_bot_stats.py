"""The statistics screen shows the same heatmap the digest uses."""

from __future__ import annotations

from aiogram import Bot, Dispatcher

from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.container import Services
from tests.conftest import RecordingSession
from tests.test_bot_flow import send


async def test_stats_include_a_heatmap(
    dispatcher: Dispatcher,
    bot: Bot,
    recording: RecordingSession,
    services: Services,
    user: User,
) -> None:
    await services.card_service.add_draft(user, CardDraft(term="run", translation="бежать"))
    await services.commit()

    await send(dispatcher, bot, "/stats", update_id=100)

    sent = recording.last_text()
    assert "Статистика" in sent
    assert "⬜" in sent
    assert sent.count("\n") > 4
