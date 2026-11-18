#!/usr/bin/env bash
# Portable installer & self-check for the Dynamic Fractal Planner Antigravity plugin.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET_DIR="${GEMINI_HOME:-$HOME/.gemini}/config/plugins/dynamic-fractal-planner"
MODE="copy"
RUN_TESTS=1

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install or verify the Dynamic Fractal Planner global Antigravity plugin.

Options:
  --target <dir>   Custom destination plugin directory
                   (default: ${GEMINI_HOME:-$HOME/.gemini}/config/plugins/dynamic-fractal-planner)
  --symlink        Symlink the repository instead of copying files
  --skip-tests     Skip running tests/run_plugin_tests.py after installation
  -h, --help       Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET_DIR="${2:?Missing value for --target}"
      shift 2
      ;;
    --symlink)
      MODE="symlink"
      shift
      ;;
    --skip-tests)
      RUN_TESTS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required (Python 3.9+)." >&2
  exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"Error: Python 3.9+ required, found {sys.version.split()[0]}")
PY

mkdir -p "$(dirname "$TARGET_DIR")"
TARGET_PARENT_REAL="$(cd "$(dirname "$TARGET_DIR")" && pwd -P)"
TARGET_REAL="${TARGET_PARENT_REAL}/$(basename "$TARGET_DIR")"

if [[ "$SCRIPT_DIR" != "$TARGET_REAL" ]]; then
  if [[ "$MODE" == "symlink" ]]; then
    rm -rf "$TARGET_REAL"
    ln -s "$SCRIPT_DIR" "$TARGET_REAL"
    echo "Linked plugin: $TARGET_REAL -> $SCRIPT_DIR"
  else
    mkdir -p "$TARGET_REAL"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' "$SCRIPT_DIR/" "$TARGET_REAL/"
    else
      (cd "$SCRIPT_DIR" && tar --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' -cf - .) | (cd "$TARGET_REAL" && tar -xf -)
    fi
    echo "Installed plugin to: $TARGET_REAL"
  fi
else
  echo "Plugin already at destination: $TARGET_REAL"
fi

chmod +x "$TARGET_REAL/install.sh" "$TARGET_REAL"/hooks/*.py "$TARGET_REAL"/scripts/*.py "$TARGET_REAL"/tests/*.py 2>/dev/null || true

python3 - "$TARGET_REAL" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
for name in ("plugin.json", "hooks.json"):
    p = root / name
    json.loads(p.read_text(encoding="utf-8"))
print(f"Validated manifests in {root}")
PY

if [[ "$RUN_TESTS" -eq 1 ]]; then
  echo "Running plugin regression tests..."
  python3 "$TARGET_REAL/tests/run_plugin_tests.py"
fi

echo "Dynamic Fractal Planner is ready."
