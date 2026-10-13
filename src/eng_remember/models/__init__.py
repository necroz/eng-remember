"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic's autogenerate and the test-time ``create_all`` rely on.
"""

from eng_remember.models.card import Card
from eng_remember.models.deck import Deck
from eng_remember.models.llm_call import LlmCall
from eng_remember.models.review import Review
from eng_remember.models.streak import Streak
from eng_remember.models.user import User

__all__ = ["Card", "Deck", "LlmCall", "Review", "Streak", "User"]
