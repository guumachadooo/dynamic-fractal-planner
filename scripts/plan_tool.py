#!/usr/bin/env python3
"""Universal Dynamic Fractal Plan Engine & Project Bootstrapper for Antigravity workspaces.

Persists machine state in <artifact_dir>/scratch/plan_state.json, renders
<artifact_dir>/TASK_PLAN.md, loads optional <workspace>/.agents/planner_profile.json,
and can bootstrap universal memory/planning docs in any project (`bootstrap`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = SCRIPTS_DIR.parent / "hooks"
for p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from complexity_triage import (  # noqa: E402
    UNIVERSAL_DOMAINS,
    analyze_prompt,
    build_recommended_modules,
    load_project_profile,
)
from planner_state import auto_upgrade_workspace, bootstrap_workspace, mark_docs_migrated  # noqa: E402
from schema_migrations import CURRENT_DOCS_FORMAT_VERSION, CURRENT_SCHEMA_VERSION, migrate_profile  # noqa: E402

def _load_plan_domain_tables() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    raw = json.loads((SCRIPTS_DIR / "domains.json").read_text(encoding="utf-8"))
    checklists: dict[str, list[str]] = {}
    for k, v in (raw.get("universal_checklists_extra") or {}).items():
        if k in ("discovery", "contract"):
            checklists[str(k)] = list(v or [])
    for k, spec in (raw.get("triage_domains") or {}).items():
        if isinstance(spec, dict) and spec.get("checklist"):
            checklists[str(k)] = list(spec["checklist"])
    for k, v in (raw.get("universal_checklists_extra") or {}).items():
        if str(k) not in checklists:
            checklists[str(k)] = list(v or [])
    steps = {str(k): list(v or []) for k, v in (raw.get("default_module_steps") or {}).items()}
    return checklists, steps


UNIVERSAL_CHECKLISTS, DEFAULT_MODULE_STEPS = _load_plan_domain_tables()

TIER_NAMES = {0: "DIRECT", 1: "STANDARD", 2: "META_MODULES", 3: "FRACTAL_DEEP"}
STATUS_ICON = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "done": "[x]",
    "verified": "[x]",
    "blocked": "[!]",
    "awaiting_user": "[?]",
}

def resolve_workspace_root(cli_ws: str | None = None) -> Path:
    if cli_ws:
        return Path(cli_ws).expanduser().resolve()
    env_ws = os.environ.get("PLANNER_WORKSPACE_ROOT", "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    cwd = Path.cwd()
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip())
    except Exception:
        pass
    return cwd


def resolve_artifact_dir(cli_dir: str | None) -> Path:
    if cli_dir:
        return Path(cli_dir).expanduser().resolve()
    for key in ("PLANNER_ARTIFACT_DIR", "CAVERN_ARTIFACT_DIR"):
        val = os.environ.get(key, "").strip()
        if val:
            return Path(val).expanduser().resolve()
    conv_id = (
        os.environ.get("PLANNER_CONV_ID")
        or os.environ.get("CAVERN_CONV_ID")
        or "default"
    ).strip() or "default"
    safe_conv = "".join(c for c in conv_id if c.isalnum() or c in "-_") or "default"
    return Path(f"/tmp/.agy_planner_{safe_conv}")


def state_paths(artifact_dir: Path) -> tuple[Path, Path, Path]:
    scratch = artifact_dir / "scratch"
    return artifact_dir / "TASK_PLAN.md", scratch / "plan_state.json", scratch / ".hook_state.json"


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        raise FileNotFoundError(f"Plan state not found at {state_file}. Run `plan_tool.py init` first.")
    return json.loads(state_file.read_text(encoding="utf-8"))


def save_and_render(artifact_dir: Path, state: dict[str, Any]) -> None:
    md_file, state_file, hook_file = state_paths(artifact_dir)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = int(time.time())
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    md_file.write_text(render_markdown(state), encoding="utf-8")

    try:
        hook_data = json.loads(hook_file.read_text(encoding="utf-8")) if hook_file.exists() else {}
    except Exception:
        hook_data = {}
    hook_data["code_reads_without_plan"] = 0
    hook_data["read_files_without_plan"] = []
    hook_data["plan_updated_at"] = state["updated_at"]
    if hook_data.get("turn_sig"):
        hook_data["plan_turn_sig"] = hook_data["turn_sig"]
    if state.get("prompt_hash"):
        hook_data["plan_prompt_hash"] = state["prompt_hash"]
    hook_file.write_text(json.dumps(hook_data, ensure_ascii=False, indent=2), encoding="utf-8")


def _compute_module_status(mod: dict[str, Any]) -> str:
    steps = mod.get("steps") or []
    if not steps:
        return mod.get("status", "pending")
    statuses = [s.get("status", "pending") for s in steps]
    if all(st == "done" for st in statuses):
        return "done"
    if any(st in ("in_progress", "done") for st in statuses):
        return "in_progress"
    if any(st == "blocked" for st in statuses):
        return "blocked"
    return "pending"


def render_markdown(state: dict[str, Any]) -> str:
    tier = int(state.get("tier", 1))
    tier_name = state.get("tier_name") or TIER_NAMES.get(tier, "STANDARD")
    reqs = state.get("requirements") or []
    mods = state.get("modules") or []

    verified_reqs = sum(1 for r in reqs if r.get("status") == "verified")
    done_mods = sum(1 for m in mods if m.get("status") == "done")

    lines = [
        f"# Task Plan — T{tier} ({tier_name})",
        "",
        f"- **Goal:** {state.get('goal', '')}",
        f"- **State:** `{state.get('lifecycle', 'active')}`"
        + (f" — {state['pause_reason']}" if state.get("pause_reason") else ""),
        f"- **Progress:** `{verified_reqs}/{len(reqs)}` REQs verified | `{done_mods}/{len(mods)}` modules done",
        f"- **Domains:** `{', '.join(state.get('domains') or ['general'])}`",
        f"- **Strategy:** `{state.get('research_strategy', 'scoped_grep_and_line_slices')}`",
        "",
        "---",
        "",
        "## 1. REQs (Phase 0)",
        "| ID | Status | REQ | Acceptance / Evidence |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for r in reqs:
        icon = STATUS_ICON.get(r.get("status", "pending"), "[ ]")
        ev = r.get("evidence") or r.get("acceptance") or "-"
        chk = f" ({r['check_note']})" if r.get("check_note") else ""
        lines.append(f"| `{r['id']}` | `{icon}` {r.get('status', 'pending')} | {r['summary']} | {ev}{chk} |")

    lines.extend(["", "## 2. Modules", ""])
    for m in mods:
        m_icon = STATUS_ICON.get(m.get("status", "pending"), "[ ]")
        linked = ", ".join(m.get("linked_reqs") or []) or "All"
        if m.get("status") == "done":
            n_steps = len(m.get("steps") or [])
            lines.append(f"- {m_icon} `{m['id']}` | {m['title']} ({n_steps} steps done | REQs: {linked})")
            continue
        lines.append(f"### {m_icon} `{m['id']}` — {m['title']} (`{m.get('domain', 'general')}` | REQs: {linked})")
        if m.get("checklist"):
            lines.append("**Guardrails:**")
            for c in m["checklist"]:
                lines.append(f"- {c}")
        lines.append("**Steps:**")
        for idx, s in enumerate(m.get("steps") or [], start=1):
            s_icon = STATUS_ICON.get(s.get("status", "pending"), "[ ]")
            ev = f" — *{s['evidence']}*" if s.get("evidence") else ""
            lines.append(f"{idx}. {s_icon} {s['title']}{ev}")
            for sub_idx, sub in enumerate(s.get("substeps") or [], start=1):
                sub_icon = STATUS_ICON.get(sub.get("status", "pending"), "[ ]")
                sev = f" — *{sub['evidence']}*" if sub.get("evidence") else ""
                lines.append(f"   {idx}.{sub_idx}. {sub_icon} {sub['title']}{sev}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _domain_checklist(domain: str, profile: dict[str, Any]) -> list[str]:
    custom = (profile.get("domains") or {}).get(domain) if isinstance(profile.get("domains"), dict) else None
    if isinstance(custom, dict) and custom.get("checklist"):
        return list(custom["checklist"])
    return list(UNIVERSAL_CHECKLISTS.get(domain, UNIVERSAL_CHECKLISTS["general"]))


def _domain_is_ship(domain: str, profile: dict[str, Any]) -> bool:
    custom = (profile.get("domains") or {}).get(domain) if isinstance(profile.get("domains"), dict) else None
    if isinstance(custom, dict) and "is_ship" in custom:
        return bool(custom["is_ship"])
    base = UNIVERSAL_DOMAINS.get(domain)
    return bool(base and base.get("is_ship"))


def _make_step(title: str) -> dict[str, Any]:
    return {"title": title, "status": "pending", "evidence": "", "substeps": []}


def _make_module(
    mod_id: str,
    title: str,
    domain: str,
    linked_reqs: list[str],
    profile: dict[str, Any],
    steps: list[str] | None = None,
) -> dict[str, Any]:
    custom = (profile.get("domains") or {}).get(domain) if isinstance(profile.get("domains"), dict) else None
    custom_steps = custom.get("steps") if isinstance(custom, dict) else None
    step_texts = steps or custom_steps or DEFAULT_MODULE_STEPS.get(domain) or [
        f"Implement scoped changes for `{mod_id}` ({title}).",
        "Verify modified symbols/files against acceptance criteria.",
    ]
    return {
        "id": mod_id,
        "title": title,
        "domain": domain,
        "is_ship": _domain_is_ship(domain, profile),
        "linked_reqs": linked_reqs,
        "status": "pending",
        "checklist": _domain_checklist(domain, profile),
        "steps": [_make_step(t) for t in step_texts],
    }


def _parse_req_arg(raw: str, idx: int) -> dict[str, Any]:
    if ":" in raw and raw.split(":", 1)[0].strip().upper().startswith("REQ-"):
        req_id, summary = raw.split(":", 1)
        req_id = req_id.strip().upper()
    else:
        req_id = f"REQ-{idx:02d}"
        summary = raw
    return {
        "id": req_id,
        "summary": summary.strip(),
        "acceptance": "",
        "status": "pending",
        "evidence": "",
        "check_note": "",
    }


def cmd_init(args: argparse.Namespace, artifact_dir: Path, ws_root: Path) -> None:
    profile = load_project_profile(ws_root)
    if args.json:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text(encoding="utf-8")
        spec = json.loads(raw)
        goal = spec.get("goal") or args.goal or "Dynamic Task"
        triage = analyze_prompt(goal + "\n" + "\n".join(r.get("summary", "") for r in spec.get("requirements", [])), ws_root)
        tier = int(spec.get("tier") or triage["tier"] or 1)
        reqs = []
        for idx, r in enumerate(spec.get("requirements") or [], start=1):
            if isinstance(r, str):
                reqs.append(_parse_req_arg(r, idx))
            else:
                reqs.append({
                    "id": (r.get("id") or f"REQ-{idx:02d}").upper(),
                    "summary": r.get("summary") or "",
                    "acceptance": r.get("acceptance") or "",
                    "status": "pending",
                    "evidence": "",
                    "check_note": "",
                })
        mods = []
        for idx, m in enumerate(spec.get("modules") or [], start=1):
            dom = m.get("domain") or "general"
            raw_steps = m.get("steps") or None
            mods.append(
                _make_module(
                    mod_id=m.get("id") or f"MOD-{idx:02d}",
                    title=m.get("title") or f"Module {idx}",
                    domain=dom,
                    linked_reqs=m.get("linked_reqs") or [r["id"] for r in reqs],
                    profile=profile,
                    steps=[s["title"] if isinstance(s, dict) else str(s) for s in raw_steps] if raw_steps else None,
                )
            )
        domains = spec.get("domains") or triage["detected_domains"] or ["general"]
        research_strategy = spec.get("research_strategy") or triage["research_strategy"]
    else:
        goal = args.goal or "Workspace Task"
        req_inputs = args.req or [goal]
        reqs = [_parse_req_arg(r, i) for i, r in enumerate(req_inputs, start=1)]
        combined_text = goal + "\n" + "\n".join(r["summary"] for r in reqs)
        triage = analyze_prompt(combined_text, ws_root)
        tier = int(args.tier) if args.tier is not None else max(1, int(triage["tier"]))
        domains = [d.strip() for d in args.modules.split(",") if d.strip()] if args.modules else triage["detected_domains"]
        research_strategy = triage["research_strategy"]

        merged_specs = dict(UNIVERSAL_DOMAINS)
        if isinstance(profile.get("domains"), dict):
            merged_specs.update(profile["domains"])
        blueprints = build_recommended_modules(tier, domains, triage["depth_level"], merged_specs)
        all_req_ids = [r["id"] for r in reqs]
        mods = [
            _make_module(
                mod_id=bp["id"],
                title=bp["title"],
                domain=bp["domain"],
                linked_reqs=all_req_ids,
                profile=profile,
            )
            for bp in blueprints
        ]

    if not mods:
        mods = [_make_module("MOD-IMPL", "Core Implementation", "general", [r["id"] for r in reqs], profile)]

    p_hash = (os.environ.get("PLANNER_LAST_PROMPT_HASH") or os.environ.get("CAVERN_LAST_PROMPT_HASH") or "").strip()
    if not p_hash:
        _, _, hook_file = state_paths(artifact_dir)
        try:
            if hook_file.exists():
                p_hash = (json.loads(hook_file.read_text(encoding="utf-8")).get("prompt_hash") or "").strip()
        except Exception:
            p_hash = ""
    if not p_hash:
        p_hash = hashlib.sha256(goal.encode("utf-8", errors="ignore")).hexdigest()[:16]

    state = {
        "version": 1,
        "workspace_root": str(ws_root),
        "goal": goal,
        "tier": tier,
        "tier_name": TIER_NAMES.get(tier, "STANDARD"),
        "lifecycle": "active",
        "pause_reason": "",
        "prompt_hash": p_hash,
        "domains": domains or ["general"],
        "research_strategy": research_strategy,
        "requirements": reqs,
        "modules": mods,
    }
    save_and_render(artifact_dir, state)
    print(f"Initialized Tier {tier} ({state['tier_name']}) plan at {artifact_dir / 'TASK_PLAN.md'}")
    print(format_compact_status(state))


def cmd_bootstrap(args: argparse.Namespace, ws_root: Path) -> None:
    """Scaffold universal .agents memory docs and an auto-detected planner_profile.json."""
    created, skipped = bootstrap_workspace(
        ws_root,
        name=args.name,
        force=args.force,
        embed_plugin=bool(args.embed_plugin),
    )

    print(f"Bootstrapped universal agent structure in `{ws_root}`:")
    if created:
        print("  Created:", ", ".join(created))
    if skipped:
        print("  Preserved existing:", ", ".join(skipped))


def cmd_add_module(args: argparse.Namespace, artifact_dir: Path, ws_root: Path) -> None:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    profile = load_project_profile(ws_root)
    linked = [r.strip().upper() for r in (args.req or []) if r.strip()]
    if not linked:
        linked = [r["id"] for r in state.get("requirements") or []]
    mod = _make_module(
        mod_id=args.id,
        title=args.title,
        domain=args.domain,
        linked_reqs=linked,
        profile=profile,
        steps=args.step or None,
    )
    mods = state["modules"]
    if args.domain != "audit" and not mod["is_ship"]:
        insert_at = next(
            (i for i, m in enumerate(mods) if m["domain"] == "audit" or m.get("is_ship")),
            len(mods),
        )
        mods.insert(insert_at, mod)
    else:
        mods.append(mod)

    save_and_render(artifact_dir, state)
    print(f"Added module `{args.id}` ({args.domain}) with {len(mod['steps'])} step(s).")


def cmd_expand_step(args: argparse.Namespace, artifact_dir: Path) -> None:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    mod = next((m for m in state["modules"] if m["id"] == args.module), None)
    if not mod:
        raise SystemExit(f"Module `{args.module}` not found.")
    idx = args.step - 1
    if idx < 0 or idx >= len(mod["steps"]):
        raise SystemExit(f"Step {args.step} out of range for `{args.module}`.")
    step_obj = mod["steps"][idx]
    for title in args.substep:
        step_obj["substeps"].append({"title": title, "status": "pending", "evidence": ""})
    if int(state.get("tier", 1)) < 3:
        state["tier"] = 3
        state["tier_name"] = "FRACTAL_DEEP"
    mod["status"] = _compute_module_status(mod)
    save_and_render(artifact_dir, state)
    print(f"Expanded {args.module} step {args.step} with {len(args.substep)} substep(s) (Tier -> {state['tier_name']}).")


def cmd_start(args: argparse.Namespace, artifact_dir: Path) -> None:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    state["lifecycle"] = "active"
    state["pause_reason"] = ""
    mod = next((m for m in state["modules"] if m["id"] == args.module), None)
    if not mod:
        raise SystemExit(f"Module `{args.module}` not found.")

    if args.step is not None:
        idx = args.step - 1
        if idx < 0 or idx >= len(mod["steps"]):
            raise SystemExit(f"Step {args.step} out of range for `{args.module}`.")
        if args.substep is not None:
            sidx = args.substep - 1
            subs = mod["steps"][idx].get("substeps") or []
            if sidx < 0 or sidx >= len(subs):
                raise SystemExit(f"Substep {args.substep} out of range.")
            subs[sidx]["status"] = "in_progress"
        mod["steps"][idx]["status"] = "in_progress"
    else:
        for s in mod["steps"]:
            if s["status"] == "pending":
                s["status"] = "in_progress"
                break
    mod["status"] = "in_progress"

    for rid in mod.get("linked_reqs") or []:
        for r in state["requirements"]:
            if r["id"] == rid and r["status"] == "pending":
                r["status"] = "in_progress"

    save_and_render(artifact_dir, state)
    print(f"Started `{mod['id']}` — {mod['title']} ({mod['domain']})")
    if mod.get("checklist"):
        print("Just-in-Time Guardrails:")
        for c in mod["checklist"]:
            print(f"  * {c}")


def cmd_done(args: argparse.Namespace, artifact_dir: Path) -> None:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    mod = next((m for m in state["modules"] if m["id"] == args.module), None)
    if not mod:
        raise SystemExit(f"Module `{args.module}` not found.")

    if args.step is None:
        for s in mod["steps"]:
            s["status"] = "done"
            for sub in s.get("substeps") or []:
                sub["status"] = "done"
        if args.evidence and mod["steps"]:
            mod["steps"][-1]["evidence"] = args.evidence
    else:
        idx = args.step - 1
        if idx < 0 or idx >= len(mod["steps"]):
            raise SystemExit(f"Step {args.step} out of range for `{args.module}`.")
        step_obj = mod["steps"][idx]
        if args.substep is not None:
            sidx = args.substep - 1
            subs = step_obj.get("substeps") or []
            if sidx < 0 or sidx >= len(subs):
                raise SystemExit(f"Substep {args.substep} out of range.")
            subs[sidx]["status"] = "done"
            if args.evidence:
                subs[sidx]["evidence"] = args.evidence
            if all(sub["status"] == "done" for sub in subs):
                step_obj["status"] = "done"
        else:
            step_obj["status"] = "done"
            for sub in step_obj.get("substeps") or []:
                sub["status"] = "done"
            if args.evidence:
                step_obj["evidence"] = args.evidence

    mod["status"] = _compute_module_status(mod)
    save_and_render(artifact_dir, state)
    print(f"Updated `{mod['id']}` -> `{mod['status']}`")
    print(format_compact_status(state))


def cmd_req_verify(args: argparse.Namespace, artifact_dir: Path) -> None:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    req_id = args.req_id.strip().upper()
    req = next((r for r in state["requirements"] if r["id"] == req_id), None)
    if not req:
        raise SystemExit(f"Requirement `{req_id}` not found.")
    if not args.evidence.strip():
        raise SystemExit("Cannot verify requirement without concrete `--evidence`.")
    req["status"] = "verified"
    req["evidence"] = args.evidence.strip()
    req["check_note"] = (args.check or "").strip()
    save_and_render(artifact_dir, state)
    print(f"Verified `{req_id}`: {req['evidence']}")


def cmd_pause(args: argparse.Namespace, artifact_dir: Path) -> None:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    state["lifecycle"] = args.state
    state["pause_reason"] = args.reason
    save_and_render(artifact_dir, state)
    print(f"Plan marked `{args.state}`: {args.reason}")


def format_compact_status(state: dict[str, Any]) -> str:
    reqs = state.get("requirements") or []
    mods = state.get("modules") or []
    verified = sum(1 for r in reqs if r.get("status") == "verified")
    done_m = sum(1 for m in mods if m.get("status") == "done")
    lines = [
        f"[Plan Tier {state.get('tier')} {state.get('tier_name')}] REQs: {verified}/{len(reqs)} verified | Modules: {done_m}/{len(mods)} done | State: {state.get('lifecycle')}"
    ]
    pending_reqs = [f"{r['id']}({r['status']})" for r in reqs if r.get("status") != "verified"]
    if pending_reqs:
        lines.append("Unverified REQs: " + ", ".join(pending_reqs[:6]) + (f" (+{len(pending_reqs)-6})" if len(pending_reqs) > 6 else ""))
    active_mod = next((m for m in mods if m.get("status") == "in_progress"), None)
    if not active_mod:
        active_mod = next((m for m in mods if m.get("status") != "done"), None)
    if active_mod:
        next_step = next(
            ((i + 1, s) for i, s in enumerate(active_mod.get("steps") or []) if s.get("status") != "done"),
            None,
        )
        step_desc = f"Step {next_step[0]}: {next_step[1]['title']}" if next_step else "Finish module"
        lines.append(f"Next Action: [{active_mod['id']}] {step_desc}")
    else:
        lines.append("Next Action: Run `plan_tool.py audit` before reporting final result.")
    return "\n".join(lines)


def cmd_migrate(args: argparse.Namespace, ws_root: Path) -> int:
    """Upgrade `.agents/planner_profile.json` on disk using the chained schema migrator."""
    profile_path = ws_root / ".agents" / "planner_profile.json"
    if not profile_path.is_file():
        print(f"No profile found at {profile_path}. Run `plan_tool.py bootstrap` first.")
        return 1

    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to parse {profile_path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(raw, dict):
        print(f"Invalid profile format in {profile_path}: expected JSON object.", file=sys.stderr)
        return 1

    raw_ver = raw.get("schema_version")
    migrated, resolved_ver, warnings = migrate_profile(raw)
    for w in warnings:
        print(f"[WARN] {w}")

    if isinstance(raw_ver, int) and not isinstance(raw_ver, bool) and raw_ver > CURRENT_SCHEMA_VERSION:
        print(f"Skipped disk write: profile schema_version {raw_ver} > supported {CURRENT_SCHEMA_VERSION}.")
        return 0

    if raw == migrated:
        print(f"Profile `{profile_path}` is already up to date at schema_version {resolved_ver}.")
        if not getattr(args, "dry_run", False) and (ws_root / ".agents" / "docs").is_dir():
            up = auto_upgrade_workspace(ws_root)
            if up.get("boilerplate_upgraded"):
                print("AUTO_UPGRADED|templates=" + ",".join(up["boilerplate_upgraded"]))
            if up.get("docs_needing_ai_conversion"):
                print(
                    "DOCS_MIGRATION_PENDING|files="
                    + ",".join(up["docs_needing_ai_conversion"])
                    + "|run lossless rewrite -> plan_tool.py docs-migrated"
                )
        return 0

    if getattr(args, "dry_run", False):
        print(f"[DRY RUN] Would migrate `{profile_path}` -> schema_version {resolved_ver}.")
        return 0

    if not getattr(args, "no_backup", False):
        bak_path = profile_path.with_name(profile_path.name + ".bak")
        bak_path.write_text(profile_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created backup `{bak_path}`.")

    profile_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    old_label = raw_ver if raw_ver is not None else "1 (implicit)"
    print(f"Migrated `{profile_path}`: v{old_label} -> schema_version {resolved_ver}.")
    if (ws_root / ".agents" / "docs").is_dir():
        up = auto_upgrade_workspace(ws_root)
        if up.get("boilerplate_upgraded"):
            print("AUTO_UPGRADED|templates=" + ",".join(up["boilerplate_upgraded"]))
        if up.get("docs_needing_ai_conversion"):
            print(
                "DOCS_MIGRATION_PENDING|files="
                + ",".join(up["docs_needing_ai_conversion"])
                + "|run lossless rewrite -> plan_tool.py docs-migrated"
            )
    return 0


def cmd_docs_migrated(args: argparse.Namespace, ws_root: Path) -> int:
    """Validate symbolic telegraphic EN marker across managed project docs and stamp docs_format_version."""
    ok, msg, files = mark_docs_migrated(ws_root, force=bool(getattr(args, "force", False)))
    if not ok:
        print(f"DOCS_MIGRATION_INCOMPLETE|{msg}|missing={','.join(files)}")
        return 1
    print(f"DOCS_MIGRATED|docs_format_version={CURRENT_DOCS_FORMAT_VERSION}|files={','.join(files)}|profile={msg}")
    return 0


def cmd_audit(artifact_dir: Path) -> int:
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    missing_reqs = [r for r in state.get("requirements") or [] if r.get("status") != "verified"]
    open_mods = [m for m in state.get("modules") or [] if m.get("status") != "done"]

    if not missing_reqs and not open_mods:
        print("PLAN AUDIT PASS: All modules done and all requirements verified with evidence.")
        return 0

    print("PLAN AUDIT INCOMPLETE:")
    for r in missing_reqs:
        print(f"  - Unverified {r['id']} ({r.get('status')}): {r['summary']}")
    for m in open_mods:
        print(f"  - Open module {m['id']} ({m.get('status')}): {m['title']}")
    return 1


def cmd_finish(args: argparse.Namespace, artifact_dir: Path) -> int:
    """Close all modules/steps and verify all REQs in 1 turn with observed evidence."""
    ev = (args.evidence or "").strip()
    if not ev:
        raise SystemExit("`finish` requires non-empty `--evidence`.")
    _, state_file, _ = state_paths(artifact_dir)
    state = load_state(state_file)
    state["lifecycle"] = "active"
    state["pause_reason"] = ""
    for m in state.get("modules") or []:
        for s in m.get("steps") or []:
            s["status"] = "done"
            if not s.get("evidence"):
                s["evidence"] = ev
            for sub in s.get("substeps") or []:
                sub["status"] = "done"
                if not sub.get("evidence"):
                    sub["evidence"] = ev
        m["status"] = "done"
    for r in state.get("requirements") or []:
        if r.get("status") != "verified":
            r["status"] = "verified"
            r["evidence"] = ev
    save_and_render(artifact_dir, state)
    print(format_compact_status(state))
    return cmd_audit(artifact_dir)


def cmd_handoff(args: argparse.Namespace, ws_root: Path) -> int:
    """Write or clear a compact <=20L `.agents/docs/HANDOFF.md` for low-quota fresh chats."""
    handoff_file = ws_root / ".agents" / "docs" / "HANDOFF.md"
    if getattr(args, "clear", False):
        if handoff_file.exists():
            handoff_file.unlink()
            print(f"HANDOFF_CLEARED|{handoff_file}")
        else:
            print(f"HANDOFF_ABSENT|{handoff_file}")
        return 0

    summary = (args.summary or "").strip().replace("\n", " ")
    next_act = (args.next or "").strip().replace("\n", " ")
    roadmap = (getattr(args, "roadmap", "") or "").strip().replace("\n", " ")
    wave = (getattr(args, "wave", "") or "").strip().replace("\n", " ")
    archives = [str(a).strip().replace("\n", " ") for a in (getattr(args, "archive", None) or []) if str(a).strip()]

    if handoff_file.is_file() and (not roadmap or not wave or not archives):
        try:
            for prev_ln in handoff_file.read_text(encoding="utf-8", errors="ignore").splitlines()[:25]:
                s = prev_ln.strip()
                if s.startswith("ROADMAP|") and not roadmap:
                    roadmap = s[8:].strip()
                elif s.startswith("WAVE|") and not wave:
                    wave = s[5:].strip()
                elif s.startswith("ARCHIVE|") and not archives:
                    raw_arch = s[8:].split("(!read_full", 1)[0].strip()
                    if raw_arch:
                        archives = [raw_arch]
        except Exception:
            pass

    if not any((summary, next_act, roadmap, wave)):
        raise SystemExit("`handoff` requires `--summary`, `--next`, `--roadmap`, or `--wave` (or `--clear`).")

    import host_context  # noqa: E402

    host_id = host_context.detect_host(ws_root).get("host_id") or "local-ide"

    branch, head, dirty = "-", "-", "0"
    dirty_preview = ""
    if (ws_root / ".git").exists():
        def _g(gargs: list[str]) -> str:
            r = subprocess.run(["git", "-C", str(ws_root), *gargs], capture_output=True, text=True, timeout=3, check=False)
            return r.stdout.strip() if r.returncode == 0 else ""
        branch = _g(["branch", "--show-current"]) or "-"
        head = _g(["rev-parse", "--short", "HEAD"]) or "-"
        st = [ln.strip() for ln in _g(["status", "--porcelain"]).splitlines() if ln.strip()]
        dirty = str(len(st))
        if st:
            dirty_preview = ", ".join(st[:5]) + (f" (+{len(st) - 5})" if len(st) > 5 else "")

    lines = [
        "# Session Handoff",
        f"ts={int(time.time())}|branch={branch}|head={head}|dirty={dirty}|host={host_id}|fmt={CURRENT_DOCS_FORMAT_VERSION}",
    ]
    if roadmap:
        lines.append(f"ROADMAP|{roadmap[:180]}")
    if wave:
        lines.append(f"WAVE|{wave[:120]}")
    if archives:
        lines.append(f"ARCHIVE|{', '.join(archives)[:220]} (!read_full; slice only on demand)")
    if summary:
        lines.append(f"summary|{summary[:300]}")
    if next_act:
        lines.append(f"next|{next_act[:300]}")
    if dirty_preview:
        lines.append(f"files|{dirty_preview[:240]}")

    compound_note = (getattr(args, "compound_note", "") or "").strip().replace("\n", " ")
    if compound_note:
        lines.append(f"compound|{compound_note[:240]}")

    handoff_file.parent.mkdir(parents=True, exist_ok=True)
    handoff_file.write_text("\n".join(lines[:20]) + "\n", encoding="utf-8")
    print(f"HANDOFF_WRITTEN|{handoff_file}|lines={len(lines[:20])}")

    try:
        import error_learner  # noqa: E402

        prof = load_project_profile(ws_root)
        csum = error_learner.summarize_compound_status(ws_root, profile=prof)
        dom_counts = csum.get("by_domain") or {}
        dom_str = ",".join(f"{k}:{dom_counts.get(k, 0)}" for k in ("BL", "UI", "ARCH", "DSP", "AI"))
        t_ok = "OK" if csum.get("timeless_ok", True) else f"FAIL({csum.get('audit_errors', 0)})"
        print(
            f"COMPOUND|lessons={csum.get('total_lessons', 0)}({dom_str})|"
            f"timeless={t_ok}|fitness_rules={csum.get('fitness_rules', 0)}|monoliths={csum.get('monoliths', 0)}"
        )
    except Exception:
        pass
    return 0


def cmd_recon(args: argparse.Namespace, artifact_dir: Path, ws_root: Path) -> int:
    """Manage read-only forensic recon sweep state (`MOD-RECON-SWEEP`)."""
    _, _, hook_file = state_paths(artifact_dir)
    hook_state: dict[str, Any] = {}
    if hook_file.is_file():
        try:
            loaded = json.loads(hook_file.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(loaded, dict):
                hook_state = loaded
        except Exception:
            hook_state = {}

    sub = args.recon_cmd
    if sub == "start":
        scope = (getattr(args, "scope", "") or "").strip().replace("\n", " ")
        hook_state["recon"] = {
            "active": True,
            "scope": scope,
            "started_at": int(time.time()),
            "workspace": str(ws_root),
        }
        hook_state["recon_injected"] = False
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text(json.dumps(hook_state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"RECON_START|active=true|scope={scope or 'full'}|file={hook_file}")
        print("TAXONOMY|[BUG] | [UNWIRED] | [STRUCTURAL] | [UPGRADE] @ path:line (code edits blocked until `recon stop`)")
        return 0
    if sub == "stop":
        prev = hook_state.get("recon") if isinstance(hook_state.get("recon"), dict) else {}
        hook_state["recon"] = {
            "active": False,
            "scope": str(prev.get("scope") or ""),
            "started_at": int(prev.get("started_at") or 0),
            "stopped_at": int(time.time()),
            "workspace": str(ws_root),
        }
        hook_state["recon_injected"] = False
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text(json.dumps(hook_state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"RECON_STOP|active=false|file={hook_file}")
        return 0
    rec = hook_state.get("recon") if isinstance(hook_state.get("recon"), dict) else {}
    active = bool(rec.get("active"))
    scope = str(rec.get("scope") or "-")
    print(f"RECON_STATUS|active={'true' if active else 'false'}|scope={scope}|taxonomy=[BUG]|[UNWIRED]|[STRUCTURAL]|[UPGRADE] @ path:line")
    return 0


def cmd_undo_turn(args: argparse.Namespace, artifact_dir: Path, ws_root: Path) -> int:
    """Inspect or roll back code edits made in the active turn using `.hook_state.json` snapshot."""
    _, _, hook_file = state_paths(artifact_dir)
    if not hook_file.is_file():
        print("UNDO_SKIP|no .hook_state.json")
        return 0
    hook_state = json.loads(hook_file.read_text(encoding="utf-8"))
    edited = [str(f).replace("\\", "/") for f in (hook_state.get("code_files_edited") or []) if f]
    snap_ref = (hook_state.get("turn_snapshot_ref") or "").strip()
    base_head = (hook_state.get("turn_base_head") or "").strip()
    pre_dirty = set(str(x).replace("\\", "/") for x in (hook_state.get("turn_preexisting_dirty") or []))

    if not edited:
        print("UNDO_SKIP|no code_files_edited in turn")
        return 0

    def _exists_in_ref(ref: str, rel: str) -> bool:
        if not ref:
            return False
        r = subprocess.run(["git", "-C", str(ws_root), "cat-file", "-e", f"{ref}:{rel}"], capture_output=True, text=True, timeout=3, check=False)
        return r.returncode == 0

    actions: list[tuple[str, str, str]] = []
    for rel in edited:
        if snap_ref and _exists_in_ref(snap_ref, rel):
            actions.append(("RESTORE", rel, snap_ref))
        elif base_head and _exists_in_ref(base_head, rel):
            actions.append(("RESTORE", rel, base_head))
        elif rel not in pre_dirty:
            actions.append(("DELETE", rel, "new_in_turn"))

    if getattr(args, "dry_run", False):
        print(f"UNDO_DRY_RUN|snap={snap_ref or '-'}|head={base_head or '-'}|actions={len(actions)}")
        for act, rel, src in actions:
            print(f"{act}|{rel}|{src}")
        return 0

    restored, deleted = 0, 0
    for act, rel, src in actions:
        if act == "RESTORE":
            res = subprocess.run(["git", "-C", str(ws_root), "checkout", src, "--", rel], capture_output=True, text=True, timeout=5, check=False)
            if res.returncode == 0:
                restored += 1
        elif act == "DELETE":
            p = ws_root / rel
            if p.is_file():
                p.unlink()
                deleted += 1

    hook_state["code_files_edited"] = []
    hook_state["needs_verify"] = False
    hook_state["needs_build"] = False
    hook_file.write_text(json.dumps(hook_state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"UNDO_APPLIED|restored={restored}|deleted={deleted}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Aggregate or reset local plugin telemetry events in `.metrics.jsonl`."""
    metrics_env = os.environ.get("PLANNER_METRICS_FILE", "").strip()
    metrics_file = Path(metrics_env) if metrics_env else (SCRIPTS_DIR.parent / ".metrics.jsonl")
    if getattr(args, "reset", False):
        if metrics_file.exists():
            metrics_file.unlink()
        print(f"STATS_RESET|{metrics_file}")
        return 0

    if not metrics_file.is_file():
        print("STATS|total=0|empty")
        return 0

    counts: dict[str, int] = {}
    total = 0
    for raw in metrics_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            ev = str(obj.get("ev") or "unknown")
            counts[ev] = counts.get(ev, 0) + 1
            total += 1
        except Exception:
            continue
    print(f"STATS|total={total}|file={metrics_file.name}")
    for ev, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{ev}|{cnt}")
    return 0


def cmd_diagnostics(args: argparse.Namespace) -> int:
    """Inspect or clear self-diagnostic anomaly logs in `.diagnostics/anomalies.jsonl`."""
    from plugin_diagnostics import clear_anomalies, format_diagnostics_report  # noqa: E402

    if getattr(args, "clear", False):
        removed, target = clear_anomalies(kind_filter=getattr(args, "kind", "") or "")
        if removed == 0:
            print("DIAGNOSTICS_CLEARED|removed=0|clean")
        else:
            print(f"DIAGNOSTICS_CLEARED|removed={removed}|file={target}")
        return 0
    print(
        format_diagnostics_report(
            kind_filter=getattr(args, "kind", "") or "",
            limit=int(getattr(args, "limit", 25) or 25),
            raw=bool(getattr(args, "raw", False)),
        )
    )
    return 0


def cmd_model_mode(args: argparse.Namespace, ws_root: Path) -> int:
    """Inspect, persist, or clear cached `auto` model-mode classifications."""
    from model_mode_cache import (  # noqa: E402
        clear_model_mode,
        list_model_modes,
        lookup_model_mode,
        set_model_mode,
    )

    sub = args.mm_cmd
    if sub == "set":
        entry = set_model_mode(args.model, args.mode, why=args.why, source=args.source)
        print(f"MODEL_MODE_SET|key={entry['key']}|mode={entry['mode']}|src={entry['source']}|why={entry['why']}")
        return 0
    if sub == "get":
        prof = load_project_profile(ws_root)
        mode, src, key = lookup_model_mode(args.model, profile=prof)
        print(f"MODEL_MODE|key={key}|mode={mode or 'unseen'}|src={src}")
        return 0
    if sub == "list":
        rows, pending = list_model_modes()
        print("MODEL|MODE|SRC|WHY")
        for r in rows:
            print(f"{r['key']}|{r['mode']}|{r['source']}|{r['why']}")
        for k in sorted(pending.keys()):
            p = pending[k] if isinstance(pending[k], dict) else {}
            print(f"PENDING|{k}|{p.get('status', 'pending')}|next_retry={p.get('next_retry_ts', 0)}")
        return 0
    if sub == "clear":
        if not getattr(args, "all", False) and not (getattr(args, "model", "") or "").strip():
            print("MODEL_MODE_ERROR|pass <model> or --all", file=sys.stderr)
            return 1
        removed = clear_model_mode(getattr(args, "model", ""), clear_all=bool(getattr(args, "all", False)))
        print(f"MODEL_MODE_CLEARED|removed={removed}")
        return 0
    return 1


def cmd_evolve(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to universal autonomous dual-scope self-evolution classifier & sync ledger."""
    import self_evolve  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), args.evolve_cmd]
    if args.evolve_cmd == "classify":
        sub_argv.extend(["--desc", args.desc])
        if args.files:
            sub_argv.extend(["--files", *args.files])
        if args.json:
            sub_argv.append("--json")
    elif args.evolve_cmd == "sync":
        sub_argv.extend(["--scope", args.scope, "--title", args.title, "--summary", args.summary, "--commit", args.commit, "--agent", args.agent])
        if args.files:
            sub_argv.extend(["--files", *args.files])
    elif args.evolve_cmd == "sync-plugin":
        if args.apply:
            sub_argv.append("--apply")
        if args.global_dir:
            sub_argv.extend(["--global-dir", args.global_dir])
        if args.plugin_rel:
            sub_argv.extend(["--plugin-rel", args.plugin_rel])
        if args.json:
            sub_argv.append("--json")
    elif args.evolve_cmd == "sync-all":
        if args.apply:
            sub_argv.append("--apply")
        if args.push:
            sub_argv.append("--push")
        if args.no_test:
            sub_argv.append("--no-test")
        if args.commit:
            sub_argv.extend(["--commit", args.commit])
        if args.global_dir:
            sub_argv.extend(["--global-dir", args.global_dir])
        if args.plugin_rel:
            sub_argv.extend(["--plugin-rel", args.plugin_rel])
        for repo in (args.repo or []):
            sub_argv.extend(["--repo", repo])
        if args.json:
            sub_argv.append("--json")
    elif args.evolve_cmd == "register-workspace":
        if args.global_dir:
            sub_argv.extend(["--global-dir", args.global_dir])
        if args.json:
            sub_argv.append("--json")
    return self_evolve.main(sub_argv)


def cmd_learn(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to universal dynamic timeless error learner."""
    import error_learner  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), *(args.extra_args or [])]
    return error_learner.main(sub_argv)


def cmd_live_diag(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to universal uncached GitHub live diagnostics fetcher."""
    import live_diagnostics  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), *(args.extra_args or [])]
    return live_diagnostics.main(sub_argv)


def cmd_blueprint(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to Universal SOTA Blueprint Engine (`blueprint_engine.py`) or `wave_scheduler.py`."""
    extra = list(args.extra_args or [])
    if extra and extra[0] in ("schedule", "fanout", "stage1-3", "epoch-begin", "epoch-barrier", "lease", "checkpoint"):
        import wave_scheduler  # noqa: E402

        return wave_scheduler.main(["--workspace", str(ws_root), *extra])
    import blueprint_engine  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), *extra]
    return blueprint_engine.main(sub_argv)


def cmd_wave(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate directly to `wave_scheduler.py` (`schedule|fanout|stage1-3|epoch-begin|epoch-barrier|lease|checkpoint`)."""
    import wave_scheduler  # noqa: E402

    return wave_scheduler.main(["--workspace", str(ws_root), *(args.extra_args or [])])


def cmd_qa(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to Universal Adversarial QA Gate & Regression Contracts Engine (`qa_gate.py`)."""
    import qa_gate  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), *(args.extra_args or [])]
    return qa_gate.main(sub_argv)


def cmd_coherence(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to Zero-Static Ecosystem Coherence Engine (`coherence_engine.py`)."""
    import coherence_engine  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), *(args.extra_args or [])]
    return coherence_engine.main(sub_argv)


def cmd_token_audit(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to Zero-Accumulation Token-Tax & Overhead Auto-Governor (`token_governor.py audit`)."""
    import token_governor  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), "audit"]
    if getattr(args, "no_hooks", False):
        sub_argv.append("--no-hooks")
    if getattr(args, "json", False):
        sub_argv.append("--json")
    return token_governor.main(sub_argv)


def cmd_closeout(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate to 1-shot batched post-finish closeout (`token_governor.py closeout`)."""
    import token_governor  # noqa: E402

    sub_argv = ["--workspace", str(ws_root), "closeout"]
    for flag, attr in (
        ("--summary", "summary"),
        ("--next", "next"),
        ("--roadmap", "roadmap"),
        ("--wave", "wave"),
        ("--compound-note", "compound_note"),
    ):
        val = getattr(args, attr, "")
        if val:
            sub_argv.extend([flag, str(val)])
    for arch in getattr(args, "archive", None) or []:
        sub_argv.extend(["--archive", str(arch)])
    if getattr(args, "no_sync", False):
        sub_argv.append("--no-sync")
    if getattr(args, "no_verify", False):
        sub_argv.append("--no-verify")
    if getattr(args, "json", False):
        sub_argv.append("--json")
    return token_governor.main(sub_argv)


def cmd_host(args: argparse.Namespace, ws_root: Path) -> int:
    """Delegate multi-host boot, status, matrix audit, or tool classification to `host_context.py`."""
    import host_context  # noqa: E402

    sub_cmd = args.cmd.removeprefix("host-")
    sub_argv = ["--workspace", str(ws_root), sub_cmd]
    if sub_cmd == "classify":
        sub_argv.append(args.path)
        if args.category:
            sub_argv.extend(["--category", args.category])
        if args.purpose:
            sub_argv.extend(["--purpose", args.purpose])
        if args.entry_id:
            sub_argv.extend(["--id", args.entry_id])
        if args.apply:
            sub_argv.append("--apply")
    if getattr(args, "json", False):
        sub_argv.append("--json")
    return host_context.main(sub_argv)


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal Dynamic Fractal Plan Engine & Project Bootstrapper.")
    parser.add_argument("--dir", default=None, help="Artifact directory override.")
    parser.add_argument("--workspace", default=None, help="Workspace root override.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_triage = sub.add_parser("triage")
    p_triage.add_argument("--prompt", default="")
    p_triage.add_argument("--stdin", action="store_true")
    p_triage.add_argument("--json", action="store_true")

    p_boot = sub.add_parser("bootstrap", help="Scaffold universal memory/extra-requests/profile docs in a project.")
    p_boot.add_argument("--name", default="")
    p_boot.add_argument("--force", action="store_true")
    p_boot.add_argument("--embed-plugin", action="store_true")

    p_mig = sub.add_parser("migrate", help="Upgrade .agents/planner_profile.json on disk to current schema.")
    p_mig.add_argument("--dry-run", action="store_true")
    p_mig.add_argument("--no-backup", action="store_true")

    p_dm = sub.add_parser("docs-migrated", help="Validate converted project docs and stamp docs_format_version.")
    p_dm.add_argument("--force", action="store_true")

    p_init = sub.add_parser("init")
    p_init.add_argument("--goal", default="")
    p_init.add_argument("--tier", type=int, choices=[1, 2, 3], default=None)
    p_init.add_argument("--req", action="append")
    p_init.add_argument("--modules", default="")
    p_init.add_argument("--json", default="")

    p_add = sub.add_parser("add-module")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--domain", default="general")
    p_add.add_argument("--req", action="append")
    p_add.add_argument("--step", action="append")

    p_exp = sub.add_parser("expand-step")
    p_exp.add_argument("--module", required=True)
    p_exp.add_argument("--step", type=int, required=True)
    p_exp.add_argument("--substep", action="append", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("module")
    p_start.add_argument("step", type=int, nargs="?", default=None)
    p_start.add_argument("--substep", type=int, default=None)

    p_done = sub.add_parser("done")
    p_done.add_argument("module")
    p_done.add_argument("step", type=int, nargs="?", default=None)
    p_done.add_argument("--substep", type=int, default=None)
    p_done.add_argument("--evidence", default="")

    p_rv = sub.add_parser("req-verify")
    p_rv.add_argument("req_id")
    p_rv.add_argument("--evidence", required=True)
    p_rv.add_argument("--check", default="")

    p_pause = sub.add_parser("pause")
    p_pause.add_argument("--state", choices=["awaiting_user", "blocked"], default="awaiting_user")
    p_pause.add_argument("--reason", required=True)

    sub.add_parser("status")
    sub.add_parser("next")
    sub.add_parser("audit")

    p_fin = sub.add_parser("finish", help="Close all modules and verify all requirements in 1 turn.")
    p_fin.add_argument("--evidence", required=True)

    p_ho = sub.add_parser("handoff", help="Write or clear compact <=20L .agents/docs/HANDOFF.md.")
    p_ho.add_argument("--summary", default="")
    p_ho.add_argument("--next", default="")
    p_ho.add_argument("--roadmap", default="")
    p_ho.add_argument("--wave", default="")
    p_ho.add_argument("--archive", action="append")
    p_ho.add_argument("--compound-note", default="")
    p_ho.add_argument("--clear", action="store_true")

    p_rec = sub.add_parser("recon", help="Toggle or inspect read-only forensic recon mode.")
    rec_sub = p_rec.add_subparsers(dest="recon_cmd", required=True)
    p_rec_start = rec_sub.add_parser("start")
    p_rec_start.add_argument("--scope", default="")
    rec_sub.add_parser("stop")
    rec_sub.add_parser("status")

    p_undo = sub.add_parser("undo-turn", help="Preview or revert code edits from the active turn.")
    p_undo.add_argument("--dry-run", action="store_true")

    p_st = sub.add_parser("stats", help="Show or reset local plugin telemetry stats.")
    p_st.add_argument("--reset", action="store_true")

    p_diag = sub.add_parser("diagnostics", aliases=["diag"], help="Inspect or clear zero-token plugin anomaly logs.")
    p_diag.add_argument("--clear", action="store_true")
    p_diag.add_argument("--kind", default="")
    p_diag.add_argument("--limit", type=int, default=25)
    p_diag.add_argument("--raw", action="store_true")

    p_mm = sub.add_parser("model-mode", help="Inspect, persist, or clear cached auto model-mode verdicts.")
    mm_sub = p_mm.add_subparsers(dest="mm_cmd", required=True)
    p_mm_set = mm_sub.add_parser("set")
    p_mm_set.add_argument("model")
    p_mm_set.add_argument("mode", choices=["lean", "strict"])
    p_mm_set.add_argument("--why", default="")
    p_mm_set.add_argument("--source", choices=["ai", "user", "native_plan_tool"], default="ai")
    p_mm_get = mm_sub.add_parser("get")
    p_mm_get.add_argument("model")
    mm_sub.add_parser("list")
    p_mm_clr = mm_sub.add_parser("clear")
    p_mm_clr.add_argument("model", nargs="?", default="")
    p_mm_clr.add_argument("--all", action="store_true")

    p_evo = sub.add_parser("evolve", help="Classify improvements (project-only / plugin-universal / dual) or record dual sync ledger entries.")
    evo_sub = p_evo.add_subparsers(dest="evolve_cmd", required=True)
    p_evo_cls = evo_sub.add_parser("classify")
    p_evo_cls.add_argument("--desc", required=True)
    p_evo_cls.add_argument("--files", nargs="*", default=[])
    p_evo_cls.add_argument("--json", action="store_true")
    p_evo_sync = evo_sub.add_parser("sync")
    p_evo_sync.add_argument("--scope", choices=["project", "plugin", "dual"], required=True)
    p_evo_sync.add_argument("--title", required=True)
    p_evo_sync.add_argument("--summary", required=True)
    p_evo_sync.add_argument("--files", nargs="*", default=[])
    p_evo_sync.add_argument("--commit", default="pending-push")
    p_evo_sync.add_argument("--agent", default="AI")
    p_evo_psync = evo_sub.add_parser("sync-plugin")
    p_evo_psync.add_argument("--apply", action="store_true")
    p_evo_psync.add_argument("--global-dir", default="")
    p_evo_psync.add_argument("--plugin-rel", default=".agents/plugins/dynamic-fractal-planner")
    p_evo_psync.add_argument("--json", action="store_true")
    p_evo_all = evo_sub.add_parser("sync-all")
    p_evo_all.add_argument("--apply", action="store_true")
    p_evo_all.add_argument("--push", action="store_true")
    p_evo_all.add_argument("--no-test", action="store_true")
    p_evo_all.add_argument("--commit", default="")
    p_evo_all.add_argument("--global-dir", default="")
    p_evo_all.add_argument("--plugin-rel", default=".agents/plugins/dynamic-fractal-planner")
    p_evo_all.add_argument("--repo", action="append", default=[])
    p_evo_all.add_argument("--json", action="store_true")
    p_evo_reg = evo_sub.add_parser("register-workspace")
    p_evo_reg.add_argument("--global-dir", default="")
    p_evo_reg.add_argument("--json", action="store_true")

    p_learn = sub.add_parser("learn", help="Run universal timeless build-error learner (forwards remaining args).")
    p_learn.add_argument("extra_args", nargs=argparse.REMAINDER)

    p_ldiag = sub.add_parser("live-diag", help="Fetch uncached live CI & runtime diagnostics from GitHub (forwards remaining args).")
    p_ldiag.add_argument("extra_args", nargs=argparse.REMAINDER)

    p_bp = sub.add_parser(
        "blueprint",
        help="Universal SOTA Blueprint Engine (classify|init|lint|score|status|list|handoff|hygiene|hub|compact|match|transition|schedule|fanout|stage1-3).",
    )
    p_bp.add_argument("extra_args", nargs=argparse.REMAINDER)

    p_wave = sub.add_parser(
        "wave",
        help="Deterministic Multi-Wave DAG Epoch Scheduler & Lease Guard (schedule|fanout|stage1-3|epoch-begin|epoch-barrier|lease|checkpoint).",
    )
    p_wave.add_argument("extra_args", nargs=argparse.REMAINDER)

    p_qa = sub.add_parser(
        "qa",
        help="Universal Adversarial QA Gate & Frozen Regression Contracts (status|verify-contracts|gate|prompt|add-contract).",
    )
    p_qa.add_argument("extra_args", nargs=argparse.REMAINDER)

    p_coh = sub.add_parser(
        "coherence",
        help="Zero-Static Ecosystem Coherence & Meta-Governance Engine (check|sync|audit|gov-audit|canary|deprecate|fleet|prompt).",
    )
    p_coh.add_argument("extra_args", nargs=argparse.REMAINDER)

    p_tok = sub.add_parser("token-audit", help="Measure hook payload & always-loaded doc token overhead (`--no-hooks`, `--json`).")
    p_tok.add_argument("--no-hooks", action="store_true")
    p_tok.add_argument("--json", action="store_true")

    p_cls = sub.add_parser("closeout", help="1-shot batched post-finish closeout: sync + static verify + gov-audit + token-audit + handoff.")
    p_cls.add_argument("--summary", default="")
    p_cls.add_argument("--next", default="")
    p_cls.add_argument("--roadmap", default="")
    p_cls.add_argument("--wave", default="")
    p_cls.add_argument("--archive", action="append")
    p_cls.add_argument("--compound-note", default="")
    p_cls.add_argument("--no-sync", action="store_true")
    p_cls.add_argument("--no-verify", action="store_true")
    p_cls.add_argument("--json", action="store_true")

    for h_cmd, h_help in (
        ("host-boot", "Run unified host-aware session boot (Antigravity local vs AI Studio cloud)."),
        ("host-status", "Show detected host capabilities, matrix view, and baton state."),
        ("host-audit", "Audit .agents/host_matrix.json for unclassified or stale tools."),
    ):
        p_h = sub.add_parser(h_cmd, help=h_help)
        p_h.add_argument("--json", action="store_true")

    p_hcls = sub.add_parser("host-classify", help="Classify and optionally register a tool path in .agents/host_matrix.json.")
    p_hcls.add_argument("path")
    p_hcls.add_argument("--category", default="")
    p_hcls.add_argument("--purpose", default="")
    p_hcls.add_argument("--id", dest="entry_id", default="")
    p_hcls.add_argument("--apply", action="store_true")
    p_hcls.add_argument("--json", action="store_true")

    args = parser.parse_args()
    ws_root = resolve_workspace_root(args.workspace)
    if args.cmd == "triage":
        text = sys.stdin.read() if args.stdin else args.prompt
        res = analyze_prompt(text, ws_root)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"Tier {res['tier']} ({res['tier_name']}) | Breadth~{res['breadth_est']} | Depth={res['depth_level']}")
            for m in res["recommended_modules"]:
                print(f"  - {m['id']} ({m['domain']}): {m['title']}")
        return
    if args.cmd == "bootstrap":
        cmd_bootstrap(args, ws_root)
        return
    if args.cmd == "migrate":
        raise SystemExit(cmd_migrate(args, ws_root))
    if args.cmd == "docs-migrated":
        raise SystemExit(cmd_docs_migrated(args, ws_root))
    if args.cmd == "handoff":
        raise SystemExit(cmd_handoff(args, ws_root))
    if args.cmd == "stats":
        raise SystemExit(cmd_stats(args))
    if args.cmd in ("diagnostics", "diag"):
        raise SystemExit(cmd_diagnostics(args))
    if args.cmd == "model-mode":
        raise SystemExit(cmd_model_mode(args, ws_root))
    if args.cmd == "evolve":
        raise SystemExit(cmd_evolve(args, ws_root))
    if args.cmd == "learn":
        raise SystemExit(cmd_learn(args, ws_root))
    if args.cmd == "live-diag":
        raise SystemExit(cmd_live_diag(args, ws_root))
    if args.cmd == "blueprint":
        raise SystemExit(cmd_blueprint(args, ws_root))
    if args.cmd == "wave":
        raise SystemExit(cmd_wave(args, ws_root))
    if args.cmd == "qa":
        raise SystemExit(cmd_qa(args, ws_root))
    if args.cmd == "coherence":
        raise SystemExit(cmd_coherence(args, ws_root))
    if args.cmd == "token-audit":
        raise SystemExit(cmd_token_audit(args, ws_root))
    if args.cmd == "closeout":
        raise SystemExit(cmd_closeout(args, ws_root))
    if args.cmd in ("host-boot", "host-status", "host-audit", "host-classify"):
        raise SystemExit(cmd_host(args, ws_root))

    art_dir = resolve_artifact_dir(args.dir)
    if args.cmd == "init":
        cmd_init(args, art_dir, ws_root)
    elif args.cmd == "add-module":
        cmd_add_module(args, art_dir, ws_root)
    elif args.cmd == "expand-step":
        cmd_expand_step(args, art_dir)
    elif args.cmd == "start":
        cmd_start(args, art_dir)
    elif args.cmd == "done":
        cmd_done(args, art_dir)
    elif args.cmd == "req-verify":
        cmd_req_verify(args, art_dir)
    elif args.cmd == "pause":
        cmd_pause(args, art_dir)
    elif args.cmd in ("status", "next"):
        _, state_file, _ = state_paths(art_dir)
        print(format_compact_status(load_state(state_file)))
    elif args.cmd == "audit":
        raise SystemExit(cmd_audit(art_dir))
    elif args.cmd == "finish":
        raise SystemExit(cmd_finish(args, art_dir))
    elif args.cmd == "undo-turn":
        raise SystemExit(cmd_undo_turn(args, art_dir, ws_root))
    elif args.cmd == "recon":
        raise SystemExit(cmd_recon(args, art_dir, ws_root))


if __name__ == "__main__":
    main()
