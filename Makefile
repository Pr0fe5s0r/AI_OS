COMPOSE ?= docker compose

.PHONY: up ingest analyze test lint down logs psql boundary

## Bring up postgres+redis+api+worker+web, wait for health, run migrations.
up:
	$(COMPOSE) up -d --build --wait postgres redis api worker
	$(COMPOSE) up -d --build web
	$(COMPOSE) exec -T api alembic upgrade head
	@echo "----- tables -----"
	$(COMPOSE) exec -T postgres psql -U aios -d aios -c "\dt"
	@echo "\nNow open http://localhost:3000 -> Connections -> connect a repo -> Sync -> Run analysis"

## Pull real events from every connected source onto the arq pipeline.
ingest:
	$(COMPOSE) exec -T api python -m verticals.software.ingest
	$(COMPOSE) exec -T postgres psql -U aios -d aios -c "SELECT (SELECT count(*) FROM events) AS events, (SELECT count(*) FROM event_embeddings) AS embeddings, (SELECT count(*) FROM nodes) AS nodes, (SELECT count(*) FROM edges) AS edges;"

## Learn norms -> detect situations -> assemble briefs -> deliver.
analyze:
	curl -s -X POST "http://localhost:8000/api/analyze?company_id=default"

## Import-boundary guardrail (fails if core imports a vertical).
boundary:
	$(COMPOSE) exec -T api python scripts/check_core_boundary.py

test:
	$(COMPOSE) exec -T api pytest

lint:
	$(COMPOSE) exec -T api ruff check .
	$(COMPOSE) exec -T api mypy packages apps verticals

down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f api worker

psql:
	$(COMPOSE) exec postgres psql -U aios -d aios
