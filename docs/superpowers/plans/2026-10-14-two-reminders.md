# Батч E: два напоминания в день — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ровно два слота в день: утро — разбор очереди, вечер — остаток разбора и предупреждение о серии в одном сообщении.

**Architecture:** `domain/reminders.py` переходит на виды `MORNING`/`EVENING` с новым правилом порядка; план несёт `streak_warning`; notifier и тексты собирают вечернее сообщение из блоков.

**Spec:** `docs/superpowers/specs/2026-10-14-two-reminders-design.md`

## Global Constraints

- `ReminderKind`: `MORNING`, `EVENING`; `covers_morning(EVENING) = True`.
- После `evening_at` утренний слот не отправляется; вечер fires при `has_work` или угрозе серии; иначе тишина.
- `evening_at is None` → только утренний слот (как раньше).
- Максимум два сообщения в день; марки и поведение недоставки не меняются.
- `make check` + `make test-integration` зелёные; русские коммиты.

## Review Focus

1. Вечер с остатком без угрозы, с угрозой без остатка, с обоими; тишина, когда ни того, ни другого.
2. После вечера утреннее не догоняется; вечер покрывает пропущенное утро (бот лежал весь день).
3. Вечер выключен — ровно прежнее поведение.
4. `streak_warning` доезжает до текста; вечер без угрозы не показывает «серия под угрозой».
5. Блокировка/недоставка помечает слот, как раньше.

---

### Task 1: Домен и сервис

**Files:**
- Modify: `src/eng_remember/domain/reminders.py`, `src/eng_remember/services/reminders.py`
- Test: `tests/test_domain_reminders.py`, `tests/test_services_reminders.py`

**Interfaces:**
- Produces: `ReminderKind.MORNING/EVENING`; `decide` с новым порядком; `ReminderPlan.streak_warning: bool = False`.

- [ ] **Step 1: Failing tests** (переписать существующие под новые виды + новые сценарии: утро после вечера не шлётся; вечер только с угрозой; вечер выключен; `streak_warning` в плане).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `decide` по псевдокоду спеки; `_plan` ставит `streak_warning`; `record_reminder` — без изменений (вид слота).
- [ ] **Step 4: Run tests + `make check` → PASS.**
- [ ] **Step 5: Commit** `feat: два слота напоминаний с серией внутри вечернего`.

---

### Task 2: Notifier, тексты, документация

**Files:**
- Modify: `src/eng_remember/bot/notifier.py`, `src/eng_remember/bot/texts.py`, `src/eng_remember/bot/routers/preferences.py`, `README.md`
- Test: `tests/test_bot_reminders.py`, `tests/test_bot_flow.py` (если тексты затронуты)

**Interfaces:**
- Produces: `texts.evening_reminder(*, streak_days, due_now, new_available, streak_warning)`; `queue_reminder` без изменений.

- [ ] **Step 1: Failing tests** (вечер с угрозой содержит «под угрозой» и остаток; вечер без угрозы — «Вечерняя проверка» и без «под угрозой»; утреннее как раньше).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** notifier-выбор и `evening_reminder`; обновить `/remind` usage и строку настроек; README.
- [ ] **Step 4: Run `make check` + `make test-integration` → PASS.**
- [ ] **Step 5: Commit** `feat: вечернее сообщение с остатком и серией`.

## Self-Review

- Спека: решения 1–4 — Task 1; 5 — Task 1 (марки не тронуты) + Task 2; 6 — Task 2; 7–9 — зафиксированы как поведение/вне объёма.
- Review Focus: 1/2/3 — Task 1 тесты; 4 — Task 1 (план) + Task 2 (текст); 5 — Task 1.
