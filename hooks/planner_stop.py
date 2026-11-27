#!/usr/bin/env python3
"""Universal Stop hook: runs fast in-process static auto-verify when `needs_verify` (<0.5s),
blocks stop on static errors / failed build / missing build / pending plugin tests / EXTRA_REQUESTS.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def allow_stop() -> None:
    print("{}")


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        allow_stop()
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}

        if payload.get("continuationActive"):
            allow_stop()
            return
        if payload.get("fullyIdle") is False:
            allow_stop()
            return
        if payload.get("terminationReason") not in ("NO_TOOL_CALL", "", None):
            allow_stop()
            return

        for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
            if p not in sys.path:
                sys.path.insert(0, p)

        from planner_state import (  # noqa: E402
            get_paths,
            get_pending_extra_requests,
            has_active_turn_plan,
            load_json,
            load_project_profile,
            resolve_repo_root,
            save_json,
            unique_edited_files,
        )

        repo = resolve_repo_root(payload)
        profile = load_project_profile(repo)
        _, _, plan_json, hook_file = get_paths(payload)
        hook_state = load_json(hook_file)
        plan_state = load_json(plan_json)

        if plan_state.get("lifecycle") in ("awaiting_user", "blocked"):
            allow_stop()
            return

        reasons: list[str] = []

        if plan_state and has_active_turn_plan(payload, hook_state):
            unverified = [r["id"] for r in (plan_state.get("requirements") or []) if r.get("status") != "verified"]
            open_mods = [m["id"] for m in (plan_state.get("modules") or []) if m.get("status") != "done"]
            if unverified or open_mods:
                parts = []
                if open_mods:
                    parts.append(f"open_mods={','.join(open_mods[:5])}")
                if unverified:
                    parts.append(f"unverified_reqs={','.join(unverified[:6])}")
                reasons.append(
                    f"Active plan incomplete ({' | '.join(parts)}) -> run `plan_tool.py finish --evidence \"...\"` or `pause`."
                )

        if bool(hook_state.get("needs_plugin_tests")):
            plugin_tests = PLUGIN_ROOT / "tests" / "run_plugin_tests.py"
            verify_cli = SCRIPTS_DIR / "verify_universal.py"
            reasons.append(
                f"Plugin edited without passing tests -> run `python3 {verify_cli} --plugin-tests --git-stat` (runs `{plugin_tests}`)."
            )

        # Silent in-process static auto-verify (<0.5s, never heavy build/plugin tests)
        if bool(hook_state.get("needs_verify")):
            from verify_universal import run_static_checks  # noqa: E402

            _, static_fails, _ = run_static_checks(repo, profile)
            if static_fails:
                reasons.append("Static auto-verify failed:\n  " + "\n  ".join(static_fails[:8]))
            else:
                hook_state["needs_verify"] = False
                hook_state["last_verify_ts"] = time.time()
                save_json(hook_file, hook_state)

        if bool(hook_state.get("last_build_failed")):
            reasons.append("Last build/test failed -> fix errors && re-run passing build/tests (or inform user).")
        elif bool(hook_state.get("needs_build")):
            verify_cli = SCRIPTS_DIR / "verify_universal.py"
            reasons.append(f"Compiled code modified without build -> run `python3 {verify_cli} --build` first.")

        if hook_state.get("code_files_edited"):
            extra_rel, pending_extra = get_pending_extra_requests(repo, profile)
            if pending_extra:
                reasons.append(f"`{extra_rel}` has {len(pending_extra)} pending item(s) -> resolve before finishing.")
            if not hook_state.get("eco_mode") and not hook_state.get("needs_verify"):
                from schema_migrations import resolve_adversarial_qa_config  # noqa: E402

                qa_cfg = resolve_adversarial_qa_config(profile)
                if qa_cfg.get("enabled") and qa_cfg.get("enforce_stop_contracts"):
                    try:
                        import qa_gate  # noqa: E402

                        qa_fails: list[str] = []
                        qa_warns: list[str] = []
                        qa_gate.check_adversarial_qa_contracts(repo, profile, qa_fails, qa_warns)
                        if qa_fails:
                            reasons.append("QA_GATE frozen contract failure:\n  " + "\n  ".join(qa_fails[:5]))
                    except Exception:
                        pass

        if not reasons:
            if hook_state.get("stop_block_streak") or hook_state.get("stop_block_sig"):
                hook_state["stop_block_sig"] = ""
                hook_state["stop_block_streak"] = 0
                save_json(hook_file, hook_state)
            triage = hook_state.get("triage") or {}
            tier = int(triage.get("tier") or 0)
            n_ed = len(unique_edited_files(hook_state))
            if triage and tier <= 1 and n_ed >= 5:
                from plugin_diagnostics import record_anomaly  # noqa: E402

                src = triage.get("source") or "prompt"
                conf = triage.get("confidence") or "?"
                record_anomaly(
                    "triage_mismatch",
                    "planner_stop.py",
                    f"T{tier}:edited_{n_ed}",
                    f"T{tier} under-triaged -> edited {n_ed} files | src={src} conf={conf}",
                    repo=repo,
                )
            allow_stop()
            return

        sig = "|".join(r.split("->")[0].strip()[:40] for r in reasons[:2])
        streak = (int(hook_state.get("stop_block_streak") or 0) + 1) if hook_state.get("stop_block_sig") == sig else 1
        max_streak = max(2, int(os.environ.get("PLANNER_STOP_MAX_STREAK") or 3))
        hook_state["stop_block_sig"] = sig
        hook_state["stop_block_streak"] = streak
        save_json(hook_file, hook_state)
        if streak >= 2:
            from plugin_diagnostics import record_anomaly  # noqa: E402

            detail = f"streak={streak} | {reasons[0][:140]}"
            if streak >= max_streak:
                detail = f"breaker_tripped=true | {detail}"
            record_anomaly("stop_gate_loop", "planner_stop.py", sig, detail, repo=repo)
        if streak >= max_streak:
            hook_state["stop_block_streak"] = 0
            hook_state["stop_block_sig"] = ""
            save_json(hook_file, hook_state)
            allow_stop()
            return

        print(
            json.dumps(
                {
                    "decision": "continue",
                    "reason": "VERIFICATION GATE BEFORE FINISHING:\n- " + "\n- ".join(reasons),
                },
                ensure_ascii=False,
            )
        )
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_stop.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_stop.py", exc)
        except Exception:
            pass
        print("{}")
