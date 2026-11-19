#!/usr/bin/env python3
"""Global PreInvocation hook: Neon-handoff-safe turn detection, lean/strict triage, ECO_MODE,
Circuit Breaker, Memory Anchor (every 14 invocations if open_reqs>=3), and long-chat handoff nudge.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

NEON_HANDOFF_WINDOW_SECS = 600.0
ANCHOR_INTERVAL = 14
LONG_CHAT_TURN_NUDGE = 25


def _ensure_paths() -> None:
    for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _compact_domain_checklists(domains: list[str], profile: dict[str, Any], max_items: int = 6) -> list[str]:
    from plan_tool import UNIVERSAL_CHECKLISTS  # noqa: E402

    custom_doms = profile.get("domains") if isinstance(profile.get("domains"), dict) else {}
    out: list[str] = []
    seen: set[str] = set()
    for dom in domains or ["general"]:
        spec = custom_doms.get(dom) if isinstance(custom_doms, dict) else None
        items = spec.get("checklist") if (isinstance(spec, dict) and spec.get("checklist")) else UNIVERSAL_CHECKLISTS.get(dom, [])
        for c in items or []:
            c_s = str(c).strip()
            if c_s and c_s not in seen:
                seen.add(c_s)
                out.append(f"{dom}|{c_s}")
                if len(out) >= max_items:
                    return out
    return out


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        print("{}")
        return

    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    _ensure_paths()

    from planner_state import (  # noqa: E402
        get_paths,
        get_planner_mode,
        has_active_turn_plan,
        is_eco_mode_prompt,
        load_json,
        load_project_profile,
        prompt_hash,
        record_telemetry,
        resolve_repo_root,
        save_json,
    )

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)
    mode = get_planner_mode(profile)

    _, task_md, plan_json, hook_file = get_paths(payload)
    hook_state = load_json(hook_file)

    last_input = payload.get("lastUserInput") or ""
    p_hash = prompt_hash(last_input)
    raw_inv = payload.get("invocationNum")
    inv_num = int(raw_inv) if raw_inv is not None else 1
    prev_inv = int(hook_state.get("last_invocation_num") or 0)
    prev_hash = hook_state.get("prompt_hash") or ""
    now = time.time()
    prev_act_ts = float(hook_state.get("last_activity_ts") or hook_state.get("turn_started_ts") or 0.0)

    # Fix false new-turn reset during Orchestrator (Neon) Plan->Execute handoff
    is_neon_same_turn = (
        bool(p_hash)
        and p_hash == prev_hash
        and raw_inv is not None
        and inv_num <= prev_inv
        and prev_act_ts > 0.0
        and (now - prev_act_ts) <= NEON_HANDOFF_WINDOW_SECS
    )
    is_new_turn = (p_hash != prev_hash) or (
        not is_neon_same_turn and raw_inv is not None and inv_num <= prev_inv
    )

    triage: dict[str, Any]
    if is_new_turn:
        from complexity_triage import analyze_prompt  # noqa: E402

        turn_id = int(hook_state.get("turn_id") or 0) + 1
        total_turns = int(hook_state.get("total_turns") or 0) + 1
        prev_needs_pt = bool(hook_state.get("needs_plugin_tests"))
        prev_pe_ts = float(hook_state.get("last_plugin_edit_ts") or 0.0)
        prev_pt_ts = float(hook_state.get("last_plugin_test_ts") or 0.0)
        prev_last_build_failed = bool(hook_state.get("last_build_failed"))
        prev_handoff_nudged = bool(hook_state.get("handoff_nudged"))
        prev_post_commit = bool(hook_state.get("post_commit_nudge"))
        eco = is_eco_mode_prompt(last_input)
        triage = analyze_prompt(last_input, repo)
        hook_state = {
            "turn_id": turn_id,
            "total_turns": total_turns,
            "turn_sig": f"{turn_id}:{p_hash}",
            "prompt_hash": p_hash,
            "turn_started_ts": now,
            "last_activity_ts": now,
            "last_invocation_num": inv_num,
            "triage": triage,
            "triage_injected": False,
            "runaway_warned": False,
            "eco_mode": eco,
            "eco_injected": False,
            "code_reads_without_plan": 0,
            "read_files_without_plan": [],
            "code_files_edited": [],
            "plugin_files_edited": [],
            "file_edit_counts": {},
            "same_cmd_fails": 0,
            "last_failed_cmd_sig": "",
            "cb_triggered_key": "",
            "turn_snapshot_taken": False,
            "turn_snapshot_ref": "",
            "turn_base_head": "",
            "turn_preexisting_dirty": [],
            "invocations_since_anchor": 0,
            "needs_verify": False,
            "needs_build": False,
            "last_build_failed": prev_last_build_failed,
            "needs_plugin_tests": prev_needs_pt,
            "last_plugin_edit_ts": prev_pe_ts,
            "last_plugin_test_ts": prev_pt_ts,
            "handoff_nudged": prev_handoff_nudged,
            "post_commit_nudge": prev_post_commit,
        }
        record_telemetry("turn", {"mode": mode, "tier": triage.get("tier")})
    else:
        triage = hook_state.get("triage") or {}
        if not triage:
            from complexity_triage import analyze_prompt  # noqa: E402

            triage = analyze_prompt(last_input, repo)
        hook_state["triage"] = triage
        hook_state["last_invocation_num"] = inv_num
        hook_state["last_activity_ts"] = now

    inject_lines: list[str] = []
    tier = int(triage.get("tier") or 0)
    eco_mode = bool(hook_state.get("eco_mode"))
    plan_ready = has_active_turn_plan(payload, hook_state)
    plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"

    if eco_mode and not hook_state.get("eco_injected"):
        hook_state["eco_injected"] = True
        inject_lines.append("[ECO_MODE|ON] Batch reads/greps in 1 turn && surgical edits only && terse <=8L reply && no optional docs.")

    # Circuit Breaker: >=2 consecutive same cmd fails OR >=4 edits to same file without passing build
    same_fails = int(hook_state.get("same_cmd_fails") or 0)
    file_counts = hook_state.get("file_edit_counts") or {}
    hot_files = [(f, int(c)) for f, c in file_counts.items() if int(c) >= 4]
    cb_key = ""
    cb_reason = ""
    if same_fails >= 2:
        cb_key = f"cmd:{hook_state.get('last_failed_cmd_sig')}:{same_fails}"
        cb_reason = f"same cmd failed {same_fails}x (`{hook_state.get('last_failed_cmd_sig')}`)"
    elif hot_files:
        top_f, top_c = max(hot_files, key=lambda kv: kv[1])
        cb_key = f"edit:{top_f}:{top_c}"
        cb_reason = f"`{top_f}` edited {top_c}x without passing build/test"
    if cb_key and hook_state.get("cb_triggered_key") != cb_key:
        hook_state["cb_triggered_key"] = cb_key
        record_telemetry("circuit_breaker", {"reason": cb_reason})
        inject_lines.append(
            f"[CIRCUIT BREAKER|{cb_reason}] STOP blind retries -> re-read exact error, rethink root cause, or ask user."
        )

    # Triage injection (once per turn when plan not created)
    if (
        not plan_ready
        and not hook_state.get("triage_injected")
        and (tier >= 2 or (tier == 1 and int(triage.get("breadth_est") or 0) >= 2))
    ):
        hook_state["triage_injected"] = True
        domains = list(triage.get("detected_domains") or [])
        mods = ",".join(m["id"] for m in (triage.get("recommended_modules") or [])) or "MOD-IMPL,MOD-AUDIT"
        dom_str = ",".join(domains) or "general"

        if mode == "lean":
            from complexity_triage import extract_auto_reqs  # noqa: E402

            auto_reqs = extract_auto_reqs(last_input)
            jits = _compact_domain_checklists(domains, profile, max_items=6)
            inject_lines.append(
                f"[Planner Triage|T{tier} {triage.get('tier_name')}|mode=lean|dom={dom_str}|mods={mods}|strat={triage.get('research_strategy')}]"
            )
            if auto_reqs:
                inject_lines.append("REQS|" + " | ".join(auto_reqs))
            if jits:
                inject_lines.append("JIT|" + " ; ".join(jits))
        else:
            skill_md = PLUGIN_ROOT / "skills" / "dynamic-planner" / "SKILL.md"
            inject_lines.append(
                f"[Planner Triage|T{tier} {triage.get('tier_name')}|mode=strict|dom={dom_str}|mods={mods}|strat={triage.get('research_strategy')}]\n"
                f"PHASE 0 FIRST -> follow `{skill_md}` && lock REQs via `python3 {plan_cli} init ...` at `{task_md}`."
            )

    # Runaway read guard (strict mode only)
    elif (
        mode == "strict"
        and not plan_ready
        and tier >= 1
        and int(hook_state.get("code_reads_without_plan") or 0) >= 4
        and not hook_state.get("runaway_warned")
    ):
        hook_state["runaway_warned"] = True
        inject_lines.append(
            f"[PLANNER GUARD|{hook_state['code_reads_without_plan']} reads w/o plan] Run `python3 {plan_cli} init ...` at `{task_md}` NOW."
        )

    # Working-Memory Anchor (strict/active plans, interval=14, open_reqs>=3, suppressed in ECO_MODE)
    elif not eco_mode and plan_ready and plan_json.is_file():
        since = int(hook_state.get("invocations_since_anchor") or 0) + 1
        state = load_json(plan_json)
        reqs = state.get("requirements") or []
        mods = state.get("modules") or []
        open_reqs = [r["id"] for r in reqs if r.get("status") != "verified"]
        open_mods = [m for m in mods if m.get("status") != "done"]
        if len(open_reqs) >= 3 and state.get("lifecycle", "active") == "active" and since >= ANCHOR_INTERVAL:
            hook_state["invocations_since_anchor"] = 0
            cur_mod = next((m for m in open_mods if m.get("status") == "in_progress"), open_mods[0] if open_mods else None)
            mod_txt = cur_mod["id"] if cur_mod else "MOD-AUDIT"
            inject_lines.append(
                f"[Memory Anchor|mod={mod_txt}|open_reqs={','.join(open_reqs[:6])}] `python3 {plan_cli} status`"
            )
        else:
            hook_state["invocations_since_anchor"] = since

    # Long-chat quota nudge (~25 turns or post-commit, suppressed in ECO_MODE)
    total_turns = int(hook_state.get("total_turns") or 1)
    if (
        not eco_mode
        and not hook_state.get("handoff_nudged")
        and (total_turns >= LONG_CHAT_TURN_NUDGE or hook_state.get("post_commit_nudge"))
    ):
        hook_state["handoff_nudged"] = True
        hook_state["post_commit_nudge"] = False
        inject_lines.append(
            f"[QUOTA TIP|turns={total_turns}] Run `python3 {plan_cli} handoff --summary \"...\" --next \"...\"` && start fresh chat to cut context cost."
        )

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
