# Исправления по код-ревью — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** исправить все Critical/Important/Minor находки ревью 2026-11-14 и внедрить предложенные улучшения, сохранив зелёный набор проверок.

**Architecture:** точечные исправления в существующей архитектуре (bot → services → repositories → db; services → domain). Крупные изменения: process-wide LLM-транспорт вместо per-request клиента; централизованный контроль длины исходящих сообщений; FSM-aware guard'ы; единое правило дневного бюджета новых карточек; оптимистичная блокировка расписания; upsert'ы и функциональный unique-индекс.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy 2 async (PostgreSQL/SQLite), Alembic, py-fsrs, openai SDK, pydantic-settings, structlog, pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-code-review-findings.md` (идентификаторы C1…U7 ниже ссылаются на неё).

## Global Constraints

- Python 3.12; `uv run` для всех команд; worktree `/home/necroz/.local/share/opencode/worktree/dfb017/review-fixes`, ветка `fix/review-findings`.
- После каждой задачи: `uv run ruff check src tests alembic && uv run ruff format --check src tests alembic && uv run mypy` — чисто; `uv run pytest -m "not integration"` — зелёный. Интеграционные (`make test-integration`) — после задач, трогающих БД/модели/миграции, и в конце фазы.
- UI-тексты на русском; стиль существующих текстов (`bot/texts.py`), HTML-экранирование через `escape()` в точке интерполяции.
- Зависимости направлены bot → services → repositories → db; services → domain; domain без БД/Telegram.
- Новые runtime-зависимости не добавлять.
- TDD: сначала падающий тест, затем реализация. Коммит на каждую задачу (сообщение в стиле проекта: `fix: …` / `feat: …`).
- Миграции: линейная цепочка от текущего head, downgrade обязателен, `alembic check` чисто; DDL и backfill в разных ревизиях, если таблица может быть большой.
- Номера строк в спеке — ориентир на момент ревью; исполнитель сверяется с текущим кодом.

## Review Focus

1. Список из 200 пар «слово — перевод» в одном сообщении → предпросмотр рендерится, пакет сохраняется, бот не падает (задачи 4, 9).
2. Два одинаковых ответа подряд на разные карточки в review → оба засчитаны, dedup не мешает (задача 10).
3. Пользователь заблокировал бота, вернулся и написал /start → полностью восстановлен, /review работает (задача 1).
4. Бот поднялся в понедельник после вечернего часа → недельный дайджест всё равно приходит (задача 5).
5. Два параллельных первых сообщения от нового аккаунта → без IntegrityError и «что-то сломалось» (задача 28).

---

## Фаза 1 — быстрые исправления

### Task 1: Разблокировка по факту контакта (C1)

**Files:**
- Modify: `src/eng_remember/bot/middlewares/user.py`
- Test: `tests/test_bot_flow.py` (или новый `tests/test_bot_unblock.py`)

**Решение:** в `UserMiddleware.__call__` после `get_or_create`, если `user.is_blocked` — сбросить флаг (`user.is_blocked = False`, репозиторный метод `set_blocked(user, blocked=False)` уже существует) и продолжить обработку любого события. Удалить `_is_unblock_command` и особое исключение для `/start` — входящее событие само доказывает доставляемость. Лог `user_unblocked` (info) при сбросе.

- [ ] Тест `test_blocked_user_is_restored_on_any_message`: создать пользователя с `is_blocked=True`, отправить произвольный текст через диспетчер → ответ обработан (не `blocked_user_ignored`), флаг в БД сброшен.
- [ ] Тест полного цикла (Review Focus 3): блокировка → `/start` → `/review` работает; использовать существующий harness `tests/conftest.py`.
- [ ] Проверить падение тестов до фикса, реализовать, зелёный, коммит `fix: разблокировка пользователя при входящем событии`.

### Task 2: Фильтр приватных чатов (I9a)

**Files:**
- Modify: `src/eng_remember/bot/dispatcher.py`
- Test: `tests/test_bot_private_only.py` (новый)

**Решение:** на уровне диспетчера `dp.message.filter(F.chat.type == "private")` и `dp.callback_query.filter(F.message.chat.type == "private")` (или общий outer-middleware, отбрасывающий не-приватные апдейты до БД — предпочтительно middleware, чтобы не открывать сессию). Комментарий `rendering.py:66-72` остаётся верным.

- [ ] Тест `test_group_messages_are_ignored`: апдейт из группы (chat.type="group") не создаёт пользователя и не вызывает хендлеры; приватный апдейт работает.
- [ ] TDD-цикл, коммит `fix: бот обслуживает только приватные чаты`.

### Task 3: Надёжность файлового роутера (I9b, M27, M4)

**Files:**
- Modify: `src/eng_remember/bot/routers/files.py`
- Test: `tests/test_bot_files.py`

**Решение:**
1. В `import_document` проверять `document.file_size` (если известен) против `MAX_FILE_BYTES` ДО скачивания; послезагрузочная проверка остаётся backstop.
2. FSM очищать только после успешной валидации файла (размер/кодировка/парсинг), а не до.
3. Экспорт: при `total > EXPORT_LIMIT` caption честно сообщает «показаны 5000 из N».

- [ ] Тест `test_oversized_document_rejected_before_download`: `document.file_size > MAX_FILE_BYTES`, `_read_document` не вызывается (мок падает, если вызван), пользователь получает сообщение о лимите, активный FSM review не сброшен.
- [ ] Тест `test_export_caption_mentions_truncation`: коллекция > EXPORT_LIMIT → caption содержит «из N».
- [ ] TDD-цикл, коммит `fix: проверка размера CSV до загрузки и честный экспорт`.

### Task 4: Согласование лимитов схемы и БД (I6, I1-часть)

**Files:**
- Modify: `src/eng_remember/services/llm/schema.py`, `src/eng_remember/repositories/cards.py`
- Test: `tests/test_llm_schema.py`, `tests/test_repositories.py`

**Решение:**
1. `PART_OF_SPEECH_LIMIT = 32` (равно ширине колонки `models/card.py:88`).
2. `ParseResponse.answer`: валидатор с лимитом 2000 символов (многострочный допускается, но с потолком) — по образцу остальных свободных полей.
3. `ParseResponse.cards`: кап 50 элементов (валидатор, лишние отбрасываются).
4. `CardDraft.normalized()`: защитная обрезка строковых полей до ширины колонок (`part_of_speech` 32; остальные сверить с `models/card.py` и применить тот же принцип последней точки перед персистентностью).

- [ ] Тест `test_part_of_speech_limit_matches_column`: `PART_OF_SPEECH_LIMIT == 32`; ответ модели с частью речи 40 символов нормализуется до 32.
- [ ] Тест `test_answer_is_capped`: answer на 5000 символов → ≤ 2000 после валидации.
- [ ] Тест `test_cards_list_is_capped`: 200 карточек в ответе → ≤ 50.
- [ ] Тест `test_normalized_draft_truncates_to_column_widths`: draft с part_of_speech 33+ символов сохраняется без ошибки (интеграционный, против PG).
- [ ] TDD-цикл, `make test-integration`, коммит `fix: лимиты LLM-схемы согласованы с шириной колонок`.

### Task 5: Приоритет дайджеста в его день (I4)

**Files:**
- Modify: `src/eng_remember/domain/reminders.py`
- Test: `tests/test_domain_reminders.py`

**Решение:** в `decide()` проверку `_digest_owed` поднять ВЫШЕ вечернего короткого замыкания (строки 81-82). Дайджест уже закрывает утренний слот через `covers_morning`; порядок: enabled/has_cards → digest-owed → evening → morning. Комментарий обновить.

- [ ] Тест `test_digest_sent_when_first_tick_is_monday_evening` (Review Focus 4): контекст с `last_digest_on=None`, `last_morning_on=None`, понедельник 19:00, очередь непустая → `ReminderKind.DIGEST` (не EVENING).
- [ ] Тест `test_digest_sent_monday_evening_with_empty_queue_and_dead_streak`: очередь пуста, серия мертва → всё равно DIGEST.
- [ ] Регрессия: вторник вечер → EVENING как раньше; существующие тесты не ломаются.
- [ ] TDD-цикл, коммит `fix: дайджест не теряется при первом тике вечером`.

### Task 6: Повторные алерты глобального бюджета (I8)

**Files:**
- Modify: `src/eng_remember/services/llm/budget.py`
- Test: `tests/test_budget.py`

**Решение:** в `check()` сбрасывать `_warning_sent`, когда `used * 100 < limit * global_alert_percent`, и `_critical_sent`, когда `used < limit` (расход скользящего окна упал — алерт может повториться). `reset()` больше НЕ обнуляет `_notifier` (только флаги). Докстринг модуля обновить («один алерт на эпизод превышения», а не «один за жизнь процесса»).

- [ ] Тест `test_warning_repeats_after_usage_drops`: used=85% → warning; used=50% → сброс; used=85% снова → второй warning (notifier вызван дважды).
- [ ] Тест `test_critical_repeats_after_usage_drops`: аналогично для critical.
- [ ] Тест `test_reset_keeps_notifier`: set_notifier → reset → check с расходом выше порога → notifier вызван.
- [ ] TDD-цикл, коммит `fix: алерты бюджета повторяются после спада расхода`.

### Task 7: Юникод-цифры в /limit и parse_clock (M1)

**Files:**
- Modify: `src/eng_remember/bot/routers/preferences.py`, `src/eng_remember/utils/time.py`
- Test: `tests/test_bot_preferences.py` (или существующий файл настроек), `tests/test_utils_time.py`

**Решение:** в обработчике `/limit` заменить `str.isdigit()` на `try: int(value) except ValueError` с валидационным сообщением (диапазон сверить с существующей валидацией). В `parse_clock` — тот же принцип: принимать только ASCII-цифры (`value.isascii() and value.isdigit()`), иначе `ValueError`/сообщение об ошибке формата.

- [ ] Тест `test_limit_rejects_unicode_digits`: `/limit ²` → валидационное сообщение, не generic-ошибка.
- [ ] Тест `test_parse_clock_rejects_unicode_digits`: «①②:③④» → ошибка формата.
- [ ] TDD-цикл, коммит `fix: юникод-цифры не роняют /limit и разбор времени`.

### Task 8: Документационная согласованность (M10, M30-часть, M35-часть)

**Files:**
- Modify: `README.md`, `docs/security.md`, `.env.example`

**Решение:**
1. README: порог «Легко» — 10 секунд (дефолт `config.py`), упомянуть настройку `REVIEW__EASY_THRESHOLD_SECONDS`.
2. security.md: `DB_ECHO=true` логирует SQL с пользовательскими текстами — предупреждение; семантика квот «60 вызовов = 60 provider-запросов, включая ретраи»; audit-строки могут откатиться при сбое хендлера после вызова (известное ограничение учёта).
3. `.env.example`: комментарии к `LLM__REASONING_EFFORT` (пусто = провайдер решает; дефолт меняется в задаче 15 — здесь только документация текущего поведения, обновится в 15).

- [ ] Вычитка: grep «5 секунд» в README — нет; коммит `docs: согласованы порог оценки, квоты и DB_ECHO`.

---

## Фаза 2 — надёжность bot-слоя

### Task 9: Safe-send и капы рендера (I1)

**Files:**
- Modify: `src/eng_remember/bot/rendering.py`, `src/eng_remember/bot/texts.py`, `src/eng_remember/bot/notifier.py`
- Test: `tests/test_rendering.py` (новый), `tests/test_texts.py` (новый или существующий)

**Interfaces:**
- Produces: `rendering.TELEGRAM_TEXT_LIMIT = 4096`; `rendering.fit_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str` — если текст длиннее лимита, обрезает по последней границе строки до лимита и добавляет «\n… (сообщение сокращено)»; гарантированно ≤ limit. Не режет внутри HTML-тега: если граница строки попадает внутрь тега, обрезает по предыдущей границе строки.

**Решение:**
1. `edit_message_or_send`, `edit_or_send` и отправка в `notifier.py` прогоняют текст через `fit_text` перед вызовом Telegram.
2. Капы рендера в `texts.py`: `import_preview` — первые 30 драфтов + «Показаны первые 30 из N — сохранение добавит все»; `discovery_preview` — тот же кап 30; `deck_list` — первые 50 колод + «…и ещё N»; `paused_list` — первые 20 + «…и ещё N». Константы в texts.py.
3. `explain` уже защищён капом answer (задача 4) + fit_text.

- [ ] Тест `test_fit_text_truncates_on_line_boundary`: текст 6000 символов из строк → результат ≤ 4096, заканчивается суффиксом, теги не разорваны.
- [ ] Тест `test_import_preview_caps_drafts` (Review Focus 1): 200 драфтов → рендер ≤ 4096, содержит «30 из 200».
- [ ] Тест `test_deck_list_caps_decks`: 200 колод → рендер ≤ 4096, «…и ещё 150».
- [ ] Тест `test_edit_or_send_fits_long_text`: бот-мок получает текст ≤ 4096.
- [ ] TDD-цикл, коммит `fix: исходящие сообщения не превышают лимит Telegram`.

### Task 10: FSM-aware dedup и rate-limit (I2-часть, M25, U4)

**Files:**
- Modify: `src/eng_remember/bot/middlewares/dedup.py`, `src/eng_remember/bot/middlewares/rate_limit.py`, `src/eng_remember/config.py`
- Test: `tests/test_middlewares_guards.py` (новый)

**Решение:**
1. Dedup пропускает (не считает дубликатом): сообщения, когда у пользователя активно FSM-состояние (`await state.get_state() is not None`), и тексты reply-кнопок меню (константы клавиатур из `keyboards.py`). Ключ освобождать и при обработанных отказах — для этого dedup освобождает ключ, если хендлер вернул результат без исключения И состояние не активно (упрощение: освобождать ключ всегда после успешной обработки, если сообщение не было сохранено в FSM — исполнитель выбирает минимальный корректный вариант; цель: немедленный повтор после `cannot_parse` не считается дубликатом).
2. Rate-limit: дефолт `messages_per_minute` поднять до 30; сообщения при активном FSM-состоянии не засчитываются (льгота review/practice/capture). Ответ об отказе слать не чаще одного раза в окно на пользователя.
3. Периодический prune: rate-limit удаляет пустые/устаревшие бакеты при обходе (например, при каждом N-ном вызове); dedup чистит протухшие ключи при каждом обращении, не только при ≥1000.
4. Middleware получают доступ к FSM через `data["state"]` (aiogram кладёт FSMContext в data на outer-уровне — проверить порядок; если state недоступен на уровне middleware, читать `data["raw_state"]`/storage через `dispatcher` — исполнитель сверяется с aiogram 3.31 и выбирает рабочий путь; тесты это закрепляют).

- [ ] Тест `test_identical_review_answers_not_deduped` (Review Focus 2): активное состояние Review, два одинаковых текста подряд → оба пропущены middleware.
- [ ] Тест `test_menu_button_press_not_deduped`: повторный текст кнопки меню через минуту → пропущен.
- [ ] Тест `test_retry_after_parse_error_not_deduped`: текст → cannot_parse → тот же текст снова → обработан.
- [ ] Тест `test_active_fsm_exempt_from_rate_limit`: 30+ сообщений при активном состоянии → не отклонены.
- [ ] Тест `test_rate_limit_reply_throttled`: флуд без FSM → ровно один ответ-отказ в окне.
- [ ] Тесты гоняют middleware с ВКЛЮЧЁнными guard'ами (не как conftest-фикстура с отключёнными).
- [ ] TDD-цикл, коммит `fix: guard'ы не мешают активному флоу повторения`.

### Task 11: Stale-guard capture-кнопок и catch-all callback (I2-часть, M28)

**Files:**
- Modify: `src/eng_remember/bot/routers/capture.py`, `src/eng_remember/bot/dispatcher.py`, `src/eng_remember/bot/routers/__init__.py`
- Test: `tests/test_bot_capture_stale.py` (новый)

**Решение:**
1. Все callback-хендлеры capture (confirm, cancel, skip, more, clarify-опции) проверяют FSM: confirm/cancel/more — `AddWords.confirming`, skip/clarify — `AddWords.clarifying` (сверить с текущими состояниями). Иначе — `stale_button()`-alert и `query.answer()` без действия, по образцу `files.py:152,175`. Вынести общий хелпер `require_state(query, state, expected) -> bool` в capture.py (или rendering.py), чтобы не дублировать.
2. Dispatcher-level catch-all на `callback_query` с нераспознанным payload: `query.answer("Кнопка устарела")` + лог warning — спиннер не остаётся навсегда. Регистрируется последним.
3. `routers/__init__.py`: `__all__` дополнить `files`, `practice`.

- [ ] Тест `test_stale_cancel_does_not_kill_review`: активный Review.awaiting_answer, callback cancel из старого пакета → review жив, следующий текст оценивается.
- [ ] Тест `test_stale_confirm_ignored`: нет состояния → alert, ничего не сохранено.
- [ ] Тест `test_unknown_callback_answered`: payload `xx:1` → query.answer вызван.
- [ ] TDD-цикл, коммит `fix: устаревшие кнопки не вмешиваются в чужой флоу`.

### Task 12: Фидбек на нетекстовый контент и молчаливые сбросы (M3)

**Files:**
- Modify: `src/eng_remember/bot/routers/capture.py`, `src/eng_remember/bot/routers/review.py`, `src/eng_remember/bot/texts.py`
- Test: `tests/test_bot_feedback.py` (новый)

**Решение:**
1. Catch-all на нетекстовые сообщения (фото/голос/стикеры/документы не-CSV) в приватном чате: короткий ответ «Я понимаю текст и CSV-файлы…» (текст в texts.py). Регистрировать ПОСЛЕ files-роутера, чтобы CSV не перехватывался.
2. Review: ранний ответ (набран до показа карточки) и пустой текст получают короткое пояснение вместо молчания (тексты в texts.py; сохранить поведение «не засчитывается» — только фидбек).

- [ ] Тест `test_photo_gets_reply`: фото → ответ про текст/CSV.
- [ ] Тест `test_early_answer_gets_feedback`: ответ до показа → пояснение, оценка не засчитана.
- [ ] TDD-цикл, коммит `feat: фидбек на нетекстовые сообщения и ранние ответы`.

### Task 13: Надёжность квиза и мнемоники (M26, M2)

**Files:**
- Modify: `src/eng_remember/services/practice.py`, `src/eng_remember/bot/keyboards.py`, `src/eng_remember/bot/routers/review.py`
- Test: `tests/test_practice.py`, `tests/test_bot_practice.py`, `tests/test_bot_flow.py`

**Решение:**
1. `_restore_questions`: проверять диапазон `correct_index` (0 ≤ idx < len(options)), повреждённые записи пропускать (функция заявлена как «skipping anything malformed»).
2. Опции квиза: обрезка до 60 символов с «…» (лимит Telegram 64 байта на кнопку — кириллица 2 байта/символ, поэтому 60 символов НЕ безопасно: обрезать по байтам — ≤ 60 байт UTF-8, граница символа). Если после обрезки два варианта совпали, добавить различающий суффикс (номер) — исполнитель реализует минимально: уникальные лейблы через нумерацию при коллизии.
3. Мнемоника/меню лича: до LLM-вызова edit сообщения в «Готовлю…» со снятием кнопок (или per-card резервация по образцу ANSWERING_KEY) — двойной тап не запускает два вызова.

- [ ] Тест `test_restore_skips_out_of_range_index`: запись с correct_index=99 → пропущена, без IndexError.
- [ ] Тест `test_quiz_option_labels_fit_bytes_and_unique`: два перевода с общим префиксом 80 символов → лейблы ≤ 60 байт и различимы.
- [ ] Тест `test_double_mnemonic_tap_single_call`: два быстрых тапа → один LLM-вызов.
- [ ] TDD-цикл, коммит `fix: квиз и меню лича устойчивы к повреждённым данным и двойным тапам`.

### Task 14: Согласованная справка и фиксация parse_mode (M5, M6)

**Files:**
- Modify: `src/eng_remember/bot/routers/start.py`, `src/eng_remember/bot/routers/preferences.py`, `src/eng_remember/bot/texts.py`, `src/eng_remember/config.py`
- Test: `tests/test_bot_help.py` (новый), `tests/test_config.py`

**Решение:**
1. Единый текст справки (texts.py): все команды `/review /practice /stats /decks /paused /import /export /level /remind /tz /limit /settings /help /start` с короткими описаниями; `/help` и кнопка «❓ Помощь» рендерят его.
2. `BotSettings.parse_mode`: валидатор, принимающий только "HTML" (остальное — понятная ошибка конфигурации при старте). Тип оставить `ParseModeName` для совместимости, но сузить валидатором; комментарий «тексты бота — только HTML».

- [ ] Тест `test_help_and_settings_help_identical`: оба рендера содержат все команды и совпадают.
- [ ] Тест `test_parse_mode_markdown_rejected`: `BotSettings(parse_mode="MarkdownV2")` → ValidationError.
- [ ] TDD-цикл, коммит `fix: единая справка и фиксация HTML parse_mode`.

---

## Фаза 3 — LLM-подсистема

### Task 15: Process-wide LLM-транспорт (C2)

**Files:**
- Modify: `src/eng_remember/services/llm/client.py`, `src/eng_remember/services/llm/factory.py`, `src/eng_remember/services/container.py`, `src/eng_remember/bot/middlewares/db.py`, `src/eng_remember/__main__.py`, `src/eng_remember/remind.py`, `src/eng_remember/services/enrichment_worker.py`, `src/eng_remember/config.py`, `.env.example`, `README.md`, `tests/conftest.py`
- Test: `tests/test_llm_transport.py` (новый), `tests/test_llm_client.py`

**Interfaces:**
- Produces:
  - `client.LlmTransport(settings: LlmSettings, *, client: OpenAiLike | None = None)` — владеет ленивым `AsyncOpenAI` и изученными флагами (`json_mode`, `reasoning_effort`, поддержка `response_format`); метод `async aclose()`. Флаги живут на транспорт, не на per-request клиент.
  - `OpenAiCompatibleClient(settings, *, sink=None, transport: LlmTransport | None = None, client: OpenAiLike | None = None)` — при переданном transport использует его (и НЕ закрывает в своём aclose); без transport создаёт собственный (для тестов/изоляции). Параметр `client` сохраняется для существующих тестов.
  - `factory.build_llm_client(session, settings, *, transport: LlmTransport | None = None)`.
  - `container.build_services(session, settings, *, llm_transport: LlmTransport | None = None)`.
  - `DbSessionMiddleware(session_factory, settings, *, llm_transport: LlmTransport | None = None)`.
  - `enrichment_worker` принимает `transport` параметром (сигнатуру сверить с текущей).

**Решение:**
1. Вынести создание `AsyncOpenAI` и изученные флаги из `OpenAiCompatibleClient` в `LlmTransport`; клиент делегирует транспорту `_create` и изучение отказов. Поведение ретраев/repair не меняется.
2. Entry points (`__main__.py`, `remind.py`) создают один `LlmTransport` на процесс (если LLM настроена), передают в middleware/воркер, закрывают при shutdown. `build_services` без transport (тесты) создаёт per-request транспорт как раньше — утечки в проде нет, потому что прод всегда передаёт общий.
3. `LlmSettings.reasoning_effort` дефолт `None` (не слать параметр, пока оператор не задал явно). Комментарий в config и `.env.example` обновить; README — примечание про reasoning-модели актуализировать.
4. Тестовый conftest: фикстуры, строящие Services, передают свой транспорт с fake-клиентом (сверить текущий путь внедрения fake).

- [ ] Тест `test_transport_shared_across_updates`: два `build_services` с одним transport → один underlying-клиент (fake считает создания).
- [ ] Тест `test_learned_rejection_persists`: транспорт получил 400 на reasoning_effort → следующий вызов через ДРУГОЙ per-request клиент не шлёт параметр.
- [ ] Тест `test_default_config_sends_no_reasoning_effort`: дефолтные настройки → параметр отсутствует в запросе.
- [ ] Тест `test_transport_aclose_closes_underlying`: aclose закрывает fake-клиент; per-request aclose НЕ закрывает общий транспорт.
- [ ] Существующие тесты клиента/квот зелёные.
- [ ] TDD-цикл, коммит `fix: общий LLM-транспорт на процесс вместо клиента на каждый запрос`.

### Task 16: Fail-closed квоты и TTL-кэш глобального расхода (M21, M24)

**Files:**
- Modify: `src/eng_remember/services/llm/quota.py`
- Test: `tests/test_llm_quota.py`

**Решение:**
1. При `user_id=None`: глобальный бюджет проверять ВСЕГДА (если настроен); per-user квоты и lock неприменимы — документировать в докстринге. Отказ глобального бюджета бросает `LlmGlobalBudgetExceededError` как обычно.
2. Глобальный расход кэшировать на процессе с TTL 30 с (`time.monotonic()`): повторные вызовы в окне TTL не делают SUM-запрос. Кэш инвалидируется при отказе бюджета не требуется (окно короткое). Кэш — атрибут `QuotaLimitedClient`-обёртки или модульный (исполнитель выбирает; цель — один SUM на 30 с, тест это проверяет).

- [ ] Тест `test_global_budget_enforced_without_user_id`: user_id=None, глобальный расход ≥ лимита → отказ.
- [ ] Тест `test_global_usage_cached_within_ttl`: два вызова подряд → один запрос `global_usage_since` (мок-счётчик); после TTL — второй.
- [ ] TDD-цикл, коммит `fix: глобальный бюджет проверяется всегда и кэшируется`.

### Task 17: Защитное фластение в промпт-билдерах (M22)

**Files:**
- Modify: `src/eng_remember/services/llm/prompts.py`
- Test: `tests/test_security.py`

**Решение:** `build_expansion_prompt` (topic/seeds/known), `build_user_prompt`, `build_clarified_prompt` прогоняют недоверенные строки через `flatten_line` (schema.py) перед вставкой — единообразно с остальными билдерами. Поведение для уже очищенных входов не меняется.

- [ ] Тест `test_expansion_prompt_flattens_raw_topic`: topic с `\n` и `<tag>` → в промпте одна строка без `<>`.
- [ ] Тест `test_user_prompt_flattens_raw_text`: аналогично для пользовательского текста.
- [ ] TDD-цикл, коммит `fix: единообразное фластение во всех промпт-билдерах`.

### Task 18: Робастность клиента: repair, JSON-сканер, дедлайн (M23)

**Files:**
- Modify: `src/eng_remember/services/llm/client.py`, `src/eng_remember/services/llm/schema.py`
- Test: `tests/test_llm_client.py`, `tests/test_llm_schema.py`

**Решение:**
1. Repair-сообщение: плохой ответ модели усекать до 2000 символов с пометкой «(обрезано)» перед переотправкой.
2. `extract_json_object`: балансный сканер строк/скобок (учитывать строки в кавычках и экранирование) вместо среза «первая { … последняя }»; при нескольких объектах брать первый сбалансированный. Существующие тесты на обёртки/markdown должны остаться зелёными.
3. Общий дедлайн: весь `complete_json` (все попытки) обёрнут в `asyncio.timeout(settings.timeout_seconds)`; per-attempt таймаут остаётся, но сумма попыток не превышает общий. Зависший провайдер больше не блокирует пользователя на retries×timeout.

- [ ] Тест `test_repair_echoes_truncated_bad_answer`: плохой ответ 5000 символов → repair-сообщение содержит ≤ 2000 + пометку.
- [ ] Тест `test_extract_json_balanced_with_nested_braces`: текст с prose-скобками и двумя объектами → первый валидный объект.
- [ ] Тест `test_total_deadline_bounds_retries`: fake-клиент висит дольше таймаута → общий дедлайн срабатывает раньше, чем retries×timeout (управляемое время через monkeypatch настроек с малым таймаутом).
- [ ] TDD-цикл, коммит `fix: repair дешевле, JSON-сканер балансный, общий дедлайн вызова`.

### Task 19: Structured outputs с детектом поддержки (U1)

**Files:**
- Modify: `src/eng_remember/services/llm/client.py`
- Test: `tests/test_llm_client.py`

**Решение:** при `json_mode` клиент сначала пробует `response_format={"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": true}}`; при отказе провайдера (BadRequest) изучает отказ на транспорте (как сейчас для json_object) и деградирует до `json_object`, затем до plain. Pydantic-схемы с `Optional`/дефолтами могут не пройти strict — при ошибке схемы тоже деградировать. Изученный флаг живёт на транспорте (задача 15).

- [ ] Тест `test_json_schema_used_when_supported`: fake принимает json_schema → запрос содержит strict-схему.
- [ ] Тест `test_json_schema_rejection_degrades_once`: fake отвергает json_schema → следующий вызов сразу json_object (без повторного отказа).
- [ ] TDD-цикл, коммит `feat: structured outputs с автоматической деградацией`.

### Task 20: Фоллбэк colon-форма и __all__ (M7, M11-часть)

**Files:**
- Modify: `src/eng_remember/services/llm/fallback.py`, `src/eng_remember/services/llm/__init__.py`
- Test: `tests/test_fallback_parser.py`

**Решение:**
1. `save: спасать`, `add: …`, `new: …` в colon-форме сохраняются как карточки (поведение согласовано с дефисной формой): шумовые слова исключать только когда за ними НЕ следует перевод-пара (сверить текущую логику `_NOISE_WITH_COLON`/`_NOISE_BARE` и минимально поправить).
2. `llm/__init__.py`: `__all__` привести в соответствие с реально импортированными именами (добавить импорты `Clarification`, `ExpansionResponse` либо убрать из `__all__` — выбрать импорт, если это публичные схемы).

- [ ] Тест `test_colon_form_save_is_a_card`: `save: спасать` → карточка save/спасать.
- [ ] Тест `test_noise_label_still_skipped`: `добавь: …` ведёт себя как раньше (регрессия существующих тестов).
- [ ] Тест `test_star_import_works`: `from eng_remember.services.llm import *` без AttributeError.
- [ ] TDD-цикл, коммит `fix: colon-форма фоллбэка и корректный __all__`.

---

## Фаза 4 — домен и сервисы

### Task 21: Единое правило «сколько новых доступно» (I3)

**Files:**
- Modify: `src/eng_remember/repositories/reviews.py`, `src/eng_remember/services/reminders.py`
- Test: `tests/test_services_reminders.py`, `tests/test_repositories.py`

**Interfaces:**
- Produces: `ReviewRepository.introduced_counts(user_ids: Sequence[int], *, since_by_user: Mapping[int, datetime]) -> dict[int, int]` — батчевый аналог `count_cards_introduced_since`: группировка пользователей по значению границы суток (образец `_answered_today` в reminders.py), один запрос на группу, `GROUP BY user_id, card_id HAVING min(reviewed_at) >= since`.

**Решение:**
1. Добавить `introduced_counts` в репозиторий.
2. В тике напоминаний после `queue_counts` считать для каждого пользователя `allowance = max(0, per_day - introduced_today)` (граница суток — по TZ пользователя, как в `_answered_today`) и `new_available = min(raw_new, allowance)`; `due` не меняется. `per_day = user.new_cards_per_day or settings.bot.new_cards_per_day`.
3. Докстринг `queue_counts` уточнить: «сырые счётчики; бюджет применяет вызывающий».
4. Проверить путь `/stats`: если он показывает новые через `build_queue` — уже корректен; если через сырой count — применить то же правило.

- [ ] Тест `test_reminder_does_not_promise_new_over_budget` (ключевой): пользователь выбрал лимит 20, ввёл 20 сегодня, новых в коллекции 100, due=0 → `has_work=False`, напоминание не обещает новых.
- [ ] Тест `test_reminder_new_count_respects_partial_budget`: ввёл 5 из 20, новых 100 → new_available=15.
- [ ] Тест `test_introduced_counts_batch`: несколько пользователей с разными границами суток → корректные счётчики одним проходом (интеграционный).
- [ ] TDD-цикл, `make test-integration`, коммит `fix: напоминания учитывают дневной бюджет новых карточек`.

### Task 22: Оптимистичная блокировка расписания + запас очереди (I5c, M19)

**Files:**
- Modify: `src/eng_remember/repositories/cards.py`, `src/eng_remember/services/review.py`, `src/eng_remember/errors.py`, `src/eng_remember/bot/routers/review.py`
- Test: `tests/test_services.py`, `tests/test_bot_flow.py`

**Interfaces:**
- Produces: `errors.ScheduleConflictError` (наследник существующего доменного базового класса — сверить с errors.py); `CardRepository.update_schedule(card, schedule)` бросает его при конфликте версий.

**Решение:**
1. `update_schedule`: фиксировать `expected_reps = card.reps` до применения; выполнять `UPDATE cards SET … WHERE id=:id AND reps=:expected_reps`; при rowcount=0 — `ScheduleConflictError`. ORM-объект обновлять только при успехе (expire/refresh).
2. Роутер review: `ScheduleConflictError` в пути ответа обрабатывается как stale-карточка (существующий путь «карточка уже отвечена/устарела») — без generic-ошибки.
3. `build_queue`: запас кандидатов не ограничивать сверху `session_max` — `limit=max(2, wanted*2)` (комментарий про margin поправить).
4. `get_next`/`build_queue(limit=1)`: не считать `due_total`/`new_total` (ставить 0 или пропускать запросы — исполнитель выбирает минимальное изменение; цель: два лишних COUNT на каждую карточку убраны). Проверить потребителей `ReviewQueue.due_total/new_total` при limit=1.

- [ ] Тест `test_concurrent_answer_conflict`: два `answer()` по одной карточке из одного прочитанного состояния → второй бросает ScheduleConflictError, в журнале один ответ (интеграционный).
- [ ] Тест `test_conflict_answer_shows_stale_not_error`: через диспетчер — пользователю stale-сообщение, не «что-то сломалось».
- [ ] Тест `test_queue_margin_not_capped_by_session_max`: бэклог 40 новых, session_max=20, wanted=20 → candidates limit=40.
- [ ] Тест `test_get_next_skips_totals`: при limit=1 COUNT-запросы тоталов не выполняются (мок-счётчик).
- [ ] TDD-цикл, `make test-integration`, коммит `fix: оптимистичная блокировка расписания и запас очереди`.

### Task 23: Защита от перевода даты и порядок времён напоминаний (M9)

**Files:**
- Modify: `src/eng_remember/domain/gamification.py`, `src/eng_remember/domain/reminders.py`, `src/eng_remember/bot/routers/preferences.py`
- Test: `tests/test_domain_gamification.py`, `tests/test_domain_reminders.py`, `tests/test_bot_preferences.py`

**Решение:**
1. `record_activity`: при `today < accounted_on` возвращать состояние без изменений (защита по образцу `roll_forward`).
2. `decide`: слот закрыт при `last_morning_on >= today` / `last_evening_on >= today` (вместо `==`); `_digest_owed` — `last_digest_on != today` оставить, но morning-проверку привести к `>=`.
3. `/remind`: валидировать `morning_at < evening_at` при сохранении (понятное сообщение); исполнитель сверяет текущий формат команды.

- [ ] Тест `test_record_activity_ignores_date_backward`: accounted_on=сегодня, today=вчера → без изменений.
- [ ] Тест `test_morning_not_sent_twice_after_tz_west`: last_morning_on «завтра» относительно нового today → утро не шлётся.
- [ ] Тест `test_remind_rejects_evening_before_morning`: evening ≤ morning → валидационное сообщение, настройки не изменены.
- [ ] TDD-цикл, коммит `fix: защита от перевода даты и порядок времён напоминаний`.

### Task 24: Enrichment: слияние примеров и явная очередь (M8)

**Files:**
- Modify: `src/eng_remember/services/enrichment.py`, `src/eng_remember/services/enrichment_worker.py`, `src/eng_remember/services/container.py`, `src/eng_remember/__main__.py`
- Test: `tests/test_enrichment.py`

**Решение:**
1. Воркер после генерации перечитывает актуальные `examples` карточки и мержит (добавляет только новые предложения, сохраняя пользовательские; порядок: пользовательские первыми — инвариант merge_examples уже есть в repositories/cards.py). Запись — только если карточка всё ещё актуальна (существующая перепроверка).
2. Очередь backfill из модульного глобала — в явное владение: экземпляр очереди создаётся в entry point/контейнере и передаётся `EnrichmentService` и воркеру (сигнатуры сверить; тесты строят свои экземпляры).

- [ ] Тест `test_worker_preserves_user_example_added_during_generation`: снапшот без пользовательского примера → пользователь добавил пример и закоммитил → воркер завершил генерацию → пользовательский пример на месте, новые добавлены.
- [ ] Тест `test_two_service_instances_have_independent_queues`: два экземпляра не делят очередь.
- [ ] TDD-цикл, коммит `fix: воркер примеров не затирает пользовательские данные`.

### Task 25: Единая свёртка терминов, ошибка пустого драфта, защита истории похвал (M14, M16, M17)

**Files:**
- Modify: `src/eng_remember/domain/term_match.py`, `src/eng_remember/services/cards.py`, `src/eng_remember/services/discovery.py`, `src/eng_remember/services/praise.py`, `src/eng_remember/errors.py`
- Test: `tests/test_services.py`, `tests/test_domain_term_match.py`, `tests/test_services_streak_praise.py`

**Решение:**
1. Хелпер `fold_term(term: str) -> str` в `domain/term_match.py` (NFC + casefold + strip); использовать во всех Python-путях дедупликации (`services/cards.py`, `services/discovery.py`, `_one_per_term` в review.py уже casefold — привести к хелперу). SQL `func.lower` остаётся (ограничение ß документировано в спеке); функциональный индекс — задача 29.
2. Пустой термин/перевод: отдельная ошибка `EmptyDraftError` (в errors.py, наследник доменного базового) вместо `DuplicateCardError("<empty>")`; вызывающий код показывает понятное сообщение (сверить пути capture/files).
3. `PraiseService._history`: `if not isinstance(raw, dict): return {}`.

- [ ] Тест `test_fold_term_casefold_german`: «Straße» и «STRASSE» → одинаковый ключ.
- [ ] Тест `test_empty_draft_raises_empty_draft_error`: пустой термин → EmptyDraftError, не DuplicateCardError.
- [ ] Тест `test_praise_history_list_is_ignored`: JSON-список в истории → ответ не падает.
- [ ] TDD-цикл, коммит `fix: единая свёртка терминов и честные ошибки драфтов`.

### Task 26: Матчинг ответов и утилиты времени (M20, M18)

**Files:**
- Modify: `src/eng_remember/domain/answers.py`, `src/eng_remember/domain/term_match.py`, `src/eng_remember/utils/time.py`
- Test: `tests/test_domain_answers.py`, `tests/test_domain_term_match.py`, `tests/test_utils_time.py`

**Решение:**
1. `normalize` в answers.py: убирать апострофы (’ и ') — «dont» матчит «don't» без модели.
2. Стем-правило term_match: для токенов короче 4 символов требовать точное совпадение (не префикс) — «go» не матчит «good».
3. `utils/time.py`: удалить мёртвую DST-ветку (`midnight.utcoffset() is None` для aware-datetime никогда не истинно) либо заменить корректной конструкцией через `datetime.combine` (как в next_local_day_start); `humanize_interval`: floor на границах (59.6 мин → «59 мин», 23.6 ч → «23 ч», не «60 мин»/«24 ч»).

- [ ] Тест `test_apostrophe_insensitive_match`: «dont» vs «don't» → CORRECT без модели.
- [ ] Тест `test_short_token_requires_exact_match`: «go» не матчит «good»; «go» матчит «go».
- [ ] Тест `test_humanize_interval_floor_boundaries`: 3599 с → «59 мин», 86399 с → «23 ч».
- [ ] TDD-цикл, коммит `fix: апострофы, короткие токены и округление интервалов`.

### Task 27: CSV-детектор разделителя по нескольким строкам (M15)

**Files:**
- Modify: `src/eng_remember/services/csv_io.py`
- Test: `tests/test_csv_io.py` (или существующий файл CSV-тестов)

**Решение:** `_sniff` выбирает разделитель по нескольким строкам (до 10): выигрывает разделитель, дающий стабильное число колонок > 1 на большинстве строк; при равенстве — приоритет `;` затем `,` (текущий порядок сверить). Файлы с заголовком работают как раньше.

- [ ] Тест `test_sniff_semicolon_with_commas_in_translation`: `hello;привет, здравствуйте, дорогой` (без заголовка, несколько строк) → разделитель `;`, термин «hello».
- [ ] Регрессия: существующие round-trip тесты зелёные.
- [ ] TDD-цикл, коммит `fix: разделитель CSV определяется по нескольким строкам`.

---

## Фаза 5 — данные и инфраструктура

### Task 28: Upsert get_or_create, троттлинг touch, count(*) (I5a, M29, M11-часть)

**Files:**
- Modify: `src/eng_remember/repositories/users.py`, `src/eng_remember/repositories/decks.py`, `src/eng_remember/repositories/streaks.py`, `src/eng_remember/repositories/reviews.py`, `src/eng_remember/bot/middlewares/user.py`
- Test: `tests/test_repositories.py`, `tests/test_bot_flow.py`

**Решение:**
1. `get_or_create` (users/decks/streaks): `INSERT … ON CONFLICT DO NOTHING` (SQLAlchemy `insert().on_conflict_do_nothing()` — работает и в PG, и в SQLite) + повторный SELECT; `created` = успех вставки. При IntegrityError от другой гонки — перечитать. Сохранить сигнатуры `(obj, created)`.
2. `users.touch`: обновлять `last_seen_at` не чаще раза в 5 минут (сравнение с текущим значением; константа в репозитории).
3. `DeckRepository.count_for_user`: `SELECT count(*)` вместо загрузки id.
4. Удалить мёртвый `ReviewRepository.daily_counts` (не используется; `update_content` НЕ удалять — понадобится в задаче 40).

- [ ] Тест `test_concurrent_get_or_create_no_error` (Review Focus 5): два параллельных `get_or_create` нового пользователя (asyncio.gather, интеграционный) → один объект, без IntegrityError.
- [ ] Тест `test_touch_throttled`: два touch подряд → один UPDATE (мок/счётчик запросов или проверка last_seen).
- [ ] Тест `test_count_for_user_uses_count`: результат корректен (поведение), запрос — count (инспекция SQL или счётчик).
- [ ] TDD-цикл, `make test-integration`, коммит `fix: upsert вместо check-then-insert и троттлинг touch`.

### Task 29: Индексы: функциональный unique, covering для reviews, удаление избыточных (I5b, I7d, M11-часть)

**Files:**
- Create: `alembic/versions/<new>_index_hardening.py`
- Modify: `src/eng_remember/models/card.py`, `src/eng_remember/models/review.py`, `src/eng_remember/models/user.py`, `src/eng_remember/models/deck.py`, `src/eng_remember/models/streak.py`
- Test: `tests/test_postgres_integration.py`

**Решение (одна миграция, таблицы маленькие — допустимо):**
1. Заменить `uq_cards_user_id_deck_id_term_language_direction` функциональным unique-индексом `(user_id, deck_id, lower(term), language, direction)` (в PG — `sa.text("lower(term)")`; в SQLite миграция не применяется — бот на SQLite создаёт схему через create_all; функциональный индекс на `lower(term)` объявить в модели так, чтобы он работал в обеих СУБД).
2. Добавить `ix_reviews_user_id_card_id_reviewed_at` (index-only scan под группировку первых ответов).
3. Удалить избыточные (сначала проверить grep'ом запросов и `pg_stat_user_indexes` на тестовой БД): `ix_cards_user_id`, `ix_cards_deck_id`, `ix_cards_term`, `ix_cards_due_at`, `ix_reviews_user_id`, `ix_streaks_user_id`, `ix_decks_user_id`. Если какой-то индекс реально используется отдельным запросом — оставить и задокументировать.
4. Модели привести в соответствие; `alembic check` чисто; downgrade восстанавливает прежние индексы.

- [ ] Тест `test_case_insensitive_duplicate_rejected_by_db` (интеграционный): «run» и «Run» в одной колоде/языке/направлении → IntegrityError на уровне БД.
- [ ] Тест `test_reviews_covering_index_exists`: индекс присутствует (инспекция).
- [ ] `make migrate-check` (или `alembic check`) против тестовой БД — чисто.
- [ ] TDD-цикл, `make test-integration`, коммит `fix: функциональный unique терминов и индексы под горячие запросы`.

### Task 30: User без eager-загрузок (I7c)

**Files:**
- Modify: `src/eng_remember/models/user.py`
- Test: `tests/test_repositories.py` (или новый `tests/test_models_user.py`)

**Решение:** `User.decks` и `User.streak`: `lazy="raise"` + `passive_deletes=True` (БД-каскады уже есть; ORM-удаление пользователя подтверждено интеграционным тестом). Атрибуты нигде не используются (проверено ревью).

- [ ] Тест `test_user_load_does_not_select_decks_streak`: загрузка пользователя — один SELECT (счётчик запросов через event listener).
- [ ] Регрессия: интеграционный тест удаления пользователя с каскадами зелёный.
- [ ] TDD-цикл, `make test-integration`, коммит `perf: пользователь грузится без лишних selectin`.

### Task 31: Тик напоминаний: коммит по пачкам + graceful shutdown (I7a, M13, M31-часть)

**Files:**
- Modify: `src/eng_remember/services/reminders.py`, `src/eng_remember/__main__.py`, `src/eng_remember/remind.py`, `docker-compose.yml`
- Test: `tests/test_reminder_loop.py` (новый), `tests/test_services_reminders.py`

**Решение:**
1. `ReminderService.run()` коммитит после каждой пачки (сервис получает сессию или commit-callback — сверить с текущей сборкой `build_reminder_service`; выбрать минимальное изменение). Докстринг `reminder_loop` привести в соответствие с реальностью.
2. `docker-compose.yml`: `stop_grace_period: 130s` для bot (LLM-таймаут 120 с + запас).
3. `remind.py`: локальную переменную `run` переименовать (не затеняет модульную функцию).
4. Тест entrypoint: `reminder_loop` с фейковым notifier — падение на второй пачке → watermark'и первой пачки закоммичены, цикл жив; отмена (CancelledError) корректно завершает.

- [ ] Тест `test_crash_mid_tick_keeps_sent_watermarks`: notifier падает на 2-й пачке → записи 1-й пачки в БД, 2-й — нет; следующий тик не дублирует 1-ю.
- [ ] Тест `test_reminder_loop_survives_tick_exception`: исключение тика → лог, цикл продолжается.
- [ ] TDD-цикл, `make test-integration`, коммит `fix: тик напоминаний коммитит пачки и переживает crash`.

### Task 32: Пул, валидация конфига, SQLite WAL (I7b, M12, M30, U7)

**Files:**
- Modify: `src/eng_remember/config.py`, `src/eng_remember/db/engine.py`, `.env.example`
- Test: `tests/test_config.py`, `tests/test_db_engine.py` (новый)

**Решение:**
1. Дефолты пула: `db_pool_size=10`, `db_max_overflow=15` (сумма 25 ≥ tasks_concurrency_limit 20 + фоновые задачи). В `.env.example` добавить `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_ECHO` с комментарием о связи с `BOT__TASKS_CONCURRENCY_LIMIT`.
2. Валидатор `database_url`: допустимые схемы `postgresql+asyncpg://`, `sqlite+aiosqlite://`, `sqlite://` (сверить с engine.py); иначе понятная ошибка.
3. Валидатор `log_level`: только DEBUG/INFO/WARNING/ERROR/CRITICAL (регистронезависимо, нормализовать к верхнему).
4. SQLite: при подключении к файловому SQLite — `PRAGMA journal_mode=WAL` и `busy_timeout=5000` (через event listener connect); in-memory не трогать.

- [ ] Тест `test_pool_defaults_cover_concurrency`: pool_size + max_overflow ≥ tasks_concurrency_limit + 5.
- [ ] Тест `test_database_url_rejects_unknown_scheme`: `mysql://…` → ValidationError.
- [ ] Тест `test_log_level_normalized_and_validated`: «debug» → DEBUG; «verbose» → ValidationError.
- [ ] Тест `test_file_sqlite_uses_wal`: файловая SQLite → journal_mode=wal (интеграционный не нужен, tmp_path).
- [ ] TDD-цикл, коммит `fix: пул под конкурентность, валидация конфига, WAL для SQLite`.

### Task 33: Деплой: OWNER, bind, POSTGRES_* (I9c)

**Files:**
- Modify: `docker/postgres-init.sql`, `docker-compose.yml`, `.env.example`, `docs/deploy.md`

**Решение:**
1. `postgres-init.sql`: `CREATE DATABASE eng_remember_test;` без `OWNER` (владелец — инициализирующий пользователь).
2. Compose: публикация порта `127.0.0.1:55432:5432`; `POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB` через `${…:-eng/eng/eng_remember}` как сейчас.
3. `.env.example`: блок `POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB` с комментарием «смените перед продом».
4. deploy.md: подчеркнуть смену пароля и что порт слушает только localhost.

- [ ] Проверка: `docker compose config` валиден; init-скрипт не содержит OWNER.
- [ ] Коммит `fix: деплой БД без хардкода владельца и с локальным bind`.

### Task 34: Retention llm_calls (U2-часть)

**Files:**
- Modify: `src/eng_remember/repositories/llm_calls.py`, `src/eng_remember/__main__.py`, `src/eng_remember/config.py`, `docs/security.md`
- Test: `tests/test_repositories.py` (или `tests/test_llm_retention.py`)

**Решение:**
1. `Settings.llm_call_retention_days: int = Field(default=90, ge=0)` (0 — отключено).
2. `LlmCallRepository.delete_older_than(cutoff: datetime) -> int` — DELETE по `created_at < cutoff` (индекс уже есть).
3. В `reminder_loop` раз в сутки (по monotonic-таймеру или границе UTC-дня) вызывать чистку; лог `llm_calls_pruned` с числом строк.
4. security.md: обновить «метрики бессрочно» → «аудит 90 дней, настраивается»; reviews хранится бессрочно (осознанно, нужно для истории).

- [ ] Тест `test_delete_older_than_prunes_only_old`: строки старше cutoff удалены, свежие остались (интеграционный).
- [ ] Тест `test_retention_disabled_with_zero`: 0 → чистка не вызывается.
- [ ] TDD-цикл, `make test-integration`, коммит `feat: retention аудита LLM-вызовов`.

### Task 35: Alembic: advisory lock и правила миграций (M33)

**Files:**
- Modify: `alembic/env.py`, `alembic.ini` (если нужно), `docs/deploy.md` (или новый `docs/migrations.md`)

**Решение:**
1. В `env.py` (online-режим, PG): `pg_advisory_lock` на время прогона миграций (константный ключ), освобождение в finally; для SQLite — пропуск.
2. `transaction_per_migration=True` в `context.configure` (каждая миграция — своя транзакция).
3. Документировать правила будущих миграций: DDL и backfill — разные ревизии; backfill батчами по диапазонам id; индексы `postgresql_concurrently=True` + autocommit-блок на больших таблицах; разрушающий downgrade — в докстринге.

- [ ] Проверка: `make migrate-check` против тестовой БД; повторный `alembic upgrade head` идемпотентен.
- [ ] Коммит `fix: advisory lock и правила безопасных миграций`.

### Task 36: CI и тестовая инфраструктура (M34, M32)

**Files:**
- Modify: `.github/workflows/ci.yml`, `tests/conftest.py`
- Test: `tests/test_config.py`, `tests/test_services_reminders.py`

**Решение:**
1. CI: джоб сборки Docker-образа (`docker build .`); прогон no-secrets хука (`pre-commit run detect-private-key --all-files` или весь хук-набор); раундтрип `alembic downgrade -1 && alembic upgrade head` на тестовой БД; `pytest-cov` (добавить в dev-группу, отчёт в CI, порог не задирать).
2. Фикстура `settings`: нейтрализовать переменные окружения (`LLM__API_KEY`, `BOT_TOKEN`, `DATABASE_URL` и пр.) через monkeypatch.delenv — тесты не зависят от шелла разработчика.
3. Тест `require_bot_token`: пустой/отсутствующий токен → ConfigurationError с понятным текстом; заданный → значение.
4. Тест пагинации кандидатов: `batch_size=2`, 5 пользователей → все обработаны за >1 пачку (уменьшить batch в тесте).

- [ ] Локально: `uv run pytest -m "not integration"` зелёный с нейтрализованным окружением (проверить с `LLM__API_KEY=fake` в env).
- [ ] CI-yaml валиден (actionlint если доступен, иначе вычитка).
- [ ] Коммит `test: нейтрализация окружения, покрытие entrypoint-утилит, CI полного цикла`.

### Task 37: Наблюдаемость тика и процесса (U3, M31-часть)

**Files:**
- Modify: `src/eng_remember/__main__.py`, `src/eng_remember/logging_setup.py`, `src/eng_remember/bot/notifier.py`
- Test: `tests/test_logging_setup.py` (новый), `tests/test_reminder_loop.py`

**Решение:**
1. События тика: `reminder_tick_finished` с duration_ms, users_processed, sent counts; `reminder_tick_crashed` — алерт в admin-чат через существующий notifier (если задан), не только лог.
2. Heartbeat: периодическое событие `bot_heartbeat` (раз в N тиков) с pool.checkedout()/size — внешний мониторинг видит «процесс жив».
3. Тест `configure_logging`: JSON-формат при log_json=True, уровень применяется, stdlib-мост работает.

- [ ] Тест `test_tick_crash_alerts_admin`: падение тика при заданном admin_chat_id → notifier вызван.
- [ ] Тест `test_configure_logging_json_and_level`: caplog/структурный вывод соответствует настройкам.
- [ ] TDD-цикл, коммит `feat: метрики тика, алерт о падении и heartbeat`.

### Task 38: Автоматический бэкап (U2-часть)

**Files:**
- Create: `docker/backup.sh`, `docker/backup.crontab`
- Modify: `docker-compose.yml`, `docs/deploy.md`

**Решение:** сервис `backup` под профилем `backup` (образ postgres:17-alpine, entrypoint — crond с `backup.sh`): ежедневный `pg_dump` в том `/backups`, ротация последних 7, имя с датой; env `POSTGRES_*` и `BACKUP_KEEP_DAYS`. Документировать restore-процедуру (`pg_restore`/`psql`) и учебный прогон. Без новых зависимостей в Python-коде.

- [ ] Проверка: `docker compose --profile backup config` валиден; скрипт проходит `bash -n`.
- [ ] Коммит `feat: ежедневный автобэкап с ротацией`.

---

## Фаза 6 — продуктовые улучшения

### Task 39: Управление колодами (U5-часть)

**Files:**
- Modify: `src/eng_remember/bot/routers/library.py`, `src/eng_remember/bot/keyboards.py`, `src/eng_remember/bot/callbacks.py`, `src/eng_remember/bot/texts.py`, `src/eng_remember/bot/states.py`, `src/eng_remember/repositories/decks.py`
- Test: `tests/test_bot_library.py` (новый)

**Решение:**
1. `/decks`: список колод (кап 50 из задачи 9) с inline-кнопками «✏️» и «🗑» на колоду; callback `dk:<action>:<deck_id>` (action: rn/dl), типизированный CallbackData по образцу существующих.
2. Удаление: подтверждение отдельным сообщением («Удалить колоду N слов?») → `DeckRepository.delete` (каскад карточек уже в БД); дефолтную колоду удалять нельзя (понятный отказ). После удаления — обновлённый список.
3. Переименование: FSM-состояние ожидания имени → валидация (непустое, ≤ 64 символов, уникальное в пределах пользователя) → `DeckRepository.rename`; отмена кнопкой.
4. Все обращения скоупятся user_id; stale-кнопки (колода удалена) — понятный alert.

- [ ] Тест `test_delete_deck_cascades_cards`: удаление колоды → карточки удалены (интеграционный через диспетчер).
- [ ] Тест `test_default_deck_cannot_be_deleted`: отказ, колода на месте.
- [ ] Тест `test_rename_deck_validates_uniqueness`: занятое имя → сообщение, имя не изменено.
- [ ] Тест `test_stale_deck_button_alert`: кнопка удалённой колоды → alert без падения.
- [ ] TDD-цикл, `make test-integration`, коммит `feat: переименование и удаление колод`.

### Task 40: Редактирование карточек (U5-часть)

**Files:**
- Modify: `src/eng_remember/bot/routers/review.py`, `src/eng_remember/bot/keyboards.py`, `src/eng_remember/bot/callbacks.py`, `src/eng_remember/bot/texts.py`, `src/eng_remember/bot/states.py`, `src/eng_remember/services/cards.py`, `src/eng_remember/repositories/cards.py`
- Test: `tests/test_bot_card_edit.py` (новый)

**Решение:**
1. Кнопка «✏️ Правка» на сообщении результата ответа (callback `ce:<card_id>`); вход только для своей карточки (скоуп user_id).
2. FSM `CardEdit`: последовательные вопросы «перевод → пример → заметки», на каждом шаге кнопки «⏭ Пропустить» и «✅ Готово»; пустой ввод = пропуск. Термин и язык не редактируются (README).
3. Сохранение через `CardService.update_content` (существующий метод; привести его нормализацию к `CardDraft.normalized()` — задача 4 уже добавила обрезку); расписание FSRS не меняется.
4. Stale-карточка (удалена/отвечена в другом флоу) — понятный alert; выход из редактирования не ломает активный review (кнопка доступна только на результате ответа, review в этот момент не ожидает ввода).

- [ ] Тест `test_edit_translation_persists`: правка перевода → в БД новый перевод, расписание не изменилось.
- [ ] Тест `test_edit_skip_keeps_old_values`: все шаги пропущены → карточка без изменений.
- [ ] Тест `test_edit_foreign_card_rejected`: чужой card_id → отказ, данные не тронуты.
- [ ] TDD-цикл, `make test-integration`, коммит `feat: редактирование перевода, примера и заметок карточки`.

### Task 41: Per-user семафор конкурентности (U6)

**Files:**
- Create: `src/eng_remember/bot/middlewares/concurrency.py`
- Modify: `src/eng_remember/bot/dispatcher.py`, `src/eng_remember/config.py`
- Test: `tests/test_middlewares_concurrency.py` (новый)

**Решение:** outer-middleware после rate-limit: per-user `asyncio.Semaphore(settings.bot.user_concurrency_limit)` (дефолт 3, `Field(ge=1)`); реестр с подсчётом держателей по образцу `utils/user_locks.py` (удаление после последнего держателя, без утечки на аккаунт). Один аккаунт не занимает все слоты `tasks_concurrency_limit` в ожидании per-user LLM-lock.

- [ ] Тест `test_fourth_concurrent_update_waits`: 4 параллельных апдейта одного пользователя при лимите 3 → четвёртый ждёт освобождения (управляемые события).
- [ ] Тест `test_different_users_not_blocked`: два пользователя параллельно → не блокируют друг друга.
- [ ] TDD-цикл, коммит `feat: per-user лимит конкурентности апдейтов`.

---

## Финал

- [ ] Полный прогон: `make check && make test-integration` — зелёный.
- [ ] `make migrate-check` против тестовой БД — модели и миграции не разошлись.
- [ ] Ревью всей ветки свежим ревьюером (шаблон code-reviewer.md, диапазон main..HEAD).
- [ ] Исправить находки финального ревью (Critical/Important — обязательно).
- [ ] Скилл finishing-a-development-branch: предложить пользователю merge/PR.