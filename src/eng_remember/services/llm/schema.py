"""Structured output contract for the model.

The schema is the interface between "a sentence a human typed" and the rest of
the system: the model fills these objects, pydantic validates them, and only then
does anything reach the database. Validators here are intentionally forgiving —
a slightly sloppy answer from a small local model should still produce cards.

One rule is not forgiving. Everything the model returns is untrusted input: the
values are stored, and later embedded into prompts (``<topic>…</topic>``, the
"already known" list) and into Telegram HTML. A newline or an angle bracket
inside such a value is structure, not text — so every free-form string is
flattened to one capped line at this boundary. A stored topic can then never
close the wrapper it sits in on the next request, start a line that reads as an
instruction, or carry markup past the escaping at render time.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from eng_remember.repositories.cards import CardDraft

__all__ = [
    "Clarification",
    "ExpansionResponse",
    "Intent",
    "ParseResponse",
    "ParsedCard",
    "extract_json_object",
    "flatten_line",
]

# Control characters (including newlines and tabs) become spaces; everything
# else keeps its meaning.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ANGLE_BRACKETS = re.compile(r"[<>]")

# Caps are generous for the field they guard: a term is a word or a short
# phrase, an example is one sentence.
TERM_LIMIT = 80
TRANSLATION_LIMIT = 200
TRANSCRIPTION_LIMIT = 80
PART_OF_SPEECH_LIMIT = 40
EXAMPLE_LIMIT = 300
NOTES_LIMIT = 300
TAG_LIMIT = 40
TOPIC_LIMIT = 80
QUESTION_LIMIT = 200
OPTION_LIMIT = 48


def flatten_line(value: str, *, limit: int, strip_brackets: bool = False) -> str:
    """One line, whitespace collapsed, capped at ``limit``.

    ``strip_brackets`` is for values that will be interpolated into a prompt
    wrapper or a Telegram message: with ``<`` and ``>`` gone the value cannot
    imitate the tags around it.
    """
    flat = _CONTROL_CHARS.sub(" ", value)
    if strip_brackets:
        flat = _ANGLE_BRACKETS.sub("", flat)
    return " ".join(flat.split())[:limit].strip()


class Intent(StrEnum):
    """What the user is asking for."""

    ADD_CARDS = "add_cards"
    EXPAND = "expand"
    EXPLAIN = "explain"
    UNKNOWN = "unknown"


# Synonyms seen in practice from smaller models, mapped onto our three intents.
_INTENT_ALIASES: dict[str, Intent] = {
    "add": Intent.ADD_CARDS,
    "adding": Intent.ADD_CARDS,
    "more": Intent.EXPAND,
    "more_words": Intent.EXPAND,
    "expand": Intent.EXPAND,
    "suggest": Intent.EXPAND,
    "generate": Intent.EXPAND,
    "similar": Intent.EXPAND,
    "from_text": Intent.EXPAND,
    "add_word": Intent.ADD_CARDS,
    "add_words": Intent.ADD_CARDS,
    "create_cards": Intent.ADD_CARDS,
    "save": Intent.ADD_CARDS,
    "vocabulary": Intent.ADD_CARDS,
    "explain_word": Intent.EXPLAIN,
    "question": Intent.EXPLAIN,
    "ask": Intent.EXPLAIN,
    "translate": Intent.EXPLAIN,
    "greeting": Intent.UNKNOWN,
    "chat": Intent.UNKNOWN,
    "other": Intent.UNKNOWN,
    "none": Intent.UNKNOWN,
}


class ParsedCard(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    term: str = Field(description="English word or short phrase")
    translation: str = Field(description="Russian translation")
    transcription: str | None = Field(default=None, description="IPA, with slashes")
    part_of_speech: str | None = Field(default=None, description="noun, verb, adjective…")
    example: str | None = Field(default=None, description="One short English example sentence")
    notes: str | None = Field(default=None, description="Usage hint, false friend, register")
    tags: list[str] = Field(default_factory=list)

    @field_validator("term")
    @classmethod
    def _clean_term(cls, value: str) -> str:
        # The term is stored and later re-enters prompts in the "already known"
        # list, so one line is a hard requirement, not cosmetics.
        return flatten_line(value, limit=TERM_LIMIT)

    @field_validator("translation")
    @classmethod
    def _clean_translation(cls, value: str) -> str:
        return flatten_line(value, limit=TRANSLATION_LIMIT)

    @field_validator("transcription")
    @classmethod
    def _wrap_transcription(cls, value: str | None) -> str | None:
        """Normalise IPA to the /…/ form learners expect."""
        if not value:
            return None
        stripped = flatten_line(value, limit=TRANSCRIPTION_LIMIT).strip("[]")
        if not stripped:
            return None
        if stripped.startswith("/") and stripped.endswith("/"):
            return stripped
        return f"/{stripped.strip('/')}/"

    @field_validator("part_of_speech", "example", "notes")
    @classmethod
    def _blank_to_none(cls, value: str | None, info: ValidationInfo) -> str | None:
        """``str_strip_whitespace`` turns "  " into "", not into ``None``.

        The distinction matters downstream: an absent example should be treated
        as absent, not rendered as an empty line on the card.
        """
        if value is None:
            return None
        limits = {
            "part_of_speech": PART_OF_SPEECH_LIMIT,
            "example": EXAMPLE_LIMIT,
            "notes": NOTES_LIMIT,
        }
        stripped = flatten_line(value, limit=limits[info.field_name or ""])
        return stripped or None

    @field_validator("part_of_speech")
    @classmethod
    def _lower_pos(cls, value: str | None) -> str | None:
        return value.lower() if value else None

    @field_validator("tags", mode="before")
    @classmethod
    def _accept_string_tags(cls, value: object) -> object:
        """Some models return "a, b" or a bare string instead of a list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, tags: list[str]) -> list[str]:
        # Tags become the deck's remembered topic, which is interpolated into
        # later prompts — same one-line rule as the topic itself.
        cleaned = [flatten_line(tag, limit=TAG_LIMIT, strip_brackets=True) for tag in tags]
        return [tag for tag in cleaned if tag]

    def to_draft(self) -> CardDraft:
        return CardDraft(
            term=self.term,
            translation=self.translation,
            transcription=self.transcription,
            part_of_speech=self.part_of_speech,
            example=self.example,
            notes=self.notes,
            tags=tuple(self.tags),
        ).normalized()


class Clarification(BaseModel):
    """A question the model wants answered before it can act.

    Options become quick-reply buttons, so the common answers cost one tap
    instead of a sentence. The bot always adds its own "decide for me" escape,
    so an empty list is fine.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def _clean_question(cls, value: str) -> str:
        # The question is repeated to the model in the clarified prompt,
        # wrapped in <question_you_asked>; flattening keeps it inside.
        cleaned = flatten_line(value, limit=QUESTION_LIMIT, strip_brackets=True)
        if not cleaned:
            raise ValueError("clarification question is empty")
        return cleaned

    @field_validator("options", mode="before")
    @classmethod
    def _accept_string_options(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("options")
    @classmethod
    def _keep_options_short(cls, options: list[str]) -> list[str]:
        # They have to fit on a Telegram button.
        cleaned = [
            flatten_line(option, limit=OPTION_LIMIT, strip_brackets=True) for option in options[:4]
        ]
        return [option for option in cleaned if option]


class ParseResponse(BaseModel):
    """Top-level object the model must return."""

    model_config = ConfigDict(str_strip_whitespace=True)

    intent: Intent = Intent.ADD_CARDS
    cards: list[ParsedCard] = Field(default_factory=list)
    answer: str | None = Field(
        default=None,
        description="Short reply for the user when intent is explain",
    )
    topic: str | None = Field(
        default=None,
        description="Domain or context for expand, e.g. 'переговоры', 'медицина'",
    )
    count: int | None = Field(
        default=None,
        description="How many words were asked for, when the user named a number",
    )
    clarification: Clarification | None = Field(
        default=None,
        description="Set when the request cannot be acted on without one more detail",
    )

    @field_validator("count")
    @classmethod
    def _sane_count(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return value if value > 0 else None

    @field_validator("topic")
    @classmethod
    def _blank_topic(cls, value: str | None) -> str | None:
        # The topic is stored on the deck and interpolated into later prompts;
        # flattening here is what keeps that loop safe.
        cleaned = flatten_line(value or "", limit=TOPIC_LIMIT, strip_brackets=True)
        return cleaned or None

    @field_validator("intent", mode="before")
    @classmethod
    def _coerce_intent(cls, value: object) -> object:
        """Accept near-misses instead of discarding the whole answer.

        A model that replies "greeting" or "save_word" has clearly not followed
        the instructions, but the cards in the same payload may still be perfect.
        Rejecting the payload over a label would throw that away — and the audit
        table already records what was asked and what came back.
        """
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in Intent._value2member_map_:
            return normalized
        return _INTENT_ALIASES.get(normalized, Intent.UNKNOWN)

    @field_validator("cards")
    @classmethod
    def _drop_incomplete(cls, cards: list[ParsedCard]) -> list[ParsedCard]:
        return [card for card in cards if card.term and card.translation]

    def to_drafts(self) -> list[CardDraft]:
        return [card.to_draft() for card in self.cards]


class ExpansionResponse(BaseModel):
    """Words found for a topic or inside a text."""

    model_config = ConfigDict(str_strip_whitespace=True)

    topic: str = Field(default="", description="How the model understood the domain")
    cards: list[ParsedCard] = Field(default_factory=list)

    @field_validator("topic")
    @classmethod
    def _clean_topic(cls, value: str) -> str:
        return flatten_line(value, limit=TOPIC_LIMIT, strip_brackets=True)

    @field_validator("cards")
    @classmethod
    def _drop_incomplete(cls, cards: list[ParsedCard]) -> list[ParsedCard]:
        return [card for card in cards if card.term and card.translation]

    def to_drafts(self) -> list[CardDraft]:
        return [card.to_draft() for card in self.cards]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_object(raw: str) -> str:
    """Pull a JSON object out of a model answer.

    Local models routinely wrap the payload in a markdown fence or add a
    sentence around it even when asked not to, so the client tries the fenced
    block first and then the outermost braces before giving up.
    """
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced is not None:
        text = fenced.group(1).strip()

    try:
        json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        text = text[start : end + 1]
    return text
