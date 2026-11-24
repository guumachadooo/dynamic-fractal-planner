#!/usr/bin/env python3
"""Universal PreToolUse hook (`invoke_subagent|swarm|run_workflow`): enforces thin-coordinator
complexity-tiered model routing and Google/Gemini-only quota protection.
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

COMPLEX_MARKERS = (
    "[l3]",
    "architect",
    "dsp",
    "physics",
    "math",
    "security",
    "concurrency",
    "race",
    "deadlock",
    "sota",
    "root cause",
    "escalat",
    "forensic",
    "deep audit",
)
INHERIT_MARKERS = ("[l3]", "[inherit]")
WORKFLOW_MODEL_RE = re.compile(r"""model\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


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
            record_guard_deny_if_burst("planner_guard_delegation.py", sig, reason, hook_file, hook_state, repo=r_path)
        except Exception:
            pass


def _find_blocked_vendor(model: str, blocked_patterns: list[str]) -> str:
    ml = (model or "").strip().lower()
    if not ml:
        return ""
    if ml in ("o1", "o3", "o4"):
        return ml
    for pat in blocked_patterns:
        p = str(pat).strip().lower()
        if p and p in ml:
            return p
    return ""


def _check_invoke_subagent(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str] | None:
    type_name = str(args.get("TypeName") or args.get("typeName") or "").strip()
    exempt = {str(x).strip().lower() for x in (cfg.get("exempt_subagent_types") or []) if str(x).strip()}
    if type_name.lower() in exempt:
        return None

    model = str(args.get("Model") or args.get("model") or "").strip()
    blocked_patterns = list(cfg.get("blocked_model_patterns") or [])
    if cfg.get("enforce_google_only", True) and model:
        hit = _find_blocked_vendor(model, blocked_patterns)
        if hit:
            return (
                f"DENY|google_only: blocked non-Google model `{model}` (matched `{hit}`). Use Google/Gemini (`flash_lite`|`flash`|`pro`).",
                f"DENY:google_only:{hit}",
            )

    if cfg.get("require_explicit_model", True) and not model:
        return (
            "DENY|explicit_model: omit Model -> defaults to costly `inherit`. Pass explicit `flash_lite` (L0), `flash` (L1-2), or `pro` (L3).",
            "DENY:explicit_model",
        )

    ml = model.lower()
    prompt_role = f"{args.get('Role') or args.get('role') or ''} {args.get('Prompt') or args.get('prompt') or ''}".lower()

    if cfg.get("prefer_cheap_models", True) and ml == "inherit":
        if not any(m in prompt_role for m in INHERIT_MARKERS):
            return (
                "DENY|prefer_cheap_models: `inherit` unannotated -> pick explicit `flash_lite` (L0) / `flash` (L1-2) / `pro` (L3), or tag `[L3]`/`[inherit]`.",
                "DENY:inherit_model",
            )

    if cfg.get("prefer_cheap_models", True) and ml == "pro" and type_name.lower() == "research":
        if not any(m in prompt_role for m in COMPLEX_MARKERS):
            return (
                "DENY|prefer_cheap_models: trivial `research` on `pro` -> use `flash_lite`/`flash` or tag `[L3]` / complex domain.",
                "DENY:research_pro",
            )

    return None


def _check_swarm(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str] | None:
    action = str(args.get("action") or args.get("Action") or "").strip().lower()
    if action not in ("add", "model"):
        return None

    model = str(args.get("model") or args.get("Model") or "").strip()
    blocked_patterns = list(cfg.get("blocked_model_patterns") or [])
    if cfg.get("enforce_google_only", True) and model:
        hit = _find_blocked_vendor(model, blocked_patterns)
        if hit:
            return (
                f"DENY|google_only: blocked non-Google swarm model `{model}` (matched `{hit}`). Use Google/Gemini (`flash_lite`|`flash`|`pro`|`gemini-*`).",
                f"DENY:swarm_google_only:{hit}",
            )

    if action == "add" and cfg.get("require_explicit_model", True) and not model:
        return (
            "DENY|explicit_model: `swarm add` missing `model`. Pass explicit cheap Google/Gemini model (`flash_lite`, `flash`, or `gemini-*-flash`).",
            "DENY:swarm_explicit_model",
        )

    return None


def _check_run_workflow(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str] | None:
    if not cfg.get("enforce_google_only", True):
        return None
    script_src = str(args.get("Script") or args.get("script") or "")
    if not script_src:
        return None
    blocked_patterns = list(cfg.get("blocked_model_patterns") or [])
    for m in WORKFLOW_MODEL_RE.findall(script_src):
        hit = _find_blocked_vendor(m, blocked_patterns)
        if hit:
            return (
                f"DENY|google_only: workflow script uses non-Google model `{m}` (matched `{hit}`). Use Google/Gemini models only.",
                f"DENY:workflow_google_only:{hit}",
            )
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
        if name not in ("invoke_subagent", "swarm", "run_workflow"):
            _allow()
            return

        _ensure_paths()
        from planner_state import load_project_profile, resolve_repo_root  # noqa: E402
        from schema_migrations import resolve_delegation_config  # noqa: E402

        repo = resolve_repo_root(payload)
        profile = load_project_profile(repo)
        cfg = resolve_delegation_config(profile)
        if not cfg.get("enabled", True):
            _allow()
            return

        hit: tuple[str, str] | None = None
        if name == "invoke_subagent":
            hit = _check_invoke_subagent(args, cfg)
        elif name == "swarm":
            hit = _check_swarm(args, cfg)
        elif name == "run_workflow":
            hit = _check_run_workflow(args, cfg)

        if hit:
            _deny(hit[0], payload=payload, repo=repo, rule_sig=hit[1])
            return

        _allow()
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            _ensure_paths()
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_guard_delegation.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _ensure_paths()
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_guard_delegation.py", exc)
        except Exception:
            pass
        _allow()
