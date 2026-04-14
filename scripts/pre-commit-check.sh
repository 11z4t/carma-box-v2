#!/bin/bash
# Pre-commit quality gate — MUST pass before every commit
# Usage: ./scripts/pre-commit-check.sh
set -e

echo "=== 1. ruff check ==="
python3 -m ruff check .

echo "=== 2. mypy --strict ==="
python3 -m mypy --strict config/ core/ adapters/ main.py tests/

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

# ══════════════════════════════════════════════════════════════
# MAGIC NUMBER CHECK — added after 11+ QC rejects
# Scans changed files for naked numeric literals in logic code
# ══════════════════════════════════════════════════════════════
echo "Checking for magic numbers in staged files..."
MAGIC_FOUND=0
for f in $(git diff --cached --name-only --diff-filter=ACMR | grep '\.py$' | grep -v test | grep -v __pycache__); do
    # Find lines with naked numbers (not in comments, imports, config defaults)
    HITS=$(grep -nP '\b\d+\.?\d*\b' "$f" 2>/dev/null | \
        grep -v '#' | \
        grep -v 'import' | \
        grep -v 'Field(' | \
        grep -v 'default=' | \
        grep -v '[A-Z_][A-Z_]' | \
        grep -v 'config\.' | \
        grep -v 'self\._config' | \
        grep -v '\.0\b' | \
        grep -v '\b[01]\b' | \
        grep -v 'range(' | \
        grep -v 'len(' | \
        grep -v 'log\.' | \
        grep -v '\.py:' | \
        grep -v 'version' | \
        grep -v '__version__')
    if [ -n "$HITS" ]; then
        echo "⚠️  POTENTIAL MAGIC NUMBERS in $f:"
        echo "$HITS" | head -5
        MAGIC_FOUND=1
    fi
done
if [ "$MAGIC_FOUND" -eq 1 ]; then
    echo ""
    echo "⚠️  Review above — are these from config/constants? If yes, proceed. If no, extract to config."
    echo "   Checklista: 'Är detta site-specifikt? → config flag. Är detta ett tal? → config/constant.'"
fi
