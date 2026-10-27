"""Exception hierarchy.

Every error raised by this project derives from :class:`EngRememberError`, so
callers can catch one type at the boundary (the bot's global error handler) and
still distinguish infrastructure failures from domain ones.
"""

from __future__ import annotations


class EngRememberError(Exception):
    """Base class for all project errors."""


class ConfigurationError(EngRememberError):
    """The process is not configured well enough to start."""


class LlmError(EngRememberError):
    """Base class for LLM failures."""


class LlmNotConfiguredError(LlmError):
    """No API key or the client is switched off in settings."""


class LlmResponseError(LlmError):
    """The model answered, but the answer could not be used.

    Raised when the payload is not JSON, does not match the expected schema, or
    the provider returned an error after the retry budget was spent.
    """


class LlmQuotaExceededError(LlmError):
    """The learner's own model usage passed a configured limit.

    Raised before the call, so a limited account spends nothing on it.
    """


class LlmGlobalBudgetExceededError(LlmError):
    """The bot-wide token budget is spent.

    Distinct from a per-learner refusal: the learner did nothing wrong, so the
    bot falls back to rules instead of telling them their own limit is gone.
    """


class RecipientUnreachable(EngRememberError):
    """The user cannot be messaged — blocked the bot, deleted the chat, gone.

    Distinct from a transport failure on purpose: retrying will never help, so
    the account is marked blocked instead of being tried again every tick.
    """


class ParseError(EngRememberError):
    """User input could not be turned into cards by any available parser."""


class DomainError(EngRememberError):
    """A rule of the domain was violated."""


class DuplicateCardError(DomainError):
    """The user already has this term in this deck."""

    def __init__(self, term: str) -> None:
        super().__init__(f"card already exists: {term}")
        self.term = term


class CardNotFoundError(DomainError):
    """A card referenced by id does not exist or is not owned by the user."""

    def __init__(self, card_id: int) -> None:
        super().__init__(f"card not found: {card_id}")
        self.card_id = card_id
