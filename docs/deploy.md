# Деплой: Docker

Операционный документ: как поставить бота на сервер, как прописать креды к LLM,
как обновлять и бэкапить. Для локальной разработки он не нужен — там хватает
`make db-up && make migrate && make run`.

## Что нужно

- Docker Engine с Compose v2.
- `BOT_TOKEN` от [@BotFather](https://t.me/BotFather).
- Ключ модели — по желанию: без него бот работает на разборе по правилам.

## Первый запуск

```bash
git clone https://github.com/necroz/eng-remember.git
cd eng-remember
cp .env.example .env          # заполнить BOT_TOKEN, при необходимости LLM__*
docker compose --profile full up -d --build
docker compose logs -f bot
```

Всё, что нужно в Telegram — токен: остальные переменные в `.env.example` имеют
рабочие значения по умолчанию. Пустой `BOT_TOKEN` — единственное, из-за чего бот
откажется стартовать, и он скажет об этом в логе, а не упадёт трейсбеком.

### Что поднимается

| Сервис | Роль |
|---|---|
| `db` | Postgres 17, данные в томе `pgdata` |
| `migrate` | одноразовый: `alembic upgrade head`, затем выходит |
| `bot` | long polling + рассылка напоминаний |
| `backup` | по профилю `backup`: ежедневный `pg_dump` в том `backups` |

`migrate` — отдельный сервис намеренно. Образ бота схему не меняет (см. Dockerfile):
старт приложения не должен молча переписывать базу. Но и оставить миграции
полностью на операторе нельзя — на пустом томе бот поднялся бы на базе без таблиц.
Поэтому между `db` и `bot` стоит одноразовый контейнер: `bot` стартует только
после того, как `migrate` завершился с кодом 0.

Напоминания рассылает сам бот (цикл внутри `__main__.py`), планировщик отдельным
контейнером не нужен — см. «Несколько реплик».

### Пароль базы данных

`POSTGRES_USER`, `POSTGRES_PASSWORD` и `POSTGRES_DB` в `.env` задают учётку
контейнера. По умолчанию это `eng` / `eng` / `eng_remember` — значения для
локальной работы. **На публичном сервере смените пароль до первого запуска:**
база слушает хост (пусть и только loopback) и хранит всю историю обучения, а
пароль по умолчанию знает любой, кто читал этот репозиторий.

Пароль применяется при инициализации пустого тома `pgdata`. Правка `.env` на уже
существующей базе пароль в ней не меняет — выполните `ALTER USER` и обновите
`.env`:

```bash
docker compose exec db psql -U eng -d postgres \
  -c "ALTER USER eng WITH PASSWORD 'новый-пароль';"
```

`DATABASE_URL` в `.env` должен указывать на те же пользователя, пароль и порт: он
нужен инструментам на хосте (`make run`, `make migrate`), тогда как контейнеры
собирают адрес сами из `POSTGRES_*`.

### Порты

Наружу публикуется только Postgres, и только на loopback: маппинг —
`127.0.0.1:55432:5432`. С хоста он доступен инструментам (`make psql`,
`make db-up`, интеграционные тесты), из сети — никому. Боту этот порт не нужен:
он ходит в `db:5432` по внутренней сети compose. Если локальный порт не нужен
даже на хосте, уберите маппинг через override-файл (см. ниже); Telegram работает
через long polling, входящие соединения боту не требуются вовсе.

## Креды к LLM

Модель включается четырьмя переменными в `.env`:

| Переменная | Смысл |
|---|---|
| `LLM__ENABLED` | `true` по умолчанию; `false` выключает модель целиком |
| `LLM__BASE_URL` | адрес OpenAI-совместимого эндпоинта |
| `LLM__API_KEY` | ключ провайдера |
| `LLM__MODEL` | имя модели у этого провайдера |

Двойное подчёркивание — разделитель вложенности (`LLM__MAX_CALLS_PER_DAY` — это
`llm.max_calls_per_day`), так что остальные переменные раздела `LLM` пишутся по
той же схеме.

### Готовые наборы

```ini
# OpenAI
LLM__BASE_URL=https://api.openai.com/v1
LLM__API_KEY=sk-proj-...
LLM__MODEL=gpt-4o-mini

# OpenRouter — один ключ на модели разных вендоров
LLM__BASE_URL=https://openrouter.ai/api/v1
LLM__API_KEY=sk-or-v1-...
LLM__MODEL=openai/gpt-4o-mini

# DeepSeek
LLM__BASE_URL=https://api.deepseek.com/v1
LLM__API_KEY=sk-...
LLM__MODEL=deepseek-chat

# vLLM или другой локальный сервер на хосте
LLM__BASE_URL=http://host.docker.internal:8000/v1
LLM__API_KEY=local
LLM__MODEL=Qwen/Qwen2.5-7B-Instruct

# Ollama на хосте
LLM__BASE_URL=http://host.docker.internal:11434/v1
LLM__API_KEY=ollama
LLM__MODEL=qwen2.5:7b
```

**`LLM__API_KEY` обязателен даже там, где провайдер его не проверяет.** Модель
считается настроенной только при непустом ключе (`LlmSettings.is_configured`).
Для Ollama и vLLM впишите любую непустую строку: она уйдёт в заголовок
`Authorization`, который локальные серверы игнорируют. Пустой ключ — это не
ошибка запуска, а тихий переход на разбор по правилам: бот поднимется и будет
работать, просто без подбора слов.

`host.docker.internal` на Linux требует, чтобы сервис знал про шлюз хоста, —
добавьте в `docker-compose.override.yml`:

```yaml
services:
  bot:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  migrate:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Этот файл compose подхватывает автоматически и мержит с основным; он в
`.gitignore`, так что правки под свой сервер не конфликтуют с `git pull`.

### Reasoning-модели

`deepseek-v4.1-flash` и подобные тратят токены сначала на рассуждение и только
потом пишут ответ. `LLM__MAX_OUTPUT_TOKENS` (по умолчанию 8000) ограничивает всю
сумму, включая рассуждение, поэтому на слишком маленьком значении ответ приходит
обрезанным или пустым: JSON не разбирается, вызов повторяется с репро-промптом и
завершается `LlmResponseError` — а бот молча уходит на разбор по правилам.
`LLM__TIMEOUT_SECONDS` (по умолчанию 120) тоже нужно с запасом: одно рассуждение
может занять десятки секунд ещё до первого символа ответа.

Для структурных JSON-задач рассуждение не нужно, но по умолчанию
`LLM__REASONING_EFFORT` пуст: параметр не отправляется вовсе, и провайдер
применяет своё значение. Так модели, которые его не знают (например,
`gpt-4o-mini`), не получают лишний `400`. Поставьте `none`, чтобы оставить весь
бюджет ответу, или `low`/`medium`/`high`, чтобы сохранить часть рассуждения; если
провайдер параметр отвергнет, процесс это запомнит и больше его не пришлёт.

Запрос «добавь 20 слов» превращается в 28 карточек после oversampling. Такой
ответ у `deepseek-v4.1-flash` занимает около 4.5k completion-токенов: ~1.9k на
рассуждение и ~2.6k на JSON. На лимите 1500 модель обрезает ответ на
`finish_reason=length`, все три попытки падают одинаково, и пользователь видит
сообщение о неподключённой модели, хотя ключ на месте. Симптом в `llm_calls` —
несколько неуспешных попыток подряд с `total_tokens` у самого потолка. Опускать
лимит ниже ~4000 не стоит, а при увеличении `DISCOVERY__MAX_COUNT` его нужно
поднимать пропорционально.

### Проверка, что модель подключилась

При старте бот пишет в лог одну строку с тем, что понял из конфига:

```bash
docker compose logs bot | grep bot_starting
```

```json
{"llm_enabled": true, "llm_model": "gpt-4o-mini", "database": "db:5432/eng_remember", "reminders": true, "event": "bot_starting"}
```

`llm_enabled: false` при заполненном ключе означает, что ключ не доехал до
контейнера: compose читает `.env` из корня проекта, и после правки `.env` нужен
`docker compose up -d` заново — переменные подставляются при создании контейнера,
а не при рестарте.

Второй способ — таблица `llm_calls`: каждый вызов модели пишется туда независимо
от успеха, с моделью, токенами и латентностью.

```bash
docker compose exec db psql -U eng -d eng_remember \
  -c "SELECT purpose, provider_model, success, total_tokens, latency_ms
      FROM llm_calls ORDER BY id DESC LIMIT 10;"
```

Если таблица пуста, а вы ждёте записей — модель не вызывается. Проверьте, что
запрос вообще требует модели: пары «слово — перевод» разбираются правилами и в
LLM не ходят.

### Лимиты расхода

Расход ограничен на пользователя до вызова, а не после:

```ini
LLM__MAX_CALLS_PER_DAY=60          # интерактивных вызовов в сутки
LLM__MAX_EXAMPLE_CALLS_PER_DAY=30  # вызовов на генерацию примеров
LLM__MAX_TOKENS_PER_DAY=100000     # токенов на пользователя в сутки
LLM__MAX_TOKENS_PER_DAY_GLOBAL=2000000 # токенов на весь бот в сутки
LLM__GLOBAL_ALERT_PERCENT=80       # предупреждение на 80% общего бюджета
```

На 80% общего бюджета бот пишет `llm_global_budget_warning` и, если задан
`BOT__ADMIN_CHAT_ID`, отправляет алерт в Telegram. На 100% модельные вызовы
отключаются, а парсер переходит на правила.

Входящий поток ограничен на пользователя:

```ini
BOT__MESSAGES_PER_MINUTE=30
BOT__CALLBACKS_PER_MINUTE=30
BOT__DUPLICATE_MESSAGE_TTL_SECONDS=60
```

Алерты приходят в `BOT__ADMIN_CHAT_ID` (ваш Telegram ID); без него они остаются
только в логе.

Исчерпание лимита — это отказ пользователю и событие `llm_quota_exceeded` в логе,
а не счёт от провайдера. Аномалии и разбор инцидентов — в
[security.md](security.md#runbook).

### Ключ — это секрет

- `.env` в `.gitignore`; не коммитить и не класть в образ (в `.dockerignore` он
  тоже исключён — конфиг приходит только через переменные окружения).
- Права на файл: `chmod 600 .env`.
- Ключ попадает в `docker compose config` в открытом виде, поэтому вывод этой
  команды нельзя прикладывать к тикетам и переписке.
- Утечка — отозвать ключ у провайдера и выпустить новый; порядок в
  [security.md](security.md#runbook).

## Обновление

```bash
git pull
docker compose --profile full up -d --build
```

`migrate` перезапустится вместе с новым образом и применит появившиеся миграции
до старта бота. Отдельная команда для них нужна, только если хочется применить
схему, не трогая бота:

```bash
docker compose run --rm migrate
```

## Бэкап и восстановление

### Автоматический бэкап

Сервис `backup` снимает дамп раз в сутки и хранит последние семь:

```bash
docker compose --profile backup up -d
docker compose logs -f backup
```

Профиль отдельный намеренно: на машине разработчика планировщик не нужен, на
сервере — нужен, и включается одной командой рядом с `--profile full`:

```bash
docker compose --profile full --profile backup up -d --build
```

Что происходит:

- В 03:17 UTC BusyBox `crond` внутри контейнера запускает `docker/backup.sh dump`.
- `pg_dump -Fc` пишет `/backups/eng-remember-ГГГГ-ММ-ДД.dump` — сжатый custom
  format, из которого восстанавливает `pg_restore`. Дамп идёт по сети к `db`, том
  `pgdata` сервису не нужен.
- Файлы старше последних `BACKUP_KEEP_DAYS` (по умолчанию 7) удаляются. Меняется
  в `.env`: `BACKUP_KEEP_DAYS=14`.
- Дамп за сегодняшний день один: повторный запуск перезаписывает файл, а не
  плодит копии.
- Дампы лежат в томе `backups`, который переживает `docker compose down`.

Снять дамп руками, не дожидаясь ночи (сервис может быть не запущен):

```bash
docker compose --profile backup run --rm backup dump
docker compose --profile backup run --rm --entrypoint ls backup -lh /backups
```

**Том на том же диске — это не бэкап.** Копия переживает `docker compose down`,
но не потерю сервера, поэтому дампы нужно забирать с машины:

```bash
docker run --rm -v eng-remember_backups:/backups -v "$PWD":/out \
  postgres:17-alpine \
  cp /backups/eng-remember-$(date +%F).dump /out/
```

(`eng-remember_` — префикс проекта из `name:` в `docker-compose.yml`.)

### Восстановление

Остановите бота, чтобы он не писал в базу во время загрузки:

```bash
docker compose stop bot
docker compose --profile backup run --rm -e PGPASSWORD=eng \
  --entrypoint pg_restore backup \
  --host db --username eng --dbname eng_remember \
  --clean --if-exists --no-owner /backups/eng-remember-2026-11-15.dump
docker compose --profile full up -d
```

`--clean --if-exists` делает восстановление повторяемым: существующие таблицы
удаляются перед загрузкой. `--no-owner` избавляет от зависимости от имён ролей в
дампе. Пароль, пользователя и базу подставьте свои, если меняли `POSTGRES_*`.

Если нужен SQL-текст дампа, а не custom format (посмотреть содержимое,
применить выборочно):

```bash
docker compose --profile backup run --rm --entrypoint pg_restore backup \
  --clean --if-exists --no-owner -f - /backups/eng-remember-2026-11-15.dump \
  | docker compose exec -T db psql -U eng -d eng_remember
```

### Учебный прогон

Проверять восстановление впервые в день аварии — плохая идея. Прогоните его на
пустой базе заранее: это ничего не перезаписывает и отвечает на вопрос, цел ли
дамп.

```bash
# 1. Свежий дамп.
docker compose --profile backup run --rm backup dump

# 2. Пустая база под прогон.
docker compose exec db createdb -U eng eng_remember_rehearsal

# 3. Восстановление в неё.
docker compose --profile backup run --rm -e PGPASSWORD=eng \
  --entrypoint pg_restore backup \
  --host db --username eng --dbname eng_remember_rehearsal \
  --no-owner /backups/eng-remember-2026-11-15.dump

# 4. Данные на месте: число строк сравнимо с рабочей базой.
docker compose exec db psql -U eng -d eng_remember_rehearsal \
  -c "SELECT count(*) FROM cards; SELECT count(*) FROM reviews;"

# 5. Убрать за собой.
docker compose exec db dropdb -U eng eng_remember_rehearsal
```

Дату в имени файла подставьте свою — список дампов печатает команда `ls` выше.

### Ручной дамп

Без сервиса `backup` то же самое делается вручную:

```bash
# дамп
docker compose exec -T db pg_dump -U eng -d eng_remember -Fc > eng-remember-$(date +%F).dump

# восстановление поверх текущей базы
docker compose exec -T db pg_restore -U eng -d eng_remember --clean --if-exists < eng-remember-2026-10-13.dump
```

Имя пользователя и базы подставьте свои, если меняли `POSTGRES_USER` / `POSTGRES_DB`.

Том `pgdata` — единственное место, где лежат данные: карточки, история повторений,
серии и аудит вызовов модели. Всё остальное в контейнерах воспроизводится из
образа. Поэтому и бэкапится только он — дампом, а не копией файлов тома: копия
работающего Postgres несогласована.

## Диагностика

```bash
docker compose ps                      # кто запущен и здоров ли Postgres
docker compose logs -f bot             # логи, JSON
docker compose logs migrate            # что применилось на старте
docker compose run --rm migrate alembic current   # на какой ревизии база
```

Отдельно стоит команда напоминаний — она отвечает на вопрос «почему не пришло»:

```bash
docker compose run --rm bot eng-remember-remind
# candidates=0 sent=0 unreachable=0 failed=0
```

Команда — явная отправка: она шлёт напоминания независимо от
`REMINDERS__ENABLED` и заодно выполняет суточную чистку аудита LLM, если та ещё
не была сделана (побочный эффект диагностического запуска).

**Побочный эффект:** команда заодно выполняет суточную чистку аудита
`llm_calls` — удаляет строки старше `LLM_CALL_RETENTION_DAYS` (по умолчанию 90;
`0` выключает). Чистка идемпотентна и запускается не чаще раза в UTC-день, так
что ручной диагностический прогон безопасен, но в логе появится событие
`llm_calls_pruned`. Если `LLM_CALL_RETENTION_DAYS` не задан осознанно, после
такой команды часть истории вызовов модели может исчезнуть — это ожидаемо, а
не сбой.

Это один проход планировщика прямо сейчас. `candidates` — сколько пользователей
попало под условие, `unreachable` — те, кто заблокировал бота. Тик идемпотентен
по дню, поэтому безопасен рядом с работающим ботом. Команда шлёт всегда:
`REMINDERS__ENABLED` управляет только внутрипроцессным циклом.

## Несколько реплик

Бот — один процесс на long polling, и в типовом деплое реплика одна. Если их
появилось несколько:

- `REMINDERS__ENABLED=false` в `.env` и запуск `eng-remember-remind` из cron —
  иначе каждая реплика отправит своё напоминание. Флаг выключает только
  внутрипроцессный цикл; сама команда из cron шлёт напоминания при любом его
  значении. Тик идемпотентен по дню, но разные процессы о гонке не знают.
- Лимиты расхода `LLM__MAX_*` считаются по процессу, а не глобально: при двух
  репликах фактический потолок удваивается.
- `migrate` должен остаться ровно один: несколько параллельных `alembic upgrade`
  на одной базе — гарантированный конфликт.

## Полная остановка

```bash
docker compose --profile full down       # остановить, данные остаются
docker compose --profile full down -v    # вместе с томами pgdata и backups — удалит всё
```

`-v` удаляет и том `backups`: если бэкапы нужны, сначала заберите их с машины или
запускайте остановку без флага. Сервис `backup` останавливается отдельно —
`docker compose --profile backup stop`.
