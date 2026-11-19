#!/usr/bin/env bash
# Portable installer, snapshotter & 1-second rollback for Dynamic Fractal Planner.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET_DIR="${GEMINI_HOME:-$HOME/.gemini}/config/plugins/dynamic-fractal-planner"
MODE="copy"
RUN_TESTS=1
DO_ROLLBACK=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--target <dir>] [--symlink] [--skip-tests] [--rollback] [-h|--help]
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
    --rollback)
      DO_ROLLBACK=1
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

mkdir -p "$(dirname "$TARGET_DIR")"
TARGET_PARENT_REAL="$(cd "$(dirname "$TARGET_DIR")" && pwd -P)"
TARGET_REAL="${TARGET_PARENT_REAL}/$(basename "$TARGET_DIR")"
BACKUP_DIR="${TARGET_REAL}/.last_good_backup"

if [[ "$DO_ROLLBACK" -eq 1 ]]; then
  if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "Error: no rollback snapshot at $BACKUP_DIR" >&2
    exit 1
  fi
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude '.git' --exclude '.last_good_backup' --exclude '.metrics.jsonl' --exclude '.model_modes.json' --exclude '.bypass_gates' --exclude '.diagnostics' --exclude '__pycache__' "$BACKUP_DIR/" "$TARGET_REAL/"
  else
    python3 - "$BACKUP_DIR" "$TARGET_REAL" <<'PY'
import shutil, sys
from pathlib import Path
bak, dst = Path(sys.argv[1]), Path(sys.argv[2])
keep = {".git", ".last_good_backup", ".metrics.jsonl", ".model_modes.json", ".bypass_gates", ".diagnostics", "__pycache__"}
for item in dst.iterdir():
    if item.name in keep:
        continue
    if item.is_dir() and not item.is_symlink():
        shutil.rmtree(item)
    else:
        item.unlink()
for item in bak.iterdir():
    target = dst / item.name
    if item.is_dir():
        shutil.copytree(item, target, dirs_exist_ok=True)
    else:
        shutil.copy2(item, target)
PY
  fi
  chmod +x "$TARGET_REAL/install.sh" "$TARGET_REAL"/hooks/*.py "$TARGET_REAL"/scripts/*.py "$TARGET_REAL"/tests/*.py 2>/dev/null || true
  python3 - "$TARGET_REAL" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
for name in ("plugin.json", "hooks.json"):
    json.loads((root / name).read_text(encoding="utf-8"))
print(f"Rollback complete + manifests valid in {root}")
PY
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required (Python 3.9+)." >&2
  exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"Error: Python 3.9+ required, found {sys.version.split()[0]}")
PY

if [[ "$SCRIPT_DIR" != "$TARGET_REAL" ]]; then
  if [[ "$MODE" == "symlink" ]]; then
    rm -rf "$TARGET_REAL"
    ln -s "$SCRIPT_DIR" "$TARGET_REAL"
    echo "Linked plugin: $TARGET_REAL -> $SCRIPT_DIR"
  else
    mkdir -p "$TARGET_REAL"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete --exclude '.git' --exclude '.last_good_backup' --exclude '.metrics.jsonl' --exclude '.model_modes.json' --exclude '.bypass_gates' --exclude '.diagnostics' --exclude '__pycache__' --exclude '*.pyc' "$SCRIPT_DIR/" "$TARGET_REAL/"
    else
      (cd "$SCRIPT_DIR" && tar --exclude='.git' --exclude='.last_good_backup' --exclude='.metrics.jsonl' --exclude='.model_modes.json' --exclude='.bypass_gates' --exclude='.diagnostics' --exclude='__pycache__' --exclude='*.pyc' -cf - .) | (cd "$TARGET_REAL" && tar -xf -)
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
    json.loads((root / name).read_text(encoding="utf-8"))
print(f"Validated manifests in {root}")
PY

if [[ "$RUN_TESTS" -eq 1 ]]; then
  echo "Running plugin regression tests..."
  python3 "$TARGET_REAL/tests/run_plugin_tests.py"
fi

# Snapshot clean verified state to .last_good_backup for instant --rollback
python3 - "$TARGET_REAL" "$BACKUP_DIR" <<'PY'
import shutil, sys
from pathlib import Path
src, bak = Path(sys.argv[1]), Path(sys.argv[2])
tmp_bak = src / ".last_good_backup.tmp"
if tmp_bak.exists():
    shutil.rmtree(tmp_bak)
ignore = shutil.ignore_patterns(".git", ".last_good_backup", ".last_good_backup.tmp", ".metrics.jsonl", ".model_modes.json", ".bypass_gates", ".diagnostics", "__pycache__", "*.pyc")
shutil.copytree(src, tmp_bak, ignore=ignore)
if bak.exists():
    shutil.rmtree(bak)
tmp_bak.rename(bak)
print(f"Snapshot saved at {bak}")
PY

echo "Dynamic Fractal Planner v3 is ready."
