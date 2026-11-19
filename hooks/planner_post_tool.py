#!/usr/bin/env python3
"""Global PostToolUse hook: tracks code reads/edits, per-file edit counts, cmd fail streaks,
universal build/test status (`needs_build` + `last_build_failed`), and plugin test stamps.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent

TRACKED_TOOLS = frozenset({
    "read_file",
    "view_file",
    "edit_file",
    "write_file",
    "replace_file_content",
    "write_to_file",
    "run_command",
})

UNIVERSAL_BUILD_TEST_RE = re.compile(
    r"(?:^|\s|&&|\|\||;)(?:"
    r"(?:/root/\.dotnet/)?dotnet\s+(?:build|test)\b|"
    r"npm\s+(?:test|run\s+(?:test|build|typecheck|lint))\b|"
    r"npx\s+tsc\b|"
    r"pytest\b|"
    r"cargo\s+(?:test|check|build)\b|"
    r"go\s+(?:test|build)\b|"
    r"(?:\./)?gradlew?\s+(?:test|check)\b|"
    r"flutter\s+(?:test|analyze)\b|"
    r"python(?:\d+(?:\.\d+)?)?\s+[^\n]*verify_universal\.py[^\n]*--(?:force-)?build\b|"
    r"python(?:\d+(?:\.\d+)?)?\s+[^\n]*run_plugin_tests\.py\b"
    r")",
    re.IGNORECASE,
)


def _command_failed(payload: dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    if payload.get("exitCode") not in (None, 0):
        return True
    resp = payload.get("toolResponse") or payload.get("response")
    if isinstance(resp, dict) and resp.get("exitCode") not in (None, 0):
        return True
    return False


def _cmd_signature(cmd: str) -> str:
    clean = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", (cmd or "").strip())
    tokens = clean.split()
    return " ".join(tokens[:3])[:80]


def _matches_build_rule(cmd: str, profile: dict[str, Any]) -> bool:
    if UNIVERSAL_BUILD_TEST_RE.search(cmd):
        return True
    low_cmd = cmd.lower()
    for rule in profile.get("build_rules") or []:
        r_cmd = str(rule.get("command") or "").strip().lower()
        if not r_cmd:
            continue
        if r_cmd in low_cmd:
            return True
        # Match script basename (e.g. `check_android_cs.py`)
        for tok in r_cmd.split():
            if tok.endswith((".py", ".sh")) and Path(tok).name in low_cmd:
                return True
    return False


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        print("{}")
        return

    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    tool = payload.get("toolCall") or {}
    name = (tool.get("name") or "").lower()
    if name not in TRACKED_TOOLS:
        print("{}")
        return

    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    from planner_state import (  # noqa: E402
        get_paths,
        has_active_turn_plan,
        is_app_code_path,
        is_build_relevant_path,
        is_plugin_path,
        is_verifiable_path,
        load_json,
        load_project_profile,
        normalize_rel_path,
        resolve_repo_root,
        save_json,
    )

    args = tool.get("args") or {}
    has_error = _command_failed(payload)

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)
    _, _, _, hook_file = get_paths(payload)
    art_dir = hook_file.parents[1]
    hook_state = load_json(hook_file)
    now = time.time()
    hook_state["last_activity_ts"] = now
    changed = False

    if name in ("read_file", "view_file"):
        path = args.get("Path") or args.get("AbsolutePath") or args.get("TargetFile") or ""
        plugin_rel, in_plugin = is_plugin_path(path)
        is_plugin_code = in_plugin and plugin_rel.lower().endswith((".py", ".json", ".md", ".sh"))
        if path and (is_app_code_path(path, repo, profile) or is_plugin_code) and not has_active_turn_plan(payload, hook_state):
            key = f"plugin:{plugin_rel}" if in_plugin else normalize_rel_path(path, repo)[0]
            seen = list(hook_state.get("read_files_without_plan") or [])
            if key not in seen:
                seen.append(key)
                hook_state["read_files_without_plan"] = seen
                hook_state["code_reads_without_plan"] = len(seen)
                changed = True

    elif name in ("edit_file", "write_file", "replace_file_content", "write_to_file") and not has_error:
        path = args.get("Path") or args.get("TargetFile") or ""
        if path:
            try:
                p_resolved = Path(path).expanduser().resolve()
                if p_resolved.parent == art_dir.resolve() and p_resolved.suffix.lower() == ".md" and "plan" in p_resolved.name.lower():
                    if hook_state.get("turn_sig"):
                        hook_state["plan_turn_sig"] = hook_state["turn_sig"]
                        hook_state["code_reads_without_plan"] = 0
                        hook_state["read_files_without_plan"] = []
                        changed = True
            except Exception:
                pass
        plugin_rel, in_plugin = is_plugin_path(path)
        if path and in_plugin and plugin_rel.lower().endswith((".py", ".json", ".md")):
            hook_state["needs_plugin_tests"] = True
            hook_state["last_plugin_edit_ts"] = now
            p_edited = list(hook_state.get("plugin_files_edited") or [])
            if plugin_rel not in p_edited:
                p_edited.append(plugin_rel)
            hook_state["plugin_files_edited"] = p_edited
            counts = dict(hook_state.get("file_edit_counts") or {})
            ek = f"plugin:{plugin_rel}"
            counts[ek] = int(counts.get(ek) or 0) + 1
            hook_state["file_edit_counts"] = counts
            changed = True
        if path and is_verifiable_path(path, repo, profile):
            rel, _ = normalize_rel_path(path, repo)
            hook_state["needs_verify"] = True
            hook_state["last_edit_ts"] = now
            if is_app_code_path(path, repo, profile):
                edited = list(hook_state.get("code_files_edited") or [])
                if rel not in edited:
                    edited.append(rel)
                hook_state["code_files_edited"] = edited
                counts = dict(hook_state.get("file_edit_counts") or {})
                counts[rel] = int(counts.get(rel) or 0) + 1
                hook_state["file_edit_counts"] = counts
                if is_build_relevant_path(path, repo, profile):
                    hook_state["needs_build"] = True
            changed = True

    elif name == "run_command":
        cmd = args.get("CommandLine") or ""
        sig = _cmd_signature(cmd)
        if has_error:
            prev_sig = hook_state.get("last_failed_cmd_sig") or ""
            if sig and sig == prev_sig:
                hook_state["same_cmd_fails"] = int(hook_state.get("same_cmd_fails") or 0) + 1
            else:
                hook_state["last_failed_cmd_sig"] = sig
                hook_state["same_cmd_fails"] = 1
            if _matches_build_rule(cmd, profile):
                hook_state["last_build_failed"] = True
                hook_state["needs_build"] = True
            changed = True
        else:
            if hook_state.get("same_cmd_fails") or hook_state.get("last_failed_cmd_sig"):
                hook_state["same_cmd_fails"] = 0
                hook_state["last_failed_cmd_sig"] = ""
                changed = True

            if re.search(r"(?:^|\s|&&|;)git(?:\s+-C\s+\S+)?\s+commit\b", cmd):
                hook_state["post_commit_nudge"] = True
                changed = True

            ran_plugin_tests = (
                ("run_plugin_tests.py" in cmd)
                or ("verify_universal.py" in cmd and "--plugin-tests" in cmd)
            )
            if ran_plugin_tests and "||" not in cmd and ";" not in cmd:
                last_pe = float(hook_state.get("last_plugin_edit_ts") or 0.0)
                last_pt = float(hook_state.get("last_plugin_test_ts") or 0.0)
                if not (last_pt >= last_pe and last_pt > 0 and hook_state.get("needs_plugin_tests") is False):
                    hook_state["last_plugin_test_ts"] = max(now, last_pe)
                    hook_state["needs_plugin_tests"] = False
                    changed = True

            if ("verify_universal.py" in cmd or "verify_changes.py" in cmd) and "||" not in cmd and ";" not in cmd:
                last_v = float(hook_state.get("last_verify_ts") or 0.0)
                last_e = float(hook_state.get("last_edit_ts") or 0.0)
                if not (last_v >= last_e and last_v > 0):
                    hook_state["needs_verify"] = False
                    hook_state["last_verify_ts"] = now
                    if "--build" in cmd or "--force-build" in cmd:
                        hook_state["needs_build"] = False
                        hook_state["last_build_failed"] = False
                        hook_state["last_build_ts"] = now
                        hook_state["file_edit_counts"] = {}
                        hook_state["cb_triggered_key"] = ""
                    changed = True
            elif _matches_build_rule(cmd, profile) and "||" not in cmd and ";" not in cmd:
                hook_state["needs_build"] = False
                hook_state["last_build_failed"] = False
                hook_state["last_build_ts"] = now
                hook_state["file_edit_counts"] = {}
                hook_state["cb_triggered_key"] = ""
                changed = True

    if changed:
        save_json(hook_file, hook_state)

    print("{}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
