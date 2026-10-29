# Мультиязычность: 17 изучаемых языков — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить бота принимать слова на 17 изучаемых языках, определять язык каждой карточки, сохранять его до повторения и показывать ожидаемый язык явно; русский остаётся единственным языком перевода.

**Architecture:** Язык — свойство карточки, а не пользователя. Единый реестр `domain/languages.py` даёт коды, названия, флаги, письменности и алиасы; `Card.language` и `CardDraft.language` несут ISO 639-3 через парсер, FSM, CSV и БД; LLM получает язык из реестра, а не из пользовательского текста. Без модели язык определяется по письменности, латиница считается английской.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy async, Alembic, Pydantic v2, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-29-multilanguage-design.md`

## Global Constraints

- Изучаемые коды ровно такие: `eng`, `cmn`, `hin`, `spa`, `arb`, `fra`, `ben`, `por`, `ind`, `deu`, `jpn`, `vie`, `swh`, `tur`, `kor`, `tha`, `ita`. Порядок — как в дизайне.
- `cmn` — только мандарин, `arb` — только современный стандартный арабский. Диалектов нет.
- `rus` есть в реестре для отображения и нормализации русского ответа, но отсутствует в списке изучаемых. `resolve_target_language()` превращает `ru`/`rus`/«русский» в `eng`.
- `DEFAULT_LANGUAGE = "eng"`, `NATIVE_LANGUAGE = "rus"`.
- `Card.language` — `String(8)`, `nullable=False`, `default="eng"`, `server_default="eng"`. Старые карточки получают `eng`.
- `CardDraft.language` — обычное поле со значением по умолчанию `eng`; `normalized()` обязан его сохранить и привести к поддерживаемому коду. Язык уже сохранённой карточки не редактируется.
- Язык карточки участвует в уникальном ключе и проверке дублей: `hotel` на английском и французском — разные карточки.
- Промпты получают название языка только из реестра. Пользовательский текст в промпт как название языка не попадает.
- `/language` не добавляем; язык задаётся автоматически или явно в тексте сообщения. UI остаётся русским.
- Никаких OpenCC/конвертации упрощённого и традиционного китайского, HSK/JLPT и диалектов в этой версии.
- TDD: сначала падающий тест, затем реализация. После каждого таска `make check` зелёный. Коммиты русские.

## Review Focus

1. **Язык не теряется по пути.** Парсер → `ParsedCard.to_draft()` → `CardDraft.normalized()` → `asdict` в FSM → `CardDraft(**item)` → репозиторий → БД → CSV → повторение. Особое внимание: `normalized()` пересоздаёт dataclass; `_present`/`_ask_clarification` хранят язык подбора; `more_suggestions` и `clarify_without_answer` пересобирают `ParseResponse` и обязаны передать `LANGUAGE_KEY`.
2. **Дубли и уникальность.** Один термин на разных языках сосуществует; тот же термин на том же языке блокируется. Смешанная пачка не даёт ложных пропусков и `IntegrityError`; `find_twin()` не возвращает карточку другого языка; CSV-дедуп идёт по `(language, term.casefold())`.
3. **Фоллбэк без LLM.** Письменность → язык; латиница → `eng`; кириллица, греческий, иврит и прочие неподдерживаемые письменности отвергаются. Транскрипция в скобках извлекается до определения языка и не превращает китайское слово в английское. Подсказка в превью честно говорит, что латиница считается английской.
4. **Подбор слов.** `ParseResponse.language` — язык подбора, а не обязательно язык первой карточки. Карточки из `ExpansionResponse` наследуют язык запроса. Известные слова фильтруются по языку. `appears_in_text()` работает для CJK подстрокой, для арабского — с нормализацией, для латиницы — Unicode-токенами и стемом. Кнопка «Ещё слова» не сбрасывает язык в английский.
5. **Повторение.** Прямое направление проверяет русский ответ русской нормализацией; обратное — ответ на языке карточки. Французский `café` не равен `cafe` при точном совпадении, но LLM-чекер может простить отсутствующий акцент. Арабская диакритика не мешает точному совпадению. На фронте явно написано, какой язык ожидается; подсказка-`cloze` работает с Unicode и CJK; перевод остаётся под `🇷🇺`.
6. **CSV и UI.** Девятая колонка `language` необязательна; старые файлы дают `None` и определяются по письменности. Язык виден в предпросмотре импорта. Переводы дозаполняются группами по языкам. Бейдж языка берётся из реестра, а не из пользовательского текста, и не ломает HTML-экранирование.
7. **Миграция.** Старые строки получают `eng`, старый unique constraint удаляется, новый создаётся по `(user_id, deck_id, term, language, direction)`. Downgrade не падает на смешанных данных. `make migrate-check` подтверждает, что модели и миграции не разошлись.

---

### Task 1: Реестр языков

**Files:**
- Create: `src/eng_remember/domain/languages.py`
- Test: `tests/test_languages.py`

**Interfaces:**
- Produces: `Language`, `SUPPORTED_LANGUAGES`, `LANGUAGES`, `SUPPORTED_CODES`, `DEFAULT_LANGUAGE`, `NATIVE_LANGUAGE`, `language_for_code(value: str) -> Language | None`, `resolve_language(value: str | None) -> str`, `resolve_target_language(value: str | None) -> str`, `detect_script_language(text: str) -> str | None`, `language_name_en(code: str) -> str`, `language_name_ru(code: str) -> str`, `language_badge(code: str) -> str`, `language_target_phrase(code: str) -> str`.
- `Language`: `code`, `name_ru`, `name_en`, `flag`, `script`, `rtl: bool = False`, `name_ru_in: str = ""`.

- [ ] **Step 1: Write the failing tests.** `tests/test_languages.py`:
  - ровно 17 кодов в заданном порядке; `rus` есть в `LANGUAGES`, но не в `SUPPORTED_CODES`;
  - алиасы: `en→eng`, `zh→cmn`, `zh-Hant→cmn`, `zh_CN→cmn`, `fr→fra`, `fr-FR→fra`, `pt-BR→por`, `ar-EG→arb`, `French→fra`, `французский→fra`, неизвестное→`eng`; `language_for_code("xx") is None`;
  - `resolve_language("ru") == "rus"`, но `resolve_target_language("ru") == "eng"`; `resolve_target_language("fra") == "fra"`;
  - `detect_script_language`: `hello→eng`, `你好→cmn`, `東京→cmn`, `日本語→cmn` (только ханьцзы), `こんにちは→jpn`, `日本語です→jpn` (кана важнее ханьцзы), `한국어→kor`, `العربية→arb`, `हिन्दी→hin`, `বাংলা→ben`, `ไทย→tha`, `Привет→None`, `Ελληνικά→None`, смесь кириллицы с латиницей→`None`;
  - `language_badge("fra") == "🇫🇷 Французский"`, неизвестный код даёт английский бейдж; `language_target_phrase("fra") == "на французском"`; `rtl` только у `arb`.
- [ ] **Step 2: Run** `uv run pytest tests/test_languages.py -q` → FAIL (модуля нет).
- [ ] **Step 3: Implement.** Реестр — кортеж в порядке дизайна; для каждого языка заполнить `name_ru`, `name_en`, `flag`, `script`, `name_ru_in`. Добавить `rus` отдельно. Алиасы собрать в `_ALIASES`: ISO 639-1, полные английские и русские названия, самоназвания и базовые теги; региональный тег разбирать по первому подтегу (`fr-FR`, `pt-BR`, `zh-Hant`, `en-US`, `ar-EG`). `detect_script_language` считать буквы по диапазонам письменностей; при любой букве неподдерживаемой письменности (кириллица, греческий, иврит, тамильский и т. п.) возвращать `None`; кана → `jpn`, хангыль → `kor`, иначе доминирующая письменность. `language_badge` и `language_target_phrase` для неизвестного кода используют `eng`.
- [ ] **Step 4: Run** `uv run pytest tests/test_languages.py -q` and `make check` → PASS.
- [ ] **Step 5: Commit** `feat: реестр 17 изучаемых языков`.

---

### Task 2: Язык карточки в модели, репозитории, сервисе и миграции

**Files:**
- Modify: `src/eng_remember/models/card.py`
- Modify: `src/eng_remember/repositories/cards.py`
- Modify: `src/eng_remember/services/cards.py`
- Create: `alembic/versions/b7c1d9e4f2a3_card_language.py`
- Test: `tests/test_repositories.py`, `tests/test_services.py`, `tests/test_postgres_integration.py`

**Interfaces:**
- Produces: `Card.language: Mapped[str]`, constraint `uq_cards_user_id_deck_id_term_language_direction`;
  `CardDraft.language: str = DEFAULT_LANGUAGE`; `CardDraft.normalized()` сохраняет язык через `resolve_target_language`;
  `CardRepository.add()` пишет `cleaned.language`;
  `find_by_term(user_id, term, language=DEFAULT_LANGUAGE)`, `find_twin()` фильтрует по `Card.language`,
  `all_terms(user_id, *, limit=5000, language: str | None = None)`, `existing_terms(user_id, terms, language: str | None = None)`; оба возвращают `casefold`-термины.
- Consumes: Task 1.

- [ ] **Step 1: Write the failing tests.**
  - `test_repositories.py`: helper `add(..., language="eng")` передаёт язык в `CardDraft`; `find_by_term` не находит французский `hotel`, если ищут английский; `existing_terms`/`all_terms` фильтруют по языку; `find_twin` возвращает twin того же языка; вставка двух карточек с одним термином и разными языками проходит, с одинаковым языком и направлением — `IntegrityError`.
  - `test_services.py`: `CardDraft(term="bonjour", translation="здравствуйте", language="fr").normalized().language == "fra"`; `add_draft` сохраняет язык на обеих сторонах; `hotel` на `eng` и `fra` — две разные карточки; `find_twin` не путает языки; смешанная пачка (`eng` + `fra`, включая одинаковый термин) сохраняется без ложных `skipped_existing`/`skipped_in_batch` и без `IntegrityError`; повтор того же языка по-прежнему отклоняется.
  - `test_postgres_integration.py`: один термин на двух языках вставляется на реальном Postgres; дубль в рамках одного языка нарушает unique constraint.
- [ ] **Step 2: Run** `uv run pytest tests/test_repositories.py tests/test_services.py -q` → FAIL.
- [ ] **Step 3: Implement.**
  - `Card`: добавить `language` после `translation`, обновить `__table_args__` на новый unique constraint и docstring термина («word or phrase in the card's language»).
  - `CardDraft`: поле `language` в конце, чтобы старые позиционные вызовы не сломались; `normalized()` передаёт `language=resolve_target_language(self.language)`.
  - `CardRepository.add`: `language=cleaned.language`; `find_by_term` фильтрует `Card.language == resolve_target_language(language)`; `find_twin` добавляет `Card.language == card.language`; `all_terms`/`existing_terms` при заданном языке добавляют фильтр и возвращают `term.casefold()` (SQL-сравнение остаётся через `func.lower`).
  - `CardService.add_draft`: проверка дубля с `cleaned.language`.
  - `CardService.add_drafts`: сначала `cleaned = [draft.normalized() for draft in drafts]`, затем `existing_terms` отдельно для каждого языка, `seen` — `set[tuple[str, str]]` по `(language, term.casefold())`.
  - Alembic: `down_revision = "420f510e800f"`, `revision = "b7c1d9e4f2a3"`. `upgrade`: `add_column` с `server_default="eng"`, `drop_constraint("uq_cards_user_id_deck_id_term_direction")`, `create_unique_constraint("uq_cards_user_id_deck_id_term_language_direction", ["user_id", "deck_id", "term", "language", "direction"])`. `downgrade`: удалить строки `language <> 'eng'` (старая схема не умеет их хранить), удалить новый constraint, вернуть старый, удалить колонку.
- [ ] **Step 4: Run** `uv run pytest tests/test_repositories.py tests/test_services.py -q` and `make check` → PASS; затем `make migrate-check` при поднятом Postgres.
- [ ] **Step 5: Commit** `feat: язык карточки в модели, репозитории и сервисе`.

---

### Task 3: Языковая нормализация ответов, `cloze` и Unicode-поиск

**Files:**
- Modify: `src/eng_remember/domain/answers.py`
- Create: `src/eng_remember/domain/term_match.py`
- Modify: `src/eng_remember/domain/cloze.py`
- Modify: `src/eng_remember/services/grading.py`
- Modify: `src/eng_remember/bot/routers/review.py`
- Test: `tests/test_domain_answers.py`, `tests/test_domain_cloze.py`, `tests/test_term_match.py`, `tests/test_grading.py`

**Interfaces:**
- Produces: `normalize(text: str, language: str = NATIVE_LANGUAGE) -> str`, `matches(answer: str, expected: str, language: str = NATIVE_LANGUAGE) -> bool`;
  `term_spans(text: str, term: str, language: str = DEFAULT_LANGUAGE) -> list[tuple[int, int]]`, `contains_term(text: str, term: str, language: str = DEFAULT_LANGUAGE) -> bool`;
  `cloze(example: str, term: str, language: str = DEFAULT_LANGUAGE) -> str`.
- `AnswerGradingService._verdict()`: прямое направление — `matches(..., NATIVE_LANGUAGE)`, обратное — `matches(..., card.language)`.
- `review.py`: `cloze(example, card.term, card.language)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_domain_answers.py`: `ё→е` только для `rus`; `normalize` для `arb` снимает харакаты/татвиль и унифицирует алиф/йа; `matches("café", "café", "fra")` истинно, `matches("cafe", "café", "fra")` ложно; русские варианты через запятую по-прежнему работают при языке по умолчанию; `normalize("书", "cmn") != normalize("書", "cmn")`.
  - `test_domain_cloze.py`: китайская подстрока (`"我正在学习中文。"` → `"我正在学习____。"`), японская подстрока, арабское слово с диакритикой, французский `hôtel` внутри `l’hôtel` → `"Il a réservé l’____."`, отсутствие совпадения возвращает пример нетронутым.
  - `test_term_match.py`: `contains_term("中文", "我正在学习中文。", "cmn")`; арабская диакритика; латинский стем (`negotiate` в `negotiating`); `hôtel` не находится в `hotel`; пустой термин/текст — `False`.
  - `test_grading.py`: обратная французская карточка принимает `café` точным совпадением и не принимает `cafe` без модели; прямое направление остаётся русским.
- [ ] **Step 2: Run** `uv run pytest tests/test_domain_answers.py tests/test_domain_cloze.py tests/test_term_match.py tests/test_grading.py -q` → FAIL.
- [ ] **Step 3: Implement.**
  - `answers.normalize`: NFKC → `casefold`; для `rus` `ё→е`; для `arb` снять `[\u064b-\u065f\u0670\u0640]`, свести `آأإٱ→ا`, `ىئ→ي`; punctuation (включая `،؛؟。、「」『』`) → пробел; схлопнуть пробелы. Для остальных диакритику не снимать.
  - `matches`: при `language == NATIVE_LANGUAGE` — текущие `translation_variants`; иначе сравнивать нормализованные строки целиком.
  - `term_match.term_spans`: для `cmn`/`jpn` — точные вхождения подстроки; для остальных — Unicode-токены (буквы + комбинирующие marks; апостроф/дефис — разделители, поэтому `l’hôtel` даёт токен `hôtel`). Для нелатинских письменностей — нормализованное вхождение термина в токен; для латиницы/кириллицы — текущее 4-символьное стем-правило с минимумом 2 символа. Многословный термин скрывается одним span-ом, если части идут подряд; иначе — отдельными span-ами, если присутствуют все части; иначе `[]`.
  - `cloze` заменяет все span-ы на `BLANK`; если span-ов нет — возвращает пример без изменений.
  - `grading` передаёт язык в `matches`; `review` передаёт `card.language` в `cloze`.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: языковая нормализация ответов и Unicode-подсказки`.

---

### Task 4: Язык в LLM-схеме и фоллбэк-парсере

**Files:**
- Modify: `src/eng_remember/services/llm/schema.py`
- Modify: `src/eng_remember/services/llm/fallback.py`
- Test: `tests/test_llm_schema.py`, `tests/test_fallback_parser.py`

**Interfaces:**
- Produces: `ParsedCard.language: str = DEFAULT_LANGUAGE` (ISO 639-3, валидатор через `resolve_target_language`);
  `ParsedCard.to_draft()` передаёт язык;
  `ParseResponse.language: str = DEFAULT_LANGUAGE` — язык подбора; если поле не задано, берётся язык первой карточки, иначе `eng`;
  `parse_text()` заполняет язык каждой карточки и `ParseResponse.language`.
- Consumes: Task 1, Task 2.

- [ ] **Step 1: Write the failing tests.**
  - `test_llm_schema.py`: `fr→fra`, `zh-Hant→cmn`, `pt-BR→por`, `ar-EG→arb`, неизвестный→`eng`, `ru→eng`; `to_draft().language`; `ParseResponse` без верхнего языка и с французской карточкой даёт `fra`; явный верхний `language="eng"` побеждает; пустой ответ даёт `eng`; `language` карточки не влияет на `translation`.
  - `test_fallback_parser.py`: `书（shū）— книга` → `cmn`; `こんにちは - привет` → `jpn`; `한국어 - корейский` → `kor`; `العربية - арабский` → `arb`; `हिन्दी - хинди` → `hin`; `বাংলা - бенгальский` → `ben`; `ไทย - тайский` → `tha`; латиница → `eng`; кириллица и греческий отвергаются; пиньинь в скобках не делает термин английским; `ParseResponse.language` равен языку первой карточки; полноширинные `：`/`；` и китайские скобки разбираются.
- [ ] **Step 2: Run** `uv run pytest tests/test_llm_schema.py tests/test_fallback_parser.py -q` → FAIL.
- [ ] **Step 3: Implement.**
  - `ParsedCard`: поле `language` с `field_validator(mode="before")`, возвращающим `resolve_target_language(value if isinstance(value, str) else None)`; `to_draft` передаёт `language`.
  - `ParseResponse`: поле `language` с таким же валидатором и `model_validator(mode="after")`: если `"language" not in self.model_fields_set`, взять `self.cards[0].language` при наличии карточек, иначе `DEFAULT_LANGUAGE`.
  - Fallback: сначала извлечь транскрипцию (ASCII и полноширинные скобки), затем `detect_script_language(term)`; `None` → отвергнуть строку; иначе `ParsedCard(..., language=detected)`. `_looks_like_term` становится проверкой `detect_script_language(term) is not None`. Добавить `：`, `；` в разделители и `_ENTRY_SPLIT`, `（）` в `_TRANSCRIPTION`. `parse_text` возвращает `ParseResponse(..., language=cards[0].language)`.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: определение языка в LLM-схеме и фоллбэке`.

---

### Task 5: Языконейтральные промпты и LLM-сервисы

**Files:**
- Modify: `src/eng_remember/services/llm/prompts.py`
- Modify: `src/eng_remember/services/llm/checker.py`
- Modify: `src/eng_remember/services/llm/explorer.py`
- Modify: `src/eng_remember/services/llm/enricher.py`
- Modify: `src/eng_remember/services/llm/translator.py`
- Modify: `src/eng_remember/services/enrichment.py`
- Test: `tests/test_llm_checker.py`, `tests/test_llm_enricher.py`, `tests/test_llm_translator.py`, `tests/test_grading.py`, `tests/test_discovery.py`, `tests/test_enrichment.py`, `tests/test_security.py`

**Interfaces:**
- `build_expansion_prompt(..., language: str = DEFAULT_LANGUAGE)`;
  `build_answer_check_prompt(..., language: str = DEFAULT_LANGUAGE)`;
  `build_enrich_examples_prompt(..., language: str = DEFAULT_LANGUAGE)`;
  `build_mnemonic_prompt(..., language: str = DEFAULT_LANGUAGE)`;
  `build_translation_prompt(..., language: str = DEFAULT_LANGUAGE)`.
- `AnswerChecker.check(..., language: str = DEFAULT_LANGUAGE)`;
  `WordExplorer.expand(..., language: str = DEFAULT_LANGUAGE)`;
  `CardEnricher.examples/mnemonic(..., language: str = DEFAULT_LANGUAGE)`;
  `TermTranslator.translate(..., language: str = DEFAULT_LANGUAGE)`.
- `AnswerGradingService` передаёт `language=NATIVE_LANGUAGE` для прямого направления и `card.language` для обратного в `checker.check`.
- `WordExplorer.expand` после ответа модели перезаписывает `card.language` языком запроса.
- `EnrichmentService` передаёт `card.language` в `examples` и `mnemonic`.

- [ ] **Step 1: Write the failing tests.**
  - `test_llm_checker.py`: промпт содержит `<target_language>French</target_language>` при `language="fra"`; `AnswerChecker.check(language="fra")` передаёт язык; обратный системный промпт больше не говорит «English», а говорит о целевом языке; существующий тест переименовать из «english word» в «target word».
  - `test_grading.py`: near-miss на французской обратной карточке уходит в checker с `language="fra"`; прямое направление уходит с `rus`; промпт содержит французский target language.
  - `test_llm_enricher.py`: `examples(..., language="fra")` и `mnemonic(..., language="fra")` дают в промпте `Target language: French (fra).`; системный промпт не требует английских примеров.
  - `test_llm_translator.py`: `translate(terms=[...], language="fra")` даёт target-language строку в промпте.
  - `test_discovery.py`: `build_expansion_prompt(..., language="fra")` содержит `Target language: French (fra).`; начало промпта — `Find N words.`, без `English`; тест `test_more_than_asked...` обновить на `"Find 14 words."`.
  - `test_enrichment.py`: `FakeEnricher.examples/mnemonic` принимают `language` (игнорируют его), иначе вызовы `EnrichmentService` сломаются; существующие assertions не менять.
  - `test_security.py`: при `language="<b>bad</b>"` промпт содержит `English (eng)`, а не пользовательскую строку; `PARSE_SYSTEM_PROMPT` содержит все 17 кодов/имён, явно говорит, что русский — язык перевода, не цель, и содержит пример `bonjour — здравствуйте`.
- [ ] **Step 2: Run** соответствующие тесты → FAIL.
- [ ] **Step 3: Implement.**
  - `PARSE_SYSTEM_PROMPT` собрать из реестра: список `code — English name (Russian name)`, правило определения языка, запрет русского как цели, правило «латиница без указания языка → eng», пример французской карточки, JSON-форма с `language` у карточки и верхнего уровня. JSON-фигурные скобки экранировать или собирать через `.replace`.
  - `EXPANSION_SYSTEM_PROMPT` — «target language named in the request», без English. `build_expansion_prompt` начинается с `Find {count} words.` и добавляет `Target language: {name_en} ({code}). Translation language: Russian.`.
  - `ANSWER_CHECK_SYSTEM_PROMPT`/`_REVERSE_` — без English; reverse говорит о целевом слове; `build_answer_check_prompt` добавляет `<target_language>…</target_language>` в оба направления; добавить правило про диакритику/варианты иероглифов.
  - `ENRICH_EXAMPLES_SYSTEM_PROMPT`, `MNEMONIC_SYSTEM_PROMPT`, `TRANSLATION_SYSTEM_PROMPT` — без English; `build_*` добавляют `Target language: …` и требуют примеры/мнемонику/перевод в нужном языке.
  - `AnswerChecker` передаёт `language` в промпт; `AnswerGradingService` передаёт `rus`/`card.language` в `checker.check`; `WordExplorer` передаёт язык и перезаписывает языки карточек; `CardEnricher`, `TermTranslator` и `EnrichmentService` передают язык.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: языконейтральные промпты и LLM-сервисы`.

---

### Task 6: Язык подбора в discovery и проверка вхождения в текст

**Files:**
- Modify: `src/eng_remember/services/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- `DiscoveryResult.language: str = DEFAULT_LANGUAGE`.
- `DiscoveryService.expand(..., language: str = DEFAULT_LANGUAGE)`: нормализует язык, передаёт его в `all_terms`, `WordExplorer.expand` и `appears_in_text`, кладёт в `DiscoveryResult.language`.
- `appears_in_text(term, text, language=DEFAULT_LANGUAGE)` делегирует `contains_term`.

- [ ] **Step 1: Write the failing tests.**
  - `appears_in_text("中文", "我正在学习中文。", "cmn")`; `appears_in_text("العربية", "أنا أدرس اللغة العربية.", "arb")`; арабская диакритика в тексте не мешает; `hôtel` не находится в `hotel`; старые латинские тесты остаются зелёными.
  - `expand(..., language="fra")` с ответом модели, где карточки помечены `eng`, возвращает `fra` у всех drafts и `result.language == "fra"`.
  - известные слова фильтруются по языку: английский `fever` не мешает французскому `fever`, французский `fever` — мешает; `result.dropped_known` корректен.
  - промпт содержит `Target language: French (fra).` и `Find ... words.`; нет `English`.
- [ ] **Step 2: Run** `uv run pytest tests/test_discovery.py -q` → FAIL.
- [ ] **Step 3: Implement.** Заменить `_WORDS`/`_STEM`/`_token_appears` на `term_match.contains_term`; `appears_in_text` оставить тонкой обёрткой с языком. `expand` нормализует язык, читает `all_terms(user.id, language=language)`, передаёт `language` в explorer, проверяет контекст с языком и заполняет `DiscoveryResult.language`. `seen` можно оставить по термину: все карточки ответа уже одного языка.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: язык подбора в поиске слов`.

---

### Task 7: Язык подбора в FSM и кнопке «Ещё слова»

**Files:**
- Modify: `src/eng_remember/bot/states.py`
- Modify: `src/eng_remember/bot/routers/capture.py`
- Test: `tests/test_bot_discovery.py`

**Interfaces:**
- `LANGUAGE_KEY = "language"`.
- `run_discovery(..., language: str | None = None)`: использует `language or response.language`, передаёт в `DiscoveryService.expand` и `_present`.
- `_present(..., language: str | None = None)`: сохраняет `LANGUAGE_KEY` в FSM.
- `_ask_clarification` сохраняет `LANGUAGE_KEY`.
- `clarify_without_answer` и `more_suggestions` пересобирают `ParseResponse(..., language=...)` из `LANGUAGE_KEY`.

- [ ] **Step 1: Write the failing tests.**
  - Французская карточка + `more_words=True` → expansion-промпт содержит `French (fra)`; ответ модели с `eng`-карточками сохраняется как `fra`; после тапа `im:more` второй expansion-промпт снова французский; после `im:confirm` все карточки в БД имеют `language="fra"`.
  - Clarification skip: ответ парсера с `clarification` и `language="fra"`; тап `cl:skip`; expansion-промпт французский (язык пережил FSM).
  - Смешанный запрос: своя английская карточка + французский подбор; в БД у своей карточки `eng`, у найденных `fra`.
- [ ] **Step 2: Run** `uv run pytest tests/test_bot_discovery.py -q` → FAIL.
- [ ] **Step 3: Implement.** Добавить ключ и провести язык через все ветки `run_discovery`/`_present`, включая ветки ошибок и пустого результата. В `handle_request` для «только свои слова» передать `response.language`. В `clarify_without_answer` и `more_suggestions` передать `language` из состояния с фолбэком `DEFAULT_LANGUAGE`.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: язык подбора в FSM и кнопке ещё слова`.

---

### Task 8: Язык на карточках, в предпросмотре и в подсказках

**Files:**
- Modify: `src/eng_remember/bot/texts.py`
- Modify: `src/eng_remember/bot/routers/review.py`
- Modify: `src/eng_remember/bot/routers/capture.py`
- Modify: `src/eng_remember/bot/routers/preferences.py`
- Modify: `src/eng_remember/__main__.py`
- Test: `tests/test_bot_flow.py`, `tests/test_bot_reverse.py`, `tests/test_security.py`

**Interfaces:**
- `card_front(card)`: прямое направление — `"{flag} <i>{name_ru} · переведите на русский</i>"`; обратное — `"{flag} <i>{name_ru} · напишите слово {name_ru_in}</i>"`.
- `cards_listing(drafts)`: каждая карточка начинается с `• {language_badge(draft.language)}`.
- `import_preview(..., engine)`: при `engine != "rules"` — подсказка «Если язык определён неверно, укажите его в сообщении: «французское слово hotel — отель».»; при `rules` — «Без модели язык определяется по письменности; латиница считается английской.».
- `file_import_preview`: бейдж языка у строки.
- `file_import_prompt`: упомянуть необязательную колонку `language`.
- `/add`, welcome, `cannot_parse` и `/help`/`/settings`: «иностранные слова», примеры неанглийских языков, `/level — уровень изучаемого языка`.
- `__main__`: описание `/level` без «английского».

- [ ] **Step 1: Write the failing tests.**
  - `test_bot_reverse.py`: обратная английская карточка показывает `напишите слово на английском`, а не `Переведите на английский`; новая французская обратная карточка показывает `🇫🇷 Французский` и `на французском`; `café` в ответе проверяется как французское слово.
  - `test_bot_flow.py`: прямой фронт показывает `переведите на русский` и флаг; LLM-превью французской карточки содержит `🇫🇷 Французский` и подсказку про исправление языка; fallback-превью содержит строку про латиницу; приветствие говорит «иностранные слова».
  - `test_security.py`: `CardDraft(..., language="<b>bad</b>").normalized().language == "eng"`; `cards_listing` с французской карточкой показывает бейдж из реестра и не пропускает пользовательский HTML.
- [ ] **Step 2: Run** эти тесты → FAIL.
- [ ] **Step 3: Implement.** В `card_front` брать `language_for_code(card.language)` (fallback `eng`), не интерполировать код/имя из текста. В `cards_listing` использовать `language_badge` (он экранирует не требуется, значения из реестра). В `import_preview` добавить подсказку после списка, до строки про движок. В `file_import_preview` — бейдж. Обновить тексты `/add`, welcome, `cannot_parse`, `/settings`, `__main__`. В `review.py` вызов `cloze(..., card.language)` уже из Task 3; проверить, что он не потерян.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: язык на карточках и в предпросмотре`.

---

### Task 9: Язык в CSV-экспорте и импорте

**Files:**
- Modify: `src/eng_remember/services/csv_io.py`
- Modify: `src/eng_remember/bot/routers/files.py`
- Test: `tests/test_csv_io.py`, `tests/test_bot_files.py`

**Interfaces:**
- `CsvRow.language: str | None = None`; `_HEADER` оканчивается на `language`.
- `cards_to_csv` пишет `card.language or DEFAULT_LANGUAGE` девятым полем.
- `parse_cards_csv` читает девятое поле; старые 8-колоночные файлы дают `None`.
- `_clean_row`: `language = row.language or detect_script_language(row.term) or DEFAULT_LANGUAGE`, затем `ParsedCard(..., language=...)`.
- `replace_translation` сохраняет язык; `_row_to_draft`, `_row_to_dict`, `_rows_from_state` носят язык.
- `_prepare_rows`: дедуп по `(language, term.casefold())`; `existing_terms` вызывать по группам языков; переводы дозаполнять группами по языкам, не более 30 строк суммарно.
- `file_import_preview` показывает бейдж (Task 8).

- [ ] **Step 1: Write the failing tests.**
  - `test_csv_io.py`: обновить `HEADER` на `...;deck;language`; точные строки экспорта оканчиваются `;eng`; французская карточка экспортируется с `;fra`; roundtrip сохраняет язык; файл без девятой колонки даёт `language is None`; файл с `language=fra` даёт `fra`.
  - `test_bot_files.py`: preview показывает `🇫🇷` для французской строки; confirm сохраняет `language="fra"`; файл без колонки сохраняет латиницу как `eng`, а китайский термин — как `cmn`; один и тот же термин на `eng` и `fra` в одном файле сохраняется двумя карточками; существующий английский термин не блокирует французский; переводы двух языков дозаполняются двумя вызовами с разными target-language строками; FSM сохраняет язык после предпросмотра.
- [ ] **Step 2: Run** `uv run pytest tests/test_csv_io.py tests/test_bot_files.py -q` → FAIL.
- [ ] **Step 3: Implement.** Добавить поле и колонку; `_to_row` читает индекс 8; `_clean_row` определяет язык по письменности, если колонки нет; `_prepare_rows` сначала чистит все строки, затем группирует `existing_terms` по языку, затем дедуп по `(language, term.casefold())`, затем переводы по языковым группам. `_row_to_draft` при отсутствии языка использует `detect_script_language`/`DEFAULT_LANGUAGE`. `_row_to_dict`/`_rows_from_state` добавляют `language`.
- [ ] **Step 4: Run** тесты + `make check` → PASS.
- [ ] **Step 5: Commit** `feat: язык в CSV-экспорте и импорте`.

---

### Task 10: README и финальная проверка

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README.** Заменить утверждения «для английского» на «17 изучаемых языков, перевод на русский»; добавить список языков и правило автоопределения/исправления языка; в описании двух сторон написать «иностранное слово → перевод» и «ждёт слово на изучаемом языке». Не обещать диалекты, HSK/JLPT, `/language` или редактирование языка карточки.
- [ ] **Step 2: Run** `make check` → PASS.
- [ ] **Step 3: Run** `make test-integration` (нужен `TEST_DATABASE_URL`/Postgres) → PASS.
- [ ] **Step 4: Run** `make migrate-check` (нужен поднятый Postgres) → PASS.
- [ ] **Step 5: Commit** `docs: 17 изучаемых языков в README`.

---

## Self-Review

- **Покрытие спеки:** реестр — Task 1; данные/миграция/дубли — Task 2; нормализация ответов, `cloze`, `appears_in_text` — Task 3 и Task 6; схема/фоллбэк — Task 4; промпты и LLM-сервисы — Task 5; discovery и FSM — Task 6–7; UI/повторение — Task 8; CSV — Task 9; README и проверки — Task 10.
- **Review Focus:** 1 — Task 2, 4, 7, 9; 2 — Task 2, 9; 3 — Task 1, 4, 8; 4 — Task 5, 6, 7; 5 — Task 3, 5, 8; 6 — Task 8, 9; 7 — Task 2, 10.
- **Типы и имена:** `resolve_target_language` используется для всего, что сохраняется как язык карточки; `resolve_language` — только для отображения/нормализации, где допустим `rus`. `CardDraft.language` добавляется в конец dataclass, чтобы старые вызовы и FSM-состояния без поля не ломались. `ParseResponse.language` — `str`, после валидатора всегда заполнен.
- **Пропорция:** каждый таск — один слой (реестр, данные, текст, LLM, discovery, FSM, UI, CSV, docs) и заканчивается зелёным `make check`. Задачи 2, 3, 5, 9 содержат больше тестов, но не смешивают несвязанные слои.
- **Осознанные упрощения:** нет OpenCC, `/language`, HSK/JLPT, редактирования языка, диалектов; латиница без LLM всегда `eng`; `书` и `書` — разные карточки; CEFR остаётся общим.
