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

echo "=== 4. Anti-pattern checks (from QC rejects) ==="

# QC reject pattern: global pytestmark asyncio on files with sync tests
BAD_PYTESTMARK=$(grep -rn "^pytestmark = pytest.mark.asyncio" tests/ 2>/dev/null || true)
if [ -n "$BAD_PYTESTMARK" ]; then
    echo "FAIL: Global pytestmark asyncio found (use per-class instead):"
    echo "$BAD_PYTESTMARK"
    exit 1
fi

# QC reject pattern: hardcoded SoC floor values in logic functions (not dataclass defaults)
BAD_HARDCODE=$(grep -rn "floor\s*=\s*15\.0\|floor\s*=\s*20\.0\|floor\s*=\s*25\.0" core/ adapters/ 2>/dev/null | grep -v "Config\|default=\|Field(" || true)
if [ -n "$BAD_HARDCODE" ]; then
    echo "FAIL: Hardcoded SoC floor values in logic code (use config):"
    echo "$BAD_HARDCODE"
    exit 1
fi

echo "=== ALL CHECKS PASSED ==="
