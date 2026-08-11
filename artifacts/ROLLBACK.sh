#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PATCH_FILE="$SCRIPT_DIR/telegram-file-too-big.patch"

git -C "$ROOT_DIR" apply --reverse --check "$PATCH_FILE"
git -C "$ROOT_DIR" apply --reverse "$PATCH_FILE"
printf '%s\n' 'ROLLBACK_RESULT=restored README.md, src/telegram.mjs, and test/crosspost.test.mjs'
