"""FSM states and the keys stored under them."""

from __future__ import annotations

from typing import cast

from aiogram.fsm.state import State, StatesGroup

__all__ = [
    "ANSWERED_KEY",
    "ANSWERING_KEY",
    "CARD_ID_KEY",
    "CONTEXT_KEY",
    "CORRECT_COUNT_KEY",
    "COUNT_KEY",
    "DECK_ID_KEY",
    "DRAFT_KEY",
    "EDITED_EXAMPLE_KEY",
    "EDITED_NOTES_KEY",
    "EDITED_TRANSLATION_KEY",
    "EDIT_PREVIOUS_STATE_KEY",
    "ENGINE_KEY",
    "EXAMPLE_KEY",
    "FILE_DUPLICATES_KEY",
    "FILE_FILLED_KEY",
    "FILE_ROWS_KEY",
    "FILE_SKIPPED_KEY",
    "FILE_UNTRANSLATED_KEY",
    "FRONT_MESSAGE_KEY",
    "HINT_USED_KEY",
    "LANGUAGE_KEY",
    "OPTIONS_KEY",
    "ORIGINAL_KEY",
    "OWN_KEY",
    "PRACTICE_ANSWER_KEY",
    "PRACTICE_TOKEN_KEY",
    "QUESTIONS_KEY",
    "QUESTION_INDEX_KEY",
    "QUESTION_KEY",
    "RENAME_PREVIOUS_STATE_KEY",
    "ROUND_KEY",
    "SESSION_KEY",
    "SESSION_LIMIT_KEY",
    "SHOWN_AT_KEY",
    "SHOWN_CARD_KEY",
    "TEXT_INPUT_STATES",
    "TOPIC_KEY",
    "AddWords",
    "CardEdit",
    "DeckRename",
    "ImportFile",
    "Practice",
    "Review",
    "is_text_input_state",
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
LANGUAGE_KEY = "language"

# The clarification dialogue: what was asked, what the answer options were, and
# which round this is — without the round counter a model that keeps asking the
# same thing would loop forever.
ORIGINAL_KEY = "clarify_original"
QUESTION_KEY = "clarify_question"
OPTIONS_KEY = "clarify_options"
ROUND_KEY = "clarify_round"

# The review dialogue: which card is waiting, when its front was shown (the
# speed signal), which message carries it (edited into the result), and whether
# an answer is already being checked (reservation against a double send). The
# session token identifies one run of the loop: a paused or restarted review
# replaces or drops it, so a slow model answer cannot land in a new session.
# The chosen example travels with the card too: the hint and the revealed back
# must show the same sentence even after the card's review counter moves on.
SHOWN_AT_KEY = "shown_at"
SHOWN_CARD_KEY = "shown_card"
FRONT_MESSAGE_KEY = "front_message"
ANSWERING_KEY = "answering"
ANSWERED_KEY = "answered"
SESSION_KEY = "review_session"
# How many cards this run may answer. Chosen when the run starts and kept for
# its whole life: the queue is rebuilt after every answer, so without a stored
# goal a backlog would keep feeding the loop until it is empty.
SESSION_LIMIT_KEY = "review_limit"
EXAMPLE_KEY = "review_example"
HINT_USED_KEY = "hint_used"

# The practice quiz: the questions built once at start (plain dicts, so a Redis
# FSM storage swap stays possible), which one is on screen, the session token
# every button carries, the running score, and the in-flight reservation that
# keeps a double tap from counting twice.
QUESTIONS_KEY = "practice_questions"
QUESTION_INDEX_KEY = "practice_index"
CORRECT_COUNT_KEY = "practice_correct"
PRACTICE_TOKEN_KEY = "practice_token"
PRACTICE_ANSWER_KEY = "practice_answering"

# A CSV import waiting for confirmation: the cleaned rows (plain dicts) and the
# counters the preview showed, so the result message can stay honest about what
# was skipped and why.
FILE_ROWS_KEY = "file_rows"
FILE_SKIPPED_KEY = "file_skipped"
FILE_DUPLICATES_KEY = "file_duplicates"
FILE_UNTRANSLATED_KEY = "file_untranslated"
FILE_FILLED_KEY = "file_filled"

# The deck-rename dialogue: which deck the name being typed belongs to. Stored
# rather than carried by the message, because the learner answers the prompt in
# a message of their own and it must not be able to rename another deck.
DECK_ID_KEY = "deck_id"
# The flow the chat was in before the rename dialogue opened. Renaming starts
# from the deck list, which can be reached with a review still waiting for an
# answer, so leaving the dialogue must put that flow back.
RENAME_PREVIOUS_STATE_KEY = "rename_previous_state"

# The card-edit dialogue: which card is being edited, the values typed so far
# (``None`` is "not edited", not "clear"), and the state to return to. Editing
# starts from a result in the chat history, possibly in the middle of a review
# run, so leaving it must put the review back exactly where it stood.
CARD_ID_KEY = "edit_card"
EDITED_TRANSLATION_KEY = "edit_translation"
EDITED_EXAMPLE_KEY = "edit_example"
EDITED_NOTES_KEY = "edit_notes"
EDIT_PREVIOUS_STATE_KEY = "edit_previous_state"


class AddWords(StatesGroup):
    """Waiting for the user to confirm a parsed batch of cards."""

    confirming = State()
    """Waiting for an answer to the clarification the bot asked for."""

    clarifying = State()


class Review(StatesGroup):
    """Waiting for the learner to type the translation of the shown card."""

    awaiting_answer = State()


class Practice(StatesGroup):
    """Waiting for the learner to pick one of the quiz options."""

    awaiting_answer = State()


class ImportFile(StatesGroup):
    """Waiting for the learner to confirm a CSV import."""

    confirming = State()


class DeckRename(StatesGroup):
    """Waiting for the new name of a deck the /decks list offered to rename."""

    awaiting_name = State()


class CardEdit(StatesGroup):
    """The three questions of a content edit, in the order they are asked.

    One state per field rather than an index: the handler that reads a typed
    message knows what the text means from the state alone.
    """

    translation = State()
    example = State()
    notes = State()


# States in which the next plain text is consumed by the flow itself: a review
# answer, a clarification answer, a new deck name, an edited card field. Guards
# give these a pass, because repeating an answer is legitimate. Button-only
# states (``AddWords.confirming``, ``ImportFile.confirming``,
# ``Practice.awaiting_answer``) are deliberately absent: nothing in them reads a
# typed message, so it falls through to the capture router and can cost a model
# call — it must stay guarded.
#
# A new text-consuming flow belongs here; the guards read this single set
# through :func:`is_text_input_state`.
TEXT_INPUT_STATES: frozenset[str] = frozenset(
    {
        # ``State.state`` is typed ``str | None`` (a state may lack a group),
        # but these states belong to groups, so the full name is always there.
        cast(str, AddWords.clarifying.state),
        cast(str, Review.awaiting_answer.state),
        cast(str, DeckRename.awaiting_name.state),
        cast(str, CardEdit.translation.state),
        cast(str, CardEdit.example.state),
        cast(str, CardEdit.notes.state),
    }
)


def is_text_input_state(raw_state: str | None) -> bool:
    """Whether ``raw_state`` (as aiogram puts it in handler data) reads text."""
    return raw_state in TEXT_INPUT_STATES
