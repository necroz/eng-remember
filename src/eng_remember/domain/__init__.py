"""Pure domain layer: no database, no Telegram, no I/O.

Everything here can be imported and tested without a running bot or a database
connection, which is what makes the scheduling rules cheap to reason about.
"""

from eng_remember.domain.enums import CardState, ReviewRating
from eng_remember.domain.gamification import (
    StreakState,
    StreakTransition,
    record_activity,
    roll_forward,
)
from eng_remember.domain.praise import PraiseCategory, PraisePick, select
from eng_remember.domain.srs import CardSchedule, SchedulePreview, SrsScheduler

__all__ = [
    "CardSchedule",
    "CardState",
    "PraiseCategory",
    "PraisePick",
    "ReviewRating",
    "SchedulePreview",
    "SrsScheduler",
    "StreakState",
    "StreakTransition",
    "record_activity",
    "roll_forward",
    "select",
]
