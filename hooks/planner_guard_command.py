#!/usr/bin/env python3
"""Universal PreToolUse hook for run_command: enforces EXTRA_REQUESTS gate, active plan
completeness on commit, memory feedback reminder, declarative project command_guards[],
and transparent PLANNER_ARTIFACT_DIR / tool-path routing.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from planner_state import (  # noqa: E402
    DEFAULT_CODE_EXTS,
    DEFAULT_MEMORY_DOCS,
    eval_conditions,
    get_paths,
    get_pending_extra_requests,
    has_active_turn_plan,
    load_json,
    load_project_profile,
    prompt_hash,
    resolve_artifact_dir,
    resolve_plugin_root,
    resolve_repo_root,
)


def allow(overwrite_cmd: str | None = None) -> None:
    if overwrite_cmd is not None:
        print(json.dumps({"decision": "allow", "overwrite": {"CommandLine": overwrite_cmd}}, ensure_ascii=False))
    else:
        print(json.dumps({"decision": "allow"}))


def deny(reason: str) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))


def split_shell_segments(cmd: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    in_sq = False
    in_dq = False
    escape = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\" and not in_sq:
            buf.append(ch)
            escape = True
            i += 1
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            buf.append(ch)
            i += 1
            continue
        if not in_sq and not in_dq:
            if ch in ";\n" or ch == "|":
                if cmd[i : i + 2] == "||":
                    seg = "".join(buf).strip()
                    if seg:
                        segments.append(seg)
                    buf.clear()
                    i += 2
                    continue
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf.clear()
                i += 1
                continue
            if cmd[i : i + 2] == "&&":
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf.clear()
                i += 2
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def strip_env_prefix(segment: str) -> tuple[dict[str, str], str]:
    envs: dict[str, str] = {}
    rest = segment.lstrip()
    env_pat = re.compile(r"""^([A-Za-z_][A-Za-z0-9_]*)=('[^']*'|"[^"]*"|\S+)\s*""")
    while True:
        m = env_pat.match(rest)
        if not m:
            break
        envs[m.group(1)] = m.group(2).strip("'\"")
        rest = rest[m.end() :].lstrip()
    return envs, rest


def check_extra_requests_before_ship(segments: list[str], repo: Path, profile: dict[str, Any]) -> str | None:
    is_ship_cmd = any(
        re.match(r"^git(?:\s+-C\s+\S+)?\s+(commit|push)\b", strip_env_prefix(seg)[1])
        for seg in segments
    )
    if not is_ship_cmd:
        return None
    rel, pending = get_pending_extra_requests(repo, profile)
    if not pending:
        return None
    preview = "\n".join(f"  - {line[:120]}" for line in pending[:5])
    if len(pending) > 5:
        preview += f"\n  (+{len(pending) - 5} line(s))"
    return (
        f"BLOCKED BEFORE COMMIT/PUSH: pending extra requests exist in `{rel}` (from parallel `/btw` threads):\n"
        f"{preview}\n\n"
        f"Read `{rel}`, plan and implement every item, move them to `## Completed Requests`, and only then commit/push."
    )


def check_plan_before_commit(segments: list[str], payload: dict[str, Any]) -> str | None:
    commit_seg = None
    for seg in segments:
        _, rest = strip_env_prefix(seg)
        if re.match(r"^git(?:\s+-C\s+\S+)?\s+commit\b", rest):
            commit_seg = seg
            break
    if not commit_seg or "--amend" in commit_seg:
        return None

    _, _, plan_json, hook_file = get_paths(payload)
    hook_state = load_json(hook_file)
    plan_state = load_json(plan_json)
    if not plan_state or not has_active_turn_plan(payload, hook_state):
        return None
    if plan_state.get("lifecycle") in ("awaiting_user", "blocked"):
        return None

    mods = plan_state.get("modules") or []
    ship_only_reqs = set()
    non_ship_reqs = set()
    for m in mods:
        if m.get("domain") == "audit":
            continue
        is_ship_mod = bool(m.get("is_ship")) or m.get("domain") in ("build-ci", "build-deliver")
        for rid in (m.get("linked_reqs") or []):
            if is_ship_mod:
                ship_only_reqs.add(rid)
            else:
                non_ship_reqs.add(rid)
    ship_only_reqs -= non_ship_reqs

    open_mods = [
        m["id"]
        for m in mods
        if m.get("status") != "done" and not m.get("is_ship") and m.get("domain") not in ("build-ci", "build-deliver", "audit")
    ]
    unverified_reqs = [
        r["id"]
        for r in (plan_state.get("requirements") or [])
        if r.get("status") != "verified" and r["id"] not in ship_only_reqs
    ]
    if not open_mods and not unverified_reqs:
        return None

    return (
        "BLOCKED BEFORE COMMIT: Active dynamic plan still has unfinished items:\n"
        + (f"  - Open modules: {', '.join(open_mods)}\n" if open_mods else "")
        + (f"  - Unverified requirements: {', '.join(unverified_reqs)}\n" if unverified_reqs else "")
        + "Complete modules (`plan_tool.py done ...`), verify requirements (`plan_tool.py req-verify ...`), "
        "and run the verifier before committing."
    )


def check_commit_memory_feedback(
    segments: list[str],
    payload: dict[str, Any],
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    commit_seg = None
    for seg in segments:
        _, rest = strip_env_prefix(seg)
        if re.match(r"^git(?:\s+-C\s+\S+)?\s+commit\b", rest):
            commit_seg = seg
            break
    if not commit_seg or "--amend" in commit_seg or "[skip-history]" in commit_seg:
        return None
    if not (repo / ".git").exists():
        return None

    mem_cfg = profile.get("memory_feedback") or {}
    if mem_cfg.get("enabled") is False:
        return None

    doc_rels = mem_cfg.get("docs") or list(DEFAULT_MEMORY_DOCS)
    existing_docs = [repo / r for r in doc_rels if (repo / r).is_file()]
    if not existing_docs:
        return None

    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        changed = [l[3:].strip() for l in res.stdout.splitlines() if len(l) > 3]
    except Exception:
        return None

    exts = tuple(e.lower() for e in (profile.get("code_extensions") or DEFAULT_CODE_EXTS))
    changed_code = [f for f in changed if f.lower().endswith(exts)]
    min_files = int(mem_cfg.get("min_code_files") or 2)
    if len(changed_code) < min_files:
        return None

    now = time.time()
    for mf in existing_docs:
        if (now - mf.stat().st_mtime) < 3600:
            return None

    conv_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(payload.get("conversationId") or "default"))
    marker = Path(f"/tmp/.agy_mem_reminded_{conv_id}")
    if marker.exists() and (now - marker.stat().st_mtime) < 900:
        return None

    try:
        marker.touch()
    except Exception:
        pass

    custom_msg = mem_cfg.get("message")
    if custom_msg:
        return custom_msg
    target_doc = next((r for r in doc_rels if (repo / r).is_file()), doc_rels[0])
    return (
        f"Memory Feedback Reminder: {len(changed_code)} code files are modified in this commit "
        f"without a recent update to `{target_doc}`. "
        "If this cycle fixed a non-obvious bug, changed architecture/UX, or uncovered a reusable pitfall, "
        f"record a concise entry in `{target_doc}` before committing — or retry `git commit` if no new entry is needed."
    )


def check_declarative_command_guards(
    segments: list[str],
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    guards = profile.get("command_guards") or []
    if not guards:
        return None

    session_envs: set[str] = set()
    for seg in segments:
        stripped = seg.strip()
        if re.match(r"^export\s+", stripped):
            for em in re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", stripped):
                session_envs.add(em.group(1))
        inline_envs, rest = strip_env_prefix(stripped)
        active_envs = session_envs | set(inline_envs.keys())

        for g in guards:
            if not eval_conditions(g.get("conditions"), repo):
                continue
            match_pat = g.get("match_regex") or ""
            if match_pat and not re.search(match_pat, rest):
                continue

            forbid_args = g.get("forbid_args_regex") or ""
            if forbid_args and re.search(forbid_args, rest):
                return g.get("reason") or "Command blocked by project command guard."

            req_envs = g.get("require_env_vars") or []
            if req_envs:
                missing = [v for v in req_envs if v not in active_envs]
                if missing:
                    tmpl = g.get("reason") or "Missing required environment variables: {missing}."
                    return tmpl.replace("{missing}", ", ".join(missing))
    return None


def maybe_rewrite_command(cmd: str, repo: Path, payload: dict[str, Any]) -> str | None:
    plugin_root = resolve_plugin_root()
    updated = cmd

    if ".agents/scripts/plan_tool.py" in updated and not (repo / ".agents/scripts/plan_tool.py").exists():
        updated = updated.replace(".agents/scripts/plan_tool.py", str(plugin_root / "scripts/plan_tool.py"))
    if ".agents/scripts/verify_universal.py" in updated and not (repo / ".agents/scripts/verify_universal.py").exists():
        updated = updated.replace(".agents/scripts/verify_universal.py", str(plugin_root / "scripts/verify_universal.py"))

    invokes_tool = any(
        ("plan_tool.py" in seg or "verify_universal.py" in seg or "verify_changes.py" in seg or "run_plugin_tests.py" in seg)
        and re.search(
            r"(?:^|\s)(?:(?:python(?:\d+(?:\.\d+)?)?|/[^\s]+/python(?:\d+(?:\.\d+)?)?)\s+)?[^\n]*(?:plan_tool|verify_universal|verify_changes|run_plugin_tests)\.py\b",
            seg,
        )
        for seg in split_shell_segments(updated)
    )
    if invokes_tool and "PLANNER_ARTIFACT_DIR=" not in updated:
        art_dir = str(resolve_artifact_dir(payload))
        conv_id = str(payload.get("conversationId") or "default")
        p_hash = prompt_hash(payload.get("lastUserInput") or "")
        prefix = (
            f"export PLANNER_ARTIFACT_DIR={shlex.quote(art_dir)} "
            f"CAVERN_ARTIFACT_DIR={shlex.quote(art_dir)} "
            f"PLANNER_CONV_ID={shlex.quote(conv_id)} "
            f"PLANNER_LAST_PROMPT_HASH={shlex.quote(p_hash)} "
            f"PLANNER_WORKSPACE_ROOT={shlex.quote(str(repo))}"
        )
        updated = f"{prefix}\n{updated}"

    return updated if updated != cmd else None


def main() -> None:
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    tool = payload.get("toolCall") or {}
    args = tool.get("args") or {}
    cmd = (args.get("CommandLine") or "").strip()
    if not cmd:
        allow()
        return

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)
    segments = split_shell_segments(cmd)

    extra_msg = check_extra_requests_before_ship(segments, repo, profile)
    if extra_msg:
        deny(extra_msg)
        return

    plan_msg = check_plan_before_commit(segments, payload)
    if plan_msg:
        deny(plan_msg)
        return

    mem_msg = check_commit_memory_feedback(segments, payload, repo, profile)
    if mem_msg:
        deny(mem_msg)
        return

    cmd_msg = check_declarative_command_guards(segments, repo, profile)
    if cmd_msg:
        deny(cmd_msg)
        return

    allow(maybe_rewrite_command(cmd, repo, payload))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"decision": "allow"}')
