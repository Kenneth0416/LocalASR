.PHONY: install run test test-python test-ui test-e2e \
       setup docker-up docker-down docker-build docker-logs docker-ps

VENV ?= venv
PYTHON ?= $(VENV)/bin/python
PIP ?= $(VENV)/bin/pip
NODE ?= node

# ── Local Development ──────────────────────────────────────

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

setup: install env-check  ## First-time local setup

env-check:
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example — review and edit as needed")

run:
	$(PYTHON) server.py

test: test-python test-ui

test-python:
	$(PYTHON) -m unittest discover -s tests

test-ui:
	$(NODE) --test tests/test_ui_formatters.mjs

test-e2e:
	$(NODE) tests/test_phase1_e2e.mjs

# ── Docker ─────────────────────────────────────────────────

docker-up:  ## Start all services (Ollama + app)
	@test -f .env || cp .env.example .env
	docker compose up --build -d
	@echo ""
	@echo "───────────────────────────────────────────────"
	@echo "  App:       http://127.0.0.1:$$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8800)"
	@echo "  Ollama:    http://127.0.0.1:11434"
	@echo "  Logs:      make docker-logs"
	@echo "  Stop:      make docker-down"
	@echo "───────────────────────────────────────────────"

docker-down:  ## Stop all services
	docker compose down

docker-build:  ## Rebuild app image
	docker compose build app

docker-logs:  ## Follow app logs
	docker compose logs -f app

docker-ps:  ## Show running containers
	docker compose ps
