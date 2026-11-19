#!/usr/bin/env python3
"""Universal SessionStart hook: compact pipe-delimited live git/version/HANDOFF/memory status."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent


def run_git(repo: Path, args: list[str]) -> str:
    if not (repo / ".git").exists():
        return ""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        print("{}")
        return

    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))

    from planner_state import (  # noqa: E402
        BOOTSTRAP_REQUIRED_FILES,
        DEFAULT_EXTRA_REQUESTS_FILE,
        bootstrap_workspace,
        detect_doc_mcps,
        eval_conditions,
        get_paths,
        get_pending_extra_requests,
        is_real_project_root,
        load_project_profile_with_warnings,
        resolve_planner_mode_info,
        resolve_repo_root,
    )

    repo = resolve_repo_root(payload)

    auto_initialized: list[str] = []
    if is_real_project_root(repo) and any(not (repo / rel).exists() for rel in BOOTSTRAP_REQUIRED_FILES):
        auto_initialized, _ = bootstrap_workspace(repo)

    profile, profile_warnings = load_project_profile_with_warnings(repo)
    _, task_md, _, _ = get_paths(payload)

    proj_name = profile.get("project_name") or repo.name
    mode, mode_src, model_name = resolve_planner_mode_info(profile, payload)
    if mode_src == "auto:unseen" and model_name:
        mode_tag = f"{mode}(auto:{model_name}|unseen)"
    elif mode_src.startswith("auto:") and model_name and model_name.lower() != "auto":
        mode_tag = f"{mode}(auto:{model_name})"
    else:
        mode_tag = mode
    branch = run_git(repo, ["branch", "--show-current"])
    recent_commits = run_git(repo, ["log", "-n", "4", "--oneline"])
    dirty_lines = [l for l in run_git(repo, ["status", "-s"]).splitlines() if l.strip()]

    tree_state = "clean" if not dirty_lines else f"{len(dirty_lines)} dirty"
    head_meta = f"[{proj_name}|mode={mode_tag}" + (f"|branch={branch}|tree={tree_state}]" if branch else f"|ws={repo}]")
    lines = [head_meta]

    session_cfg: dict[str, Any] = profile.get("session") or {}

    ver_parts: list[str] = []
    for vf in session_cfg.get("version_files") or []:
        rel_p = vf.get("path") or ""
        pat = vf.get("regex") or ""
        label = vf.get("label") or rel_p
        if rel_p and pat:
            txt = read_text(repo / rel_p)
            m = re.search(pat, txt)
            ver_parts.append(f"{label}={m.group(1).strip() if m else '?'}")
    if ver_parts:
        lines.append("VERSIONS|" + " | ".join(ver_parts))

    if recent_commits:
        lines.append("COMMITS|" + " ; ".join(recent_commits.splitlines()))
    if dirty_lines:
        preview = ", ".join(l.strip() for l in dirty_lines[:6])
        if len(dirty_lines) > 6:
            preview += f" (+{len(dirty_lines) - 6})"
        lines.append(f"DIRTY|{preview}")

    for note in session_cfg.get("env_notes") or []:
        if eval_conditions(note.get("conditions"), repo) and note.get("text"):
            lines.append(f"ENV|{note['text']}")

    for wp in session_cfg.get("watch_paths") or []:
        raw_path = wp.get("path") or ""
        if not raw_path:
            continue
        target = Path(raw_path).expanduser() if raw_path.startswith(("/", "~")) else (repo / raw_path)
        if target.exists():
            label = wp.get("label") or "watch"
            if wp.get("show_size_mb") and target.is_file():
                mb = target.stat().st_size / (1024 * 1024)
                lines.append(f"WATCH|{label}: `{target}` ({mb:.1f} MB)")
            else:
                lines.append(f"WATCH|{label}: `{target}`")

    handoff_file = repo / ".agents" / "docs" / "HANDOFF.md"
    if handoff_file.is_file():
        h_lines = [ln for ln in read_text(handoff_file).splitlines()[:20] if ln.strip()]
        if h_lines:
            lines.append("HANDOFF|\n" + "\n".join(h_lines))

    mem_candidates = []
    for rel in (
        ".agents/docs/PROJECT_HISTORY.md",
        ".agents/docs/SAVED_IDEAS.md",
        *(session_cfg.get("extra_docs") or []),
    ):
        if rel not in mem_candidates and (repo / rel).is_file():
            mem_candidates.append(rel)
    if mem_candidates:
        lines.append("DOCS|" + ",".join(f"`{m}`" for m in mem_candidates))

    hist_p = repo / ".agents" / "docs" / "PROJECT_HISTORY.md"
    if hist_p.is_file():
        hist_len = len(read_text(hist_p).splitlines())
        if hist_len > 150:
            lines.append(f"MEMORY_WEIGHT|`PROJECT_HISTORY.md`={hist_len}L (>150L) -> archive old entries to `.agents/docs/archive/`.")

    extra_rel, pending_extras = get_pending_extra_requests(repo, profile)
    extra_p = repo / extra_rel
    if extra_p.is_file():
        if pending_extras:
            lines.append(
                f"ATTENTION — Pending extra requests in `{extra_rel}` ({len(pending_extras)} line(s)) -> implement & clear before commit/ship!"
            )
        else:
            lines.append(f"EXTRA_QUEUE|`{extra_rel}` clean (check before commit/ship)")
        m_done = re.search(
            r"^#{1,3}\s+(?:Completed Requests|Conclu[ií]dos|Completed)(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#{1,3}\s+|\Z)",
            read_text(extra_p),
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if m_done:
            done_items = [l for l in m_done.group(1).splitlines() if l.strip() and not l.strip().lower().startswith(("(no completed", "(none", "<!--"))]
            if len(done_items) > 15:
                lines.append(f"MEMORY_WEIGHT|`{extra_rel}` completed={len(done_items)} (>15) -> archive old items.")

    if auto_initialized:
        lines.append("Auto-initialized project docs/profile: " + ", ".join(f"`{p}`" for p in auto_initialized))
    for w in profile_warnings:
        lines.append(f"Schema warning: {w}")

    doc_mcps = detect_doc_mcps(profile)
    doc_mcp_parts: list[str] = []
    if doc_mcps.get("gdk"):
        doc_mcp_parts.append("google-developer-knowledge(search_documents/answer_query)")
    if doc_mcps.get("context7"):
        doc_mcp_parts.append("context7(resolve-library-id->query-docs)")
    if doc_mcp_parts:
        lines.append("DOC_MCP|" + " + ".join(doc_mcp_parts))

    plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
    verify_cli = PLUGIN_ROOT / "scripts" / "verify_universal.py"
    lines.append(f"TOOLS|plan=`python3 {plan_cli}`|verify=`python3 {verify_cli}`|board=`{task_md}`")

    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": "\n".join(lines)}}]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
