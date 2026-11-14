"""Text rendering stays inside what one Telegram message can carry."""

from __future__ import annotations

from datetime import datetime

from eng_remember.bot import rendering, texts
from eng_remember.models.card import Card
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.discovery import DiscoveryResult


def drafts(count: int) -> list[CardDraft]:
    return [CardDraft(term=f"word{index}", translation=f"перевод{index}") for index in range(count)]


def test_import_preview_caps_drafts() -> None:
    preview = texts.import_preview(drafts(200), "rules")

    assert len(preview) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "Показаны первые 30 из 200 — сохранение добавит все" in preview
    assert "word0" in preview
    assert "word29" in preview
    assert "word30" not in preview
    assert "word199" not in preview


def test_discovery_preview_caps_drafts() -> None:
    result = DiscoveryResult(drafts=drafts(200), requested=200, topic="спорт")

    preview = texts.discovery_preview(result, context_used=False, deck_name="Спорт")

    assert len(preview) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "Показаны первые 30 из 200 — сохранение добавит все" in preview
    assert "word30" not in preview


def test_deck_list_caps_decks() -> None:
    rows = [(f"deck{index}", index) for index in range(200)]

    rendered = texts.deck_list(rows, total=19900)

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "deck0" in rendered
    assert "deck49" in rendered
    assert "deck50" not in rendered
    assert "…и ещё 150" in rendered


def test_paused_list_caps_cards() -> None:
    cards = [
        Card(
            term=f"word{index}",
            translation=f"перевод{index}",
            user_id=1,
            deck_id=1,
            due_at=datetime(2026, 5, 14),
        )
        for index in range(200)
    ]

    rendered = texts.paused_list(cards, total=200)

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "word0" in rendered
    assert "word19" in rendered
    assert "word20" not in rendered
    assert "…и ещё 180" in rendered


def test_import_preview_short_batch_has_no_cap_note() -> None:
    preview = texts.import_preview(drafts(3), "rules")

    assert "Показаны первые" not in preview
    assert "word2" in preview
