"""When a word has failed often enough to deserve intervention.

Leeches are the cards that keep coming back and keep being forgotten: they eat
time and morale. The rule is deliberately not the FSRS ``lapses`` counter —
that one only grows once a card has left learning, which misses exactly the
words stuck in the earliest stage.

Instead every failed answer counts, and the bot offers help every ``threshold``
failures. ``level`` is the highest multiple of the threshold already handled, so
after a deploy with pre-existing failures the very next failure brings the menu
(the user has clearly been struggling) without a burst of messages after it.
"""

from __future__ import annotations

__all__ = ["next_level", "should_offer"]


def should_offer(again_count: int, level: int, *, threshold: int) -> bool:
    """Whether this failure crosses into the next unattended threshold."""
    return again_count >= (level + 1) * threshold


def next_level(again_count: int, *, threshold: int) -> int:
    """The level to record after offering help at ``again_count`` failures."""
    return again_count // threshold
