.PHONY: dev-db dev-down dev worker worker-ai migrate test lint format build
dev-db:        ## start Postgres (pgvector) and Redis on loopback
	docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d db redis
dev-down:      ## stop local development dependencies
	docker compose -f docker-compose.yaml -f docker-compose.dev.yaml down
migrate:
	uv run alembic upgrade head
dev:           ## run the web app with reload
	uv run uvicorn pensieve.main:app --reload --port 8000
worker:        ## fetch worker (default queue)
	uv run arq pensieve.worker.WorkerSettings
worker-ai:     ## AI worker (queue pensieve:ai)
	uv run arq pensieve.worker.AIWorkerSettings
test:
	uv run pytest -q
lint:
	uv run ruff check .
format:
	uv run ruff format .
build:
	docker compose build
