"""Resolve the Telegram account into a project user."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.types import User as TelegramUser

from eng_remember.models.user import User
from eng_remember.repositories.users import TelegramProfile
from eng_remember.services.container import Services
from eng_remember.utils.time import utcnow

__all__ = ["UserMiddleware"]

log = structlog.get_logger(__name__)


class UserMiddleware(BaseMiddleware):
    """Create the user row on first contact and inject it into the handler.

    Runs inside ``DbSessionMiddleware``: it needs the session to exist, and every
    handler below it can then assume ``user`` is present.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        services = cast(Services, data.get("services"))
        from_user = cast(TelegramUser | None, data.get("event_from_user"))
        if services is None or from_user is None:
            return await handler(event, data)

        user, created = await services.users.get_or_create(
            TelegramProfile(
                telegram_id=from_user.id,
                username=from_user.username,
                first_name=from_user.first_name,
                language_code=from_user.language_code,
            ),
            default_timezone=services.settings.bot.default_timezone,
            new_cards_per_day=services.settings.bot.new_cards_per_day,
        )
        await services.users.touch(user, when=utcnow())

        if created:
            # Onboard the account completely. Creating the default deck here
            # rather than lazily means no read path has to create state as a
            # side effect, and the welcome text is truthful from the start.
            await services.decks.get_or_create_default(
                user.id, name=services.settings.bot.default_deck_name
            )

        data["user"] = user
        data["is_new_user"] = created

        if user.is_blocked and not _is_unblock_command(event):
            log.info("blocked_user_ignored", telegram_id=from_user.id)
            return None

        return await handler(event, data)


def _is_unblock_command(event: TelegramObject) -> bool:
    """``/start`` is allowed through so a blocked user can ask to be restored."""
    return isinstance(event, Message) and event.text is not None and event.text.startswith("/start")


def as_user(data: dict[str, Any]) -> User:
    return cast(User, data["user"])
