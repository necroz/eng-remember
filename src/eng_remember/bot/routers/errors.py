"""Last-resort error handling.

Every unhandled exception from a handler ends up here. It is logged with the
full traceback and the user gets one plain sentence; the update is marked as
handled so a broken handler does not turn into an infinite retry loop.
"""

from __future__ import annotations

import structlog
from aiogram import Bot, Router
from aiogram.types import CallbackQuery, ErrorEvent, Message

from eng_remember.bot import texts
from eng_remember.bot.rendering import callback_chat_id


def build_router() -> Router:
    """Create the router and register its handlers.

    A factory rather than a module-level singleton: aiogram refuses to
    attach one Router to two Dispatchers, which would make a second
    dispatcher (a test harness, a second bot) impossible.
    """
    router = Router(name="errors")

    log = structlog.get_logger(__name__)

    @router.errors()
    async def handle_error(event: ErrorEvent, bot: Bot) -> bool:
        update = event.update
        log.error(
            "handler_failed",
            error=str(event.exception),
            error_type=type(event.exception).__name__,
            update_id=update.update_id,
            exc_info=event.exception,
        )

        try:
            await _apologise(bot, update.message, update.callback_query)
        except Exception as exc:
            log.warning("error_notice_failed", error=str(exc))

        return True

    async def _apologise(
        bot: Bot,
        message: Message | None,
        callback: CallbackQuery | None,
    ) -> None:
        if callback is not None:
            chat_id = callback_chat_id(callback)
            try:
                await callback.answer("Что-то сломалось, уже смотрю логи", show_alert=True)
            except Exception:
                # The callback may have expired; a plain message still reaches
                # the user, which is the point of this handler.
                await bot.send_message(chat_id, texts.global_error())
            return

        if message is not None:
            await message.answer(texts.global_error())

    return router
