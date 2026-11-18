#!/usr/bin/env python3
"""Universal SessionStart hook: injects live git workspace state, memory doc pointers,
EXTRA_REQUESTS status, session plan board, and optional declarative profile watches.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from planner_state import (  # noqa: E402
    BOOTSTRAP_REQUIRED_FILES,
    bootstrap_workspace,
    eval_conditions,
    get_paths,
    get_pending_extra_requests,
    is_real_project_root,
    load_project_profile_with_warnings,
    resolve_plugin_root,
    resolve_repo_root,
)


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
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    plugin_root = resolve_plugin_root()
    repo = resolve_repo_root(payload)

    auto_initialized: list[str] = []
    if is_real_project_root(repo) and any(not (repo / rel).exists() for rel in BOOTSTRAP_REQUIRED_FILES):
        auto_initialized, _ = bootstrap_workspace(repo)

    profile, profile_warnings = load_project_profile_with_warnings(repo)
    _, task_md, _, _ = get_paths(payload)

    proj_name = profile.get("project_name") or repo.name
    branch = run_git(repo, ["branch", "--show-current"])
    recent_commits = run_git(repo, ["log", "-n", "4", "--oneline"])
    dirty_lines = [l for l in run_git(repo, ["status", "-s"]).splitlines() if l.strip()]

    lines = [f"[{proj_name} — Live Workspace Status]"]
    if branch:
        tree_state = "clean" if not dirty_lines else f"{len(dirty_lines)} modified/untracked file(s)"
        lines.append(f"- Repo: {repo} | Branch: {branch} | Working tree: {tree_state}")
    else:
        lines.append(f"- Workspace: {repo}")

    session_cfg: dict[str, Any] = profile.get("session") or {}

    # Declarative version extraction (e.g., csproj + CI workflow)
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
        lines.append("- Configured versions (live): " + " | ".join(ver_parts))

    if recent_commits:
        lines.append("- Recent commits:\n  " + "\n  ".join(recent_commits.splitlines()))
    if dirty_lines:
        preview = ", ".join(l.strip() for l in dirty_lines[:6])
        if len(dirty_lines) > 6:
            preview += f" (+{len(dirty_lines) - 6} more)"
        lines.append(f"- Local dirty files (possible concurrent work): {preview}")

    # Declarative environment notes
    for note in session_cfg.get("env_notes") or []:
        if eval_conditions(note.get("conditions"), repo) and note.get("text"):
            lines.append(f"- {note['text']}")

    # Declarative file watches (e.g., latest APK size, shared device log)
    for wp in session_cfg.get("watch_paths") or []:
        raw_path = wp.get("path") or ""
        if not raw_path:
            continue
        target = Path(raw_path).expanduser() if raw_path.startswith(("/", "~")) else (repo / raw_path)
        if target.exists():
            label = wp.get("label") or "Watched file"
            if wp.get("show_size_mb") and target.is_file():
                mb = target.stat().st_size / (1024 * 1024)
                lines.append(f"- {label}: `{target}` ({mb:.1f} MB)")
            else:
                lines.append(f"- {label}: `{target}`")

    # Project memory & reference docs discovery
    mem_candidates = []
    for rel in (
        ".agents/docs/PROJECT_HISTORY.md",
        ".agents/docs/SAVED_IDEAS.md",
        *(session_cfg.get("extra_docs") or []),
    ):
        if rel not in mem_candidates and (repo / rel).is_file():
            mem_candidates.append(rel)
    if mem_candidates:
        lines.append("- On-demand memory & specs: " + ", ".join(f"`{m}`" for m in mem_candidates))

    # Parallel /btw queue check (universal whenever EXTRA_REQUESTS.md exists)
    extra_rel, pending_extras = get_pending_extra_requests(repo, profile)
    if (repo / extra_rel).is_file():
        if pending_extras:
            lines.append(
                f"- ATTENTION — Pending extra requests in `{extra_rel}` ({len(pending_extras)} line(s)): "
                "read, plan, implement, and clear them before committing or shipping!"
            )
        else:
            lines.append(f"- Parallel `/btw` queue: always check `{extra_rel}` before finishing or committing.")

    if auto_initialized:
        lines.append("- Auto-initialized project docs/profile: " + ", ".join(f"`{p}`" for p in auto_initialized))
    for w in profile_warnings:
        lines.append(f"- Schema warning: {w}")

    plan_cli = plugin_root / "scripts" / "plan_tool.py"
    verify_cli = plugin_root / "scripts" / "verify_universal.py"
    lines.append(
        f"- Dynamic Fractal Planner: board `{task_md}` | `python3 {plan_cli}` | `python3 {verify_cli}` "
        "(skills: `dynamic-planner`, `project-memory`)."
    )

    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": "\n".join(lines)}}]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
