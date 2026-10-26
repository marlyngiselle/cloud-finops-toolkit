.DEFAULT_GOAL := help
PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: help venv install lint format typecheck test check demo clean

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

venv: $(BIN)/python ## Create the virtual environment

install: venv ## Install the package with dev dependencies (editable)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]"

lint: ## Ruff lint and format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Apply ruff formatting and safe fixes
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck: ## Mypy (strict)
	$(BIN)/mypy

test: ## Pytest with coverage
	$(BIN)/pytest

check: lint typecheck test ## Everything CI runs

demo: ## Run every command against the bundled synthetic data
	$(BIN)/finops --demo idle
	$(BIN)/finops --demo tags --policy examples/tag-policy.yaml
	$(BIN)/finops --demo rightsize
	$(BIN)/finops --demo hybrid-benefit
	$(BIN)/finops --demo schedule run --at 2026-10-27T18:10:00Z

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
