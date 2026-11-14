"""Text rendering stays inside what one Telegram message can carry."""

from __future__ import annotations

import re
from datetime import datetime

from eng_remember.bot import rendering, texts
from eng_remember.models.card import Card
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.csv_io import CsvRow
from eng_remember.services.discovery import DiscoveryResult


def drafts(count: int) -> list[CardDraft]:
    return [CardDraft(term=f"word{index}", translation=f"перевод{index}") for index in range(count)]


def long_drafts(count: int) -> list[CardDraft]:
    """Drafts at the repository's maximum sizes, to defeat any count-only cap."""
    return [
        CardDraft(
            term=f"w{index:03d}" + "x" * 251,
            translation="п" * 2000,
            example="e" * 300,
        )
        for index in range(count)
    ]


def deck_rows(count: int) -> list[tuple[str, int]]:
    return [(f"{index:03d}-" + "d" * 124, index) for index in range(count)]


def bullet_count(rendered: str) -> int:
    return sum(1 for line in rendered.splitlines() if line.startswith("• "))


def hidden_count(rendered: str, pattern: str) -> int:
    match = re.search(pattern, rendered)
    assert match is not None, rendered[-200:]
    return int(match.group(1))


def test_import_preview_caps_drafts() -> None:
    preview = texts.import_preview(drafts(200), "rules")

    assert len(preview) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "Показаны 30 из 200 — сохранение добавит все" in preview
    assert "word0" in preview
    assert "word29" in preview
    assert "word30" not in preview
    assert "word199" not in preview


def test_import_preview_respects_content_budget() -> None:
    """Thirty huge drafts cannot be rendered: the message must stop and say so."""
    preview = texts.import_preview(long_drafts(30), "rules")

    assert len(preview) <= rendering.TELEGRAM_TEXT_LIMIT
    shown = bullet_count(preview)
    assert 0 < shown < 30
    assert f"Показаны {shown} из 30 — сохранение добавит все" in preview
    assert "w029" not in preview


def test_import_preview_counts_own_and_found_together() -> None:
    """The cap is 30 drafts per message, not 30 in each of the two sections."""
    own = [CardDraft(term=f"own{index}", translation=f"перевод{index}") for index in range(25)]
    found = DiscoveryResult(
        drafts=[
            CardDraft(term=f"found{index}", translation=f"перевод{index}") for index in range(25)
        ],
        requested=25,
        topic="спорт",
    )

    preview = texts.import_preview(own, "rules", found=found)

    assert len(preview) <= rendering.TELEGRAM_TEXT_LIMIT
    assert bullet_count(preview) == 30
    assert "Показаны 30 из 50 — сохранение добавит все" in preview
    assert "own24" in preview
    assert "found4" in preview
    assert "found5" not in preview


def test_discovery_preview_caps_drafts() -> None:
    result = DiscoveryResult(drafts=drafts(200), requested=200, topic="спорт")

    preview = texts.discovery_preview(result, context_used=False, deck_name="Спорт")

    assert len(preview) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "Показаны 30 из 200 — сохранение добавит все" in preview
    assert "word30" not in preview


def test_discovery_preview_respects_content_budget() -> None:
    result = DiscoveryResult(drafts=long_drafts(200), requested=200, topic="спорт")

    rendered = texts.discovery_preview(result, context_used=False, deck_name="Спорт")

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    shown = bullet_count(rendered)
    assert 0 < shown < 30
    assert f"Показаны {shown} из 200 — сохранение добавит все" in rendered


def test_deck_list_caps_decks() -> None:
    rows = [(f"deck{index}", index) for index in range(200)]

    rendered = texts.deck_list(rows, total=19900)

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    assert "deck0" in rendered
    assert "deck49" in rendered
    assert "deck50" not in rendered
    assert "…и ещё 150" in rendered


def test_deck_list_respects_content_budget() -> None:
    rows = deck_rows(200)

    rendered = texts.deck_list(rows, total=19900)

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    shown = bullet_count(rendered)
    assert 0 < shown < 50
    assert hidden_count(rendered, r"…и ещё (\d+)") == 200 - shown
    assert f"{shown - 1:03d}-" in rendered
    assert f"{shown:03d}-" not in rendered


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


def test_paused_list_respects_content_budget() -> None:
    cards = [
        Card(
            term=f"w{index:03d}" + "x" * 251,
            translation="п" * 2000,
            user_id=1,
            deck_id=1,
            due_at=datetime(2026, 5, 14),
        )
        for index in range(20)
    ]

    rendered = texts.paused_list(cards, total=200)

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    shown = bullet_count(rendered)
    assert 0 < shown < 20
    assert f"…и ещё {200 - shown}" in rendered
    assert "Нажмите на слово, чтобы вернуть его в повторения." in rendered


def test_file_import_preview_respects_content_budget() -> None:
    rows = [
        CsvRow(term=f"w{index:03d}" + "x" * 251, translation="п" * 2000) for index in range(100)
    ]

    rendered = texts.file_import_preview(
        rows,
        duplicates=3,
        untranslated=2,
        skipped=1,
        filled=4,
    )

    assert len(rendered) <= rendering.TELEGRAM_TEXT_LIMIT
    shown = bullet_count(rendered)
    assert 0 < shown < 10
    assert f"… и ещё {100 - shown}" in rendered
    assert "уже было: 3" in rendered


def test_import_preview_short_batch_has_no_cap_note() -> None:
    preview = texts.import_preview(drafts(3), "rules")

    assert "Показаны" not in preview
    assert "word2" in preview
