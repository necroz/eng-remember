"""Finding more words for a topic, or inside a text the learner pasted.

The model proposes, this service disposes. Everything the model cannot know is
checked here:

* words the learner already has are dropped, exactly rather than approximately;
* when a source text was given, a suggestion that does not actually occur in it
  is dropped — that check is the difference between "10 words from your
  paragraph" and "10 words the model made up";
* the result is cut to the number that was asked for.

Suggestions are requested with a margin, because both checks above only ever
remove entries.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog

from eng_remember.config import DiscoverySettings
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft, CardRepository
from eng_remember.services.llm.explorer import WordExplorer

__all__ = ["DiscoveryResult", "DiscoveryService", "appears_in_text"]

log = structlog.get_logger(__name__)

# Latin words with internal hyphens and apostrophes: "well-known", "don't".
_WORDS = re.compile(r"[a-z][a-z'\-]*")

# How many leading characters have to match for a word to count as the same
# lemma. Four handles the common English inflections — run/running,
# develop/development — without matching unrelated words.
_STEM = 4

# Terms named in the prompt as already known.
_KNOWN_HINTS = 40


@dataclass(slots=True)
class DiscoveryResult:
    """Suggestions plus the accounting behind them."""

    topic: str = ""
    drafts: list[CardDraft] = field(default_factory=list)
    requested: int = 0
    engine: str = ""
    dropped_known: int = 0
    dropped_repeated: int = 0
    dropped_out_of_text: int = 0

    @property
    def found(self) -> int:
        return len(self.drafts)

    @property
    def short_by(self) -> int:
        """How many of the requested words are missing."""
        return max(0, self.requested - self.found)


class DiscoveryService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        explorer: WordExplorer,
        settings: DiscoverySettings,
    ) -> None:
        self._cards = cards
        self._explorer = explorer
        self._settings = settings

    @property
    def available(self) -> bool:
        return self._explorer.available

    def resolve_count(self, requested: int | None) -> int:
        """Clamp a requested number into the configured range."""
        if requested is None:
            return self._settings.default_count
        return max(1, min(requested, self._settings.max_count))

    async def expand(
        self,
        user: User,
        *,
        topic: str | None = None,
        context_text: str | None = None,
        seeds: Sequence[str] = (),
        count: int | None = None,
        exclude: Sequence[str] = (),
        user_id: int | None = None,
    ) -> DiscoveryResult:
        """Ask the model for words and keep only the ones that hold up.

        ``exclude`` drops terms without mentioning them to the model — used when
        the learner asks for another round from a batch still on screen, where
        naming the rejected words would only anchor the next answer to them.
        """
        wanted = self.resolve_count(count)
        topic = (topic or "").strip()
        context = (context_text or "").strip() or None

        existing = await self._cards.all_terms(user.id)
        seed_terms = [seed.strip() for seed in seeds if seed.strip()]
        excluded = (
            existing
            | {seed.lower() for seed in seed_terms}
            | {term.strip().lower() for term in exclude if term.strip()}
        )

        ask_for = min(
            self._settings.max_count + _KNOWN_HINTS,
            max(wanted, math.ceil(wanted * self._settings.oversample)),
        )

        response = await self._explorer.expand(
            count=ask_for,
            topic=topic or None,
            context_text=context,
            seeds=seed_terms,
            known=sorted(existing)[:_KNOWN_HINTS],
            user_id=user_id if user_id is not None else user.id,
        )

        result = DiscoveryResult(
            topic=(response.topic or topic).strip(),
            requested=wanted,
            engine=self._explorer.engine,
        )
        seen: set[str] = set()

        for draft in response.to_drafts():
            key = draft.term.lower()
            if key in excluded:
                result.dropped_known += 1
                continue
            if key in seen:
                result.dropped_repeated += 1
                continue
            if context is not None and not appears_in_text(draft.term, context):
                result.dropped_out_of_text += 1
                continue

            seen.add(key)
            result.drafts.append(draft)
            if len(result.drafts) >= wanted:
                break

        if result.dropped_out_of_text:
            # A lot of these means the model is inventing words rather than
            # reading the text, which is worth seeing in the logs.
            log.warning(
                "discovery_out_of_text",
                dropped=result.dropped_out_of_text,
                kept=result.found,
                topic=result.topic,
            )
        return result


def appears_in_text(term: str, text: str) -> bool:
    """Whether the term — or a plausible form of it — occurs in the text.

    Matching is by four-character stem, so "run" is accepted for "running" and
    the other way round. Irregular forms ("go"/"went") fail the check and are
    dropped; that is the deliberate direction of the error, because a word that
    is not in the text is worse than one word short of the requested count.
    """
    words = _WORDS.findall(text.lower())
    if not words:
        return False

    needle = term.lower().strip()
    if not needle:
        return False

    parts = [part for part in re.split(r"[\s\-]+", needle) if part]
    if not parts:
        return False
    return all(_token_appears(part, words) for part in parts)


def _token_appears(token: str, words: Sequence[str]) -> bool:
    stem = token[:_STEM]
    if not stem:
        return False
    return any(word.startswith(stem) or token.startswith(word[:_STEM]) for word in words)
