#!/usr/bin/env python3
"""Universal Stop hook: prevents premature finish when plan, verifier, or EXTRA_REQUESTS are incomplete."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from planner_state import (  # noqa: E402
    get_paths,
    get_pending_extra_requests,
    has_active_turn_plan,
    load_json,
    load_project_profile,
    resolve_repo_root,
)


def allow_stop() -> None:
    print("{}")


def main() -> None:
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
                parts.append(f"unfinished module(s): {', '.join(open_mods[:5])}")
            if unverified:
                parts.append(f"unverified requirement(s): {', '.join(unverified[:6])}")
            reasons.append(
                "Dynamic plan is still incomplete (" + "; ".join(parts) + "). "
                "Complete them or call `plan_tool.py pause` if waiting on the user."
            )

    needs_plugin_tests = bool(hook_state.get("needs_plugin_tests"))
    if needs_plugin_tests:
        plugin_tests = PLUGIN_ROOT / "tests" / "run_plugin_tests.py"
        reasons.append(
            f"Plugin files were modified without passing regression tests. "
            f"Run `python3 {plugin_tests}` (or `verify_universal.py --plugin-tests`) first."
        )

    needs_verify = bool(hook_state.get("needs_verify"))
    needs_build = bool(hook_state.get("needs_build"))
    if needs_verify or needs_build:
        verify_cli = PLUGIN_ROOT / "scripts" / "verify_universal.py"
        cmd = f"python3 {verify_cli} --build" if needs_build else f"python3 {verify_cli}"
        reasons.append(f"Modified files have not been verified since the last edit. Run `{cmd}` first.")

    if hook_state.get("code_files_edited"):
        extra_rel, pending_extra = get_pending_extra_requests(repo, profile)
        if pending_extra:
            reasons.append(
                f"`{extra_rel}` has {len(pending_extra)} pending extra request(s). "
                "Read and resolve them before finishing."
            )

    if not reasons:
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


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
