.PHONY: db up down migrate install test lint discover snapshot run

install:
	cd collector && python -m pip install -e ".[dev]"

up:
	docker compose up -d db

down:
	docker compose down

migrate:
	cd collector && python -m gcb.cli migrate

test:
	cd collector && python -m pytest -q

lint:
	cd collector && python -m ruff check src tests

discover:
	cd collector && python -m gcb.cli discover widilo

snapshot:
	cd collector && python -m gcb.cli snapshot widilo $(SLUG)

run:
	cd collector && python -m gcb.cli run widilo --limit $(or $(LIMIT),20)
