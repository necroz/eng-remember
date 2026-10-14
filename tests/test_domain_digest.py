"""The heatmap: answer counts turned into emoji cells, and gap-free series."""

from __future__ import annotations

from datetime import date

from eng_remember.domain.digest import daily_series, heatmap


def test_the_scale_is_white_yellow_green() -> None:
    cells = heatmap([0, 1, 4, 5, 12])

    assert len(cells) == 28
    assert cells[-5:] == ["⬜", "🟨", "🟨", "🟩", "🟩"]


def test_missing_history_is_padded_at_the_front() -> None:
    assert heatmap([7]) == ["⬜"] * 27 + ["🟩"]


def test_nothing_is_all_white() -> None:
    assert heatmap([]) == ["⬜"] * 28


def test_only_the_last_28_values_survive() -> None:
    assert heatmap([9] * 30) == ["🟩"] * 28


def test_daily_series_fills_gaps_with_zeroes() -> None:
    counts = {date(2026, 5, 5): 3, date(2026, 5, 11): 5}

    series = daily_series(counts, end=date(2026, 5, 11), length=7)

    assert series == [3, 0, 0, 0, 0, 0, 5]
