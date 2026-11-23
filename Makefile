# One command that runs every check CI runs, except the Docker image, Lighthouse and Playwright.
# Needs the shared virtualenv (see CLAUDE.md) and `bun install --frozen-lockfile` in site/.
# On Windows: make VENV=server/.venv/Scripts
VENV ?= server/.venv/bin
PY := $(VENV)/python

.PHONY: check lint test site-check

check: lint test site-check

lint:
	$(VENV)/ruff check .

test:
	cd bot && ../$(PY) -m pytest -q -p no:warnings
	cd server && ../$(PY) -m pytest -q tests

site-check:
	cd site && bun run check && bun run knip && bun run build
