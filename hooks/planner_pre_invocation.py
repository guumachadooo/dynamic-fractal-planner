#!/usr/bin/env python3
"""Global PreInvocation hook: runs complexity triage once per turn (without initialNumSteps drift)
and injects runaway-read containment or periodic working-memory anchors.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from complexity_triage import analyze_prompt  # noqa: E402
from planner_state import (  # noqa: E402
    get_paths,
    has_active_turn_plan,
    load_json,
    prompt_hash,
    resolve_repo_root,
    save_json,
)


def main() -> None:
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    repo = resolve_repo_root(payload)
    _, task_md, plan_json, hook_file = get_paths(payload)
    hook_state = load_json(hook_file)

    last_input = payload.get("lastUserInput") or ""
    p_hash = prompt_hash(last_input)
    raw_inv = payload.get("invocationNum")
    inv_num = int(raw_inv) if raw_inv is not None else 1
    prev_inv = int(hook_state.get("last_invocation_num") or 0)
    prev_hash = hook_state.get("prompt_hash") or ""

    # Reliable new-turn detection without relying on mutable initialNumSteps
    is_new_turn = (p_hash != prev_hash) or (raw_inv is not None and inv_num <= prev_inv)

    if is_new_turn:
        turn_id = int(hook_state.get("turn_id") or 0) + 1
        prev_needs_pt = bool(hook_state.get("needs_plugin_tests"))
        prev_pe_ts = float(hook_state.get("last_plugin_edit_ts") or 0.0)
        prev_pt_ts = float(hook_state.get("last_plugin_test_ts") or 0.0)
        triage = analyze_prompt(last_input, repo)
        hook_state = {
            "turn_id": turn_id,
            "turn_sig": f"{turn_id}:{p_hash}",
            "prompt_hash": p_hash,
            "turn_started_ts": time.time(),
            "last_invocation_num": inv_num,
            "triage": triage,
            "triage_injected": False,
            "runaway_warned": False,
            "code_reads_without_plan": 0,
            "read_files_without_plan": [],
            "code_files_edited": [],
            "plugin_files_edited": [],
            "invocations_since_anchor": 0,
            "needs_verify": False,
            "needs_build": False,
            "needs_plugin_tests": prev_needs_pt,
            "last_plugin_edit_ts": prev_pe_ts,
            "last_plugin_test_ts": prev_pt_ts,
        }
    else:
        triage = hook_state.get("triage") or analyze_prompt(last_input, repo)
        hook_state["triage"] = triage
        hook_state["last_invocation_num"] = inv_num

    inject_lines: list[str] = []
    tier = int(triage.get("tier") or 0)
    plan_ready = has_active_turn_plan(payload, hook_state)
    plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
    skill_md = PLUGIN_ROOT / "skills" / "dynamic-planner" / "SKILL.md"

    # 1. Phase 0 Triage Injection (at most ONCE per turn, only when plan doesn't exist yet and Tier >= 2 or multi-item Tier 1)
    if (
        not plan_ready
        and not hook_state.get("triage_injected")
        and (tier >= 2 or (tier == 1 and int(triage.get("breadth_est") or 0) >= 2))
    ):
        hook_state["triage_injected"] = True
        mods = ", ".join(m["id"] for m in (triage.get("recommended_modules") or [])) or "MOD-IMPL, MOD-AUDIT"
        domains = ", ".join(triage.get("detected_domains") or []) or "general"
        inject_lines.append(
            f"[Dynamic Planner Triage — Tier {tier} ({triage.get('tier_name')})]\n"
            f"- Breadth~{triage.get('breadth_est')} | Depth={triage.get('depth_level')} | Domains={domains}\n"
            f"- Recommended Modules: {mods}\n"
            f"- Research Strategy: `{triage.get('research_strategy')}`\n"
            f"- PHASE 0 FIRST (before heavy `read_file` exploration): follow `{skill_md}` (`dynamic-planner`) "
            f"and lock ALL user requirements in `{task_md}` via `python3 {plan_cli} init ...`."
        )

    # 2. Runaway Exploration Guard
    elif (
        not plan_ready
        and tier >= 1
        and int(hook_state.get("code_reads_without_plan") or 0) >= 4
        and not hook_state.get("runaway_warned")
    ):
        hook_state["runaway_warned"] = True
        inject_lines.append(
            f"[PLANNER GUARD] You inspected {hook_state['code_reads_without_plan']} source files without locking a plan. "
            f"Stop broad reading and initialize `{task_md}` NOW via `python3 {plan_cli} init ...` "
            "so no requirements are lost."
        )

    # 3. Periodic Working-Memory Anchor (every 8 invocations on long runs)
    elif plan_ready and plan_json.is_file():
        since = int(hook_state.get("invocations_since_anchor") or 0) + 1
        state = load_json(plan_json)
        reqs = state.get("requirements") or []
        mods = state.get("modules") or []
        open_reqs = [r["id"] for r in reqs if r.get("status") != "verified"]
        open_mods = [m for m in mods if m.get("status") != "done"]
        if (open_reqs or open_mods) and state.get("lifecycle", "active") == "active" and since >= 8:
            hook_state["invocations_since_anchor"] = 0
            cur_mod = next((m for m in open_mods if m.get("status") == "in_progress"), open_mods[0] if open_mods else None)
            mod_txt = f"{cur_mod['id']} ({cur_mod['title']})" if cur_mod else "MOD-AUDIT"
            inject_lines.append(
                f"[Planner Memory Anchor] Active module: {mod_txt} | "
                f"Unverified REQs: {', '.join(open_reqs[:6]) or 'none'} | "
                f"Run `python3 {plan_cli} status` if unsure."
            )
        else:
            hook_state["invocations_since_anchor"] = since

    save_json(hook_file, hook_state)

    if not inject_lines:
        print("{}")
        return

    print(
        json.dumps(
            {"injectSteps": [{"systemMessage": {"systemMessage": "\n".join(inject_lines)}}]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
