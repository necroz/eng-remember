# Батч G: обратное направление — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** На каждое слово — пара карточек (EN→RU и RU→EN) со своими расписаниями FSRS; обратный фронт показывает перевод; ответ — английское слово.

**Architecture:** `cards.direction` + расширенный unique; создание пары в `CardService`; направление проходит через `texts.card_front`, grading и LLM-чекер; практика/экспорт — forward-only; пауза/удаление симметричны.

**Spec:** `docs/superpowers/specs/2026-10-14-reverse-cards-design.md`

## Global Constraints

- Бюджет новых слов — по карточкам; новые близнецы не в одной очереди (forward первым); старым словам близнецы не создаются.
- Синоним на обратном — HARD; точное совпадение/опечатки — CORRECT; без модели — только точное совпадение.
- Практика/экспорт — forward-only; статистика удваивается по карточкам (README).
- `make check`, `make test-integration`, `make migrate-check` зелёные; русские коммиты.

## Review Focus

1. Пара создаётся всеми путями (ручное добавление, capture, discovery, CSV); `ImportOutcome.added` не удваивается.
2. Очередь: новый близнец не приходит в той же сессии; следующим днём приходит.
3. Обратный фронт показывает переввод без термина/транскрипции; бэк — всё.
4. Обратный вердикт: точное; синоним → HARD; мусор → AGAIN; без модели — WRONG.
5. Пауза/удаление симметричны; повторное добавление слова отклоняется.
6. Миграция: `direction='forward'` для старых, уникальность пары, `migrate-check` чистый.

---

### Task 1: Данные и создание пары

**Files:**
- Modify: `domain/enums.py`, `models/card.py`, `repositories/cards.py`, `services/cards.py`
- Create: миграция
- Test: `tests/test_services.py`, `tests/test_repositories.py`, `tests/test_postgres_integration.py` (миграция/уникальность)

**Interfaces:**
- Produces: `CardDirection`; `Card.direction`; `CardRepository.add(..., direction=...)`; `find_twin(card)`; `list_for_user(..., direction=None)`; `list_seen(..., direction=FORWARD)`; `CardService.add_draft/add_drafts` создают пары; `suspend/delete` симметричны.

- [ ] **Step 1: Failing tests** (add_draft создаёт обе карточки, возвращает forward; add_drafts счётчики по словам; suspend/delete зеркалят; уникальность пары; повторный add слова — DuplicateCardError; старые строки forward).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** + миграция; обновить существующие тесты, которым важна одна карточка на слово.
- [ ] **Step 4: Run tests + `make check` + `make migrate-check` → PASS.**
- [ ] **Step 5: Commit** `feat: карточки-близнецы — направление и создание пары`.

---

### Task 2: Повторение, проверка, очередь

**Files:**
- Modify: `services/llm/checker.py`, `services/llm/prompts.py`, `services/grading.py`, `services/review.py`, `bot/texts.py`, `bot/routers/review.py` (при необходимости)
- Test: `tests/test_grading.py`, `tests/test_llm_checker.py`, `tests/test_services.py`, `tests/test_bot_review_reverse.py` (новый), `tests/test_domain_answers.py` (если нужно)

**Interfaces:**
- Produces: `texts.card_front` ветка reverse; `AnswerChecker.check(..., direction: str = "forward")`; `build_answer_check_prompt(..., direction=...)`; reverse-система промпта; `AnswerGradingService` сравнивает с `term` для reverse; `ReviewService.build_queue` не допускает двух новых близнецов.

- [ ] **Step 1: Failing tests** (обратный фронт; точное/синоним/мусор/без модели; чекер получает direction и purpose; очередь с одним близнецом; на следующий день второй).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests + `make check` → PASS.**
- [ ] **Step 5: Commit** `feat: повторение и проверка обратного направления`.

---

### Task 3: Практика, экспорт/импорт, README

**Files:**
- Modify: `services/practice.py` (или репозиторий list_seen — Task 1), `bot/routers/files.py`, `README.md`
- Test: `tests/test_practice.py`, `tests/test_bot_files.py`, `tests/test_bot_practice.py`

**Interfaces:**
- Consumes: `direction`-параметры из Task 1.
- Produces: экспорт forward-only без дублей; практика без парных вопросов; README про удвоение и бюджет.

- [ ] **Step 1: Failing tests** (запуск практики с парами — одно слово один вопрос; экспорт пары — одна строка).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement + README.**
- [ ] **Step 4: Run `make check` + `make test-integration` → PASS.**
- [ ] **Step 5: Commit** `feat: практика и экспорт с парами, README`.

## Self-Review

- Спека: 1–4 — Task 1/2; 5–6 — Task 2; 7–9 — Task 3; 10–11 — Task 1 (без бэкфилла) / Task 2 (чекер).
- Review Focus: 1/5/6 — Task 1; 2/3/4 — Task 2; остальное — Task 3.
