# Батч B: тренировка вне FSRS — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Квиз из 4 вариантов по случайным уже знакомым карточкам, который не трогает FSRS, серию и статистику.

**Architecture:** Правила сборки вопросов — в новом `services/practice.py` поверх репозитория; UI — отдельный aiogram-роутер с FSM-состоянием, токеном сессии и резервацией ответа; вход — команда, кнопка меню и инлайн-предложение на пустой очереди.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy async, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-14-practice-quiz-design.md`

## Global Constraints

- Квиз из 4 вариантов; дистракторы — свои переводы, приоритет той же части речи; минимум 4 карточки с ≥4 разными переводами (`MIN_PRACTICE_CARDS = 4`).
- В тренировку входят только `reps > 0` и не на паузе; ничего не пишется в FSRS/`reviews`/серию.
- Сессия ≤ `BOT__PRACTICE_SIZE` (дефолт 10); счёт живёт только в сообщении.
- `PracticeCB(action, token, question, option)`; токен — `secrets.randbelow(10**9)` на старте; резервация «ответ в полёте» до первых `await`.
- Меню: `[[ADD, REVIEW], [PRACTICE, DECKS], [STATS, HELP]]`.
- После каждого таска `make check` зелёный; коммит на русском (`feat:`/`fix:`).

## Review Focus

1. Устаревшие кнопки прошлых сессий: одинаковые индексы вопросов не должны приниматься (токен).
2. Двойной тап по одному вопросу: без двойного счёта и двух следующих вопросов (резервация).
3. Пул с `reps=0` или на паузе не попадает в вопросы; меньше 4 карточек — экран «мало слов».
4. Финал: счёт верный, `reviews`/`reps`/`due_at` не изменились.
5. Пустая очередь повторения предлагает тренировку только когда она доступна.

---

### Task 1: PracticeService, `list_seen`, настройка

**Files:**
- Create: `src/eng_remember/services/practice.py`
- Modify: `src/eng_remember/repositories/cards.py` (`list_seen`)
- Modify: `src/eng_remember/config.py` (`BotSettings.practice_size`)
- Modify: `src/eng_remember/services/container.py` (`Services.practice`)
- Test: `tests/test_practice.py`

**Interfaces:**
- Produces:
  - `CardRepository.list_seen(user_id, *, limit=500) -> list[Card]` — `reps > 0`, `is_suspended = False`.
  - `MIN_PRACTICE_CARDS = 4`; `QuizQuestion(card_id, term, options, correct_index)`;
    `PracticeService(cards, settings, rng=None)`; `build_session(user) -> list[QuizQuestion]`;
    `available(user) -> bool` (после нормализации переводов ≥ 4 различных в пуле).
  - `Settings.bot.practice_size` (default 10, ge=4, le=50).

- [ ] **Step 1: Write the failing tests**

`tests/test_practice.py` (фикстуры conftest; карточки с `reps>0` создаются через репозиторий с `schedule(reps=1, ...)` из factories, чтобы не писать `reviews`):

```python
async def seed_cards(services, user, *pairs) -> None:
    deck = await services.card_service.default_deck(user)
    for term, translation, pos in pairs:
        await services.cards.add(
            user_id=user.id, deck_id=deck.id,
            draft=CardDraft(term=term, translation=translation, part_of_speech=pos),
            schedule=schedule(reps=1), source="manual",
        )
    await services.commit()

async def test_build_session_returns_four_unique_options_with_exact_answer(...)
    # 5 карточек, rng=random.Random(42)
    # каждый вопрос: len(options) == 4, options[correct_index] == перевод карточки,
    # len(set(options)) == 4

async def test_new_and_suspended_cards_are_excluded(...)
    # 4 seen + 1 reps=0 + 1 suspended → вопросы только по seen

async def test_same_part_of_speech_is_preferred(...)
    # 4 глагола + 1 существительное: у вопросов глаголов все опции — переводы глаголов

async def test_too_few_cards_returns_empty_and_available_false(...)
    # 3 карточки → build_session == [], available False

async def test_duplicate_translations_never_appear_twice(...)
    # две карточки с одинаковым переводом: в опциях перевод не повторяется

async def test_session_is_capped_by_practice_size(...)
    # 12 карточек, practice_size=10 → 10 вопросов (Settings с bot={"practice_size":10})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_practice.py -q`
Expected: FAIL — модуля нет, `list_seen` нет.

- [ ] **Step 3: Implement repository, config, service**

`list_seen`: select Card where user, not suspended, `reps > 0`, limit (без order — выбор делает сервис). `BotSettings.practice_size`. Сервис: `build_session` — пул `list_seen`, `available` = ≥4 различных нормализованных переводов; sample до `practice_size`; для каждой карточки кандидаты: same-POS (по `part_of_speech`, если у карточки он есть), затем остальные; исключить совпадающие по `normalize` с верным; дедуп нормализованных; взять до 3, перемешать `rng.shuffle`, зафиксировать `correct_index`. Если кандидатов меньше 3 — карточка пропускается. `container.py`: `Services.practice = PracticeService(cards=cards, settings=settings)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_practice.py -q && make check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eng_remember/services/practice.py src/eng_remember/repositories/cards.py src/eng_remember/config.py src/eng_remember/services/container.py tests/test_practice.py
git commit -m "feat: сервис тренировочного квиза"
```

---

### Task 2: Квиз в боте

**Files:**
- Modify: `src/eng_remember/bot/callbacks.py` (`PracticeCB`)
- Modify: `src/eng_remember/bot/states.py` (`Practice`, ключи)
- Modify: `src/eng_remember/bot/keyboards.py` (`practice_options`, `practice_offer`, меню)
- Modify: `src/eng_remember/bot/texts.py` (`BTN_PRACTICE`, тексты, welcome_new)
- Create: `src/eng_remember/bot/routers/practice.py`
- Modify: `src/eng_remember/bot/dispatcher.py`, `src/eng_remember/bot/routers/review.py`, `src/eng_remember/__main__.py`, `README.md`
- Test: `tests/test_bot_practice.py`

**Interfaces:**
- Consumes: `PracticeService` (Task 1).
- Produces: `PracticeCB(action: Literal["start", "answer"], token=0, question=0, option=0)`;
  `Practice.awaiting_answer`; `QUESTIONS_KEY/QUESTION_INDEX_KEY/CORRECT_COUNT_KEY/PRACTICE_TOKEN_KEY/PRACTICE_ANSWER_KEY`;
  `keyboards.practice_options(options, token, question)`, `keyboards.practice_offer()`;
  тексты `practice_question/practice_correct/practice_wrong/practice_finished/practice_unavailable`.

- [ ] **Step 1: Write the failing tests**

`tests/test_bot_practice.py` (seed через репозиторий, см. Task 1; короткие переводы, чтобы подписи кнопок не обрезались):

```python
async def test_practice_shows_a_four_option_question(...)      # /practice → 4 кнопки pr:answer
async def test_correct_tap_advances_and_counts(...)            # тап верной опции → "Верно" + следующий вопрос
async def test_wrong_tap_shows_the_right_answer(...)           # тап неверной → "Правильно: <перевод>"
async def test_finish_reports_the_score_and_touches_nothing(...) # пройти все → "Тренировка завершена"; Review count 0, reps/due_at без изменений
async def test_stale_and_repeated_taps_alert(...)              # тап прошлого вопроса и повторный тап → alert
async def test_restart_invalidates_old_buttons(...)            # /practice заново → кнопка старой сессии → alert
async def test_menu_button_starts_practice(...)                # BTN_PRACTICE
async def test_empty_review_queue_offers_practice(...)         # /review без должных, но пул есть → кнопка pr:start
async def test_too_few_cards_says_so(...)                      # 2 карточки → "мало"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bot_practice.py -q`
Expected: FAIL — колбэка/роутера нет.

- [ ] **Step 3: Implement UI**

- `PracticeCB` prefix `pr`; `Practice.awaiting_answer`; ключи в `__all__`.
- `keyboards.practice_options(options, token, question)` — подписи `option[:40]`, `adjust(1)`; `practice_offer()` — «🏋️ Потренироваться» (`PracticeCB(action="start")`); главное меню — новая раскладка.
- `texts`: `practice_question(term)` = «<b>term</b>\n\nВыберите перевод:», `practice_correct(term, translation)` = «✅ Верно: translation», `practice_wrong(...)` = «Правильно: translation», `practice_finished(correct, total)` = «Тренировка завершена: <b>N</b> из <b>M</b> верно. 👍», `practice_unavailable()` = «Для тренировки нужно хотя бы 4 слова в повторениях.», `BTN_PRACTICE`; `welcome_new` упоминает `/practice`.
- `routers/practice.py`: `start` (Command/`BTN_PRACTICE`): `state.clear()`, `_begin`; `start_from_offer` (`PracticeCB start`): `query.answer()`, `_begin`; `answer`: проверки (состояние, токен, индекс, резервация) → резервация → счёт → правка сообщения → следующий вопрос или финал (`state.clear()`, счёт+меню). `_begin` кладёт вопросы (сериализуемые dicts), токен, индекс 0, счёт 0; `_send_question` отправляет новый вопрос.
- `dispatcher.py`: включить practice после review, до capture; `review.py`: на пустой очереди `practice_offer()` при `services.practice.available(user)`; `__main__`: `BotCommand("practice", "Тренировка")`.
- README: пункт про тренировку и `/practice`.

- [ ] **Step 4: Run tests and full checks**

Run: `uv run pytest tests/test_bot_practice.py tests/test_bot_flow.py -q && make check && make test-integration`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eng_remember/bot src/eng_remember/__main__.py tests/test_bot_practice.py README.md
git commit -m "feat: режим тренировки в боте"
```

---

## Self-Review

- Покрытие спеки: сервис/пул/пороги — Task 1; callbacks/states/keyboards/texts/router/меню — Task 2; «доступность» для пустой очереди — Task 1 (`available`) + Task 2 (review.py).
- Review Focus: 1 — Task 2 restart/stale тесты; 2 — резервация (код) + stale-тест; 3 — Task 1; 4 — Task 2 `test_finish_...touches_nothing`; 5 — Task 2 offer-тест.
- Типы совпадают: `QuizQuestion` из Task 1, `PracticeCB` поля — Task 2.
