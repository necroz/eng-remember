# Батч A: контекст на карточке и лечение leech — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. (Пользователь заранее выбрал inline-исполнение без субагентов; вопросы решаются исполнителем самостоятельно.)

**Goal:** Добавить клоуз-подсказку на повторе, ротацию примеров с фоновым бэкфиллом и меню лечения для слов, которые стабильно проваливаются.

**Architecture:** Чистые правила живут в `domain/` (клоуз, выбор примера, порог leech, снятие «Легко»); данные — JSON-список `examples` плюс счётчики `again_count`/`leech_level` на карточке; LLM-обогащение — отдельный компонент `CardEnricher` с фоновым воркером на ограниченной очереди; UI — существующие aiogram-слои (states/callbacks/keyboards/texts/routers).

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy async, Alembic, pydantic, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-10-14-card-context-leeches-design.md`

## Global Constraints

- Порог leech — `REVIEW__LEECH_THRESHOLD`, дефолт 4; примеров на карточку — `REVIEW__EXAMPLES_PER_CARD`, дефолт 3; промпты просят «до трёх».
- Инвариант: `example == examples[0]`, когда список непуст.
- `leech_level` при вмешательстве = `again_count // threshold` (не «+1»).
- Подсказка снимает только EASY: CORRECT+подсказка → GOOD.
- Бэкфилл выполняется только при настроенной LLM; в `__main__`; в тестах воркер не запущен.
- Все вызовы модели проходят `user_id` (квота и аудит).
- Комментарии в коде — только там, где объясняют неочевидное решение; стиль — как в существующем коде (русские тексты, английские докстринги).
- После каждого таска: `make check` зелёный, коммит на русском с префиксом (`feat:`, `fix:`, `test:`).

## Review Focus

1. Легаси-карточка только с `example`: бэкфилл встраивает его первым элементом, ротация показывает его на первом повторе.
2. Подсказка на карточке без примера: кнопки нет; устаревший/повторный тап — тихий ответ без падения.
3. Ошибка или квота модели в фоновом воркере: цикл не рушится, карточка не портится, следующая задача обрабатывается.
4. Гонка воркера и ответа: мердж примеров не затирает существующие и не меняет `example[0]`.
5. `/paused` для удалённой/чужой карточки: alert «карточка недоступна», без исключения.
6. Снятие EASY подсказкой: GOOD остаётся GOOD, ALMOST/WRONG не затронуты.

---

### Task 1: Чистые правила — клоуз, ротация, leech, рейтинг

**Files:**
- Create: `src/eng_remember/domain/cloze.py`
- Create: `src/eng_remember/domain/examples.py`
- Create: `src/eng_remember/domain/leech.py`
- Modify: `src/eng_remember/domain/answers.py` (`rating_for`)
- Test: `tests/test_domain_cloze.py`, `tests/test_domain_examples.py`, `tests/test_domain_leech.py`, `tests/test_domain_answers.py`

**Interfaces:**
- Produces:
  - `cloze(example: str, term: str) -> str` — пример, где совпавшие со стемом термина слова заменены на `____`; без совпадений возвращает пример как есть.
  - `pick_example(examples: Sequence[str], fallback: str | None, slot: int) -> str | None`
  - `merge_examples(example: str | None, examples: Sequence[str], *, limit: int | None = None) -> list[str]` — дедуп (casefold+strip), `example` первым, опциональный потолок.
  - `should_offer(again_count: int, level: int, *, threshold: int) -> bool`
  - `next_level(again_count: int, *, threshold: int) -> int`
  - `rating_for(verdict, *, duration_seconds, easy_threshold_seconds, hint_used: bool = False)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_domain_cloze.py
from eng_remember.domain.cloze import cloze

def test_replaces_the_word():
    assert cloze("The landlord raised the rent.", "landlord") == "The ____ raised the rent."

def test_matches_inflected_forms_by_stem():
    assert cloze("He runs every morning.", "run") == "He ____ every morning."

def test_matches_multiword_phrase():
    assert cloze("They came up with a plan.", "come up with") == "They ____ a plan."

def test_case_insensitive_and_replaces_all():
    assert cloze("Landlord and landlord again.", "landlord") == "____ and ____ again."

def test_absent_word_leaves_example_untouched():
    assert cloze("No match here.", "ubiquitous") == "No match here."

def test_punctuation_is_preserved():
    assert cloze("A landlord, obviously!", "landlord") == "A ____, obviously!"
```

```python
# tests/test_domain_examples.py
from eng_remember.domain.examples import merge_examples, pick_example

def test_rotation_cycles_through_examples():
    assert pick_example(["a", "b", "c"], None, 0) == "a"
    assert pick_example(["a", "b", "c"], None, 3) == "a"
    assert pick_example(["a", "b", "c"], None, 5) == "c"

def test_fallback_when_list_empty():
    assert pick_example([], "legacy", 2) == "legacy"
    assert pick_example([], None, 2) is None

def test_merge_puts_example_first_and_dedupes():
    assert merge_examples("One.", ["two.", "ONE.", "three."]) == ["One.", "two.", "three."]

def test_merge_limit_truncates_after_dedupe():
    assert merge_examples("One.", ["two.", "three."], limit=2) == ["One.", "two."]
```

```python
# tests/test_domain_leech.py
from eng_remember.domain.leech import next_level, should_offer

def test_offers_at_threshold_multiples():
    assert should_offer(3, 0, threshold=4) is False
    assert should_offer(4, 0, threshold=4) is True
    assert should_offer(4, 1, threshold=4) is False
    assert should_offer(8, 1, threshold=4) is True

def test_next_level_uses_division_not_increment():
    assert next_level(4, threshold=4) == 1
    assert next_level(7, threshold=4) == 1
    assert next_level(8, threshold=4) == 2
```

```python
# tests/test_domain_answers.py — добавить
def test_hint_caps_fast_correct_at_good():
    from eng_remember.domain.answers import Verdict, rating_for
    from eng_remember.domain.enums import ReviewRating
    rating = rating_for(Verdict.CORRECT, duration_seconds=1.0,
                        easy_threshold_seconds=5.0, hint_used=True)
    assert rating is ReviewRating.GOOD

def test_hint_does_not_touch_other_verdicts():
    from eng_remember.domain.answers import Verdict, rating_for
    from eng_remember.domain.enums import ReviewRating
    assert rating_for(Verdict.ALMOST, duration_seconds=1.0,
                      easy_threshold_seconds=5.0, hint_used=True) is ReviewRating.HARD
    assert rating_for(Verdict.WRONG, duration_seconds=1.0,
                      easy_threshold_seconds=5.0, hint_used=True) is ReviewRating.AGAIN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_domain_cloze.py tests/test_domain_examples.py tests/test_domain_leech.py tests/test_domain_answers.py -v`
Expected: FAIL — модулей нет, `rating_for` не принимает `hint_used`.

- [ ] **Step 3: Implement the modules**

`domain/cloze.py` — токенизация регуляркой `[A-Za-z][A-Za-z'\-]*`; стем = первые 4 символа (или весь токен, если короче); для многословного термина сначала ищется непрерывный отрезок по стемам, при неудаче — одиночные совпадения; собирается строка обратно с сохранением разделителей. `BLANK = "____"`.
`domain/examples.py` — `pick_example` (индексация по модулю, с защитой от пустого списка) и `merge_examples` (порядок: `example`, затем список; дедуп по `casefold().strip()`).
`domain/leech.py` — как в тестах. `answers.py` — в ветке CORRECT первая проверка `if hint_used: return ReviewRating.GOOD`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_domain_cloze.py tests/test_domain_examples.py tests/test_domain_leech.py tests/test_domain_answers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eng_remember/domain tests/test_domain_cloze.py tests/test_domain_examples.py tests/test_domain_leech.py tests/test_domain_answers.py
git commit -m "feat: правила клоуза, ротации примеров и leech"
```

---

### Task 2: Данные карточки — examples, again_count, leech_level

**Files:**
- Modify: `src/eng_remember/models/card.py`
- Modify: `src/eng_remember/repositories/cards.py`
- Modify: `src/eng_remember/services/review.py` (инкремент `again_count`)
- Modify: `src/eng_remember/config.py` (`ReviewSettings.leech_threshold=4`, `ReviewSettings.examples_per_card=3`)
- Create: `alembic/versions/<auto>_card_examples_and_leech.py` (через `make revision`)
- Test: `tests/test_services.py`, `tests/test_repositories.py`

**Interfaces:**
- Consumes: `merge_examples` (Task 1).
- Produces:
  - `Card.examples: list[str]`, `Card.leech_level: int`, `Card.again_count: int`.
  - `CardDraft.examples: tuple[str, ...]`; `normalized()` поддерживает инвариант `example == examples[0]`.
  - `CardRepository.update_examples(card, examples)`, `update_notes(card, notes)`, `list_suspended(user_id, *, limit=20)`, `count_suspended(user_id)`.
  - `ReviewService.answer(...)` увеличивает `again_count` при `ReviewRating.AGAIN` (в любом состоянии).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services.py — добавить
async def test_draft_normalizes_example_and_examples():
    from eng_remember.repositories.cards import CardDraft
    draft = CardDraft(term="run", translation="бежать",
                      example=None, examples=("He runs.", "A run.")).normalized()
    assert draft.example == "He runs."
    assert draft.examples == ("He runs.", "A run.")

async def test_add_keeps_examples(services, user):
    from eng_remember.repositories.cards import CardDraft
    card = await services.card_service.add_draft(
        user, CardDraft(term="run", translation="бежать",
                        examples=("He runs.", "A run.")))
    assert card.examples == ["He runs.", "A run."]
    assert card.example == "He runs."

async def test_again_increments_again_count(services, user, saved_card):
    from eng_remember.domain.enums import ReviewRating
    await services.review_service.answer(user, saved_card, ReviewRating.AGAIN)
    assert saved_card.again_count == 1
    await services.review_service.answer(user, saved_card, ReviewRating.GOOD)
    assert saved_card.again_count == 1
```

```python
# tests/test_repositories.py — добавить
async def test_suspend_listing_roundtrip(session, services, user, saved_card):
    await services.card_service.suspend(user, saved_card.id)
    listed = await services.cards.list_suspended(user.id)
    assert [card.id for card in listed] == [saved_card.id]
    assert await services.cards.count_suspended(user.id) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_services.py tests/test_repositories.py -v`
Expected: FAIL — нет полей и методов.

- [ ] **Step 3: Implement model, draft, repository, service**

`models/card.py`: `examples: Mapped[list[str]] = mapped_column(JSON, default=list, server_default=text("'[]'"))`; `leech_level: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")`; `again_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")` (импорт `text`).
`repositories/cards.py`: `CardDraft` получает `examples: tuple[str, ...] = field(default_factory=tuple)`; `normalized()` чистит, дедупит, ставит `example` первым (использовать `merge_examples`); `add()` пишет `examples=list(cleaned.examples)`; `update_content()` — оба поля; новые `update_examples/update_notes/list_suspended/count_suspended`.
`services/review.py`: после `self._scheduler.review(...)` — `if rating is ReviewRating.AGAIN: card.again_count = (card.again_count or 0) + 1` (до `update_schedule`, чтобы один flush записал оба изменения).
`config.py`: в `ReviewSettings` добавить `leech_threshold: int = Field(default=4, ge=2)` и `examples_per_card: int = Field(default=3, ge=1, le=5)`.

- [ ] **Step 4: Migration**

```bash
make db-up
make revision m="card examples and leech counters"
```
Проверить сгенерированный файл: три колонки с server_default. Дописать в `upgrade()` бэкфилл:
```python
op.execute(
    "UPDATE cards SET again_count = COALESCE(("
    "SELECT COUNT(*) FROM reviews r "
    "WHERE r.card_id = cards.id AND r.rating = 'again'), 0)"
)
```
(`rating` хранится строчными именами — см. `models/review.py`, `values_callable`.)

- [ ] **Step 5: Run tests and migration check**

Run: `uv run pytest tests/test_services.py tests/test_repositories.py -v && make migrate-check`
Expected: PASS; alembic check — «No new upgrade operations detected».

- [ ] **Step 6: Commit**

```bash
git add src/eng_remember/models/card.py src/eng_remember/repositories/cards.py src/eng_remember/services/review.py alembic/versions tests/test_services.py tests/test_repositories.py
git commit -m "feat: examples, again_count и leech_level на карточке"
```

---

### Task 3: LLM — три примера в промптах и компонент CardEnricher

**Files:**
- Modify: `src/eng_remember/services/llm/schema.py` (`ParsedCard.examples`, `ExamplesResponse`, `MnemonicResponse`)
- Modify: `src/eng_remember/services/llm/prompts.py` (примеры в PARSE/EXPANSION, промпты обогащения)
- Create: `src/eng_remember/services/llm/enricher.py`
- Test: `tests/test_llm_schema.py`, `tests/test_llm_enricher.py`

**Interfaces:**
- Produces:
  - `ParsedCard.examples: list[str]`; обе формы (`example`, `examples`) сводятся друг к другу.
  - `ExamplesResponse(examples: list[str])` (чистка, дедуп, до 5), `MnemonicResponse(mnemonic: str)` (flatten, до 200, непустой).
  - `CardEnricher.available`; `CardEnricher.examples(*, term, translation, count, existing, user_id=None) -> list[str]`; `CardEnricher.mnemonic(*, term, translation, user_id=None) -> str`; purposes `card_examples`, `mnemonic`.
  - Билдеры `build_enrich_examples_prompt`, `build_mnemonic_prompt`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_schema.py — добавить
def test_parsed_card_merges_singular_and_plural_examples():
    from eng_remember.services.llm.schema import ParsedCard
    card = ParsedCard(term="run", translation="бежать", example="He runs.")
    assert card.examples == ["He runs."]
    card = ParsedCard(term="run", translation="бежать", examples="He runs.\nA run.")
    assert card.example == "He runs."
    assert card.examples == ["He runs.", "A run."]

def test_mnemonic_response_rejects_empty():
    import pytest as _pytest
    from eng_remember.services.llm.schema import MnemonicResponse
    with _pytest.raises(Exception):
        MnemonicResponse(mnemonic="   ")
```

```python
# tests/test_llm_enricher.py — новый, по образцу test_llm_checker.py
# ScriptedClient из tests/factories отвечает заранее заданными payload'ами.
def test_examples_returns_payload(llm):
    llm.script.append('{"examples": ["He runs fast.", "A quick run."]}')
    enricher = CardEnricher(llm)
    result = await enricher.examples(term="run", translation="бежать",
                                     count=2, existing=[], user_id=1)

def test_examples_without_model_refuses():
    enricher = CardEnricher(None)
    with pytest.raises(LlmNotConfiguredError):
        await enricher.examples(term="run", translation="бежать", count=1, existing=[])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm_schema.py tests/test_llm_enricher.py -v`
Expected: FAIL — нет `examples`, `ExamplesResponse`, `CardEnricher`.

- [ ] **Step 3: Implement schema and prompts**

`ParsedCard`: поле `examples: list[str] = Field(default_factory=list)`; `before`-валидатор принимает строку (разбить по переводам строк); валидатор чистит `flatten_line(limit=EXAMPLE_LIMIT)`, отбрасывает пустые, дедупит, режет до 5; `model_validator(mode="after")` сводит формы: `examples` непусто → `example = examples[0]`; `example` непусто → вставить первым в `examples`, если его там нет. `to_draft()` передаёт `examples=tuple(self.examples)`.
`prompts.py`: в `PARSE_SYSTEM_PROMPT` и `EXPANSION_SYSTEM_PROMPT` строка про `example` заменяется на `"examples"` — до трёх предложений, первое — фраза пользователя, если он её дал; поправить shape-JSON в промптах. Добавить `ENRICH_EXAMPLES_SYSTEM_PROMPT`, `MNEMONIC_SYSTEM_PROMPT` (русская ассоциация, 1–2 предложения, ≤200 символов) и билдеры с теми же обёртками/`flatten_line`, что у checker. `ExamplesResponse`, `MnemonicResponse` — по образцу существующих схем.

- [ ] **Step 4: Implement CardEnricher**

По образцу `WordExplorer`/`AnswerChecker`: `available`; оба метода бросают `LlmNotConfiguredError` без модели; `complete_json(purpose=..., system=..., user=..., schema=..., user_id=user_id)`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_llm_schema.py tests/test_llm_enricher.py tests/test_llm_checker.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/eng_remember/services/llm tests/test_llm_schema.py tests/test_llm_enricher.py
git commit -m "feat: три примера в промптах и LLM-компонент CardEnricher"
```

---

### Task 4: EnrichmentService, очередь и фоновый воркер

**Files:**
- Create: `src/eng_remember/services/enrichment.py`
- Create: `src/eng_remember/services/enrichment_worker.py`
- Modify: `src/eng_remember/services/container.py`
- Modify: `src/eng_remember/__main__.py`
- Test: `tests/test_enrichment.py`

**Interfaces:**
- Consumes: `CardEnricher` (Task 3), `merge_examples` (Task 1), репозиторий (Task 2).
- Produces:
  - `EnrichmentService(*, cards, enricher, settings)`; `needs_examples(card) -> bool`; `enrich_examples(*, card, user_id) -> list[str]`; `add_example(*, card, user_id) -> str`; `add_mnemonic(*, card, user_id) -> str`.
  - `enqueue_examples(user_id: int, card_id: int) -> bool`, `_BACKFILL_QUEUE`.
  - `enrichment_worker(session_factory, settings, stopping) -> None`; тестируемая `_enrich_card(session_factory, settings, user_id, card_id) -> bool`.
  - `Services.enrichment`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_enrichment.py — новый (сессия/сервисы из conftest; enricher — подстановка)
class FakeEnricher:
    def __init__(self, answers): self.answers = answers; self.calls = []
    @property
    def available(self): return True
    async def examples(self, *, term, translation, count, existing, user_id=None):
        self.calls.append((term, count, tuple(existing), user_id))
        return self.answers[:count]
    async def mnemonic(self, *, term, translation, user_id=None):
        return "мнемоника"

async def test_enrich_embeds_existing_example_first(...):
    card = await add_draft(..., example="My own sentence.")
    service = EnrichmentService(cards=..., enricher=FakeEnricher(["New one.", "New two."]), settings=...)
    added = await service.enrich_examples(card=card, user_id=user.id)
    assert card.examples == ["My own sentence.", "New one.", "New two."]
    assert card.example == "My own sentence."
    assert added == ["New one.", "New two."]

async def test_enrich_dedupes_and_stops_at_target(...):
    # existing ["One."], уже 3 примера → needs_examples False, добавлений нет

async def test_add_example_appends_one(...):
    added = await service.add_example(card=card, user_id=user.id)
    assert added in card.examples

async def test_mnemonic_appends_to_notes(...):
    card.notes = "про предлог"
    await service.add_mnemonic(card=card, user_id=user.id)
    assert card.notes == "про предлог · мнемоника"

async def test_worker_skips_full_cards_and_survives_errors(...):
    # _enrich_card для карточки с 3 примерами → False, клиент не зван
    # enricher кидает LlmError → _enrich_card возвращает False, исключение не летит
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_enrichment.py -v`
Expected: FAIL — модулей нет.

- [ ] **Step 3: Implement EnrichmentService**

`needs_examples`: `enricher.available and len(merge_examples(card.example, card.examples)) < settings.review.examples_per_card`.
`enrich_examples`: существующие = `merge_examples(...)`; если уже хватает — `[]`; иначе `count = target - len(existing)`; `enricher.examples(term, translation, count, existing, user_id)`; мердж; записать `update_examples`; вернуть только новые строки.
`add_example`: `enricher.examples(count=1, existing=...)`; дописать (без потолка); вернуть строку.
`add_mnemonic`: `enricher.mnemonic(...)`; `notes = f"{card.notes} · {mnemonic}" if card.notes else mnemonic`; `update_notes`; вернуть.
Очередь: `asyncio.Queue(maxsize=100)`, `enqueue_examples` через `put_nowait`, `QueueFull` → `False` + лог.

- [ ] **Step 4: Implement worker and wiring**

`enrichment_worker`: цикл с `asyncio.wait_for(queue.get(), timeout=1.0)`, выход по `stopping`; каждый элемент — `_enrich_card`; `LlmError`/прочие исключения логировать и продолжать; `CancelledError` пробрасывать. `_enrich_card`: `session_scope(session_factory)` → `build_services` → `cards.get` → `needs_examples` → `enrich_examples`; исключения → `log.warning` и `False`.
`container.py`: `EnrichmentService` собирается рядом с остальными (enricher = `CardEnricher(llm)`), кладётся в `Services.enrichment`; `services/enrichment.py` не импортирует container (цикл разрывается модулем воркера).
`__main__.py`: задача `enrichment_worker` при `settings.llm.is_configured`, отменяется в `finally` вместе с reminders.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_enrichment.py tests/test_services.py -v && make check`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/eng_remember/services/enrichment.py src/eng_remember/services/enrichment_worker.py src/eng_remember/services/container.py src/eng_remember/__main__.py tests/test_enrichment.py
git commit -m "feat: сервис обогащения, очередь и фоновый воркер бэкфилла"
```

---

### Task 5: Подсказка-клоуз в повторении

**Files:**
- Modify: `src/eng_remember/bot/states.py` (`EXAMPLE_KEY`, `HINT_USED_KEY`)
- Modify: `src/eng_remember/bot/callbacks.py` (`ReviewAction += "hint"`)
- Modify: `src/eng_remember/bot/keyboards.py` (`review_prompt(card_id, *, hint=False)`)
- Modify: `src/eng_remember/bot/texts.py` (`card_front(card, *, hint=None)`, пометка в `answer_accepted`)
- Modify: `src/eng_remember/bot/routers/review.py`
- Modify: `src/eng_remember/services/grading.py` (`hint_used`)
- Test: `tests/test_bot_hint.py` (новый), `tests/test_grading.py`

**Interfaces:**
- Consumes: `cloze`, `pick_example` (Task 1); `services.enrichment`, `enqueue_examples` (Task 4).
- Produces: `EXAMPLE_KEY = "review_example"`, `HINT_USED_KEY = "hint_used"`; `grade(..., hint_used: bool = False)`; кнопка `ReviewCB(action="hint", card_id=...)`.

- [ ] **Step 1: Write the failing bot tests**

По образцу `tests/test_bot_flow.py` (диспетчер, `RecordingSession`, `make_due`, `ScriptedClient`):
```python
# tests/test_bot_hint.py
async def test_hint_button_shown_only_with_example(...):
    # карточка с examples=["He runs every morning."] → среди кнопок фронта есть "rv:hint"
    # карточка без примеров → кнопки подсказки нет

async def test_hint_edits_front_with_cloze_and_caps_rating(...):
    # карточка с examples создана через сервис, due сдвинут make_due
    # тап rv:hint → текст фронта содержит "____"
    # быстрый точный ответ → в таблице reviews rating == "good" (не "easy"),
    # в тексте результата есть "💡 подсказка"

async def test_stale_hint_tap_answers_quietly(...):
    # тап по rv:hint после ответа на карточку → AnswerCallbackQuery, без трейсбеков
```

```python
# tests/test_grading.py — добавить
async def test_hint_downgrades_fast_correct(services, user):
    # grade(..., typed=точное совпадение, duration=1.0, hint_used=True) → rating GOOD
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bot_hint.py tests/test_grading.py -v`
Expected: FAIL — нет кнопки/логики.

- [ ] **Step 3: Implement UI and flow**

`states.py`: две новые ключ-константы в `__all__`. `callbacks.py`: `ReviewAction = Literal["dont_know", "hint"]`.
`keyboards.py`: при `hint=True` первой кнопкой `💡 Подсказка` с `ReviewCB(action="hint", card_id=...)`, `adjust(2)`; иначе как раньше.
`texts.py`: `card_front(card, *, hint: str | None = None)` — при `hint` дописывает пустую строку и `<i>{escape(hint)}</i>`; `answer_accepted(..., hint_used=False)` — при использовании подсказки в хвост `Дальше через …` добавляется ` · 💡 подсказка`; `answer_result` прокидывает флаг; `card_back(card, *, example=None)` использует переданный пример.
`grading.py`: параметр `hint_used: bool = False`, передаётся в `rating_for`.
`routers/review.py`:
- `_send_front(bot, chat_id, card, state, services)`: `example = pick_example(card.examples or [], card.example, card.reps)`; кладёт `EXAMPLE_KEY`, `HINT_USED_KEY=False`; `review_prompt(card.id, hint=example is not None)`; если `services.enrichment.needs_examples(card)` — `enqueue_examples(card.user_id, card.id)`.
- Точка входа и `_advance` передают `services` в `_send_front`.
- `_complete` читает `hint_used = bool(data.get(HINT_USED_KEY))`, передаёт в `grade(...)`; в `answer_result` передаёт `hint_used` и `example=data.get(EXAMPLE_KEY)`.
- Новый обработчик `ReviewCB.filter(F.action == "hint")`: проверки как у `dont_know`; повторный тап — `query.answer()` и выход; нет примера — `query.answer(show_alert=True)`; иначе `edit_or_send(query, texts.card_front(card, hint=cloze(example, card.term)), bot=bot)` и `HINT_USED_KEY=True`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_bot_hint.py tests/test_bot_flow.py tests/test_grading.py -v && make check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eng_remember/bot src/eng_remember/services/grading.py tests/test_bot_hint.py tests/test_grading.py
git commit -m "feat: подсказка-клоуз в повторении со снятием «Легко»"
```

---

### Task 6: Leech-меню

**Files:**
- Modify: `src/eng_remember/bot/callbacks.py` (`LeechCB`)
- Modify: `src/eng_remember/bot/keyboards.py` (`leech_menu`)
- Modify: `src/eng_remember/bot/texts.py` (тексты меню и действий)
- Modify: `src/eng_remember/bot/routers/review.py` (триггер и обработчики)
- Test: `tests/test_bot_leech.py`

**Interfaces:**
- Consumes: `should_offer`, `next_level` (Task 1); `services.enrichment.add_mnemonic/add_example` (Task 4); `services.card_service.suspend` (существует).
- Produces: `LeechCB(action: Literal["mnemonic", "example", "pause", "leave"], card_id)`; `keyboards.leech_menu(card_id)`; тексты `leech_offer/leech_mnemonic_done/leech_example_done/leech_paused/leech_left/llm_required/llm_failed`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bot_leech.py — по образцу test_bot_rewards.py
async def test_leech_menu_appears_on_fourth_failure(...):
    # 4 ответа «Не знаю» по карточке (rating AGAIN; для этого учтён again_count)
    # после 4-го — сообщение с "не даётся" и кнопками lc:mnemonic/lc:example/lc:pause/lc:leave

async def test_leech_menu_not_repeated_before_next_threshold(...):
    # 5-й провал — меню нет

async def test_mnemonic_action_saves_to_notes(...):
    # тап lc:mnemonic (llm_dispatcher со скриптом {"mnemonic": "..."})
    # notes карточки содержит мнемонику; сообщение меню отредактировано без кнопок

async def test_pause_action_suspends_and_unpauses_via_paused(...):
    # тап lc:pause → is_suspended True
    # /paused → список с кнопкой ps:resume → тап → due-очередь снова может взять карточку

async def test_llm_actions_alert_without_model(...):
    # llm не настроена → тап lc:mnemonic → alert "подключённая модель"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bot_leech.py -v`
Expected: FAIL — нет меню.

- [ ] **Step 3: Implement menu, trigger, handlers**

`callbacks.py`: `LeechCB` (prefix `lc`), `__all__` дополнить.
`keyboards.py`: `leech_menu(card_id)` — `🧠 Мнемоника`, `✍️ Свежий пример`, `⏸ Пауза`, `✅ Оставить`, `adjust(2, 2)`.
`texts.py`: тексты по спеке; `leech_offer(card)` использует `card.again_count`; `llm_required()`/`llm_failed()` — короткие фразы.
`routers/review.py` в `_complete` (после `services.commit()` и отправки результата, до `_advance`):
```python
threshold = services.settings.review.leech_threshold
if outcome.rating is ReviewRating.AGAIN and should_offer(card.again_count, card.leech_level, threshold=threshold):
    card.leech_level = next_level(card.again_count, threshold=threshold)
    await services.commit()
    await bot.send_message(chat_id, texts.leech_offer(card), reply_markup=keyboards.leech_menu(card.id))
```
Обработчики `LeechCB` (четыре): карточка через `services.cards.get`; нет — alert `card_unavailable`; LLM-действия — `try/except LlmNotConfiguredError` (alert `llm_required`) и `except LlmError` (alert `llm_failed`); успех — `services.commit()` и `edit_or_send(query, <подтверждение>, bot=bot)` (кнопки исчезают); пауза — `card_service.suspend`; «оставить» — просто подтверждение.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_bot_leech.py tests/test_bot_rewards.py -v && make check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eng_remember/bot tests/test_bot_leech.py
git commit -m "feat: меню лечения для слов, которые не даются"
```

---

### Task 7: /paused, README и финальные проверки

**Files:**
- Modify: `src/eng_remember/bot/callbacks.py` (`PauseCB`)
- Modify: `src/eng_remember/bot/keyboards.py` (`paused_cards`)
- Modify: `src/eng_remember/bot/texts.py` (`paused_list`, `paused_empty`, help)
- Modify: `src/eng_remember/bot/routers/library.py`
- Modify: `src/eng_remember/__main__.py` (BotCommand `/paused`)
- Modify: `README.md`
- Test: `tests/test_bot_paused.py`

**Interfaces:**
- Consumes: `CardRepository.list_suspended/count_suspended` (Task 2), `card_service.suspend` (существует).
- Produces: `PauseCB(action: Literal["resume"], card_id)`; `keyboards.paused_cards(cards)`; команда `/paused`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bot_paused.py
async def test_paused_lists_suspended_words(...): ...
async def test_paused_empty_message(...): ...
async def test_resume_removes_card_from_list(...): ...
async def test_resume_missing_card_alerts(...):
    # ps:resume с несуществующим card_id → alert "карточка недоступна", без исключения
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bot_paused.py -v`
Expected: FAIL — команды нет.

- [ ] **Step 3: Implement command and resume**

Роутер library: `/paused` (как в спеке, до 20 карточек, `texts.paused_list(cards, total)` + `keyboards.paused_cards`); обработчик `PauseCB(action="resume")` — `card_service.suspend(user, card_id, suspended=False)`, `None` → alert, иначе коммит и `edit_or_send` с обновлённым списком (пусто → `paused_empty`, клавиатура None). Кнопки: `▶️ {term[:26]}`. `/help` дополнить строкой про `/paused`; `COMMANDS` в `__main__` — `BotCommand(command="paused", description="Слова на паузе")`.

- [ ] **Step 4: README**

В раздел «Что умеет сейчас» добавить: подсказку-контекст, ротацию примеров, leech-меню, `/paused`; в список команд — `/paused`; упомянуть бэкфилл («примеры догенерируются в фоне, если подключена модель»).

- [ ] **Step 5: Full checks and commit**

Run: `make check && make test-integration && make migrate-check`
Expected: всё зелёное (интеграционные — против Docker Postgres).

```bash
git add src/eng_remember/bot src/eng_remember/__main__.py tests/test_bot_paused.py README.md
git commit -m "feat: команда /paused и обновление README"
```

---

## Self-Review

- Покрытие спеки: §1 примеры — Task 2/3; §2 ротация — Task 1/5; §3 бэкфилл — Task 4; §4 подсказка — Task 1/5; §5 leech — Task 1/6; §6 /paused — Task 7; данные/миграция — Task 2; настройки — Task 2 (`ReviewSettings` дополнить полями `leech_threshold`/`examples_per_card`); LLM-слой — Task 3.
- Все интерфейсы, названные в поздних тасках (`needs_examples`, `enrich_examples`, `add_example`, `add_mnemonic`, `should_offer`, `next_level`, `pick_example`, `merge_examples`, `cloze`), определены в ранних.
- Review Focus: пункты 1–2 — Task 4/5 тесты; 3–4 — Task 4; 5 — Task 7; 6 — Task 1.
