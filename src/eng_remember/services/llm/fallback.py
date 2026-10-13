"""Rule-based parser used when no model is configured.

It exists so that the bot is fully usable — and the whole test suite runnable —
without an API key, and so a provider outage degrades the bot to a plain
"word - translation" input instead of breaking it.

It understands lines such as::

    ubiquitous - вездесущий
    to run [rʌn] — бежать
    resilient: устойчивый, упругий
    слово: ubiquitous (вездесущий)
"""

from __future__ import annotations

import re

from eng_remember.errors import ParseError
from eng_remember.services.llm.schema import Intent, ParsedCard, ParseResponse

__all__ = ["FallbackParser", "parse_text"]

_ENTRY_SPLIT = re.compile(r"[\n;]+")

# Two shapes of leading chatter, and the colon is what tells them apart.
#
# "добавь слова: run - бежать" and "add new words: run - бежать" are labels and
# always end with a colon; "добавь run - бежать" is a bare imperative.
#
# Deliberately missing from the bare list: "add" and "save". Both are ordinary
# vocabulary — "save - спасать" must stay a card, not lose its first word.
_NOISE_WITH_COLON = re.compile(
    r"^\s*(?:добавь|добавить|запиши|запомни|заучи|add|save|new)\b[^:：\n]*[:：]\s*",
    re.IGNORECASE,
)
_NOISE_BARE = re.compile(
    r"^\s*(?:добавь|добавить|запиши|запомни|заучи)\b\s+",
    re.IGNORECASE,
)
_SEPARATORS = ("—", "–", "=", ":", "->", "→")
_HYPHEN = re.compile(r"\s+-\s+|\s+-\s*|^\s*-\s*")
_TRANSCRIPTION = re.compile(r"[\(\[]\s*/?([^\)\]]+/?)\s*[\)\]]")
_IPA_INLINE = re.compile(r"(?<!\w)(/[^/\s]+/)(?!\w)")


def parse_text(text: str) -> ParseResponse:
    """Best-effort extraction of cards from free text.

    Raises :class:`ParseError` when not a single line looks like a pair, so the
    caller can answer "I did not understand" instead of silently saving junk.
    """
    cards: list[ParsedCard] = []
    for chunk in _ENTRY_SPLIT.split(text):
        entry = _strip_noise(chunk).strip()
        if not entry:
            continue
        card = _parse_entry(entry)
        if card is not None:
            cards.append(card)

    if not cards:
        raise ParseError("no word/translation pairs found")
    return ParseResponse(intent=Intent.ADD_CARDS, cards=cards)


def _strip_noise(chunk: str) -> str:
    labelled = _NOISE_WITH_COLON.sub("", chunk)
    if labelled != chunk:
        return labelled
    return _NOISE_BARE.sub("", chunk)


def _parse_entry(entry: str) -> ParsedCard | None:
    term, _, translation = _split(entry)
    term = term.strip(" \t.,")
    translation = translation.strip(" \t.,")
    if not term or not translation:
        return None
    if not _looks_like_term(term):
        return None

    transcription = None
    match = _TRANSCRIPTION.search(term) or _IPA_INLINE.search(term)
    if match is not None:
        transcription = match.group(1).strip()
        term = (term[: match.start()] + term[match.end() :]).strip()

    return ParsedCard(term=term, translation=translation, transcription=transcription)


def _split(entry: str) -> tuple[str, str, str]:
    for separator in _SEPARATORS:
        if separator in entry:
            head, _, tail = entry.partition(separator)
            return head, separator, tail
    hyphen = _HYPHEN.search(entry)
    if hyphen is not None:
        return entry[: hyphen.start()], "-", entry[hyphen.end() :]
    return entry, "", ""


def _looks_like_term(term: str) -> bool:
    """Reject Russian-left/English-right mixups and pure-symbol noise."""
    if not any(char.isalpha() for char in term):
        return False
    letters = [char for char in term if char.isalpha()]
    latin = sum(1 for char in letters if char.isascii())
    return latin >= len(letters) / 2


class FallbackParser:
    """Object form of :func:`parse_text`, matching the LLM parser's interface."""

    name = "rules"

    @property
    def available(self) -> bool:
        return True

    async def parse(
        self,
        text: str,
        *,
        user_id: int | None = None,
    ) -> ParseResponse:
        del user_id
        return parse_text(text)
