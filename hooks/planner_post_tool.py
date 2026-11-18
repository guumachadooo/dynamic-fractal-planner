#!/usr/bin/env python3
"""Global PostToolUse hook: tracks code reads, edits, and verifier execution stamps."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
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


def _command_failed(payload: dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    if payload.get("exitCode") not in (None, 0):
        return True
    resp = payload.get("toolResponse") or payload.get("response")
    if isinstance(resp, dict) and resp.get("exitCode") not in (None, 0):
        return True
    return False


def main() -> None:
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    tool = payload.get("toolCall") or {}
    name = (tool.get("name") or "").lower()
    args = tool.get("args") or {}
    has_error = _command_failed(payload)

    repo = resolve_repo_root(payload)
    profile = load_project_profile(repo)
    _, _, _, hook_file = get_paths(payload)
    art_dir = hook_file.parents[1]
    hook_state = load_json(hook_file)
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
            hook_state["last_plugin_edit_ts"] = time.time()
            p_edited = list(hook_state.get("plugin_files_edited") or [])
            if plugin_rel not in p_edited:
                p_edited.append(plugin_rel)
            hook_state["plugin_files_edited"] = p_edited
            changed = True
        if path and is_verifiable_path(path, repo, profile):
            rel, _ = normalize_rel_path(path, repo)
            hook_state["needs_verify"] = True
            hook_state["last_edit_ts"] = time.time()
            if is_app_code_path(path, repo, profile):
                edited = list(hook_state.get("code_files_edited") or [])
                if rel not in edited:
                    edited.append(rel)
                hook_state["code_files_edited"] = edited
                if is_build_relevant_path(path, repo, profile):
                    hook_state["needs_build"] = True
            changed = True

    elif name == "run_command" and not has_error:
        cmd = args.get("CommandLine") or ""
        ran_plugin_tests = (
            ("run_plugin_tests.py" in cmd)
            or ("verify_universal.py" in cmd and "--plugin-tests" in cmd)
        )
        if ran_plugin_tests and "||" not in cmd and ";" not in cmd:
            now = time.time()
            last_pe = float(hook_state.get("last_plugin_edit_ts") or 0.0)
            last_pt = float(hook_state.get("last_plugin_test_ts") or 0.0)
            if not (last_pt >= last_pe and last_pt > 0 and hook_state.get("needs_plugin_tests") is False):
                hook_state["last_plugin_test_ts"] = max(now, last_pe)
                hook_state["needs_plugin_tests"] = False
                changed = True

        if "verify_universal.py" in cmd or "verify_changes.py" in cmd:
            last_v = float(hook_state.get("last_verify_ts") or 0.0)
            last_e = float(hook_state.get("last_edit_ts") or 0.0)
            if not (last_v >= last_e and last_v > 0) and "||" not in cmd and ";" not in cmd:
                hook_state["needs_verify"] = False
                hook_state["last_verify_ts"] = time.time()
                if "--build" in cmd or "--force-build" in cmd:
                    hook_state["needs_build"] = False
                    hook_state["last_build_ts"] = time.time()
                changed = True
        elif "dotnet build" in cmd:
            hook_state["needs_build"] = False
            hook_state["last_build_ts"] = time.time()
            changed = True

    if changed:
        save_json(hook_file, hook_state)

    print("{}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("{}")
