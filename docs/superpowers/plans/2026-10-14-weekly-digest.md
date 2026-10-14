# Батч F: дайджест и хитмап — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Недельный дайджест по понедельникам утром (включён, отключается в `/remind`) и эмодзи-хитмап 28 дней в дайджесте и `/stats`.

**Architecture:** `ReminderKind.DIGEST` в домене; метрики собираются в сервисе напоминаний из новых методов репозиториев; текст — `texts.weekly_digest`; `/stats` добавляет сетку из той же агрегации.

**Spec:** `docs/superpowers/specs/2026-10-14-weekly-digest-design.md`

## Global Constraints

- Метрики дайджеста: 7 полных прошедших локальных дней; хитмап: 28 дней включая сегодня, 4×7, ⬜/🟨/🟩 (0 / 1–4 / 5+).
- `new_words` — по `cards.created_at`.
- `covers_morning(DIGEST) = True`; DIGEST не требует `has_work`, но требует карточки; off → обычный понедельник.
- Дефолты `ReminderContext` не ломают старые вызовы.
- `make check`, `make test-integration`, `make migrate-check` зелёные; русские коммиты.

## Review Focus

1. Понедельник: дайджест вместо утреннего, один раз, записывает обе марки; вечер не затронут; другие дни — утро как раньше; off — утро как раньше.
2. Окна: метрики — ровно 7 полных дней до сегодня; хитмап — 28 включая сегодня; локальные даты (переход через полночь/таймзоны).
3. Пустая история: нули и ⬜-сетка без падений.
4. `/stats` показывает ту же сетку; отключение дайджеста не ломает напоминания.
5. Миграция: две колонки, `migrate-check` чистый.

---

### Task 1: Домен, данные, сервис

**Files:**
- Modify: `src/eng_remember/domain/reminders.py`, `models/user.py`, `repositories/users.py`, `repositories/reviews.py`, `repositories/cards.py`, `services/reminders.py`
- Create: `src/eng_remember/domain/digest.py`, миграция
- Test: `tests/test_domain_reminders.py`, `tests/test_domain_digest.py`, `tests/test_services_reminders.py`

**Interfaces:**
- Produces: `ReminderKind.DIGEST`; `ReminderContext.digest_enabled/digest_weekday/last_digest_on` (дефолты); `heatmap(counts) -> list[str]`; `WeeklyDigest(days, total, correct, new_words, weak, due_now, due_week)`; `ReminderPlan.digest`; `users.digest_enabled/last_digest_on`; `ReviewRepository.history_since`; `CardRepository.count_added_since/weakest/count_due_within`; `ReminderSettings.digest_weekday`.

- [ ] **Step 1: Failing tests** (DIGEST в понедельник после morning_at и только раз; off; covers; heatmap 0/1–4/5+ и добор до 28; план DIGEST с метриками за 7 полных дней и weak; марки обе).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** домен (ветка DIGEST в `decide` перед утренней, без `has_work`), `domain/digest.py`, репозитории, миграция, `_plan` c `WeeklyDigest`, `record_reminder`.
- [ ] **Step 4: Run tests + `make check` + `make migrate-check` → PASS.**
- [ ] **Step 5: Commit** `feat: недельный дайджест — домен, метрики и миграция`.

---

### Task 2: Notifier, тексты, `/remind`, `/stats`, README

**Files:**
- Modify: `src/eng_remember/bot/texts.py`, `bot/notifier.py`, `bot/routers/preferences.py`, `bot/routers/library.py`, `README.md`
- Test: `tests/test_bot_reminders.py`, `tests/test_bot_flow.py` (или новый `test_bot_stats.py`)

**Interfaces:**
- Produces: `texts.weekly_digest(digest, *, streak_days) -> str`; `texts.heatmap_grid(days) -> str`; `/remind digest on|off`.

- [ ] **Step 1: Failing tests** (notifier рендерит дайджест с сеткой и метриками; `/remind digest off` меняет флаг; `/stats` содержит сетку; текст без истории — нули/⬜).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** ветка notifier, тексты, `/remind digest`, хитмап в `/stats` (история 28 дней, агрегация — общий хелпер из сервиса), README.
- [ ] **Step 4: Run `make check` + `make test-integration` → PASS.**
- [ ] **Step 5: Commit** `feat: дайджест и хитмап в сообщениях и /stats`.

## Self-Review

- Спека: решения 1–5 — Task 1; 6 — Task 2; крайние случаи — тесты обоих тасков.
- Review Focus: 1/2/3 — Task 1; 4 — Task 2; 5 — Task 1.
