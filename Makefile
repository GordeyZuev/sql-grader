.PHONY: setup dev test test-verbose test-frontend lint format syntax-check typecheck check manifest-check docker-build up down logs backup restore check-prod-storage migrate update prod-up prod-down prod-status prod-logs prod-backup

MANIFEST ?= manifest.course.json
PROD_ENV_FILE ?= /opt/sql-trainer/.env
PROD_COMPOSE = docker compose --env-file $(PROD_ENV_FILE) -f compose.prod.yml

setup:
	uv sync --extra dev

dev:
	.venv/bin/uvicorn cabinet.api:app --reload --host 127.0.0.1 --port 8000

test:
	.venv/bin/pytest -q

test-verbose:
	.venv/bin/pytest -vv

test-frontend:
	node --test tests/frontend.test.cjs

manifest-check:
	.venv/bin/python -m cabinet.validate_manifest "$(MANIFEST)"

lint:
	.venv/bin/ruff check cabinet tests

format:
	.venv/bin/ruff format cabinet tests

syntax-check:
	.venv/bin/python -m compileall -q cabinet

typecheck: syntax-check

check: lint typecheck test test-frontend

docker-build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

backup:
	.venv/bin/python -m cabinet.backup

restore:
	@test -n "$(BACKUP)" || (echo "Usage: make restore BACKUP=path/to/cabinet-backup.sqlite3" && exit 2)
	.venv/bin/python -m cabinet.restore "$(BACKUP)" --confirm

# Run these targets from /opt/sql-trainer/app on the VM.
check-prod-storage:
	@test -f "$(PROD_ENV_FILE)" || (echo "Missing $(PROD_ENV_FILE)" && exit 2)
	@mountpoint -q /srv/sql-trainer || (echo "Persistent disk is not mounted at /srv/sql-trainer" && exit 2)


migrate: check-prod-storage
	@if [ -n "$$($(PROD_COMPOSE) ps -q cabinet backup)" ]; then $(PROD_COMPOSE) stop cabinet backup; fi
	$(PROD_COMPOSE) build cabinet
	$(PROD_COMPOSE) run --rm --no-deps cabinet python -m cabinet.migrate

update: check-prod-storage
	$(MAKE) prod-backup
	git pull --ff-only
	$(MAKE) migrate
	$(PROD_COMPOSE) up -d --build --remove-orphans
	$(PROD_COMPOSE) up -d --no-deps --force-recreate caddy

prod-up: migrate
	$(PROD_COMPOSE) up -d --build --remove-orphans
	$(PROD_COMPOSE) up -d --no-deps --force-recreate caddy

prod-down: check-prod-storage
	$(PROD_COMPOSE) down

prod-status: check-prod-storage
	$(PROD_COMPOSE) ps

prod-logs: check-prod-storage
	$(PROD_COMPOSE) logs -f

prod-backup: check-prod-storage
	$(PROD_COMPOSE) run --rm --no-deps backup python -m cabinet.backup
