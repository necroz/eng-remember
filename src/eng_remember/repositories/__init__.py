"""Data access layer.

Repositories own every SQL statement in the project. Services above them deal in
domain objects and use cases; bot handlers never touch a query directly.
"""

from eng_remember.repositories.cards import CardDraft, CardRepository
from eng_remember.repositories.decks import DeckRepository
from eng_remember.repositories.llm_calls import LlmCallRecord, LlmCallRepository
from eng_remember.repositories.reviews import ReviewRepository
from eng_remember.repositories.streaks import StreakRepository
from eng_remember.repositories.users import TelegramProfile, UserRepository

__all__ = [
    "CardDraft",
    "CardRepository",
    "DeckRepository",
    "LlmCallRecord",
    "LlmCallRepository",
    "ReviewRepository",
    "StreakRepository",
    "TelegramProfile",
    "UserRepository",
]
