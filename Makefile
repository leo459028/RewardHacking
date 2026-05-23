.PHONY: install test run lint migrate

install:
	pip install -r requirements.txt

test:
	pytest -q

migrate:
	alembic upgrade head

run:
	alembic upgrade head
	uvicorn app.main:app --reload

lint:
	ruff check app tests
