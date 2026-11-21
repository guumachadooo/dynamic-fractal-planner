#!/usr/bin/env python3
"""Global PreInvocation hook: Neon-handoff-safe turn detection, lean/strict triage, ECO_MODE,
Circuit Breaker, Memory Anchor (every 14 invocations if open_reqs>=3), and long-chat handoff nudge.
"""

from __future__ import annotations

import json
import os
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


def _compact_domain_lenses(
    domains: list[str],
    profile: dict[str, Any],
    max_lenses: int = 2,
    max_chars: int = 220,
) -> str:
    custom_doms = profile.get("domains") if isinstance(profile.get("domains"), dict) else {}
    parts: list[str] = []
    for dom in domains or []:
        spec = custom_doms.get(dom)
        if not isinstance(spec, dict):
            continue
        lens = str(spec.get("lens") or "").strip().replace("\n", " ")
        if lens:
            parts.append(f"{dom}:{lens}")
            if len(parts) >= max_lenses:
                break
    if not parts:
        return ""
    line = "LENS|" + " ; ".join(parts)
    return (line[: max_chars - 3] + "...") if len(line) > max_chars else line


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        print("{}")
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        _run_pre_invocation_inner(t0)
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            _ensure_paths()
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_pre_invocation.py", t0)


def _run_pre_invocation_inner(t0: float) -> None:
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    _ensure_paths()

    from planner_state import (  # noqa: E402
        auto_upgrade_workspace,
        build_carried_task_context,
        detect_doc_mcps,
        format_doc_mcp_hint,
        get_paths,
        has_active_turn_plan,
        is_eco_mode_prompt,
        is_real_project_root,
        load_json,
        load_project_profile,
        normalize_model_key,
        prepare_unseen_classification,
        prompt_hash,
        record_telemetry,
        resolve_extra_requests_rel,
        resolve_planner_mode_info,
        resolve_repo_root,
        save_json,
        unique_edited_files,
    )

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)

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
        prev_recon = hook_state.get("recon") if isinstance(hook_state.get("recon"), dict) else None
        carried_ctx = build_carried_task_context(hook_state, load_json(plan_json), now)
        eco = is_eco_mode_prompt(last_input)
        triage = analyze_prompt(last_input, repo, context=carried_ctx, now=now)
        hook_state = {
            "turn_id": turn_id,
            "total_turns": total_turns,
            "turn_sig": f"{turn_id}:{p_hash}",
            "prompt_hash": p_hash,
            "turn_started_ts": now,
            "last_activity_ts": now,
            "last_invocation_num": inv_num,
            "triage": triage,
            "task_context": carried_ctx,
            "triage_injected": False,
            "doc_hint_injected": False,
            "blast_radius_warned": False,
            "blast_radius_notified": False,
            "blast_radius_approved": False,
            "runaway_warned": False,
            "eco_mode": eco,
            "eco_injected": False,
            "code_reads_without_plan": 0,
            "read_files_without_plan": [],
            "code_files_edited": [],
            "plugin_files_edited": [],
            "file_edit_counts": {},
            "file_edit_last_inv": {},
            "guard_denies": {},
            "stop_block_sig": "",
            "stop_block_streak": 0,
            "cb_turn_count": 0,
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
            "docs_migrate_nudged": False,
            "model_classify_prompted": "",
            "native_plan_turn_sig": "",
            "native_plan_active_ts": 0.0,
            "recon": prev_recon,
            "recon_injected": False,
            "lens_injected": False,
            "btw_injected": False,
        }
    else:
        triage = hook_state.get("triage") or {}
        if not triage:
            from complexity_triage import analyze_prompt  # noqa: E402

            triage = analyze_prompt(last_input, repo, context=hook_state.get("task_context"), now=now)
        hook_state["triage"] = triage
        hook_state["last_invocation_num"] = inv_num
        hook_state["last_activity_ts"] = now

    mode, mode_src, model_name = resolve_planner_mode_info(profile, payload, hook_state=hook_state)
    if mode_src == "auto:unseen" and model_name:
        mode_tag = f"{mode}(auto:{model_name}|unseen)"
    elif mode_src.startswith("auto:") and model_name and model_name.lower() != "auto":
        mode_tag = f"{mode}(auto:{model_name})"
    else:
        mode_tag = mode
    if is_new_turn:
        record_telemetry("turn", {"mode": mode, "tier": triage.get("tier")})

    hook_state["model_name"] = model_name
    hook_state["resolved_mode"] = mode

    inject_lines: list[str] = []
    tier = int(triage.get("tier") or 0)
    eco_mode = bool(hook_state.get("eco_mode"))
    plan_ready = has_active_turn_plan(payload, hook_state)
    plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
    doc_mcps = detect_doc_mcps(profile)
    doc_targets = list(triage.get("doc_lookup_targets") or [])
    doc_hint_line = format_doc_mcp_hint(doc_targets, doc_mcps)
    bloat_limit = int(os.environ.get("PLANNER_INJECTION_BLOAT_CHARS") or 1200)

    def _append_within_budget(line: str) -> bool:
        if not line:
            return False
        cur_chars = sum(len(x) + 1 for x in inject_lines)
        if cur_chars + len(line) > bloat_limit:
            return False
        inject_lines.append(line)
        return True

    if eco_mode and not hook_state.get("eco_injected"):
        hook_state["eco_injected"] = True
        inject_lines.append("[ECO_MODE|ON] Batch reads/greps in 1 turn && surgical edits only && terse <=8L reply && no optional docs.")

    rec_state = hook_state.get("recon")
    if isinstance(rec_state, dict) and rec_state.get("active") and not hook_state.get("recon_injected"):
        r_scope = str(rec_state.get("scope") or "full")
        if _append_within_budget(
            f"[RECON|READ-ONLY|scope={r_scope}] Code edits blocked -> log [BUG] | [UNWIRED] | [STRUCTURAL] | [UPGRADE] @ path:line"
        ):
            hook_state["recon_injected"] = True

    if triage.get("btw_queue_intent") and not hook_state.get("btw_injected"):
        extra_rel = resolve_extra_requests_rel(profile)
        btw_line = (
            f"[BTW_QUEUE] Delegation/queue intent -> auto-append `- REQ|<scope> -> <goal>` "
            f"(symbolic EN) to `{extra_rel}` (create if absent; !ask/!wait for explicit 'add to requests') "
            f"&& reply ultra-terse (<=2L)."
        )
        if _append_within_budget(btw_line):
            hook_state["btw_injected"] = True
    is_btw_short = bool(triage.get("btw_queue_intent")) and len(last_input.strip()) <= 260

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
        cb_turn_count = int(hook_state.get("cb_turn_count") or 0) + 1
        hook_state["cb_turn_count"] = cb_turn_count
        record_telemetry("circuit_breaker", {"reason": cb_reason})
        if (cb_key.startswith("edit:") and same_fails == 0 and not hook_state.get("last_build_failed")) or cb_turn_count >= 2:
            from plugin_diagnostics import record_anomaly  # noqa: E402

            record_anomaly(
                "circuit_breaker_suspect",
                "planner_pre_invocation.py",
                cb_key,
                f"{cb_reason} | same_fails={same_fails} build_fail={bool(hook_state.get('last_build_failed'))} turn_cb={cb_turn_count}",
                repo=repo,
            )
        cb_doc_suffix = ""
        if doc_mcps.get("gdk") and doc_mcps.get("context7"):
            cb_doc_suffix = " | verify API/flags via GDK/Context7"
        elif doc_mcps.get("gdk"):
            cb_doc_suffix = " | verify API/flags via GDK"
        elif doc_mcps.get("context7"):
            cb_doc_suffix = " | verify API/flags via Context7"
        inject_lines.append(
            f"[CIRCUIT BREAKER|{cb_reason}] STOP blind retries -> re-read exact error, rethink root cause, or ask user{cb_doc_suffix}."
        )

    # Triage injection (once per turn when plan not created; skipped on short /btw prompts)
    if (
        not is_btw_short
        and not plan_ready
        and not hook_state.get("triage_injected")
        and (tier >= 2 or (tier == 1 and int(triage.get("breadth_est") or 0) >= 2))
    ):
        hook_state["triage_injected"] = True
        domains = list(triage.get("detected_domains") or [])
        mods = ",".join(m["id"] for m in (triage.get("recommended_modules") or [])) or "MOD-IMPL,MOD-AUDIT"
        dom_str = ",".join(domains) or "general"

        if mode == "lean":
            from complexity_triage import extract_auto_reqs  # noqa: E402

            auto_reqs = [] if mode_src == "auto:native_plan_turn" else extract_auto_reqs(last_input, max_reqs=6, structured_only=True)
            jits = _compact_domain_checklists(domains, profile, max_items=4)
            triage_src = str(triage.get("source") or "prompt")
            src_tag = "(ctx)" if triage_src.startswith("context_") else ("(plan)" if triage_src == "exit_plan_mode" else "")
            inject_lines.append(
                f"[Triage|T{tier}{src_tag}|mode={mode_tag}|dom={dom_str}]"
            )
            if auto_reqs:
                inject_lines.append("REQS|" + " | ".join(auto_reqs))
            if jits:
                inject_lines.append("JIT|" + " ; ".join(jits))
        else:
            skill_md = PLUGIN_ROOT / "skills" / "dynamic-planner" / "SKILL.md"
            inject_lines.append(
                f"[Planner Triage|T{tier} {triage.get('tier_name')}|mode={mode_tag}|dom={dom_str}|mods={mods}|strat={triage.get('research_strategy')}]\n"
                f"PHASE 0 FIRST -> follow `{skill_md}` && lock REQs via `python3 {plan_cli} init ...` at `{task_md}`."
            )
        if doc_hint_line and not hook_state.get("doc_hint_injected"):
            hook_state["doc_hint_injected"] = True
            inject_lines.append(doc_hint_line)
        if not eco_mode:
            if tier == 2:
                _append_within_budget("DISC|AUDIT_EXISTING: grep/inspect partial impls pre-create (!duplicate)")
            elif tier >= 3:
                _append_within_budget(
                    "DISC|AUDIT_EXISTING: grep/inspect partial impls pre-create (!duplicate) ; "
                    "CURATE_OSS: evaluate battle-tested OSS libs/standards pre-custom complex algos"
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

    # Non-blocking Blast Radius heads-up (once per turn, suppressed in ECO_MODE)
    if not eco_mode and hook_state.get("blast_radius_warned") and not hook_state.get("blast_radius_notified"):
        hook_state["blast_radius_notified"] = True
        total_ed = len(unique_edited_files(hook_state))
        inject_lines.append(
            f"[BLAST RADIUS|T{tier} edited {total_ed} files] Scope expanded beyond simple fix -> keep edits tightly scoped to the request."
        )
        from plugin_diagnostics import record_anomaly  # noqa: E402

        record_anomaly(
            "blast_radius_suspect",
            "planner_pre_invocation.py",
            f"T{tier}:warn",
            f"T{tier} edited={total_ed} (code={len(hook_state.get('code_files_edited') or [])},plugin={len(hook_state.get('plugin_files_edited') or [])})",
            repo=repo,
        )

    # Domain lenses (1-line active-domain critique, suppressed in ECO_MODE & within bloat budget)
    if not is_btw_short and not eco_mode and not plan_ready and not hook_state.get("lens_injected") and tier >= 1:
        lens_line = _compact_domain_lenses(list(triage.get("detected_domains") or []), profile)
        if lens_line and _append_within_budget(lens_line):
            hook_state["lens_injected"] = True

    # Direct doc-lookup routing for Tier 0/1 technical queries (once per turn, suppressed in ECO_MODE)
    if not is_btw_short and not eco_mode and doc_hint_line and not hook_state.get("doc_hint_injected"):
        hook_state["doc_hint_injected"] = True
        inject_lines.append(doc_hint_line)

    # Event-driven 1-time classification for unseen models (never on known/cached/seeded models)
    if mode_src == "auto:unseen" and model_name and not eco_mode:
        mkey = normalize_model_key(model_name)
        if mkey and hook_state.get("model_classify_prompted") != mkey:
            cls_prompt = prepare_unseen_classification(model_name, plan_cli=plan_cli, profile=profile)
            if cls_prompt:
                hook_state["model_classify_prompted"] = mkey
                inject_lines.append(cls_prompt)
                record_telemetry("model_mode_classify_prompt", {"model": mkey})

    # Event-driven auto-migration nudge for outdated project docs (once per turn, Neon-handoff-safe, suppressed in ECO_MODE)
    if (
        not eco_mode
        and not hook_state.get("docs_migrate_nudged")
        and is_real_project_root(repo)
        and (repo / ".agents" / "planner_profile.json").is_file()
    ):
        up_info = auto_upgrade_workspace(repo)
        pending_docs = up_info.get("docs_needing_ai_conversion") or []
        if pending_docs:
            hook_state["docs_migrate_nudged"] = True
            cur_dv = int(up_info.get("current_docs_format") or 0)
            tgt_dv = int(up_info.get("target_docs_format") or 1)
            record_telemetry("docs_auto_migrate_prompt", {"files": len(pending_docs), "from": cur_dv, "to": tgt_dv})
            inject_lines.append(
                f"[DOCS_AUTO_MIGRATE|v{cur_dv}->v{tgt_dv}|files={','.join(pending_docs)}]\n"
                f"ACT|Legacy project docs detected -> BEFORE/WITH this task, read (<=250L slices if >150L) && losslessly convert listed files to symbolic telegraphic EN "
                f"(`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:line`/`symbol()`/dates/invariants, include `STYLE|lossless symbolic telegraphic EN ONLY` header && keep exact `## Pending Extra Requests`/`## Completed Requests` headings), "
                f"then stamp `python3 {plan_cli} docs-migrated`."
            )

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
            f"[QUOTA TIP|turns={total_turns}] Run `python3 {plan_cli} handoff --summary \"...\" --next \"...\"` (symbolic telegraphic EN) && start fresh chat to cut context cost."
        )

    save_json(hook_file, hook_state)

    if not inject_lines:
        print("{}")
        return

    msg_text = "\n".join(inject_lines)
    if len(msg_text) > bloat_limit:
        from plugin_diagnostics import record_anomaly  # noqa: E402

        largest_prefix = max(inject_lines, key=len).split("|", 1)[0][:32]
        record_anomaly(
            "injection_bloat",
            "planner_pre_invocation.py",
            "pre_invocation:bloat",
            f"chars={len(msg_text)} lines={len(inject_lines)} top={largest_prefix}",
            repo=repo,
        )

    print(
        json.dumps(
            {"injectSteps": [{"systemMessage": {"systemMessage": msg_text}}]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _ensure_paths()
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_pre_invocation.py", exc)
        except Exception:
            pass
        print("{}")
