"""Application settings.

Everything tunable lives here and is read from environment variables (optionally
via a ``.env`` file). Nested sections use a double underscore, so the LLM key is
``LLM__API_KEY`` and the retention target is ``SRS__DESIRED_RETENTION``.

Settings are passed around explicitly instead of being imported as a global
singleton: the bot builds them once in ``__main__``, tests build their own.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ParseModeName = Literal["HTML", "MarkdownV2", "Markdown"]


class BotSettings(BaseModel):
    """How the Telegram side behaves."""

    parse_mode: ParseModeName = "HTML"
    default_deck_name: str = "Основная колода"
    default_timezone: str = "UTC"
    new_cards_per_day: int = Field(default=20, ge=0)
    reviews_per_session: int = Field(default=50, ge=1)
    practice_size: int = Field(default=10, ge=4, le=50, description="Questions per practice quiz")
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    drop_pending_updates: bool = False


class LlmSettings(BaseModel):
    """OpenAI-compatible chat completions endpoint.

    The same code talks to OpenAI, OpenRouter, DeepSeek, vLLM or Ollama — only
    ``base_url``, ``api_key`` and ``model`` differ. With no key the bot falls
    back to the rule-based parser and stays fully usable offline.
    """

    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr | None = None
    model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_output_tokens: int = Field(default=1500, ge=1)
    retries: int = Field(default=2, ge=0, description="Extra attempts after a bad answer")
    max_calls_per_hour: int = Field(
        default=60,
        ge=0,
        description="Model calls per learner in any rolling hour; 0 disables the check",
    )
    max_tokens_per_day: int = Field(
        default=300_000,
        ge=0,
        description="Tokens per learner in any rolling 24 hours; 0 disables the check",
    )

    @property
    def is_configured(self) -> bool:
        if not self.enabled or self.api_key is None:
            return False
        return bool(self.api_key.get_secret_value().strip())


class SrsSettings(BaseModel):
    """Tuning knobs of the FSRS scheduler.

    ``desired_retention`` is the probability of recall the scheduler aims at when
    it picks the next interval: 0.9 means "show it again when the chance of
    remembering drops to 90%". Higher values mean shorter intervals, more work,
    better retention.
    """

    desired_retention: float = Field(default=0.9, gt=0.7, le=0.99)
    maximum_interval_days: int = Field(default=365, ge=1)
    enable_fuzzing: bool = True
    learning_steps_minutes: tuple[int, ...] = (1, 10)
    relearning_steps_minutes: tuple[int, ...] = (10,)


class ReviewSettings(BaseModel):
    """How a typed answer turns into a grade."""

    easy_threshold_seconds: float = Field(
        default=5.0,
        gt=0,
        description="A correct answer faster than this earns the easy grade",
    )
    leech_threshold: int = Field(
        default=4,
        ge=2,
        description="Failed answers before the treatment menu is offered",
    )
    examples_per_card: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Example sentences kept per card; prompts ask for up to three",
    )


class StreakSettings(BaseModel):
    """Rules of the daily streak and its freezes.

    A day counts as soon as the learner answers one card; the bar is deliberately
    low, because the mechanic rewards coming back daily, not volume.
    """

    enabled: bool = True
    freeze_initial: int = Field(default=1, ge=0, le=10)
    freeze_max: int = Field(default=3, ge=0, le=10)
    freeze_every_days: int = Field(default=7, ge=0, description="0 disables earning")
    milestones: tuple[int, ...] = (3, 7, 14, 30, 50, 100, 200, 365)


class DiscoverySettings(BaseModel):
    """How many extra words to look for, and how long to keep asking."""

    default_count: int = Field(default=10, ge=1, le=50)
    max_count: int = Field(default=30, ge=1, le=100)
    # Suggestions are filtered against the collection and, for text-based
    # discovery, against the text itself — so ask for more than was requested.
    oversample: float = Field(default=1.4, ge=1.0, le=3.0)
    max_clarification_rounds: int = Field(default=2, ge=0, le=5)
    context_text_min_length: int = Field(
        default=200,
        ge=0,
        description="A message at least this long is treated as source text, not a topic",
    )


class ReminderSettings(BaseModel):
    """When the bot is allowed to write first.

    Reminders are dispatched by a periodic tick that catches up rather than by
    exact-time scheduling: a bot that was down at 09:00 still sends the morning
    reminder when it starts at 10:00, and nobody gets a message at 03:00 because
    a timer drifted.
    """

    enabled: bool = True
    tick_seconds: int = Field(default=600, ge=30, le=3600)
    default_time: time = time(9, 0)
    default_streak_time: time = time(20, 0)
    digest_weekday: int = Field(
        default=0,
        ge=0,
        le=6,
        description="Weekday of the weekly digest; 0 is Monday, as date.weekday() counts",
    )
    digest_enabled: bool = Field(
        default=True,
        description="Global switch for the digest; learners can also switch theirs off",
    )
    # How many learners are fetched and decided at once. A tick always walks the
    # whole candidate list — a ceiling here would mean accounts past it never got
    # looked at, because every tick restarts from the lowest id.
    batch_size: int = Field(default=200, ge=1, le=2000)


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: SecretStr | None = None
    # Port matches the mapping published by docker-compose.yml.
    database_url: str = "postgresql+asyncpg://eng:eng@localhost:55432/eng_remember"
    db_echo: bool = False
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    log_level: str = "INFO"
    log_json: bool = True
    log_llm_payloads: bool = False

    bot: BotSettings = BotSettings()
    llm: LlmSettings = LlmSettings()
    srs: SrsSettings = SrsSettings()
    review: ReviewSettings = ReviewSettings()
    streak: StreakSettings = StreakSettings()
    discovery: DiscoverySettings = DiscoverySettings()
    reminders: ReminderSettings = ReminderSettings()

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def require_bot_token(settings: Settings) -> str:
    """The token, or a message explaining how to get one.

    Both entry points need it, and both should fail the same readable way rather
    than with a traceback through the event loop.
    """
    from eng_remember.errors import ConfigurationError

    token = settings.bot_token
    value = token.get_secret_value().strip() if token is not None else ""
    if not value:
        raise ConfigurationError(
            "BOT_TOKEN is not set. Copy .env.example to .env and paste the token "
            "you got from @BotFather."
        )
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for process entrypoints (bot, CLI)."""
    return Settings()
