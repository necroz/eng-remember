"""Routers, in match order.

``capture`` is the catch-all for plain text: it must come after every specific
handler. The dispatcher's fallback router for unknown callbacks is registered
after all of these.
"""

from eng_remember.bot.routers import (
    capture,
    errors,
    files,
    library,
    practice,
    preferences,
    review,
    start,
)

__all__ = [
    "capture",
    "errors",
    "files",
    "library",
    "practice",
    "preferences",
    "review",
    "start",
]
