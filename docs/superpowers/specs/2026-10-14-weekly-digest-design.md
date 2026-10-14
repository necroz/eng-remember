# Батч F: недельный дайджест и хитмап

Дизайн-документ. Идея 11 дорожной карты.

## Решения человека (из Q&A)

- Дайджест — понедельник утром, включён; отключается в `/remind`.
- Хитмап — эмодзи-сетка, без картинок.

## Мои решения (можно оспорить)

1. Дайджест — третий вид напоминания `DIGEST`, в утреннем слоте выбранного
   дня (`reminders.digest_weekday`, дефолт 0 = понедельник), время —
   `morning_at` пользователя. Шлётся даже при пустой очереди (сводка ради), но
   только при наличии карточек вообще; вечерний слот дня работает как обычно.
2. `users.digest_enabled` (default true) и `users.last_digest_on`; запись
   DIGEST ставит обе марки — утреннюю и дайджестную; `/remind digest on|off`.
3. Содержимое: метрики — за 7 полных прошедших локальных дней (сегодня-7 …
   вчера-включительно; дайджест в понедельник показывает Mon–Sun, а не
   полупустой сегодняшний день): ответов всего и верных (всё, кроме AGAIN);
   добавлено слов по `cards.created_at` (рост коллекции); серия; топ-3
   проблемных слова по `again_count`; очередь сейчас и должное в ближайшие
   7 дней.
4. Хитмап — 28 локальных дней **включая сегодня**, хронологической лентой
   старых→новых, 4 ряда по 7; ⬜ 0 ответов, 🟨 1–4, 🟩 5+; в дайджесте и в
   `/stats`. Пропущенные даты — нули; `heatmap()` добивает начало ⬜.
5. Агрегация — в сервисе из `reviews.history_since` (Python, локальные даты);
   новый `cards.count_added_since`, `cards.weakest(user_id, limit)`,
   `cards.count_due_within(user_id, until)`.
6. Тексты — `texts.weekly_digest(...)`; выбор текста в notifier.

## Не-цели

- Кнопки управления в дайджесте, настройка дня/времени дайджеста, графики
  картинками, настраиваемый период.

## Дизайн

### Домен

- `ReminderKind.DIGEST`; `covers_morning(DIGEST) = True`.
- `ReminderContext` получает `digest_enabled: bool = False`,
  `digest_weekday: int = 0`, `last_digest_on: date | None = None` (дефолты —
  старые вызовы в тестах не ломаются).
- `decide`: после вечерней ветки, до утренней:
  если `digest_enabled`, локальный weekday == `digest_weekday`,
  `last_digest_on != today`, `local_clock >= morning_at` и
  `last_morning_on != today` → `DIGEST`. (Не требует `has_work`.)
- `domain/digest.py`: `heatmap(counts: list[int]) -> list[str]` (28 значений →
  эмодзи; меньше значений — добивается ⬜) и `summary_line`-хелперы не нужны.

### Данные и миграция

- `users.digest_enabled` Bool NOT NULL default true; `users.last_digest_on`
  Date NULL. Одна автогенерируемая миграция.
- `ReviewRepository.history_since(user_id, *, since) -> list[Review]`.
- `CardRepository.weakest(user_id, *, limit=3) -> list[Card]` (again_count > 0,
  по убыванию), `count_due_within(user_id, *, now, until) -> int`
  (`reps > 0`, не на паузе, `now < due_at <= until`).

### Сервис

- `ReminderPlan.digest: WeeklyDigest | None = None`;
  `WeeklyDigest(days: tuple[int, ...], total: int, correct: int, new_words: int,
  weak: tuple[tuple[str, int], ...], due_now: int, due_week: int)` — 28 значений
  ответов по локальным датам.
- `_plan`: для планов DIGEST (их единицы) собирает историю и строит
  `WeeklyDigest`; остальные планы — без изменений.
- `record_reminder` для DIGEST ставит `last_digest_on` и `last_reminder_on`.

### UI

- `texts.weekly_digest(digest, *, streak_days, timezone)` — заголовок «📅 Итоги
  недели», метрики, проблемные слова, очередь, сетка хитмапа с подписью.
- `notifier`: ветка DIGEST.
- `/stats`: добавляет хитмап (из истории 28 дней) под текущими метриками.
- `/remind`: `digest on|off`; строка в настройках.
- README.

## Крайние случаи

- Нет карточек → тишина (как у остальных напоминаний).
- digest off → понедельник как обычный день (утренний слот работает).
- Дайджест и утреннее не дублируются в один день.
- Пустая история → нули, сетка из ⬜.
- Смена недели/полночь — по локальному календарю пользователя.

## Тесты

- `test_domain_reminders.py`: DIGEST в понедельник после morning_at; не в другие
  дни; off; не повторяется; `covers_morning(DIGEST)`; вечер не затронут.
- `test_domain_digest.py`: маппинг 0/1–4/5+, добор до 28.
- `test_services_reminders.py`: план DIGEST несёт метрики (ответы, верные,
  новые слова, weak, должное), марки `last_digest_on`/`last_reminder_on`.
- `test_bot_reminders.py`: рендер дайджеста; `/remind digest off`.
- `test_bot_flow.py`/`test_bot_level`-стиль: `/stats` содержит сетку.
- Миграция: `make migrate-check`.

## Проверки

`make check`, `make test-integration`, `make migrate-check`.

## Оставлено строителю

- Точные формулировки и порядок блоков дайджеста; расположение сетки в /stats.
