#!/usr/bin/env python3
"""Universal PreToolUse hook for file edits: declarative file_guards, secret/conflict shield,
shift-left fail diff_rules, destructive write_file shield, lean blast-radius / strict plan gate,
and invisible Tier>=2 git snapshot before 1st code edit.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def _ensure_paths() -> None:
    for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def allow() -> None:
    print(json.dumps({"decision": "allow"}))


def reply(
    decision: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    repo: Path | None = None,
) -> None:
    print(json.dumps({"decision": decision, "reason": reason}, ensure_ascii=False))
    if decision in ("deny", "force_ask") and payload is not None:
        try:
            _ensure_paths()
            from planner_state import get_paths, load_json, record_rule_telemetry  # noqa: E402
            from plugin_diagnostics import record_guard_deny_if_burst  # noqa: E402

            _, _, _, hook_file = get_paths(payload)
            hook_state = load_json(hook_file)
            rule_sig = reason.split("|", 1)[0].split("(", 1)[0].strip()[:40]
            record_guard_deny_if_burst("planner_guard_files.py", rule_sig, reason, hook_file, hook_state, repo=repo)
            record_rule_telemetry(rule_sig or "file_guard", hits=1, fp_overrides=0, target_exists=True)
        except Exception:
            pass


def _strip_line_comment(line: str) -> str:
    in_str = False
    esc = False
    for i, ch in enumerate(line):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str and line[i : i + 2] == "//":
            return line[:i]
    return line


def _strip_strings(line: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', line)


def check_declarative_file_guards(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
) -> tuple[str, str] | None:
    from planner_state import eval_conditions, normalize_rel_path  # noqa: E402

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


def check_recon_block(
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
    payload: dict[str, Any],
    in_plugin: bool,
    plugin_rel: str,
) -> str | None:
    """Block code edits while MOD-RECON-SWEEP is active, allowing docs/plans/artifacts."""
    from planner_state import get_paths, is_app_code_path, load_json, normalize_rel_path, record_telemetry  # noqa: E402

    art_dir, _, _, hook_file = get_paths(payload)
    hook_state = load_json(hook_file)
    rec = hook_state.get("recon")
    if not (isinstance(rec, dict) and rec.get("active")):
        return None

    try:
        p_res = Path(raw_path).expanduser().resolve()
        if p_res == art_dir.resolve() or art_dir.resolve() in p_res.parents:
            return None
    except Exception:
        pass

    rel = plugin_rel if in_plugin else normalize_rel_path(raw_path, repo)[0]
    if in_plugin:
        if plugin_rel.lower().endswith(".md"):
            return None
    elif rel.startswith(".agents/docs/") or Path(rel).suffix.lower() == ".md":
        return None

    if is_app_code_path(raw_path, repo, profile) or (in_plugin and not plugin_rel.lower().endswith(".md")):
        record_telemetry("recon_blocked_code_edit", {"file": rel})
        scope = str(rec.get("scope") or "").strip()
        scope_tag = f"|scope={scope}" if scope else ""
        plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
        return (
            f"RECON MODE (read-only{scope_tag})|Code edit `{rel}` DENIED -> "
            f"log findings as [BUG] | [UNWIRED] | [STRUCTURAL] | [UPGRADE] @ path:line ; "
            f"run `python3 {plan_cli} recon stop` to resume coding."
        )
    return None


def check_shift_left_diff_rules(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    """Shift-left fail-severity diff_rules BEFORE disk write (never blocks severity=='warn')."""
    rules = profile.get("diff_rules") or []
    if not rules:
        return None

    from planner_state import normalize_rel_path  # noqa: E402

    rel, inside = normalize_rel_path(raw_path, repo)
    if not inside or not rel:
        return None

    fail_rules = []
    for r in rules:
        if (r.get("severity") or "fail").lower() == "warn":
            continue
        prefs: list[str] = []
        if r.get("path_prefix"):
            prefs.append(str(r["path_prefix"]).replace("\\", "/").lstrip("./"))
        for p in r.get("path_prefixes") or []:
            ps = str(p).replace("\\", "/").lstrip("./")
            if ps:
                prefs.append(ps)
        preg = r.get("path_regex") or ""
        if prefs and not any(rel.startswith(p) for p in prefs):
            continue
        if preg and not re.search(preg, rel):
            continue
        fail_rules.append(r)
    if not fail_rules:
        return None

    if tool_name in ("edit_file", "replace_file_content"):
        target = args.get("Target") or args.get("TargetContent") or ""
        replacement = args.get("Replacement") or args.get("ReplacementContent") or ""
        tgt_counts = Counter(target.splitlines())
        added_lines: list[str] = []
        for ln in replacement.splitlines():
            if tgt_counts.get(ln, 0) > 0:
                tgt_counts[ln] -= 1
            else:
                added_lines.append(ln)
        window_base = replacement
    else:
        content = args.get("Content") or args.get("CodeContent") or ""
        added_lines = content.splitlines()
        window_base = content

    if not added_lines:
        return None

    for r in fail_rules:
        strip_comm = r.get("strip_line_comments", True)
        strip_q = r.get("strip_strings", True)
        msg_tpl = r.get("message") or "Diff rule violation."
        forbid = r.get("forbid_regex") or ""
        trig = r.get("trigger_regex") or ""

        for raw_ln in added_lines:
            code = _strip_line_comment(raw_ln) if strip_comm else raw_ln
            if not code.strip():
                continue
            target_txt = _strip_strings(code) if strip_q else code
            if forbid and re.search(forbid, target_txt):
                return f"SHIFT-LEFT BLOCK|`{rel}`: {msg_tpl}"
            if trig and re.search(trig, target_txt):
                req_if = r.get("require_if_any_regex") or ""
                must_win = r.get("must_match_window_regex") or ""
                if (not req_if or re.search(req_if, window_base)) and must_win and not re.search(must_win, window_base):
                    return f"SHIFT-LEFT BLOCK|`{rel}`: {msg_tpl}"
    return None


def _is_compactable_doc(rel: str) -> bool:
    s = rel.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    low = s.lstrip("/").lower()
    name = Path(low).name
    if name in ("changelog.md", "agents.md", "skill.md"):
        return True
    return low.endswith(".md") and (low.startswith(".agents/docs/") or "/.agents/docs/" in low)


def check_destructive_write(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
    in_plugin: bool,
    plugin_rel: str,
) -> tuple[str, str] | None:
    if tool_name not in ("write_file", "write_to_file"):
        return None
    p = Path(raw_path).expanduser()
    if not p.is_file():
        return None

    from planner_state import is_app_code_path, is_verifiable_path, normalize_rel_path  # noqa: E402

    is_target = (
        is_app_code_path(raw_path, repo, profile)
        or is_verifiable_path(raw_path, repo, profile)
        or (in_plugin and plugin_rel.lower().endswith((".py", ".json", ".md", ".sh")))
    )
    if not is_target:
        return None

    orig_lines = len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
    if orig_lines <= 150:
        return None

    content = args.get("Content") or args.get("CodeContent") or ""
    new_lines = len(content.splitlines())
    rel = plugin_rel if in_plugin else normalize_rel_path(raw_path, repo)[0]
    if new_lines < 0.70 * orig_lines:
        non_empty = [ln for ln in content.splitlines() if ln.strip()]
        is_blanking = len(non_empty) < 2 or len(content.strip()) < 20
        if _is_compactable_doc(rel) and not is_blanking:
            return None
        return (
            "force_ask",
            f"DESTRUCTIVE WRITE SHIELD|`{rel}` shrunk {orig_lines}L -> {new_lines}L (<70%). Confirm deletion or use `edit_file`.",
        )
    return (
        "deny",
        f"WRITE SHIELD|Full overwrite on `{rel}` ({orig_lines}L > 150L) -> use surgical `edit_file` to save output tokens.",
    )


def _capture_invisible_snapshot(repo: Path, hook_file: Path, hook_state: dict[str, Any]) -> None:
    if not (repo / ".git").exists():
        return
    import subprocess  # noqa: E402
    from planner_state import save_json  # noqa: E402

    def _git(gargs: list[str]) -> str:
        res = subprocess.run(["git", "-C", str(repo), *gargs], capture_output=True, text=True, timeout=3, check=False)
        return res.stdout.strip() if res.returncode == 0 else ""

    head = _git(["rev-parse", "HEAD"])
    stash_ref = _git(["stash", "create"])
    st_lines = _git(["status", "--porcelain"]).splitlines()
    pre_dirty = [
        ln[3:].strip().strip('"').replace("\\", "/")
        for ln in st_lines
        if len(ln) > 3
    ]
    hook_state["turn_snapshot_taken"] = True
    hook_state["turn_base_head"] = head
    hook_state["turn_snapshot_ref"] = stash_ref
    hook_state["turn_preexisting_dirty"] = pre_dirty[:200]
    save_json(hook_file, hook_state)


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        allow()
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        tool = payload.get("toolCall") or {}
        tool_name = (tool.get("name") or "").lower()
        args = tool.get("args") or {}
        raw_path = args.get("Path") or args.get("TargetFile") or ""
        if not raw_path:
            allow()
            return

        _ensure_paths()
        from planner_state import (  # noqa: E402
            get_paths,
            get_planner_mode,
            has_active_turn_plan,
            is_app_code_path,
            is_doc_verifiable_path,
            is_plugin_path,
            load_json,
            load_project_profile,
            normalize_rel_path,
            record_doc_file_edit,
            record_telemetry,
            resolve_plugin_root,
            resolve_repo_root,
            save_json,
            scan_secrets_and_conflicts,
            unique_edited_files,
        )

        repo = resolve_repo_root(payload)
        profile = load_project_profile(repo)

        # 1. Declarative per-project file guards (e.g., targetSdkVersion="28")
        hit = check_declarative_file_guards(tool_name, args, raw_path, repo, profile)
        if hit:
            reply(hit[0], hit[1], payload=payload, repo=repo)
            return

        # 1b. Frozen historical fixture protection (v1 + v2)
        plugin_rel, in_plugin = is_plugin_path(raw_path)
        if in_plugin and re.match(r"^tests/fixtures/profile_v[12]_.*\.json$", plugin_rel):
            if (resolve_plugin_root() / plugin_rel).exists():
                reply(
                    "force_ask",
                    f"PLUGIN SAFETY GUARD|Frozen fixture `{plugin_rel}` is a backward-compat contract. Add migrator in `scripts/schema_migrations.py` instead.",
                    payload=payload,
                    repo=repo,
                )
                return

        # 1c. Secret & merge-conflict shield
        sg = profile.get("secret_guard") or {}
        if sg.get("enabled", True):
            written_text = (
                (args.get("Replacement") or args.get("ReplacementContent") or "")
                if tool_name in ("edit_file", "replace_file_content")
                else (args.get("Content") or args.get("CodeContent") or "")
            )
            rel_for_sec = plugin_rel if in_plugin else normalize_rel_path(raw_path, repo)[0]
            sec_hits = scan_secrets_and_conflicts(written_text, rel_path=rel_for_sec, check_filename=True)
            if sec_hits:
                record_telemetry("shift_left_secret_conflict", {"file": rel_for_sec})
                reply("deny", "SECRET/CONFLICT SHIELD|" + " ; ".join(sec_hits[:3]), payload=payload, repo=repo)
                return

        # 1c2. Read-only forensic recon sweep shield
        recon_hit = check_recon_block(raw_path, repo, profile, payload, in_plugin, plugin_rel)
        if recon_hit:
            reply("deny", recon_hit, payload=payload, repo=repo)
            return

        # 1c3. Parallel epoch write-set lease guard
        try:
            from wave_scheduler import check_file_lease  # noqa: E402

            rel_lease = normalize_rel_path(raw_path, repo)[0]
            if rel_lease and not rel_lease.startswith(("/", "..")):
                conv_id = str(payload.get("conversationId") or "").strip()
                if (lease_hit := check_file_lease(repo, conv_id, rel_lease, profile)):
                    record_telemetry("lease_guard_deny", {"file": rel_lease})
                    reply("deny", lease_hit, payload=payload, repo=repo)
                    return
        except Exception:
            pass

        # 1d. Shift-left fail diff_rules before disk write
        sl_hit = check_shift_left_diff_rules(tool_name, args, raw_path, repo, profile)
        if sl_hit:
            record_telemetry("shift_left_diff_rule", {"path": raw_path})
            reply("deny", sl_hit, payload=payload, repo=repo)
            return

        # 1e. Destructive write_file shield (>150L existing code)
        dw_hit = check_destructive_write(tool_name, args, raw_path, repo, profile, in_plugin, plugin_rel)
        if dw_hit:
            ev = "destructive_write_force_ask" if dw_hit[0] == "force_ask" else "full_rewrite_denied"
            record_telemetry(ev, {"path": raw_path})
            reply(dw_hit[0], dw_hit[1], payload=payload, repo=repo)
            return

        # 2. Plan Gate (strict) vs Blast Radius Guard (lean) + invisible Tier>=2 snapshot
        is_plugin_protected = in_plugin and plugin_rel.lower().endswith((".py", ".json", ".md", ".sh"))
        is_app = is_app_code_path(raw_path, repo, profile)
        hook_file_cache: Path | None = None
        hook_state_cache: dict[str, Any] | None = None
        if is_app or is_plugin_protected:
            _, task_md, _, hook_file = get_paths(payload)
            hook_state = load_json(hook_file)
            hook_file_cache = hook_file
            hook_state_cache = hook_state
            mode = get_planner_mode(profile, payload, hook_state=hook_state)
            plan_ready = has_active_turn_plan(payload, hook_state)
            triage = hook_state.get("triage")
            if not triage:
                from complexity_triage import analyze_prompt  # noqa: E402

                triage = analyze_prompt(payload.get("lastUserInput") or "", repo, context=hook_state.get("task_context"))
            tier = int(triage.get("tier") or 0)
            edited = unique_edited_files(hook_state)
            edit_key = f"plugin:{plugin_rel}" if in_plugin else normalize_rel_path(raw_path, repo)[0]

            if mode == "strict":
                if not plan_ready:
                    is_second_file = len(edited) >= 1 and edit_key not in edited
                    reads_no_plan = int(hook_state.get("code_reads_without_plan") or 0)
                    if tier >= 2 or is_second_file or reads_no_plan >= 4 or (in_plugin and plugin_rel.startswith(("hooks/", "scripts/"))):
                        why = (
                            f"Tier {tier} ({triage.get('tier_name')})"
                            if tier >= 2
                            else (
                                f"modifying core plugin file `{plugin_rel}`"
                                if in_plugin
                                else ("editing multiple source files" if is_second_file else f"read {reads_no_plan} source files w/o plan")
                            )
                        )
                        plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
                        reply(
                            "deny",
                            f"PLAN GATE ({why}): Initialize plan BEFORE editing -> `python3 {plan_cli} init --tier {max(2, tier) if (is_second_file or in_plugin) else max(1, tier)} --goal \"...\" --req \"REQ-01: ...\"` (at `{task_md}`).",
                            payload=payload,
                            repo=repo,
                        )
                        return
            else:
                br_cfg = profile.get("blast_radius") if isinstance(profile.get("blast_radius"), dict) else {}
                if br_cfg.get("enabled", True) is not False:
                    max_simple = int(br_cfg.get("simple_tier_max_files") or 3)
                    br_action = str(br_cfg.get("action") or "warn").strip().lower()
                    if tier <= 1 and not plan_ready and edit_key not in edited and len(edited) >= max_simple:
                        if br_action == "force_ask":
                            if not hook_state.get("blast_radius_approved"):
                                record_telemetry("blast_radius_force_ask", {"edited": len(edited)})
                                from plugin_diagnostics import record_anomaly  # noqa: E402

                                record_anomaly(
                                    "blast_radius_suspect",
                                    "planner_guard_files.py",
                                    f"T{tier}:force_ask",
                                    f"T{tier} edited={len(edited)} next={edit_key}",
                                    repo=repo,
                                )
                                reply(
                                    "force_ask",
                                    f"BLAST RADIUS GUARD|Simple Tier {tier} turn already edited {len(edited)} code files; editing `{edit_key}` expands scope. Confirm?",
                                    payload=payload,
                                    repo=repo,
                                )
                                return
                        elif br_action != "off" and not hook_state.get("blast_radius_warned"):
                            from planner_state import save_json  # noqa: E402

                            hook_state["blast_radius_warned"] = True
                            hook_state["blast_radius_notified"] = False
                            save_json(hook_file, hook_state)
                            record_telemetry("blast_radius_warn", {"edited": len(edited)})

            # Safe BLUEPRINT_FIRST enforcement (opt-in via blueprint.enforce_hook or env; strict exemptions)
            from schema_migrations import resolve_blueprint_config  # noqa: E402

            bp_cfg = resolve_blueprint_config(profile)
            bp_enforce = bp_cfg.get("enforce_hook") or os.environ.get("PLANNER_ENFORCE_BLUEPRINT_HOOK", "").strip()
            min_bp_tier = max(2, int(bp_cfg.get("default_min_tier") or 1))
            if (
                bp_cfg.get("require_by_default")
                and bp_enforce
                and str(bp_enforce).lower() not in ("0", "false", "off")
                and is_app
                and not in_plugin
                and tier >= min_bp_tier
                and not hook_state.get("eco_mode")
                and not hook_state.get("blueprint_gate_warned")
                and not triage.get("blueprint_opt_out")
                and not triage.get("is_plugin_sync_fastpath")
                and not triage.get("is_btw_short")
                and triage.get("intent") != "tiny_fix"
            ):
                bp_dir = repo / str(bp_cfg.get("dir") or bp_cfg.get("blueprints_dir") or ".agents/docs/blueprints")
                has_active_bp = False
                if bp_dir.is_dir():
                    for bp_file in bp_dir.glob("BP_*.md"):
                        head = bp_file.read_text(encoding="utf-8", errors="ignore")[:800].upper()
                        if any(st in head for st in ("ACTIVE", "PARTIAL", "IN_PROGRESS", "DRAFT")):
                            has_active_bp = True
                            break
                if not has_active_bp:
                    from planner_state import save_json  # noqa: E402

                    hook_state["blueprint_gate_warned"] = True
                    save_json(hook_file, hook_state)
                    decision = "deny" if str(bp_enforce).lower() == "deny" else "force_ask"
                    record_telemetry("blueprint_first_gate", {"tier": tier, "decision": decision})
                    reply(
                        decision,
                        f"BLUEPRINT_FIRST|T{tier} code edit (`{edit_key}`) w/ zero ACTIVE/PARTIAL blueprint -> "
                        "run `plan_tool.py blueprint init --format compact` (or opt out w/ `sem blueprint`).",
                        payload=payload,
                        repo=repo,
                    )
                    return

            skip_snap = False
            try:
                from wave_scheduler import has_active_parallel_leases  # noqa: E402

                skip_snap = has_active_parallel_leases(repo)
            except Exception:
                skip_snap = False
            if tier >= 2 and is_app and not in_plugin and not hook_state.get("turn_snapshot_taken") and not skip_snap:
                _capture_invisible_snapshot(repo, hook_file, hook_state)

        if tool_name in ("edit_file", "write_file", "replace_file_content", "write_to_file"):
            if is_doc_verifiable_path(raw_path, repo) or (in_plugin and plugin_rel.lower().endswith(".md")):
                if hook_file_cache is None or hook_state_cache is None:
                    _, _, _, hook_file_cache = get_paths(payload)
                    hook_state_cache = load_json(hook_file_cache)
                rel_doc = f"plugin:{plugin_rel}" if in_plugin else normalize_rel_path(raw_path, repo)[0]
                if record_doc_file_edit(hook_state_cache, rel_doc):
                    save_json(hook_file_cache, hook_state_cache)

        allow()
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            _ensure_paths()
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_guard_files.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _ensure_paths()
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_guard_files.py", exc)
        except Exception:
            pass
        print('{"decision": "allow"}')
