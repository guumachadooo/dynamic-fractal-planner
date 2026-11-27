#!/usr/bin/env python3
"""Universal PreToolUse hook (`schedule|manage_task`): blocks polling timers and
repeated task status/list polling loops in favor of reactive wakeups.
"""

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

POLLING_PROMPT_RE = re.compile(
    r"\b(?:task|status|build|ci|test|tests|wait|waiting|check|checking|watch|watching|poll|polling|gh\s+run|workflow)\b",
    re.IGNORECASE,
)
TASK_POLL_COOLDOWN_SEC = 90.0
TASK_POLL_PRUNE_SEC = 3600.0


def _ensure_paths() -> None:
    for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


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
            _ensure_paths()
            from planner_state import get_paths, load_json, resolve_repo_root  # noqa: E402
            from plugin_diagnostics import record_guard_deny_if_burst  # noqa: E402

            _, _, _, hook_file = get_paths(payload)
            hook_state = load_json(hook_file)
            sig = rule_sig or reason.split("|", 1)[0].strip()[:40]
            r_path = repo if repo is not None else resolve_repo_root(payload)
            record_guard_deny_if_burst("planner_guard_polling.py", sig, reason, hook_file, hook_state, repo=r_path)
        except Exception:
            pass


def _check_schedule(args: dict[str, Any]) -> tuple[str, str] | None:
    is_daemon = bool(args.get("IsDaemon") if "IsDaemon" in args else args.get("isDaemon"))
    cron = str(args.get("CronExpression") or args.get("cronExpression") or "").strip()
    if is_daemon or cron:
        return None

    dur_raw = args.get("DurationSeconds") if "DurationSeconds" in args else args.get("durationSeconds")
    if dur_raw is None or dur_raw == "":
        return None
    try:
        dur = float(dur_raw)
    except (TypeError, ValueError):
        return None

    prompt = str(args.get("Prompt") or args.get("prompt") or "")
    timer_raw = args.get("TimerCondition") if "TimerCondition" in args else args.get("timerCondition")
    timer_cond = "" if timer_raw is None else str(timer_raw).strip().lower()

    has_poll_prompt = bool(POLLING_PROMPT_RE.search(prompt))
    is_short_uncond = dur <= 30.0 and timer_cond in ("never", "")
    if has_poll_prompt or is_short_uncond:
        return (
            "DENY|anti_poll: timer polling via `schedule` wastes turns. "
            "Use reactive wakeup (`NotificationTimeoutSeconds` on `run_command` + end turn w/ empty reply) "
            "or blocking `gh run watch --exit-status --compact`.",
            "DENY:anti_poll:schedule",
        )
    return None


def _check_manage_task(args: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str] | None:
    action = str(args.get("Action") or args.get("action") or "").strip().lower()
    if action not in ("status", "list"):
        return None

    _ensure_paths()
    from planner_state import get_paths, load_json, save_json  # noqa: E402

    _, _, _, hook_file = get_paths(payload)
    hook_state = load_json(hook_file)
    raw_polls = hook_state.get("task_status_polls")
    now = time.time()
    polls: dict[str, float] = (
        {
            str(k): float(v)
            for k, v in raw_polls.items()
            if isinstance(v, (int, float)) and (now - float(v)) < TASK_POLL_PRUNE_SEC
        }
        if isinstance(raw_polls, dict)
        else {}
    )

    task_id = str(args.get("TaskId") or args.get("taskId") or "").strip()
    key = task_id if (action == "status" and task_id) else "__list__"
    prev = polls.get(key)
    if prev is not None and (now - prev) < TASK_POLL_COOLDOWN_SEC:
        elapsed = int(max(0.0, now - prev))
        return (
            f"DENY|anti_poll: repeated `manage_task({action})` for `{key}` within "
            f"{int(TASK_POLL_COOLDOWN_SEC)}s ({elapsed}s ago). "
            "Wait for reactive wakeup (end turn w/ empty reply) instead of polling.",
            f"DENY:anti_poll:manage_task:{key[:24]}",
        )

    polls[key] = now
    hook_state["task_status_polls"] = polls
    save_json(hook_file, hook_state)
    return None


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        _allow()
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        tool = payload.get("toolCall") or {}
        name = str(tool.get("name") or payload.get("toolName") or "").strip()
        args = tool.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in ("schedule", "manage_task"):
            _allow()
            return

        _ensure_paths()
        from planner_state import resolve_repo_root  # noqa: E402

        repo = resolve_repo_root(payload)
        hit: tuple[str, str] | None = None
        if name == "schedule":
            hit = _check_schedule(args)
        elif name == "manage_task":
            hit = _check_manage_task(args, payload)

        if hit:
            _deny(hit[0], payload=payload, repo=repo, rule_sig=hit[1])
            return

        _allow()
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            _ensure_paths()
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_guard_polling.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _ensure_paths()
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_guard_polling.py", exc)
        except Exception:
            pass
        _allow()
