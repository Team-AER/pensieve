.PHONY: dev-db dev-down dev worker worker-ai worker-capture migrate test lint format build
dev-db:        ## start Postgres (pgvector), Redis, Garage and the capture browser on loopback
	docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d db redis garage browser
	PENSIEVE_GARAGE_ADMIN_URL=http://localhost:3903 uv run python -m pensieve.archive init-storage
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
worker-capture: ## page capture worker (queue pensieve:capture; needs `uv sync --extra capture`)
	PENSIEVE_S3_ENDPOINT=http://localhost:3900 PENSIEVE_BROWSER_WS=ws://localhost:3000/ uv run --extra capture arq pensieve.worker.CaptureWorkerSettings
test:
	uv run pytest -q
lint:
	uv run ruff check .
format:
	uv run ruff format .
build:
	docker compose build web worker-capture browser
