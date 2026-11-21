#!/usr/bin/env python3
"""PreToolUse hook (`read_file|view_file`): enforces token-diet read budgets + free symbol maps."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

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


def _deny(
    reason: str,
    payload: dict[str, Any] | None = None,
    repo: Path | None = None,
    rule_sig: str = "",
) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))
    if payload is not None:
        try:
            for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
                if p not in sys.path:
                    sys.path.insert(0, p)
            from planner_state import get_paths, load_json, resolve_repo_root  # noqa: E402
            from plugin_diagnostics import record_guard_deny_if_burst  # noqa: E402

            _, _, _, hook_file = get_paths(payload)
            hook_state = load_json(hook_file)
            sig = rule_sig or reason.split("|", 1)[0].strip()[:40]
            r_path = repo if repo is not None else resolve_repo_root(payload)
            record_guard_deny_if_burst("planner_guard_read.py", sig, reason, hook_file, hook_state, repo=r_path)
        except Exception:
            pass


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        _allow()
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        tool = payload.get("toolCall") or {}
        args = tool.get("args") or {}
        raw_path = (args.get("Path") or args.get("AbsolutePath") or args.get("TargetFile") or "").strip()
        if not raw_path:
            _allow()
            return

        norm_path = raw_path.replace("\\", "/")
        for p_dir in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
            if p_dir not in sys.path:
                sys.path.insert(0, p_dir)
        from planner_state import extract_symbol_map, load_project_profile, normalize_rel_path, record_telemetry, resolve_repo_root  # noqa: E402

        if TRANSCRIPT_RE.search(norm_path):
            record_telemetry("read_blocked_transcript")
            _deny(
                "DENY|transcript*.jsonl read forbidden -> use `manage_subagents` && await summary.",
                payload=payload,
                rule_sig="DENY:transcript",
            )
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
            _deny(
                f"DENY|PROJECT_HISTORY.md={n_lines}L (>150L) without EndLine -> read StartLine=1 EndLine=60 (newest-first).",
                payload=payload,
                repo=repo,
                rule_sig="DENY:history",
            )
            return

        rel, _ = normalize_rel_path(raw_path, repo)
        archive_specs = [str(a).replace("\\", "/").lstrip("./") for a in (profile.get("archive_docs") or []) if str(a).strip()]
        handoff_p = repo / ".agents" / "docs" / "HANDOFF.md"
        if handoff_p.is_file():
            for h_ln in handoff_p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if h_ln.startswith("ARCHIVE|"):
                    arch_body = h_ln[8:].split("(", 1)[0].strip()
                    for tok in arch_body.split(","):
                        t_clean = tok.strip().strip("`").replace("\\", "/").lstrip("./")
                        if t_clean:
                            archive_specs.append(t_clean)
        is_archive = (
            rel.startswith(".agents/docs/archive/")
            or "/.agents/docs/archive/" in norm_path
            or any(rel == sp or rel.endswith("/" + sp) or norm_path.endswith("/" + sp) for sp in archive_specs)
        )
        archive_max = int(rb.get("archive_max_lines") or 150)
        archive_slice = int(rb.get("archive_max_slice_lines") or min(120, max_slice))
        if is_archive and n_lines > archive_max:
            has_bounded = (
                start_line is not None
                and end_line is not None
                and max(0, e_idx - s_idx + 1) <= archive_slice
            )
            if not has_bounded:
                record_telemetry("read_blocked_archive", {"file": rel or p.name, "lines": n_lines})
                sym_map = extract_symbol_map(lines, suf, max_entries=40)
                msg = (
                    f"DENY|ARCHIVE DOC `{rel or p.name}` ({n_lines}L > {archive_max}L) -> closed phase/archive; "
                    f"pass explicit StartLine & EndLine (<= {archive_slice}L) only for targeted lookup."
                )
                if sym_map:
                    msg += "\n[SYMBOL_MAP]\n" + "\n".join(sym_map)
                _deny(msg, payload=payload, repo=repo, rule_sig="DENY:archive")
                return

        is_log = (suf == ".log") or p.name.lower().endswith("_log.txt")
        if is_log and n_lines > max_full and not has_small_slice:
            digest_cli = PLUGIN_ROOT / "scripts" / "log_digest.py"
            record_telemetry("read_blocked_log", {"file": p.name, "lines": n_lines})
            _deny(
                f"DENY|log `{p.name}`={n_lines}L (>{max_full}L) -> run `python3 {digest_cli} {p}` "
                f"or read bounded slice <= {max_slice}L.",
                payload=payload,
                repo=repo,
                rule_sig="DENY:log",
            )
            return

        if suf in CODE_READ_EXTS and n_lines > max_full and not has_small_slice:
            sym_map = extract_symbol_map(lines, suf, max_entries=65)
            record_telemetry("read_deflected_symbol_map", {"file": p.name, "lines": n_lines})
            header = f"DENY|read-budget `{p.name}` ({n_lines}L > {max_full}L) -> pass StartLine/EndLine (<= {max_slice}L)."
            if sym_map:
                _deny(header + "\n[SYMBOL_MAP]\n" + "\n".join(sym_map), payload=payload, repo=repo, rule_sig="DENY:read-budget")
            else:
                _deny(header + " Locate target line with grep first.", payload=payload, repo=repo, rule_sig="DENY:read-budget")
            return

        _allow()
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_guard_read.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_guard_read.py", exc)
        except Exception:
            pass
        print('{"decision":"allow"}')
