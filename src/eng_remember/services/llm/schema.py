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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from eng_remember.domain.answers import Verdict
from eng_remember.domain.languages import DEFAULT_LANGUAGE, resolve_target_language
from eng_remember.repositories.cards import CardDraft

__all__ = [
    "AnswerCheckResponse",
    "Clarification",
    "ExamplesResponse",
    "ExpansionResponse",
    "Intent",
    "MnemonicResponse",
    "ParseResponse",
    "ParsedCard",
    "TermTranslation",
    "TranslationsResponse",
    "extract_json_object",
    "flatten_line",
]

# Control characters (including newlines and tabs) become spaces; everything
# else keeps its meaning. The second variant is for free text whose line
# structure is the input itself (a list typed one word per line): the newline
# survives as a separator, every other control character still becomes a space.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Prose keeps its paragraphs: only the other control characters are stripped.
_CONTROL_CHARS_EXCEPT_NEWLINES = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_HORIZONTAL_WHITESPACE = re.compile(r"[ \t\f\v]+")
_ANGLE_BRACKETS = re.compile(r"[<>]")

# Caps are generous for the field they guard: a term is a word or a short
# phrase, an example is one sentence.
TERM_LIMIT = 80
TRANSLATION_LIMIT = 200
TRANSCRIPTION_LIMIT = 80
# Equal to the ``part_of_speech`` column width: a longer value would only be
# cut by the database (or rejected) later.
PART_OF_SPEECH_LIMIT = 32
EXAMPLE_LIMIT = 300
NOTES_LIMIT = 300
MNEMONIC_LIMIT = 200
TAG_LIMIT = 40
TOPIC_LIMIT = 80
QUESTION_LIMIT = 200
OPTION_LIMIT = 48

# An explanation may be several paragraphs, but it is still one Telegram
# message; the ceiling is what keeps a runaway answer from filling the chat.
ANSWER_LIMIT = 2000

# The biggest batch a user can plausibly ask for. Everything past it is dropped
# in code rather than rejected: the first cards of a long answer are still good.
CARDS_LIMIT = 50

# The prompts ask for up to three examples; the hard ceiling is higher so a
# generous model is not rejected, and everything past it is dropped in code.
EXAMPLES_LIMIT = 5

# A CEFR level is exactly one letter and one digit; anything else (ranges,
# other frameworks, prose) is treated as "no estimate" rather than guessed at.
_CEFR = re.compile(r"^[ABC][12]$")


def _clean_target_language(value: object) -> str:
    """An ISO 639-3 target code; Russian and unknown labels degrade to English."""
    return resolve_target_language(value if isinstance(value, str) else None)


# What models actually say for frequency, mapped onto the one vocabulary.
_FREQUENCY_ALIASES: dict[str, str] = {
    "common": "common",
    "частое": "common",
    "частотное": "common",
    "uncommon": "uncommon",
    "обычное": "uncommon",
    "среднее": "uncommon",
    "rare": "rare",
    "редкое": "rare",
    "редкостное": "rare",
}


def flatten_line(
    value: str,
    *,
    limit: int,
    strip_brackets: bool = False,
    keep_newlines: bool = False,
) -> str:
    """Sanitise free text, capped at ``limit``.

    By default everything becomes one line with whitespace collapsed — the rule
    for a value that is stored (a term, a topic, an option) or placed inside a
    wrapper where a newline would be structure. ``keep_newlines`` is for a whole
    message whose line breaks are the learner's own formatting, such as a
    vocabulary list typed one word per line: newlines survive as separators,
    every other control character is neutralised, runs of horizontal whitespace
    collapse to one space, each line is stripped, and only the outer edges are
    trimmed after the cap.

    ``strip_brackets`` is for values that will be interpolated into a prompt
    wrapper or a Telegram message: with ``<`` and ``>`` gone the value cannot
    imitate the tags around it.
    """
    if keep_newlines:
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = _CONTROL_CHARS_EXCEPT_NEWLINES.sub(" ", text)
        if strip_brackets:
            text = _ANGLE_BRACKETS.sub("", text)
        lines = (_HORIZONTAL_WHITESPACE.sub(" ", line).strip() for line in text.split("\n"))
        return "\n".join(lines).strip()[:limit].strip()

    flat = _CONTROL_CHARS.sub(" ", value)
    if strip_brackets:
        flat = _ANGLE_BRACKETS.sub("", flat)
    return " ".join(flat.split())[:limit].strip()


def _accept_string_examples(value: object) -> object:
    """Some models return one newline-joined string instead of a list."""
    if isinstance(value, str):
        return [part for part in value.splitlines() if part.strip()]
    return value


def _clean_example_list(examples: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in examples:
        flat = flatten_line(item, limit=EXAMPLE_LIMIT)
        if not flat or flat.casefold() in seen:
            continue
        seen.add(flat.casefold())
        cleaned.append(flat)
        if len(cleaned) == EXAMPLES_LIMIT:
            break
    return cleaned


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

    term: str = Field(description="Word or short phrase in the studied language")
    translation: str = Field(description="Russian translation")
    language: str = Field(
        default=DEFAULT_LANGUAGE,
        description="ISO 639-3 code of the studied language",
    )
    transcription: str | None = Field(
        default=None, description="IPA or standard transcription, with slashes"
    )
    part_of_speech: str | None = Field(default=None, description="noun, verb, adjective…")
    example: str | None = Field(
        default=None, description="One short example sentence in the studied language"
    )
    examples: list[str] = Field(
        default_factory=list,
        description="Up to three short example sentences in the studied language",
    )
    notes: str | None = Field(default=None, description="Usage hint, false friend, register")
    tags: list[str] = Field(default_factory=list)
    cefr: str | None = Field(default=None, description="CEFR level, A1 to C2")
    frequency: str | None = Field(default=None, description="common, uncommon or rare")

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

    @field_validator("language", mode="before")
    @classmethod
    def _clean_language(cls, value: object) -> str:
        return _clean_target_language(value)

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

    @field_validator("examples", mode="before")
    @classmethod
    def _accept_string_examples(cls, value: object) -> object:
        return _accept_string_examples(value)

    @field_validator("examples")
    @classmethod
    def _clean_examples(cls, examples: list[str]) -> list[str]:
        return _clean_example_list(examples)

    @field_validator("cefr", mode="before")
    @classmethod
    def _clean_cefr(cls, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = flatten_line(value, limit=8, strip_brackets=True).upper()
        return cleaned if _CEFR.fullmatch(cleaned) else None

    @field_validator("frequency", mode="before")
    @classmethod
    def _clean_frequency(cls, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = flatten_line(value, limit=16, strip_brackets=True).casefold()
        return _FREQUENCY_ALIASES.get(cleaned)

    @model_validator(mode="after")
    def _sync_example_forms(self) -> ParsedCard:
        """Keep ``example`` and ``examples`` telling the same story.

        Small models fill only the singular field; ours fill the list. Both are
        accepted, and afterwards the main example is the first of the list, so
        rotation and the revealed card cannot disagree.
        """
        if not self.examples and self.example:
            self.examples = [self.example]
        elif self.examples:
            if not self.example:
                self.example = self.examples[0]
            elif self.example not in self.examples:
                self.examples = [self.example, *self.examples][:EXAMPLES_LIMIT]
        return self

    def to_draft(self) -> CardDraft:
        return CardDraft(
            term=self.term,
            translation=self.translation,
            language=self.language,
            transcription=self.transcription,
            part_of_speech=self.part_of_speech,
            example=self.example,
            examples=tuple(self.examples),
            notes=self.notes,
            tags=tuple(self.tags),
            cefr=self.cefr,
            frequency=self.frequency,
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
    language: str = Field(
        default=DEFAULT_LANGUAGE,
        description="Target language for discovery or expansion",
    )
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
    more_words: bool = Field(
        default=False,
        description=(
            "True when the message also asks for more words on the same topic, "
            "alongside the cards to save"
        ),
    )
    clarification: Clarification | None = Field(
        default=None,
        description="Set when the request cannot be acted on without one more detail",
    )

    @model_validator(mode="before")
    @classmethod
    def _split_add_and_expand(cls, data: object) -> object:
        """Read a model's own name for the mixed request as both halves of it.

        "add_and_expand" is not in the vocabulary this schema teaches, but it
        says exactly what the learner asked for — treating it as a plain
        addition would silently drop the second half, which is the failure this
        flag exists to prevent.
        """
        if isinstance(data, dict):
            raw = data.get("intent")
            if isinstance(raw, str):
                normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
                if normalized == "add_and_expand":
                    return {**data, "intent": "add_cards", "more_words": True}
        return data

    @field_validator("count")
    @classmethod
    def _sane_count(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return value if value > 0 else None

    @field_validator("language", mode="before")
    @classmethod
    def _clean_language(cls, value: object) -> str:
        return _clean_target_language(value)

    @model_validator(mode="after")
    def _language_from_first_card(self) -> ParseResponse:
        """Fill the discovery language when the model did not state one.

        A missing top-level field is different from an explicit ``null``: the
        latter was a deliberate choice and has already become English in the
        field validator. Only an absent field inherits the first card.
        """
        if "language" not in self.model_fields_set:
            self.language = self.cards[0].language if self.cards else DEFAULT_LANGUAGE
        return self

    @field_validator("topic")
    @classmethod
    def _blank_topic(cls, value: str | None) -> str | None:
        # The topic is stored on the deck and interpolated into later prompts;
        # flattening here is what keeps that loop safe.
        cleaned = flatten_line(value or "", limit=TOPIC_LIMIT, strip_brackets=True)
        return cleaned or None

    @field_validator("answer")
    @classmethod
    def _cap_answer(cls, value: str | None) -> str | None:
        """Explanations are prose, so newlines stay; only the ceiling applies.

        Other control characters do not: the answer is stored and sent to
        Telegram, and a stray NUL or escape sequence is never part of prose.
        """
        if not value:
            return None
        cleaned = _CONTROL_CHARS_EXCEPT_NEWLINES.sub(" ", value)
        return cleaned[:ANSWER_LIMIT].strip() or None

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
        # Drop first, then cap: a truncated answer's last card is still a whole
        # card, not an empty slot.
        complete = [card for card in cards if card.term and card.translation]
        return complete[:CARDS_LIMIT]

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
        return [card for card in cards if card.term and card.translation][:CARDS_LIMIT]

    def to_drafts(self) -> list[CardDraft]:
        return [card.to_draft() for card in self.cards]


class ExamplesResponse(BaseModel):
    """Fresh example sentences for an already existing card."""

    model_config = ConfigDict(str_strip_whitespace=True)

    examples: list[str] = Field(default_factory=list)

    @field_validator("examples", mode="before")
    @classmethod
    def _accept_string_examples(cls, value: object) -> object:
        return _accept_string_examples(value)

    @field_validator("examples")
    @classmethod
    def _clean_examples(cls, examples: list[str]) -> list[str]:
        return _clean_example_list(examples)


class MnemonicResponse(BaseModel):
    """One short memory hook for a card, in the learner's own language."""

    model_config = ConfigDict(str_strip_whitespace=True)

    mnemonic: str = Field(min_length=1)

    @field_validator("mnemonic")
    @classmethod
    def _clean_mnemonic(cls, value: str) -> str:
        cleaned = flatten_line(value, limit=MNEMONIC_LIMIT, strip_brackets=True)
        if not cleaned:
            raise ValueError("mnemonic is empty")
        return cleaned


class TermTranslation(BaseModel):
    """One term and its Russian meaning, as the translator returns it."""

    model_config = ConfigDict(str_strip_whitespace=True)

    term: str = Field(description="Term in the target language")
    translation: str = Field(description="Russian meaning")

    @field_validator("term")
    @classmethod
    def _clean_term(cls, value: str) -> str:
        return flatten_line(value, limit=TERM_LIMIT)

    @field_validator("translation")
    @classmethod
    def _clean_translation(cls, value: str) -> str:
        return flatten_line(value, limit=TRANSLATION_LIMIT)


class TranslationsResponse(BaseModel):
    """Batch translation of bare terms, for CSV imports."""

    model_config = ConfigDict(str_strip_whitespace=True)

    translations: list[TermTranslation] = Field(default_factory=list)

    @field_validator("translations")
    @classmethod
    def _drop_incomplete(cls, entries: list[TermTranslation]) -> list[TermTranslation]:
        return [entry for entry in entries if entry.term and entry.translation]


# Near-misses seen from smaller models, mapped onto the three verdicts.
_VERDICT_ALIASES: dict[str, Verdict] = {
    "correct": Verdict.CORRECT,
    "верно": Verdict.CORRECT,
    "верный": Verdict.CORRECT,
    "правильно": Verdict.CORRECT,
    "yes": Verdict.CORRECT,
    "exact": Verdict.CORRECT,
    "almost": Verdict.ALMOST,
    "частично": Verdict.ALMOST,
    "почти": Verdict.ALMOST,
    "почти_верно": Verdict.ALMOST,
    "partial": Verdict.ALMOST,
    "close": Verdict.ALMOST,
    "wrong": Verdict.WRONG,
    "неверно": Verdict.WRONG,
    "неправильно": Verdict.WRONG,
    "нет": Verdict.WRONG,
    "no": Verdict.WRONG,
    "incorrect": Verdict.WRONG,
    "different": Verdict.WRONG,
}


class AnswerCheckResponse(BaseModel):
    """The model's judgement of one typed answer."""

    model_config = ConfigDict(str_strip_whitespace=True)

    verdict: Verdict

    @field_validator("verdict", mode="before")
    @classmethod
    def _coerce_verdict(cls, value: object) -> object:
        """Accept near-misses instead of discarding the answer.

        A model that replies "верно" or "Almost" has made exactly the judgement
        this call exists for; rejecting the payload over its spelling would turn
        a usable verdict into the fallback "wrong" for no reason.
        """
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        return _VERDICT_ALIASES.get(normalized, normalized)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _matching_brace(text: str, start: int) -> int | None:
    """Index of the ``}`` that closes the ``{`` at ``start``, if any.

    Quoted strings are skipped, including escaped quotes inside them: a ``}``
    in ``{"c": "}"}`` is text, not structure.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def extract_json_object(raw: str) -> str:
    """Pull a JSON object out of a model answer.

    Local models routinely wrap the payload in a markdown fence or add a
    sentence around it even when asked not to. Slicing from the first ``{`` to
    the last ``}`` breaks as soon as prose contains braces of its own or the
    answer holds two objects, so this scans for the first balanced object that
    actually parses instead.
    """
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced is not None:
        text = fenced.group(1).strip()

    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        first_error = exc
    else:
        return text

    start = text.find("{")
    while start != -1:
        end = _matching_brace(text, start)
        if end is not None:
            candidate = text[start : end + 1]
            try:
                json.loads(candidate)
            except json.JSONDecodeError:
                pass
            else:
                return candidate
        start = text.find("{", start + 1)

    raise first_error
