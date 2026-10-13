"""Routers, in match order.

``capture`` is deliberately last: it catches any plain text that no specific
handler claimed, so it has to be the fallback rather than a competitor.
"""

from eng_remember.bot.routers import (
    capture,
    errors,
    library,
    preferences,
    review,
    start,
)

__all__ = ["capture", "errors", "library", "preferences", "review", "start"]
