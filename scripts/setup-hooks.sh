#!/bin/bash
# Install git hooks from scripts/hooks/ into .git/hooks/
# Run once after clone: bash scripts/setup-hooks.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_SRC="$SCRIPT_DIR/hooks"
HOOKS_DST="$(git rev-parse --show-toplevel)/.git/hooks"

if [ ! -d "$HOOKS_SRC" ]; then
    echo "ERROR: $HOOKS_SRC not found"
    exit 1
fi

for hook in "$HOOKS_SRC"/*; do
    name=$(basename "$hook")
    cp "$hook" "$HOOKS_DST/$name"
    chmod +x "$HOOKS_DST/$name"
    echo "Installed: $name"
done

echo "Done. Hooks installed in $HOOKS_DST/"
