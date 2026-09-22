VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: help venv run test lint fmt clean

help:
	@echo "make venv   create .venv and install lamplight with dev extras"
	@echo "make run    run the UI on http://127.0.0.1:3847 (needs sudo to install anything)"
	@echo "make test   run the test suite"
	@echo "make lint   ruff check"
	@echo "make fmt    ruff format"
	@echo "make clean  remove .venv and build artifacts"

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

run: venv
	$(VENV)/bin/lamplight --port 3847

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check lamplight tests

fmt:
	$(VENV)/bin/ruff format lamplight tests

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
