"""File exchange: exporting the collection and importing a CSV of words.

The import is a two-step flow like capture's confirmation: the file is parsed,
de-duplicated and (when a model is configured) enriched into rows, the learner
sees exactly what will be saved, and only the confirmation writes anything.
"""

from __future__ import annotations

import csv
import io

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Document, Message

from eng_remember.bot import keyboards, rendering, texts
from eng_remember.bot.callbacks import FileImportCB
from eng_remember.bot.states import (
    FILE_DUPLICATES_KEY,
    FILE_FILLED_KEY,
    FILE_ROWS_KEY,
    FILE_SKIPPED_KEY,
    FILE_UNTRANSLATED_KEY,
    ImportFile,
)
from eng_remember.domain.enums import CardDirection
from eng_remember.domain.languages import (
    DEFAULT_LANGUAGE,
    NATIVE_LANGUAGE,
    detect_script_language,
    resolve_language,
)
from eng_remember.errors import LlmError
from eng_remember.models.user import User
from eng_remember.repositories.cards import CardDraft
from eng_remember.services.cards import ImportOutcome
from eng_remember.services.container import Services
from eng_remember.services.csv_io import (
    MAX_FILE_BYTES,
    CsvParseResult,
    CsvRow,
    cards_to_csv,
    parse_cards_csv,
)
from eng_remember.services.llm.schema import ParsedCard
from eng_remember.utils.time import utcnow

log = structlog.get_logger(__name__)

EXPORT_LIMIT = 5000
MAX_TRANSLATION_FILL = 30
# Column widths from the models: the schema's cleaning is more generous.
TRANSCRIPTION_WIDTH = 128
PART_OF_SPEECH_WIDTH = 32
DECK_NAME_WIDTH = 128


def build_router() -> Router:
    """Create the router and register its handlers."""
    router = Router(name="files")

    @router.message(Command("export"))
    async def export_cards(message: Message, user: User, services: Services) -> None:
        # Forward cards only: a word is a pair in the database, but one row in
        # the file — a round-trip recreates the pair from the forward side.
        cards = await services.cards.list_for_user(
            user.id, limit=EXPORT_LIMIT, direction=CardDirection.FORWARD
        )
        decks = await services.decks.list_for_user(user.id)
        names = {deck.id: deck.name for deck in decks}
        content = cards_to_csv(cards, deck_names=names).encode("utf-8-sig")
        await message.answer_document(
            BufferedInputFile(content, filename=f"eng-remember-{utcnow():%Y-%m-%d}.csv"),
            caption=texts.file_export_caption(len(cards)),
        )

    @router.message(Command("import"))
    async def import_help(message: Message) -> None:
        await message.answer(texts.file_import_prompt(), reply_markup=keyboards.main_menu())

    @router.message(F.document)
    async def import_document(
        message: Message,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        document = message.document
        if document is None:
            return
        name = (document.file_name or "").lower()
        if not name.endswith((".csv", ".txt")):
            await message.answer(texts.file_import_prompt(), reply_markup=keyboards.main_menu())
            return

        await state.clear()
        payload = await _read_document(bot, document)
        if len(payload) > MAX_FILE_BYTES:
            await message.answer(texts.file_import_too_big(), reply_markup=keyboards.main_menu())
            return
        text = _decode(payload)
        if text is None:
            await message.answer(texts.file_import_unreadable(), reply_markup=keyboards.main_menu())
            return

        try:
            parsed = parse_cards_csv(text)
        except csv.Error:
            # A field beyond csv's own limit is a malformed file, not a crash.
            await message.answer(texts.file_import_unreadable(), reply_markup=keyboards.main_menu())
            return
        rows, duplicates, untranslated, filled = await _prepare_rows(user, services, parsed)
        if not rows:
            await message.answer(texts.file_import_empty(), reply_markup=keyboards.main_menu())
            return

        await state.set_state(ImportFile.confirming)
        await state.update_data(
            {
                FILE_ROWS_KEY: [_row_to_dict(row) for row in rows],
                FILE_SKIPPED_KEY: parsed.skipped,
                FILE_DUPLICATES_KEY: duplicates,
                FILE_UNTRANSLATED_KEY: untranslated,
                FILE_FILLED_KEY: filled,
            }
        )
        await message.answer(
            texts.file_import_preview(
                rows,
                duplicates=duplicates,
                untranslated=untranslated,
                skipped=parsed.skipped,
                filled=filled,
            ),
            reply_markup=keyboards.file_import_confirm(),
        )

    @router.callback_query(FileImportCB.filter(F.action == "confirm"))
    async def confirm_import(
        query: CallbackQuery,
        bot: Bot,
        user: User,
        services: Services,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        rows = _rows_from_state(data.get(FILE_ROWS_KEY))
        if await state.get_state() != ImportFile.confirming.state or not rows:
            await query.answer(texts.stale_button(), show_alert=True)
            return

        await state.clear()
        outcome = await _save_rows(user, services, rows)
        await services.commit()
        await query.answer()
        await rendering.edit_or_send(
            query,
            texts.file_import_result(outcome),
            bot=bot,
            reply_markup=keyboards.empty_keyboard(),
        )

    @router.callback_query(FileImportCB.filter(F.action == "cancel"))
    async def cancel_import(
        query: CallbackQuery,
        bot: Bot,
        state: FSMContext,
    ) -> None:
        # Only the live confirmation owns cancel: an old button from a spent
        # preview must not clear whatever flow is active now.
        if await state.get_state() != ImportFile.confirming.state:
            await query.answer(texts.stale_button(), show_alert=True)
            return
        await state.clear()
        await query.answer()
        await rendering.edit_or_send(
            query,
            texts.file_import_cancelled(),
            bot=bot,
            reply_markup=keyboards.empty_keyboard(),
        )

    return router


async def _read_document(bot: Bot, document: Document) -> bytes:
    """Download an attachment. A separate function so tests can replace it."""
    buffer = io.BytesIO()
    await bot.download(document, destination=buffer)
    return buffer.getvalue()


def _decode(payload: bytes) -> str | None:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        # cp1251 decodes nearly any byte soup, including UTF-16 files (which
        # then arrive full of NULs) — refuse rather than import mojibake.
        if "\x00" in text:
            return None
        return text
    return None


async def _prepare_rows(
    user: User,
    services: Services,
    parsed: CsvParseResult,
) -> tuple[list[CsvRow], int, int, int]:
    """Cleaned, de-duplicated rows plus the counters the preview owns."""
    cleaned: list[CsvRow] = []
    for row in parsed.rows:
        if row.language is not None:
            if resolve_language(row.language) == NATIVE_LANGUAGE:
                parsed.skipped += 1
                continue
        elif detect_script_language(row.term) is None:
            # An old file without the optional column cannot say what an
            # unsupported script means; saving it as English would be a lie.
            parsed.skipped += 1
            continue
        cleaned.append(_clean_row(row))
    terms_by_language: dict[str, list[str]] = {}
    for row in cleaned:
        language = row.language or DEFAULT_LANGUAGE
        terms_by_language.setdefault(language, []).append(row.term)

    known_by_language = {
        language: await services.cards.existing_terms(user.id, terms, language=language)
        for language, terms in terms_by_language.items()
    }

    rows: list[CsvRow] = []
    seen: set[tuple[str, str]] = set()
    duplicates = 0

    for row in cleaned:
        language = row.language or DEFAULT_LANGUAGE
        key = (language, row.term.casefold())
        if key in seen or row.term.casefold() in known_by_language.get(language, set()):
            duplicates += 1
            continue
        seen.add(key)
        rows.append(row)

    missing = [index for index, row in enumerate(rows) if row.translation is None]
    filled = 0
    if missing and services.translator.available:
        groups: dict[str, list[int]] = {}
        for index in missing:
            language = rows[index].language or DEFAULT_LANGUAGE
            groups.setdefault(language, []).append(index)
        remaining = MAX_TRANSLATION_FILL
        for language, indices in groups.items():
            if remaining <= 0:
                break
            batch = indices[:remaining]
            try:
                mapping = await services.translator.translate(
                    terms=[rows[index].term for index in batch],
                    language=language,
                    user_id=user.id,
                )
            except LlmError as exc:
                # The import is still worth doing; the preview will count what
                # remained without a meaning.
                log.warning("file_import_translation_failed", error=str(exc))
                mapping = {}
            for index in batch:
                translated = mapping.get(rows[index].term.casefold())
                if translated:
                    rows[index] = replace_translation(rows[index], translated)
                    filled += 1
            remaining -= len(batch)

    untranslated = sum(1 for row in rows if row.translation is None)
    return [row for row in rows if row.translation is not None], duplicates, untranslated, filled


def _clean_row(row: CsvRow) -> CsvRow:
    """The same cleaning model output gets, plus the column widths."""
    language = row.language or detect_script_language(row.term) or DEFAULT_LANGUAGE
    parsed = ParsedCard(
        term=row.term,
        translation=row.translation or "",
        language=language,
        transcription=row.transcription,
        part_of_speech=row.part_of_speech,
        example=row.example,
        notes=row.notes,
        tags=list(row.tags),
    )
    return CsvRow(
        term=parsed.term,
        translation=parsed.translation or None,
        transcription=(parsed.transcription or "")[:TRANSCRIPTION_WIDTH] or None,
        part_of_speech=(parsed.part_of_speech or "")[:PART_OF_SPEECH_WIDTH] or None,
        example=parsed.example,
        notes=parsed.notes,
        tags=tuple(parsed.tags),
        deck=(row.deck or "")[:DECK_NAME_WIDTH] or None,
        language=parsed.language,
    )


def replace_translation(row: CsvRow, translation: str) -> CsvRow:
    return CsvRow(
        term=row.term,
        translation=translation,
        transcription=row.transcription,
        part_of_speech=row.part_of_speech,
        example=row.example,
        notes=row.notes,
        tags=row.tags,
        deck=row.deck,
        language=row.language,
    )


async def _save_rows(user: User, services: Services, rows: list[CsvRow]) -> ImportOutcome:
    """Save per deck: a named deck is created when the file introduces it."""
    groups: dict[str | None, list[CsvRow]] = {}
    for row in rows:
        groups.setdefault(row.deck, []).append(row)

    total = ImportOutcome()
    for deck_name, deck_rows in groups.items():
        deck = None
        if deck_name:
            deck = await services.decks.get_by_name(user.id, deck_name)
            if deck is None:
                deck = await services.decks.create(user.id, deck_name)
        drafts = [_row_to_draft(row) for row in deck_rows]
        outcome = await services.card_service.add_drafts(user, drafts, deck=deck, source="import")
        total.added.extend(outcome.added)
        total.skipped_existing.extend(outcome.skipped_existing)
        total.skipped_in_batch.extend(outcome.skipped_in_batch)
    return total


def _row_to_draft(row: CsvRow) -> CardDraft:
    language = row.language or detect_script_language(row.term) or DEFAULT_LANGUAGE
    return CardDraft(
        term=row.term,
        translation=row.translation or "",
        language=language,
        transcription=row.transcription,
        part_of_speech=row.part_of_speech,
        example=row.example,
        notes=row.notes,
        tags=row.tags,
    )


def _row_to_dict(row: CsvRow) -> dict[str, object]:
    return {
        "term": row.term,
        "translation": row.translation,
        "transcription": row.transcription,
        "part_of_speech": row.part_of_speech,
        "example": row.example,
        "notes": row.notes,
        "tags": list(row.tags),
        "deck": row.deck,
        "language": row.language,
    }


def _rows_from_state(raw: object) -> list[CsvRow]:
    if not isinstance(raw, list):
        return []
    rows: list[CsvRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = item.get("term")
        translation = item.get("translation")
        if not isinstance(term, str) or not isinstance(translation, str):
            continue
        tags = item.get("tags")
        rows.append(
            CsvRow(
                term=term,
                translation=translation,
                transcription=_optional_str(item.get("transcription")),
                part_of_speech=_optional_str(item.get("part_of_speech")),
                example=_optional_str(item.get("example")),
                notes=_optional_str(item.get("notes")),
                tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
                deck=_optional_str(item.get("deck")),
                language=_optional_str(item.get("language")),
            )
        )
    return rows


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
