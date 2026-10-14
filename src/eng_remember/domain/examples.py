"""Choosing and merging the examples attached to a card.

A card carries a list of example sentences so each repetition can show a
different one — the pattern to remember is the word, not the card face. These
rules are pure: which example a repetition shows and how a new batch is folded
into the list without duplicates.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["merge_examples", "pick_example"]


def pick_example(examples: Sequence[str], fallback: str | None, slot: int) -> str | None:
    """The example for one repetition.

    ``slot`` is the number of reviews the card has already had, so consecutive
    repetitions walk the list and a full cycle starts over.
    """
    if examples:
        return examples[slot % len(examples)]
    return fallback


def merge_examples(
    example: str | None,
    examples: Sequence[str],
    *,
    limit: int | None = None,
) -> list[str]:
    """One cleaned list with the main example first and no case-duplicates.

    The main ``example`` keeps its place at the head of the list: it is the one
    the learner may have pinned to a phrase themselves, and rotation must show
    it first.
    """
    merged: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if value is None:
            return
        cleaned = value.strip()
        if not cleaned or cleaned.casefold() in seen:
            return
        seen.add(cleaned.casefold())
        merged.append(cleaned)

    add(example)
    for item in examples:
        add(item)

    if limit is not None:
        return merged[:limit]
    return merged
