# Батч D: CEFR-оценка и /level — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** LLM-оценка уровня и редкости слова с бейджем на подтверждении и в карточке; команда `/level` с мягкой подстройкой подбора.

**Architecture:** Поля `cefr`/`frequency` идут путём `ParsedCard` → `CardDraft` → модель (+миграция) → рендеры; уровень пользователя — `users.level`, читается подбором и правится `/level` в роутере preferences.

**Spec:** `docs/superpowers/specs/2026-10-14-cefr-level-design.md`

## Global Constraints

- CEFR: `^[ABC][12]$` после upper, мусор → `None`; частота: `common|uncommon|rare` (+русские алиасы), иначе `None`.
- Мягкость: уровень — просьба в промпте, не фильтр; при подборе из текста уровень не подмешивается.
- Бейджи — только у карточек от модели; CSV/старые — без бейджей.
- `/level` работает с аргументом и кнопками; без модели тоже.
- `make check` зелёный после каждого таска; русские коммиты.

## Review Focus

1. Мусор от маленькой модели в cefr/frequency → None, без падения и без ложного бейджа.
2. `/level` с аргументом (`b1`), с мусором, со сбросом; повторный тап не ломает сообщение.
3. Уровень заменяет «B1-B2» в промпте темы и не появляется при подборе из текста.
4. Бейдж не ломает HTML/лимиты в подтверждении и карточке.
5. Миграция: три nullable-колонки, `migrate-check` чистый.

---

### Task 1: Данные, схема, промпты, подбор

**Files:**
- Modify: `src/eng_remember/services/llm/schema.py` (`ParsedCard.cefr/frequency`), `prompts.py` (shape + `build_expansion_prompt(level)`), `explorer.py` (`expand(..., level=None)`), `discovery.py` (передаёт `user.level`)
- Modify: `src/eng_remember/repositories/cards.py` (`CardDraft`), `models/card.py`, `models/user.py`
- Create: миграция (`make revision m="cefr frequency and user level"`)
- Test: `tests/test_llm_schema.py`, `tests/test_services.py`, `tests/test_discovery.py`, `tests/test_llm_explorer?`

**Interfaces:**
- Produces: `ParsedCard.cefr: str | None`, `ParsedCard.frequency: str | None`; `CardDraft.cefr`, `CardDraft.frequency`; `Card.cefr/frequency`, `User.level`; `build_expansion_prompt(*, ..., level: str | None = None)`; `WordExplorer.expand(..., level: str | None = None)`.

- [ ] **Step 1: Write failing tests** (cefr `b2`→`B2`, `Z9`/`B2/C1`→None; частота «частое»→common, `RARE`→rare, мусор→None; `add_draft` сохраняет поля; промпт подбора с уровнем содержит `B1` и «far above», без уровня — «B1-B2»; при `context_text` уровнем не пахнет).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** по интерфейсам; промпты PARSE/EXPANSION просят `"cefr"`/`"frequency"` в shape; `DiscoveryService.expand` передаёт `level=user.level`.
- [ ] **Step 4: Migration** `make db-up && make revision m="cefr frequency and user level" && make migrate && make migrate-check`.
- [ ] **Step 5: Run tests + `make check` → PASS.**
- [ ] **Step 6: Commit** `feat: CEFR и редкость слова плюс уровень пользователя`.

---

### Task 2: Бейджи и `/level`

**Files:**
- Modify: `src/eng_remember/bot/texts.py` (`badge`, вставка в подтверждения и `card_back`, тексты `/level`), `keyboards.py` (`level_menu`), `callbacks.py` (`LevelCB`), `routers/preferences.py` (`/level`), `__main__.py` (BotCommand), `README.md`
- Test: `tests/test_bot_level.py`

**Interfaces:**
- Produces: `LevelCB(action: Literal["set", "reset"], level: str = "")`; `texts.badge(cefr, frequency) -> str | None`; `keyboards.level_menu(current)`; `/level [A1..C2]`.

- [ ] **Step 1: Write failing tests** (`/level` без аргумента — меню; `/level b1` сохраняет `B1`; `/level xyz` — подсказка; тап `B2` сохраняет и правит сообщение; «Сбросить»; бейдж `B2 · редкое` в подтверждении (llm_dispatcher + payload c `cefr`/`frequency`); бейдж в результате повторения; без полей — строки нет).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement**: вставка бейджа в общий рендер строк подтверждения и в `card_back`; команда `/level` с аргументом и меню; `services.commit()`.
- [ ] **Step 4: Run tests + `make check` + `make test-integration` → PASS.**
- [ ] **Step 5: Commit** `feat: бейджи CEFR и команда /level`.

## Self-Review

- Спека: решения 1–3,7 — Task 1 (данные) + Task 2 (рендер); 4,8 — Task 1 (промпт); 5,9 — Task 2.
- Review Focus: 1 — Task 1; 2 — Task 2; 3 — Task 1; 4 — Task 2; 5 — Task 1 migration.
