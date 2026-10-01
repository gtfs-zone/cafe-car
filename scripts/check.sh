#!/bin/sh
# Lint, format check and tests; run by the pre-commit hook and CI.
set -e
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
