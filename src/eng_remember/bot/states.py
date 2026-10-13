"""FSM states and the keys stored under them."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

__all__ = [
    "CONTEXT_KEY",
    "COUNT_KEY",
    "DRAFT_KEY",
    "ENGINE_KEY",
    "OPTIONS_KEY",
    "ORIGINAL_KEY",
    "OWN_KEY",
    "QUESTION_KEY",
    "ROUND_KEY",
    "SHOWN_AT_KEY",
    "SHOWN_CARD_KEY",
    "TOPIC_KEY",
    "AddWords",
]

# Keys inside FSM storage. Named constants instead of scattered string literals
# so a typo cannot silently produce an empty batch.
#
# A pending batch is split in two: the words the learner named themselves
# (OWN_KEY) and the ones the search produced (DRAFT_KEY). "Ещё слова" replaces
# only the found half, so the learner's own word cannot be lost by asking for
# another round.
DRAFT_KEY = "drafts"
OWN_KEY = "own_drafts"
ENGINE_KEY = "engine"
TOPIC_KEY = "topic"
COUNT_KEY = "count"
CONTEXT_KEY = "context"

# The clarification dialogue: what was asked, what the answer options were, and
# which round this is — without the round counter a model that keeps asking the
# same thing would loop forever.
ORIGINAL_KEY = "clarify_original"
QUESTION_KEY = "clarify_question"
OPTIONS_KEY = "clarify_options"
ROUND_KEY = "clarify_round"

# When the answer was shown, for measuring how long the learner took.
SHOWN_AT_KEY = "shown_at"
SHOWN_CARD_KEY = "shown_card"


class AddWords(StatesGroup):
    """Waiting for the user to confirm a parsed batch of cards."""

    confirming = State()
    """Waiting for an answer to the clarification the bot asked for."""

    clarifying = State()
