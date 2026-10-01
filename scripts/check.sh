#!/bin/sh
# Lint, format, dead code, dependencies and tests; run by the pre-commit hook and CI.
set -e
uv run ruff check .
uv run ruff format --check .
uv run vulture
uv run deptry src
uv run pytest -q
