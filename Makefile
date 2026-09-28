# Common developer tasks. Targets use the project virtualenv in .venv/.
.PHONY: setup load run test coverage lint format check lock

PY := .venv/bin/python
RUFF := .venv/bin/ruff

setup:  ## create .venv, install exact dev dependencies, create .env, migrate
	python3 -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements-dev.lock
	test -f .env || cp .env.example .env
	$(PY) manage.py migrate

load:  ## parse, dedupe and geocode the fuel CSV into the database (idempotent)
	$(PY) manage.py load_fuel_prices

run:  ## development server on http://localhost:8000
	$(PY) manage.py runserver

test:  ## full test suite (no network access needed)
	$(PY) -m pytest

coverage:  ## test suite with the coverage gate from pyproject.toml (fail under 95%)
	$(PY) -m pytest --cov

lint:  ## static checks: ruff lint + formatting
	$(RUFF) check . && $(RUFF) format --check .

format:  ## apply ruff formatting and safe fixes
	$(RUFF) format . && $(RUFF) check --fix .

check:  ## Django system checks + migration drift
	$(PY) manage.py check && $(PY) manage.py makemigrations --check --dry-run

lock:  ## regenerate the hashed lock files from requirements*.txt (needs uv)
	uv pip compile requirements.txt --generate-hashes --no-header --annotation-style=line -o requirements.lock
	uv pip compile requirements-dev.txt --generate-hashes --no-header --annotation-style=line -o requirements-dev.lock
