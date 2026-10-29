.PHONY: help secrets dev down api-test api-check web-check contracts check

help:
	@echo "make secrets    write fresh random dev secrets into .env (from .env.example)"
	@echo "make dev        start the full dev stack (docker compose) on http://localhost:3000"
	@echo "make down       stop the stack (keeps data); 'docker compose down -v' wipes it"
	@echo "make check      lint, types, tests, contract drift (what CI runs)"

secrets:
	@test -f .env || cp .env.example .env
	@python3 scripts/gen_secrets.py .env

dev: secrets
	docker compose up --build

down:
	docker compose down

api-check:
	cd services/api && uv run ruff check . && uv run ruff format --check . && uv run mypy src

api-test:
	cd services/api && uv run pytest

contracts:
	cd services/api && uv run python ../../scripts/export_openapi.py ../../packages/contracts/openapi.json
	cd apps/web && npm run gen:api

web-check:
	cd apps/web && npm run lint && npm run typecheck && npm run build

check: api-check api-test web-check
	git diff --exit-code packages/contracts apps/web/src/lib/api-types.ts
