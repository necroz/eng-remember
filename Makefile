.DEFAULT_GOAL := help
SHELL := /bin/bash

# Host-side address of the database published by docker-compose.yml.
DB_HOST ?= localhost
DB_PORT ?= 55432
DB_USER ?= eng
DB_PASSWORD ?= eng
DB_NAME ?= eng_remember
TEST_DB_NAME ?= eng_remember_test

DATABASE_URL ?= postgresql+asyncpg://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)
TEST_DATABASE_URL ?= postgresql+asyncpg://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(TEST_DB_NAME)

PYTEST_UNIT := uv run pytest -m "not integration"
PYTEST_INTEGRATION := TEST_DATABASE_URL="$(TEST_DATABASE_URL)" uv run pytest -m integration

.PHONY: help
help: ## Показать список команд
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- окружение --------------------------------------------------------------

.PHONY: install
install: ## Установить зависимости
	uv sync

.PHONY: run
run: ## Запустить бота (long polling)
	uv run python -m eng_remember

# --- качество ---------------------------------------------------------------

.PHONY: lint
lint: ## Проверить стиль и форматирование
	uv run ruff check src tests alembic
	uv run ruff format --check src tests alembic

.PHONY: format
format: ## Отформатировать код
	uv run ruff check --fix src tests alembic
	uv run ruff format src tests alembic

.PHONY: typecheck
typecheck: ## Проверить типы
	uv run mypy

.PHONY: audit
audit: ## Проверить зависимости на известные уязвимости (нужна сеть)
	uv run pip-audit

.PHONY: test
test: ## Юнит-тесты (без Postgres)
	$(PYTEST_UNIT)

.PHONY: test-integration
test-integration: ## Интеграционные тесты против Postgres из docker compose
	$(PYTEST_INTEGRATION)

.PHONY: test-all
test-all: test test-integration ## Все тесты

.PHONY: check
check: lint typecheck test ## Полная проверка перед коммитом

# --- база данных ------------------------------------------------------------

.PHONY: db-up
db-up: ## Поднять Postgres
	docker compose up -d db
	@echo "Ждём готовности Postgres..."
	@until docker compose exec -T db pg_isready -U $(DB_USER) -d $(DB_NAME) >/dev/null 2>&1; do sleep 1; done
	@echo "Postgres готов на порту $(DB_PORT)"

.PHONY: db-down
db-down: ## Остановить Postgres (данные сохраняются)
	docker compose down

.PHONY: db-reset
db-reset: ## Удалить Postgres вместе с данными
	docker compose down -v

.PHONY: psql
psql: ## Открыть psql
	docker compose exec db psql -U $(DB_USER) -d $(DB_NAME)

# --- миграции ---------------------------------------------------------------

.PHONY: migrate
migrate: ## Применить миграции
	DATABASE_URL="$(DATABASE_URL)" uv run alembic upgrade head

.PHONY: revision
revision: ## Создать миграцию: make revision m="add index"
	@test -n "$(m)" || (echo 'Укажите описание: make revision m="add index"' && exit 1)
	DATABASE_URL="$(DATABASE_URL)" uv run alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Откатить последнюю миграцию
	DATABASE_URL="$(DATABASE_URL)" uv run alembic downgrade -1

.PHONY: migrate-check
migrate-check: ## Убедиться, что модели и миграции не разошлись
	DATABASE_URL="$(DATABASE_URL)" uv run alembic check

# --- docker -----------------------------------------------------------------

.PHONY: docker-build
docker-build: ## Собрать образ бота
	docker build -t eng-remember:local .

.PHONY: docker-up
docker-up: ## Запустить бота, миграции и БД в контейнерах
	docker compose --profile full up -d --build

.PHONY: docker-down
docker-down: ## Остановить бота (данные сохраняются)
	docker compose --profile full down

.PHONY: docker-logs
docker-logs: ## Смотреть логи бота
	docker compose logs -f bot
