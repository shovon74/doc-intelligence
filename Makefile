.PHONY: up down restart build logs test eval migrate reindex clean

up:
	docker compose up --build

down:
	docker compose down

restart:
	docker compose down && docker compose up --build

build:
	docker compose build

logs:
	docker compose logs -f

test:
	docker compose run --rm api pytest -v tests/

test-cov:
	docker compose run --rm api pytest --cov=app --cov-report=term-missing --cov-report=html:docs/test-results/htmlcov tests/

eval:
	docker compose run --rm api python eval/generate_dataset.py
	docker compose run --rm api python eval/run_eval.py
	python backend/eval/make_report.py

migrate:
	docker compose run --rm api alembic upgrade head

reindex:
	docker compose run --rm api python -m app.search.reindex

clean:
	docker compose down -v
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
