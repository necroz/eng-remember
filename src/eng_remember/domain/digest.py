"""Answer history turned into a heatmap.

Pure functions: the repositories hand over per-day counts, the domain decides
what a day looks like, and the renderer only matches emoji to cells.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

__all__ = ["HEATMAP_DAYS", "daily_series", "heatmap"]

HEATMAP_DAYS = 28

# A day with this many answers reads as a fully green day.
BUSY_DAY = 5

WHITE = "⬜"
YELLOW = "🟨"
GREEN = "🟩"


def heatmap(counts: Sequence[int]) -> list[str]:
    """One emoji per day, oldest first; missing days pad the front with blanks."""
    cells = list(counts)[-HEATMAP_DAYS:]
    cells = [0] * (HEATMAP_DAYS - len(cells)) + cells
    return [WHITE if count <= 0 else (YELLOW if count < BUSY_DAY else GREEN) for count in cells]


def daily_series(counts: Mapping[date, int], *, end: date, length: int) -> list[int]:
    """Answer counts for the ``length`` days ending on ``end``, gaps as zeroes."""
    return [int(counts.get(end - timedelta(days=offset), 0)) for offset in reversed(range(length))]
