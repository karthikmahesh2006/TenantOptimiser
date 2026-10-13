.PHONY: test lint run clean deploy test-property test-unit

VENV ?= .venv
PYTHON = $(VENV)/bin/python
PYTEST = $(VENV)/bin/pytest
RUFF = $(VENV)/bin/ruff

test:
	$(PYTEST) tests/unit tests/property -v

test-unit:
	$(PYTEST) tests/unit -v

test-property:
	$(PYTEST) tests/property -v

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .hypothesis

deploy:
	bash scripts/deploy.sh
