"""Business logic.

Services orchestrate repositories and the domain layer. They know nothing about
Telegram and nothing about HTTP: the bot calls them, tests call them, and a
future CLI or web admin would call the same objects.
"""

from eng_remember.services.cards import CardService, ImportOutcome, UserStats
from eng_remember.services.container import Services, build_services
from eng_remember.services.discovery import DiscoveryResult, DiscoveryService
from eng_remember.services.praise import PraiseService
from eng_remember.services.review import AnswerResult, ReviewQueue, ReviewService
from eng_remember.services.streak import StreakService

__all__ = [
    "AnswerResult",
    "CardService",
    "DiscoveryResult",
    "DiscoveryService",
    "ImportOutcome",
    "PraiseService",
    "ReviewQueue",
    "ReviewService",
    "Services",
    "StreakService",
    "UserStats",
    "build_services",
]
