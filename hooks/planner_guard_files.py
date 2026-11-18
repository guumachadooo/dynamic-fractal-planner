#!/usr/bin/env python3
"""Universal PreToolUse hook for file edits: evaluates declarative project file_guards[]
and enforces the universal Pre-Edit Plan Gate.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from complexity_triage import analyze_prompt  # noqa: E402
from planner_state import (  # noqa: E402
    eval_conditions,
    get_paths,
    has_active_turn_plan,
    is_app_code_path,
    is_plugin_path,
    load_json,
    load_project_profile,
    normalize_rel_path,
    resolve_plugin_root,
    resolve_repo_root,
)


def allow() -> None:
    print(json.dumps({"decision": "allow"}))


def reply(decision: str, reason: str) -> None:
    print(json.dumps({"decision": decision, "reason": reason}, ensure_ascii=False))


def check_declarative_file_guards(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
) -> tuple[str, str] | None:
    rel, _ = normalize_rel_path(raw_path, repo)
    norm_raw = raw_path.replace("\\", "/")

    for guard in profile.get("file_guards") or []:
        if not eval_conditions(guard.get("conditions"), repo):
            continue
        suffix = (guard.get("path_suffix") or "").replace("\\", "/")
        regex = guard.get("path_regex") or ""
        matched = (suffix and (norm_raw.endswith(suffix) or rel.endswith(suffix))) or (
            regex and re.search(regex, rel)
        )
        if not matched:
            continue

        must_pat = guard.get("must_contain_regex") or ""
        touch_pat = guard.get("check_when_touching_regex") or ""
        decision = guard.get("decision") or "force_ask"
        reason = guard.get("reason") or f"Safety guard triggered for `{rel}`."

        if tool_name in ("edit_file", "replace_file_content"):
            target = args.get("Target") or args.get("TargetContent") or ""
            replacement = args.get("Replacement") or args.get("ReplacementContent") or ""
            if (not touch_pat or re.search(touch_pat, target) or re.search(touch_pat, replacement)) and must_pat:
                if not re.search(must_pat, replacement):
                    return decision, reason
        elif tool_name in ("write_file", "write_to_file"):
            content = args.get("Content") or args.get("CodeContent") or ""
            if must_pat and not re.search(must_pat, content):
                return decision, reason
    return None


def main() -> None:
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    tool = payload.get("toolCall") or {}
    tool_name = (tool.get("name") or "").lower()
    args = tool.get("args") or {}
    raw_path = args.get("Path") or args.get("TargetFile") or ""

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)

    # 1. Declarative per-project file guards (e.g., targetSdkVersion="28")
    if raw_path:
        hit = check_declarative_file_guards(tool_name, args, raw_path, repo, profile)
        if hit:
            reply(hit[0], hit[1])
            return

    # 1b. Plugin self-protection guard for frozen historical fixtures
    plugin_rel, in_plugin = is_plugin_path(raw_path)
    if in_plugin and re.match(r"^tests/fixtures/profile_v1_.*\.json$", plugin_rel):
        if (resolve_plugin_root() / plugin_rel).exists():
            reply(
                "force_ask",
                f"PLUGIN SAFETY GUARD: Frozen v1 fixture `{plugin_rel}` is a backward-compatibility contract "
                "and should not be altered. Add a chained migrator in `scripts/schema_migrations.py` instead.",
            )
            return

    # 2. Universal Pre-Edit Plan Gate (covers both workspace app code and plugin edits outside workspace)
    is_plugin_protected = in_plugin and plugin_rel.lower().endswith((".py", ".json", ".md", ".sh"))
    if raw_path and (is_app_code_path(raw_path, repo, profile) or is_plugin_protected):
        _, task_md, _, hook_file = get_paths(payload)
        hook_state = load_json(hook_file)
        if not has_active_turn_plan(payload, hook_state):
            triage = hook_state.get("triage") or analyze_prompt(payload.get("lastUserInput") or "", repo)
            tier = int(triage.get("tier") or 0)
            edited = list(hook_state.get("code_files_edited") or []) + [
                f"plugin:{p}" for p in (hook_state.get("plugin_files_edited") or [])
            ]
            if in_plugin:
                edit_key = f"plugin:{plugin_rel}"
            else:
                edit_key, _ = normalize_rel_path(raw_path, repo)
            is_second_file = len(edited) >= 1 and edit_key not in edited
            reads_no_plan = int(hook_state.get("code_reads_without_plan") or 0)

            if tier >= 2 or is_second_file or reads_no_plan >= 4 or (in_plugin and plugin_rel.startswith(("hooks/", "scripts/"))):
                why = (
                    f"Tier {tier} ({triage.get('tier_name')})"
                    if tier >= 2
                    else (
                        f"modifying core plugin file `{plugin_rel}`"
                        if in_plugin
                        else (
                            "editing multiple source files"
                            if is_second_file
                            else f"already read {reads_no_plan} source files without a plan"
                        )
                    )
                )
                plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
                reply(
                    "deny",
                    f"PLAN GATE ({why}): Lock all user requirements and initialize your dynamic plan BEFORE editing code.\n"
                    f"Run `python3 {plan_cli} init --tier {max(2, tier) if (is_second_file or in_plugin) else max(1, tier)} "
                    f'--goal "..." --req "REQ-01: ..."` (or write `{task_md}`), then re-attempt this edit.',
                )
                return

    allow()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"decision": "allow"}')
