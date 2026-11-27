#!/usr/bin/env python3
"""Universal SessionStart hook: compact pipe-delimited live git/version/HANDOFF/memory status."""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def run_git(repo: Path, args: list[str]) -> str:
    if not (repo / ".git").exists():
        return ""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        print("{}")
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
            if p not in sys.path:
                sys.path.insert(0, p)

        import planner_state as _ps  # noqa: E402

        repo = _ps.resolve_repo_root(payload)

        # C7: Run higher-version plugin sync BEFORE bootstrap/auto_upgrade_workspace, and re-exec or reload
        # modules when `.py` files change so `auto_upgrade_workspace()` NEVER runs on stale in-memory code.
        sync_notices: list[str] = []
        reexec_notice = os.environ.get("PLANNER_REEXEC_SYNC_LINE", "").strip()
        if reexec_notice:
            sync_notices.append(reexec_notice)

        if _ps.is_real_project_root(repo):
            early_profile, _ = _ps.load_project_profile_with_warnings(repo)
            early_evo = early_profile.get("plugin_evolution") or {}
            if isinstance(early_evo, dict) and early_evo.get("enabled"):
                plug_rel = str(early_evo.get("plugin_path") or ".agents/plugins/dynamic-fractal-planner").strip()
                if (repo / plug_rel / "plugin.json").is_file():
                    try:
                        import self_evolve  # noqa: E402

                        auto_sync = bool(early_evo.get("auto_sync_higher_version", True))
                        chk = self_evolve.sync_plugin_versions(repo, plugin_rel=plug_rel, apply=False)
                        direction = chk.get("direction")
                        done: dict[str, Any] = {}
                        if direction == "embedded->global":
                            if auto_sync:
                                done = self_evolve.sync_plugin_versions(repo, plugin_rel=plug_rel, apply=True)
                                sync_notices.append(
                                    f"PLUGIN_SYNCED|embedded->global|v{chk.get('global_version') or '0.0.0'}->v{done.get('embedded_version')}"
                                )
                            else:
                                sync_notices.append(
                                    f"PLUGIN_VERSION_SKEW|embedded=v{chk.get('embedded_version')}>global=v{chk.get('global_version')}|ACT:run `plan_tool.py evolve sync-plugin --apply`"
                                )
                        elif direction == "global->embedded":
                            if auto_sync:
                                done = self_evolve.sync_plugin_versions(repo, plugin_rel=plug_rel, apply=True)
                                sync_notices.append(
                                    f"PLUGIN_SYNCED|global->embedded|v{chk.get('embedded_version') or '0.0.0'}->v{done.get('embedded_version')}"
                                )
                            else:
                                sync_notices.append(
                                    f"PLUGIN_UPGRADE_AVAILABLE|global=v{chk.get('global_version')}>embedded=v{chk.get('embedded_version')}|ACT:run `plan_tool.py evolve sync-plugin --apply`"
                                )

                        copied_files = done.get("copied") or []
                        py_changed = bool(done.get("applied")) and any(str(c).endswith(".py") for c in copied_files)
                        if py_changed:
                            dst_key = "embedded_dir" if direction == "global->embedded" else "global_dir"
                            dst_raw = str(done.get(dst_key) or "").strip()
                            dst_resolved = Path(dst_raw).resolve() if dst_raw else None
                            if (
                                dst_resolved is not None
                                and dst_resolved == PLUGIN_ROOT.resolve()
                                and not os.environ.get("PLANNER_REEXEC")
                            ):
                                child_env = os.environ.copy()
                                child_env["PLANNER_REEXEC"] = "1"
                                if sync_notices:
                                    child_env["PLANNER_REEXEC_SYNC_LINE"] = sync_notices[-1]
                                proc = subprocess.run(
                                    [sys.executable, "-B", str(Path(__file__).resolve()), *sys.argv[1:]],
                                    input=json.dumps(payload, ensure_ascii=False),
                                    text=True,
                                    capture_output=True,
                                    env=child_env,
                                    timeout=15,
                                    check=False,
                                )
                                if proc.returncode == 0 and proc.stdout.strip():
                                    print(proc.stdout.strip())
                                    return
                            for mod_name in (
                                "schema_migrations",
                                "planner_state",
                                "blueprint_hub",
                                "blueprint_engine",
                                "host_context",
                                "self_evolve",
                            ):
                                mod_obj = sys.modules.get(mod_name)
                                if mod_obj is not None:
                                    try:
                                        importlib.reload(mod_obj)
                                    except Exception:
                                        pass
                            import planner_state as _ps  # noqa: E402
                    except Exception:
                        pass

        BOOTSTRAP_REQUIRED_FILES = _ps.BOOTSTRAP_REQUIRED_FILES
        auto_upgrade_workspace = _ps.auto_upgrade_workspace
        bootstrap_workspace = _ps.bootstrap_workspace
        detect_doc_mcps = _ps.detect_doc_mcps
        eval_conditions = _ps.eval_conditions
        get_paths = _ps.get_paths
        get_pending_extra_requests = _ps.get_pending_extra_requests
        is_real_project_root = _ps.is_real_project_root
        load_json = _ps.load_json
        load_project_profile_with_warnings = _ps.load_project_profile_with_warnings
        resolve_planner_mode_info = _ps.resolve_planner_mode_info

        auto_initialized: list[str] = []
        if is_real_project_root(repo) and any(not (repo / rel).exists() for rel in BOOTSTRAP_REQUIRED_FILES):
            auto_initialized, _ = bootstrap_workspace(repo)

        upgrade_info: dict[str, Any] = {}
        if is_real_project_root(repo) and (repo / ".agents").is_dir():
            upgrade_info = auto_upgrade_workspace(repo)

        if is_real_project_root(repo):
            try:
                import self_evolve  # noqa: E402

                self_evolve.register_workspace(repo)
            except Exception:
                pass

        profile, profile_warnings = load_project_profile_with_warnings(repo)
        _, task_md, _, hook_file = get_paths(payload)

        proj_name = profile.get("project_name") or repo.name
        mode, mode_src, model_name = resolve_planner_mode_info(profile, payload)
        if mode_src == "auto:unseen" and model_name:
            mode_tag = f"{mode}(auto:{model_name}|unseen)"
        elif mode_src.startswith("auto:") and model_name and model_name.lower() != "auto":
            mode_tag = f"{mode}(auto:{model_name})"
        else:
            mode_tag = mode
        st_lines = run_git(repo, ["status", "-sb"]).splitlines()
        head_ln = st_lines[0][3:] if st_lines and st_lines[0].startswith("## ") else ""
        if head_ln.startswith("No commits yet on "):
            branch = head_ln[18:].strip()
        elif head_ln.startswith("HEAD (no branch)"):
            branch = ""
        else:
            branch = head_ln.split("...", 1)[0].split(" ", 1)[0]
        recent_commits = run_git(repo, ["log", "-n", "3", "--oneline"])
        dirty_lines = [l for l in st_lines[1:] if l.strip()] if head_ln else [l for l in st_lines if l.strip()]

        tree_state = "clean" if not dirty_lines else f"{len(dirty_lines)} dirty"
        head_meta = f"[{proj_name}|mode={mode_tag}" + (f"|branch={branch}|tree={tree_state}]" if branch else f"|ws={repo}]")
        lines = [head_meta]
        try:
            import host_context  # noqa: E402

            lines.extend(host_context.format_session_start_lines(repo))
        except Exception:
            pass

        session_cfg: dict[str, Any] = profile.get("session") or {}

        ver_parts: list[str] = []
        for vf in session_cfg.get("version_files") or []:
            rel_p = vf.get("path") or ""
            pat = vf.get("regex") or ""
            label = vf.get("label") or rel_p
            if rel_p and pat:
                txt = read_text(repo / rel_p)
                m = re.search(pat, txt)
                ver_parts.append(f"{label}={m.group(1).strip() if m else '?'}")
        if ver_parts:
            lines.append("VERSIONS|" + " | ".join(ver_parts))

        if recent_commits:
            lines.append("COMMITS|" + " ; ".join(c[:78] for c in recent_commits.splitlines()))
        if dirty_lines:
            preview = ", ".join(l.strip() for l in dirty_lines[:6])
            if len(dirty_lines) > 6:
                preview += f" (+{len(dirty_lines) - 6})"
            lines.append(f"DIRTY|{preview}")

        for note in session_cfg.get("env_notes") or []:
            if eval_conditions(note.get("conditions"), repo) and note.get("text"):
                lines.append(f"ENV|{note['text']}")

        for wp in session_cfg.get("watch_paths") or []:
            raw_path = wp.get("path") or ""
            if not raw_path:
                continue
            target = Path(raw_path).expanduser() if raw_path.startswith(("/", "~")) else (repo / raw_path)
            if target.exists():
                label = wp.get("label") or "watch"
                if wp.get("show_size_mb") and target.is_file():
                    mb = target.stat().st_size / (1024 * 1024)
                    lines.append(f"WATCH|{label}: `{target}` ({mb:.1f} MB)")
                else:
                    lines.append(f"WATCH|{label}: `{target}`")

        handoff_file = repo / ".agents" / "docs" / "HANDOFF.md"
        if handoff_file.is_file():
            h_lines = [ln for ln in read_text(handoff_file).splitlines()[:20] if ln.strip()]
            if h_lines:
                roadmap_ref = ""
                wave_ref = ""
                archive_refs: list[str] = []
                body_lines: list[str] = []
                for ln in h_lines:
                    s_ln = ln.strip()
                    if s_ln.startswith("ROADMAP|"):
                        roadmap_ref = s_ln[8:].strip()
                    elif s_ln.startswith("WAVE|"):
                        wave_ref = s_ln[5:].strip()
                    elif s_ln.startswith("ARCHIVE|"):
                        archive_refs.append(s_ln[8:].split("(", 1)[0].strip())
                    else:
                        m_rm = re.match(r"^[-*]?\s*\*\*Active Roadmap:\*\*\s*`?([^`\s(]+)`?", s_ln, re.I)
                        m_wv = re.match(r"^[-*]?\s*\*\*(?:Next Wave Ready|Last Completed):\*\*\s*(.+)$", s_ln, re.I)
                        if m_rm and not roadmap_ref:
                            roadmap_ref = m_rm.group(1).strip()
                        elif m_wv and not wave_ref:
                            wave_ref = m_wv.group(1).strip()[:80]
                        body_lines.append(ln)
                if roadmap_ref or wave_ref:
                    bridge = f"WAVE_BRIDGE|roadmap={roadmap_ref or '-'}|wave={wave_ref or '-'}"
                    if archive_refs:
                        bridge += "|archive=" + ",".join(a for a in archive_refs if a)[:120]
                    lines.append(bridge)
                    actionable = [
                        ln.strip()
                        for ln in body_lines
                        if ln.strip()
                        and not ln.strip().startswith("#")
                        and not ln.strip().startswith("ts=")
                    ]
                    compact_body = " | ".join(actionable)
                    if len(compact_body) > 220:
                        compact_body = compact_body[:217] + "..."
                    if compact_body:
                        lines.append("HANDOFF|" + compact_body)
                else:
                    compact_body = "\n".join(h_lines)
                    if len(compact_body) > 420:
                        compact_body = compact_body[:417] + "..."
                    lines.append("HANDOFF|\n" + compact_body)

        hook_state = load_json(hook_file)
        rec = hook_state.get("recon")
        if isinstance(rec, dict) and rec.get("active"):
            r_scope = str(rec.get("scope") or "full")
            lines.append(f"RECON|ACTIVE|scope={r_scope}|taxonomy=[BUG]|[UNWIRED]|[STRUCTURAL]|[UPGRADE] @ path:line")

        has_agents_md = (repo / "AGENTS.md").is_file()
        mem_candidates = []
        for rel in (
            ".agents/docs/PLANS_HUB.md",
            ".agents/docs/PROJECT_HISTORY.md",
            ".agents/docs/SAVED_IDEAS.md",
            ".agents/docs/BUILD_LESSONS.md",
            ".agents/docs/EXTERNAL_AI_SYNC.md",
            *(session_cfg.get("extra_docs") or []),
        ):
            if rel not in mem_candidates and (repo / rel).is_file():
                mem_candidates.append(rel)
        if mem_candidates and not has_agents_md:
            lines.append("DOCS|" + ",".join(f"`{m}`" for m in mem_candidates))

        try:
            import blueprint_engine  # noqa: E402
            from schema_migrations import resolve_blueprint_config  # noqa: E402

            bp_cfg = resolve_blueprint_config(profile)
            hub_rel = str(bp_cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
            if (repo / hub_rel).is_file():
                h_res = blueprint_engine.hygiene_audit(repo, profile=profile)
                c = h_res.get("counts") or {}
                sync_tag = "OK" if h_res.get("hub_in_sync") else "DRIFT"
                iss_n = len(h_res.get("issues") or [])
                pol_tag = "|bp_default=ON" if bp_cfg.get("require_by_default") else ""
                iss_tag = f"|issues={iss_n}" if iss_n else ""
                arch_n = int(c.get("archived_done") or 0)
                arch_tag = f",arch={arch_n}" if arch_n > 0 else ""
                lines.append(
                    f"PLANS_HUB|bp={c.get('total', 0)}(done={c.get('done', 0)},partial={c.get('partial', 0)},active={c.get('active', 0)},def={c.get('deferred', 0)}{arch_tag})"
                    f"|sync={sync_tag}{iss_tag}{pol_tag}"
                )
        except Exception:
            pass

        evo_cfg = profile.get("plugin_evolution") or {}
        if isinstance(evo_cfg, dict) and evo_cfg.get("enabled") and not has_agents_md:
            plug_sync_rel = str(evo_cfg.get("plugin_sync_doc") or ".agents/plugins/dynamic-fractal-planner/.agents/docs/EXTERNAL_AI_SYNC.md")
            lines.append(
                f"EVOLVE|AUTO_DUAL_SCOPE: reusable -> plugin (`{plug_sync_rel}`) && app-only -> host"
            )

        hist_p = repo / ".agents" / "docs" / "PROJECT_HISTORY.md"
        if hist_p.is_file():
            hist_len = len(read_text(hist_p).splitlines())
            if hist_len > 150:
                lines.append(f"MEMORY_WEIGHT|`PROJECT_HISTORY.md`={hist_len}L (>150L) -> archive old entries to `.agents/docs/archive/`.")

        extra_rel, pending_extras = get_pending_extra_requests(repo, profile)
        extra_p = repo / extra_rel
        if extra_p.is_file():
            if pending_extras:
                lines.append(
                    f"ATTENTION — Pending extra requests in `{extra_rel}` ({len(pending_extras)} line(s)) -> implement & clear before commit/ship!"
                )
            m_done = re.search(
                r"^#{1,3}\s+(?:Completed Requests|Conclu[ií]dos|Completed)(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#{1,3}\s+|\Z)",
                read_text(extra_p),
                flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
            )
            if m_done:
                done_items = [l for l in m_done.group(1).splitlines() if l.strip() and not l.strip().lower().startswith(("(no completed", "(none", "<!--"))]
                if len(done_items) > 15:
                    lines.append(f"MEMORY_WEIGHT|`{extra_rel}` completed={len(done_items)} (>15) -> archive old items.")

        if auto_initialized:
            lines.append("Auto-initialized project docs/profile: " + ", ".join(f"`{p}`" for p in auto_initialized))
        up_parts: list[str] = []
        if upgrade_info.get("profile_migrated"):
            old_v, new_v = upgrade_info["profile_migrated"]
            up_parts.append(f"profile=v{old_v}->v{new_v}")
        if upgrade_info.get("plugin_upgraded"):
            old_pv, new_pv = upgrade_info["plugin_upgraded"]
            up_parts.append(f"plugin=v{old_pv}->v{new_pv}")
        if upgrade_info.get("boilerplate_upgraded"):
            up_parts.append("templates=" + ",".join(upgrade_info["boilerplate_upgraded"]))
        if up_parts:
            lines.append("AUTO_UPGRADED|" + "|".join(up_parts))
        if upgrade_info.get("plugin_downgrade_blocked"):
            stored_pv, cur_pv = upgrade_info["plugin_downgrade_blocked"]
            lines.append(f"PLUGIN_DOWNGRADE_GUARD|workspace=v{stored_pv}>installed=v{cur_pv}|keeping workspace profile intact")

        if sync_notices:
            lines.extend(sync_notices)

        pending_doc_mig = upgrade_info.get("docs_needing_ai_conversion") or []
        if pending_doc_mig:
            plan_cli_mig = PLUGIN_ROOT / "scripts" / "plan_tool.py"
            cur_dv = int(upgrade_info.get("current_docs_format") or 0)
            tgt_dv = int(upgrade_info.get("target_docs_format") or 1)
            lines.append(
                f"DOCS_AUTO_MIGRATE|v{cur_dv}->v{tgt_dv}|files={','.join(pending_doc_mig)}|"
                f"ACT:losslessly rewrite listed docs to symbolic telegraphic EN -> `python3 {plan_cli_mig} docs-migrated`"
            )
        for w in profile_warnings:
            lines.append(f"Schema warning: {w}")

        doc_mcps = detect_doc_mcps(profile)
        doc_mcp_parts: list[str] = []
        if doc_mcps.get("gdk"):
            doc_mcp_parts.append("google-developer-knowledge(search_documents/answer_query)")
        if doc_mcps.get("context7"):
            doc_mcp_parts.append("context7(resolve-library-id->query-docs)")
        if doc_mcp_parts:
            lines.append("DOC_MCP|" + " + ".join(doc_mcp_parts))

        plan_cli = PLUGIN_ROOT / "scripts" / "plan_tool.py"
        verify_cli = PLUGIN_ROOT / "scripts" / "verify_universal.py"
        try:
            plan_ref = str(plan_cli.resolve().relative_to(repo.resolve()))
            verify_ref = str(verify_cli.resolve().relative_to(repo.resolve()))
        except Exception:
            plan_ref = str(plan_cli)
            verify_ref = str(verify_cli)

        try:
            in_plugin_repo = repo.resolve() == PLUGIN_ROOT.resolve()
        except Exception:
            in_plugin_repo = False
        if in_plugin_repo:
            from plugin_diagnostics import summarize_anomalies  # noqa: E402

            diag_total, diag_unique = summarize_anomalies()
            if diag_total > 0:
                lines.append(
                    f"DIAGNOSTICS|{diag_total} anomaly(ies) ({diag_unique} unique) in `.diagnostics/anomalies.jsonl` -> run `python3 {plan_ref} diag` to inspect"
                )

        try:
            import coherence_engine  # noqa: E402

            coh_digest = coherence_engine.format_session_start_digest(repo, profile=profile)
            if coh_digest:
                lines.append(coh_digest[:40])
        except Exception:
            pass

        if not has_agents_md:
            lines.append(f"TOOLS|plan=`python3 {plan_ref}`|verify=`python3 {verify_ref}`|board=`{task_md}`")

        from schema_migrations import resolve_token_budget_config  # noqa: E402

        tb_cfg = resolve_token_budget_config(profile)
        default_ss_cap = int(tb_cfg.get("session_start_max_chars") or 1200)
        bloat_limit = int(os.environ.get("PLANNER_INJECTION_BLOAT_CHARS") or default_ss_cap)
        msg_text = "\n".join(lines)
        if len(msg_text) > bloat_limit:
            trimmed: list[str] = []
            for ln in lines:
                if ln.startswith("COMMITS|"):
                    parts = [p.strip()[:48] for p in ln[8:].split(";")[:2] if p.strip()]
                    ln = "COMMITS|" + " ; ".join(parts)
                elif ln.startswith("DIRTY|") and len(ln) > 80:
                    ln = f"DIRTY|{len(dirty_lines)} dirty file(s)"
                elif ln.startswith("WAVE_BRIDGE|") and len(ln) > 160:
                    rm_s = (roadmap_ref.split("(", 1)[0].strip() or "-")[:60]
                    wv_s = (wave_ref or "-")[:70]
                    ln = f"WAVE_BRIDGE|roadmap={rm_s}|wave={wv_s}"
                elif ln.startswith("HANDOFF|") and len(ln) > 170:
                    ln = ln[:167] + "..."
                elif ln.startswith("DOCS|") and len(ln) > 95:
                    ln = "DOCS|" + ",".join(Path(m).name for m in mem_candidates)
                elif ln.startswith("EVOLVE|"):
                    ln = "EVOLVE|AUTO_DUAL_SCOPE: reusable->plugin && app->host"
                trimmed.append(ln)
            lines = trimmed
            msg_text = "\n".join(lines)
            if len(msg_text) > bloat_limit:
                trimmed2: list[str] = []
                for ln in lines:
                    if ln.startswith(("ENV|", "WATCH|")) and len(ln) > 95:
                        ln = ln[:92] + "..."
                    elif ln.startswith("HANDOFF|") and len(ln) > 120:
                        ln = ln[:117] + "..."
                    trimmed2.append(ln)
                lines = trimmed2
                msg_text = "\n".join(lines)
        if profile_warnings:
            from plugin_diagnostics import record_anomaly  # noqa: E402

            record_anomaly("profile_warning", "planner_session_start.py", "schema:warning", "; ".join(profile_warnings), repo=repo)
        if len(msg_text) > bloat_limit:
            from plugin_diagnostics import record_anomaly  # noqa: E402

            largest_prefix = max(lines, key=len).split("|", 1)[0][:32]
            record_anomaly(
                "injection_bloat",
                "planner_session_start.py",
                "session_start:bloat",
                f"chars={len(msg_text)} lines={len(lines)} top={largest_prefix}",
                repo=repo,
            )

        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": msg_text}}]}, ensure_ascii=False))
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_session_start.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_session_start.py", exc)
        except Exception:
            pass
        print("{}")
