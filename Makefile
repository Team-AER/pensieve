.PHONY: dev-db dev worker migrate test lint
dev-db:        ## start Postgres (pgvector) and Redis
	docker compose up -d db redis
migrate:
	.venv/bin/alembic upgrade head
dev:           ## run the web app with reload
	.venv/bin/uvicorn pensieve.main:app --reload --port 8000
worker:
	.venv/bin/arq pensieve.worker.WorkerSettings
test:
	.venv/bin/pytest -q
lint:
	.venv/bin/ruff check pensieve tests
