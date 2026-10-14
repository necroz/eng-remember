# Батч C: экспорт и импорт CSV — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/export` отдаёт файл со всеми словами; вложение CSV/текста импортируется с предпросмотром, дедупом, созданием колод и дозаполнением переводов LLM.

**Architecture:** Чистый CSV-формат — `services/csv_io.py`; перевод терминов — новый LLM-компонент `TermTranslator`; поток и предпросмотр — новый роутер `bot/routers/files.py` с отдельным FSM-состоянием и колбэками `FileImportCB`; импортируемые поля чистятся правилами `ParsedCard`.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy async, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-14-csv-export-import-design.md`

## Global Constraints

- Формат: `;`, UTF-8 с BOM на экспорте, заголовок, колонки `term;translation;transcription;part_of_speech;example;notes;tags;deck`, теги через `|`.
- Импорт: sniff `; , \t`, необязательный заголовок, лимиты 1 МБ / 1000 строк, перевод — бюджет 30 терминов одним вызовом, остальные без перевода пропускаются.
- Чистка полей — через `ParsedCard`, затем усечение до ширин колонок (`part_of_speech` 32, `transcription` 128, `deck` 128).
- Дедуп в масштабе пользователя; пустая колода → основная; незнакомая → создаётся.
- Никаких FSRS-полей в файле; после каждого таска `make check` зелёный; русские коммиты.

## Review Focus

1. Файл с дубликатами/существующими словами: счётчики честные, данные не портятся.
2. Строки без перевода при выключенной модели: пропущены с явным счётчиком, не ломают импорт.
3. Повторный/устаревший тап «Сохранить»/«Отмена»: alert, без двойного импорта.
4. Колода из строки файла: создаётся, слова ложатся в неё; пустая → основная.
5. Битые входы: слишком большой файл, пустой, мусорные байты — понятные сообщения без трейсбеков.
6. Значения из файла не проходят в БД сырыми (prompt injection/HTML) и не превышают ширины колонок.

---

### Task 1: Чистый CSV — `services/csv_io.py`

**Files:**
- Create: `src/eng_remember/services/csv_io.py`
- Test: `tests/test_csv_io.py`

**Interfaces:**
- Produces: `CSV_DELIMITER`, `MAX_FILE_BYTES`, `MAX_ROWS`, `CsvRow`, `CsvParseResult`,
  `cards_to_csv(cards: Sequence[Card]) -> str`, `parse_cards_csv(text: str) -> CsvParseResult`.

- [ ] **Step 1: Write the failing tests** (заголовок и `;` на экспорте; `|` в тегах; roundtrip; sniff `,`/`\t`; BOM; заголовок; строка без term; отсутствие перевода в счётчике; ragged-строка; лимит строк; пустой ввод).
- [ ] **Step 2: Run** `uv run pytest tests/test_csv_io.py -q` → FAIL (модуля нет).
- [ ] **Step 3: Implement.** `cards_to_csv` через `csv.writer(StringIO, delimiter=CSV_DELIMITER, lineterminator="\r\n")`; `parse_cards_csv` — снять BOM, `csv.Sniffer` с фолбэком, позиционные колонки, до `MAX_ROWS`, `tags` сплит по `|`/`,`.
- [ ] **Step 4: Run tests + `make check`** → PASS.
- [ ] **Step 5: Commit** `feat: чистый CSV-формат экспорта и импорта`.

---

### Task 2: LLM-переводчик — `TermTranslator`

**Files:**
- Modify: `src/eng_remember/services/llm/schema.py` (`TranslationsResponse`, `TermTranslation`)
- Modify: `src/eng_remember/services/llm/prompts.py` (`TRANSLATION_SYSTEM_PROMPT`, `build_translation_prompt`)
- Create: `src/eng_remember/services/llm/translator.py`
- Modify: `src/eng_remember/services/container.py` (`Services.translator`)
- Test: `tests/test_llm_translator.py`

**Interfaces:**
- Produces: `TermTranslator.available`; `TermTranslator.translate(*, terms: Sequence[str], user_id: int | None = None) -> dict[str, str]` (ключ — термин в `casefold`); схемы с чисткой как у `ParsedCard`.

- [ ] **Step 1: Write the failing tests** (ответ модели → маппинг; пустой ответ; отказ без модели; `user_id` в вызове и `purpose == "translate_terms"`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** по образцу enricher; промпт оборачивает термины в `<terms>`; schema чистит `flatten_line`, режет пустые, дедуп по `casefold`.
- [ ] **Step 4: Run tests + `make check` → PASS.**
- [ ] **Step 5: Commit** `feat: LLM-переводчик терминов для импорта`.

---

### Task 3: Роутер файлов, предпросмотр, сохранение

**Files:**
- Modify: `src/eng_remember/bot/callbacks.py` (`FileImportCB`, prefix `fi`)
- Modify: `src/eng_remember/bot/states.py` (`ImportFile`, `FILE_ROWS_KEY`, `FILE_SKIPPED_KEY`, `FILE_UNTRANSLATED_KEY`)
- Modify: `src/eng_remember/bot/keyboards.py` (`file_import_confirm`)
- Modify: `src/eng_remember/bot/texts.py` (инструкция, предпросмотр, результат, отмены, ошибки)
- Create: `src/eng_remember/bot/routers/files.py`
- Modify: `src/eng_remember/bot/dispatcher.py`, `src/eng_remember/__main__.py`, `README.md`
- Test: `tests/test_bot_files.py` (+ `tests/factories.py`: `make_document_update`)

**Interfaces:**
- Consumes: Task 1/2; `card_service.add_drafts`, `decks.get_by_name/create`, `cards.existing_terms`.
- Produces: команды `/export`, `/import`; обработчик документов; `_read_document(bot, message) -> bytes` (точка подмены в тестах).

- [ ] **Step 1: Write the failing tests**

`tests/test_bot_files.py`:
```python
# экспорт: seed 2 карточки → /export → SendDocument, файл .csv,
# содержимое (decode utf-8-sig) содержит заголовок и термины
# /import: инструкция
# импорт: monkeypatch _read_document → CSV-байты → предпросмотр со счётчиками,
# tap fi:confirm → карточки созданы, колода из строки создана, tags на месте
# дубликаты: существующий термин → счётчик, не перезаписан
# без модели: строка без перевода пропущена, в предпросмотре это сказано
# с моделью (llm_dispatcher + push_translations): перевод дозаполнен
# отмена: fi:cancel → ничего не создано
# устаревший повторный fi:confirm → alert
# слишком большой файл / пустой → понятные сообщения
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Поток: `state.clear()` → `_read_document` → лимит → decode (`utf-8-sig` → `cp1251`) → `parse_cards_csv` → `ParsedCard`-чистка + усечение ширин → дедуп внутри файла и по `existing_terms` → перевод до 30 через `translator` (ошибки/квота → просто 0 заполненных, лог) → предпросмотр + состояние; confirm: группы по колодам, `add_drafts`, сводка, `edit_or_send`, `state.clear`; cancel: отмена. Стейл-кнопки — проверка состояния.
- [ ] **Step 4: Run tests + `make check` + `make test-integration` → PASS.**
- [ ] **Step 5: Commit** `feat: экспорт и импорт CSV в боте`.

---

## Self-Review

- Покрытие спеки: формат/парсер — Task 1; переводчик — Task 2; поток/предпросмотр/колоды/ошибки — Task 3; README/help — Task 3.
- Review Focus: 1/5 — Task 1 + Task 3 тесты; 2 — Task 3; 3 — Task 3 stale-тест; 4 — Task 3; 6 — Task 3 (чистка через ParsedCard) + Task 1.
