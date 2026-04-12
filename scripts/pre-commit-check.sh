#!/bin/bash
# Pre-commit quality gate — MUST pass before every commit
# Usage: ./scripts/pre-commit-check.sh
set -e

echo "=== 1. ruff check ==="
python3 -m ruff check .

echo "=== 2. mypy --strict ==="
python3 -m mypy --strict config/ core/ adapters/ main.py

echo "=== 3. pytest (no warnings allowed) ==="
python3 -m pytest tests/ -q --timeout=10 -W error::pytest.PytestWarning 2>&1

echo "=== ALL CHECKS PASSED ==="
