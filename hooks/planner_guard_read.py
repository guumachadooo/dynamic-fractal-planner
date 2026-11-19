#!/usr/bin/env python3
"""PreToolUse hook (`read_file|view_file`): enforces token-diet read budgets + free symbol maps."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent

CODE_READ_EXTS = frozenset({
    ".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".swift", ".dart", ".rb", ".php", ".sh", ".md", ".yml", ".yaml",
})
BINARY_READ_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".ttf", ".otf", ".woff", ".woff2", ".apk", ".dll", ".exe",
    ".so", ".a", ".zip", ".gz", ".tar", ".xz", ".mp3", ".flac", ".mp4", ".mkv", ".wav", ".pdf",
})
TRANSCRIPT_RE = re.compile(r"/\.gemini/antigravity/brain/.*/transcript[^/]*\.jsonl$", re.IGNORECASE)


def _allow() -> None:
    print('{"decision":"allow"}')


def _deny(reason: str) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        _allow()
        return

    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    tool = payload.get("toolCall") or {}
    args = tool.get("args") or {}
    raw_path = (args.get("Path") or args.get("AbsolutePath") or args.get("TargetFile") or "").strip()
    if not raw_path:
        _allow()
        return

    norm_path = raw_path.replace("\\", "/")
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    from planner_state import extract_symbol_map, load_project_profile, record_telemetry, resolve_repo_root  # noqa: E402

    if TRANSCRIPT_RE.search(norm_path):
        record_telemetry("read_blocked_transcript")
        _deny("DENY|transcript*.jsonl read forbidden -> use `manage_subagents` && await summary.")
        return

    p = Path(raw_path).expanduser()
    if not p.is_file():
        _allow()
        return

    suf = p.suffix.lower()
    if suf in BINARY_READ_EXTS:
        _allow()
        return

    start_raw = args.get("StartLine") if args.get("StartLine") is not None else args.get("start_line")
    end_raw = args.get("EndLine") if args.get("EndLine") is not None else args.get("end_line")
    try:
        start_line = int(start_raw) if start_raw is not None else None
    except (TypeError, ValueError):
        start_line = None
    try:
        end_line = int(end_raw) if end_raw is not None else None
    except (TypeError, ValueError):
        end_line = None

    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    n_lines = len(lines)

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)
    rb = profile.get("read_budget") or {}
    max_full = int(rb.get("max_full_read_lines") or 300)
    max_slice = int(rb.get("max_slice_lines") or 250)

    has_range = (start_line is not None) or (end_line is not None)
    if has_range:
        s_idx = max(1, start_line) if start_line is not None else 1
        e_idx = min(n_lines, end_line) if end_line is not None else n_lines
        has_small_slice = max(0, e_idx - s_idx + 1) <= max_slice
    else:
        has_small_slice = False

    if p.name == "PROJECT_HISTORY.md" and n_lines > 150 and end_line is None:
        record_telemetry("read_blocked_history", {"lines": n_lines})
        _deny(f"DENY|PROJECT_HISTORY.md={n_lines}L (>150L) without EndLine -> read StartLine=1 EndLine=60 (newest-first).")
        return

    is_log = (suf == ".log") or p.name.lower().endswith("_log.txt")
    if is_log and n_lines > max_full and not has_small_slice:
        digest_cli = PLUGIN_ROOT / "scripts" / "log_digest.py"
        record_telemetry("read_blocked_log", {"file": p.name, "lines": n_lines})
        _deny(
            f"DENY|log `{p.name}`={n_lines}L (>{max_full}L) -> run `python3 {digest_cli} {p}` "
            f"or read bounded slice <= {max_slice}L."
        )
        return

    if suf in CODE_READ_EXTS and n_lines > max_full and not has_small_slice:
        sym_map = extract_symbol_map(lines, suf, max_entries=65)
        record_telemetry("read_deflected_symbol_map", {"file": p.name, "lines": n_lines})
        header = f"DENY|read-budget `{p.name}` ({n_lines}L > {max_full}L) -> pass StartLine/EndLine (<= {max_slice}L)."
        if sym_map:
            _deny(header + "\n[SYMBOL_MAP]\n" + "\n".join(sym_map))
        else:
            _deny(header + " Locate target line with grep first.")
        return

    _allow()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"decision":"allow"}')
