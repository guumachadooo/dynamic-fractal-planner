#!/usr/bin/env python3
"""Zero-Static Ecosystem Coherence Engine & Hub Generator (DFP v2.22.0, <=450L, <=200ms)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from schema_migrations import compare_semver, migrate_profile  # noqa: E402

COHERENCE_HUB_REL = ".agents/docs/COHERENCE_HUB.md"
VALID_DEP_STAGES = frozenset({"WARN", "GRACE", "TOMBSTONE"})
_AUTO_BLOCK_RE = re.compile(
    r"(<!--AUTO:START:([A-Za-z0-9_.-]+)(?::sha256=([0-9a-fA-F]{8,64}))?-->)\n(.*?)\n(<!--AUTO:END:\2-->)",
    re.DOTALL,
)
_DEP_ROW_RE = re.compile(r"^DEP\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|owner=([^|]+)\|expires_ver=([^\s|]+)")


def _norm(p: str) -> str:
    s = str(p or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="ignore")) if path.is_file() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _plugin_dir_for(repo: Path) -> Path:
    if (repo / "plugin.json").is_file():
        return repo
    emb = repo / ".agents" / "plugins" / "dynamic-fractal-planner"
    return emb if (emb / "plugin.json").is_file() else _PLUGIN_ROOT


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _introspect_cli_cmds(pdir: Path) -> list[str]:
    pt = pdir / "scripts" / "plan_tool.py"
    if not pt.is_file():
        return []
    txt = pt.read_text(encoding="utf-8", errors="ignore")
    found = re.findall(r'\bsub\.add_parser\(\s*"([a-z0-9-]+)"', txt)
    for hc in ("host-boot", "host-status", "host-audit"):
        if f'"{hc}"' in txt and hc not in found:
            found.append(hc)
    seen: set[str] = set()
    return [c for c in found if not (c in seen or seen.add(c))]


def _introspect_verify_checks(pdir: Path) -> list[str]:
    vu = pdir / "scripts" / "verify_universal.py"
    return re.findall(r'@verify_check\(\s*"([a-z0-9_]+)"', vu.read_text(encoding="utf-8", errors="ignore")) if vu.is_file() else []


def _introspect_hooks_coverage(pdir: Path) -> dict[str, Any]:
    raw = json.dumps(_read_json(pdir / "hooks.json"))
    hdir = pdir / "hooks"
    disk = sorted(p.name for p in hdir.glob("*.py") if p.name != "planner_state.py") if hdir.is_dir() else []
    return {"disk": len(disk), "unwired": [fn for fn in disk if fn not in raw]}


def render_auto_block_body(block_id: str, repo: Path) -> str | None:
    """Deterministically render body for a known `<!--AUTO:START:<id>...-->` block."""
    pdir = _plugin_dir_for(repo)
    pver = str(_read_json(pdir / "plugin.json").get("version") or "0.0.0")
    dom = _read_json(pdir / "scripts" / "domains.json")
    if block_id == "dfp-cli-domains":
        cmds = "|".join(_introspect_cli_cmds(pdir))
        t_doms = ",".join(sorted((dom.get("triage_domains") or {}).keys()))
        return f"AUTO_SSOT|plugin=v{pver}|triage_domains={t_doms}|verify_checks={len(_introspect_verify_checks(pdir))}\nAUTO_CLI|`plan_tool.py`: `{cmds}`"
    if block_id == "sota-blueprint-domains":
        b_doms = ",".join(sorted((dom.get("blueprint_domains") or {}).keys()))
        return f"AUTO_SSOT|plugin=v{pver}|blueprint_domains={b_doms}|formats=mini,compact,full|ssot=scripts/domains.json"
    if block_id == "cavern-ecosystem-ssot":
        prof = migrate_profile(_read_json(repo / ".agents" / "planner_profile.json"))[0]
        hm = _read_json(repo / ".agents" / "host_matrix.json")
        fed = _read_json(repo / ".agents" / "federation.json")
        repos_s = ",".join(sorted((fed.get("repos") or {}).keys())) or "none"
        return (
            f"AUTO_SSOT|plugin=v{prof.get('plugin_version') or pver}|schema=v{prof.get('schema_version', 5)}"
            f"|host_matrix_strict={str(bool(hm.get('strict_coverage'))).lower()}|entries={len(hm.get('entries') or [])}"
            f"|fed_repos={repos_s}\n"
            f"AUTO_CANONICAL_CLI|resume=`flight_recorder.py resume`|learn=`plan_tool.py learn`"
            f"|coherence=`plan_tool.py coherence check --local`|hub=`.agents/docs/COHERENCE_HUB.md`"
        )
    return None


def _candidate_auto_docs(repo: Path) -> list[Path]:
    pdir = _plugin_dir_for(repo)
    out, seen = [], set()
    for p in (pdir / "skills" / "dynamic-planner" / "SKILL.md", pdir / "skills" / "sota-blueprint" / "SKILL.md", repo / ".agents" / "docs" / "EXTERNAL_AI_ONBOARDING.md"):
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return out


def check_auto_blocks(repo: Path) -> dict[str, Any]:
    """Read-only SHA-256 parity audit of all `<!--AUTO:START..END-->` blocks."""
    checked, drifted = 0, []
    for doc in _candidate_auto_docs(repo):
        rel = doc.relative_to(repo).as_posix() if repo.resolve() in doc.resolve().parents else doc.name
        for m in _AUTO_BLOCK_RE.finditer(doc.read_text(encoding="utf-8", errors="ignore")):
            bid, stored_sha, body = m.group(2), (m.group(3) or "").lower(), m.group(4)
            expected = render_auto_block_body(bid, repo)
            if expected is None:
                continue
            checked += 1
            exp_sha = _sha12(expected)
            if stored_sha != exp_sha or body != expected:
                drifted.append(f"{rel}#{bid} (stored={stored_sha or 'none'} expected={exp_sha})")
    return {"checked": checked, "drifted": drifted}


def sync_auto_blocks(repo: Path) -> list[str]:
    """Rewrite stale `<!--AUTO:START..END-->` blocks deterministically with SHA-256 stamps."""
    updated: list[str] = []
    for doc in _candidate_auto_docs(repo):
        txt = doc.read_text(encoding="utf-8", errors="ignore")

        def _repl(m: re.Match[str]) -> str:
            bid = m.group(2)
            expected = render_auto_block_body(bid, repo)
            return m.group(0) if expected is None else f"<!--AUTO:START:{bid}:sha256={_sha12(expected)}-->\n{expected}\n<!--AUTO:END:{bid}-->"

        new_txt = _AUTO_BLOCK_RE.sub(_repl, txt)
        if new_txt != txt:
            doc.write_text(new_txt, encoding="utf-8")
            updated.append(doc.relative_to(repo).as_posix() if repo.resolve() in doc.resolve().parents else doc.name)
    return updated


def parse_deprecated_registry(hub_path: Path) -> list[dict[str, str]]:
    if not hub_path.is_file():
        return []
    rows = []
    for raw in hub_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = _DEP_ROW_RE.match(raw.strip())
        if m:
            rows.append({"id": m.group(1).strip(), "stage": m.group(2).strip().upper(), "path": m.group(3).strip(), "replacement": m.group(4).strip(), "owner": m.group(5).strip(), "expires_ver": m.group(6).strip()})
    return rows


def _default_deprecated_entries(repo: Path) -> list[dict[str, str]]:
    if (repo / "plugin.json").is_file() and not (repo / ".agents" / "scripts").is_dir():
        return []
    return [
        {"id": "legacy-live-phone-bridge", "stage": "TOMBSTONE", "path": ".agents/scripts/live_phone_bridge.py", "replacement": "omni_agent.py cavernize-cmd", "owner": "cavern", "expires_ver": "2.22.0"},
        {"id": "flight-recorder-recover-alias", "stage": "GRACE", "path": "cli:flight_recorder.py recover", "replacement": "flight_recorder.py resume", "owner": "cavern", "expires_ver": "2.25.0"},
        {"id": "legacy-error-learner-wrapper", "stage": "WARN", "path": ".agents/scripts/error_learner.py", "replacement": "plan_tool.py learn", "owner": "dfp", "expires_ver": "2.25.0"},
    ]


def _audit_deprecated_entries(repo: Path, entries: list[dict[str, str]], cur_ver: str) -> tuple[list[str], list[str]]:
    fails, warns = [], []
    for e in entries:
        st, eid, pth = e.get("stage", ""), e.get("id", "?"), e.get("path", "")
        if st not in VALID_DEP_STAGES:
            fails.append(f"COHERENCE|DEP `{eid}` invalid stage `{st}`.")
            continue
        if not e.get("owner") or not e.get("expires_ver"):
            fails.append(f"COHERENCE|DEP `{eid}` missing owner or expires_ver.")
        if st == "TOMBSTONE" and not pth.startswith(("cli:", "sym:")) and (repo / _norm(pth)).exists():
            fails.append(f"COHERENCE|TOMBSTONE `{pth}` (`{eid}`) still exists on disk -> delete it.")
        elif st in ("WARN", "GRACE") and cur_ver and e.get("expires_ver") and compare_semver(cur_ver, e["expires_ver"]) >= 0:
            warns.append(f"COHERENCE|DEP `{eid}` ({st}) expired at v{e['expires_ver']} (current v{cur_ver}) -> promote/remove.")
    return fails, warns


def audit_fleet_parity(repo: Path, *, local_only: bool = False) -> dict[str, Any]:
    """Check cross-repo parity via `.agents/federation.json` (read-only; skips cleanly in `--local` or offline)."""
    if local_only:
        return {"status": "SKIP|local_only", "checked": 0, "fails": [], "warns": [], "repos": {}}
    fed = _read_json(repo / ".agents" / "federation.json")
    repos_map = fed.get("repos")
    if not isinstance(repos_map, dict) or not repos_map:
        return {"status": "SKIP|no_federation", "checked": 0, "fails": [], "warns": [], "repos": {}}

    mount = Path(os.environ.get("CAVERN_FEDERATION_ROOT") or fed.get("mountRoot") or "/tmp/cavern-federation").expanduser()
    import self_evolve  # noqa: E402

    repo_info, fails, warns, p_shas, fed_shas = {}, [], [], {}, {}
    for alias, spec in sorted(repos_map.items()):
        if not isinstance(spec, dict):
            continue
        rdir = mount / alias
        if not rdir.is_dir():
            tp = str(spec.get("termuxPath") or "").strip()
            if tp and Path(tp).is_dir():
                rdir = Path(tp)
        if not rdir.is_dir():
            repo_info[alias] = {"mounted": False}
            continue
        pdir = rdir if (rdir / "plugin.json").is_file() else (rdir / ".agents" / "plugins" / "dynamic-fractal-planner")
        ver, sha = self_evolve.compute_plugin_tree_sha256(pdir) if (pdir / "plugin.json").is_file() else ("", "")
        prof, hm = _read_json(rdir / ".agents" / "planner_profile.json"), _read_json(rdir / ".agents" / "host_matrix.json")
        f_fp = rdir / ".agents" / "federation.json"
        f_sha = hashlib.sha256(f_fp.read_bytes()).hexdigest()[:12] if f_fp.is_file() else ""
        repo_info[alias] = {
            "mounted": True, "plugin_ver": ver, "plugin_sha": sha[:12], "profile_schema": prof.get("schema_version"),
            "profile_plugin_ver": prof.get("plugin_version"), "host_matrix_strict": bool(hm.get("strict_coverage")), "fed_sha": f_sha,
        }
        if ver or sha:
            p_shas[alias] = (ver, sha[:12])
        if f_sha:
            fed_shas[alias] = f_sha
        if hm and not hm.get("strict_coverage"):
            warns.append(f"fleet:{alias}: host_matrix.json strict_coverage=false")
        if prof and ver and str(prof.get("plugin_version") or "") != ver:
            warns.append(f"fleet:{alias}: profile plugin_version={prof.get('plugin_version')} != plugin={ver}")

    mounted = [a for a, v in repo_info.items() if v.get("mounted")]
    if len(mounted) <= 1:
        return {"status": "SKIP|offline", "checked": len(mounted), "fails": fails, "warns": warns, "repos": repo_info}
    if len(set(p_shas.values())) > 1:
        fails.append("fleet: plugin (semver,sha) drift -> " + ", ".join(f"{a}=v{v}({s})" for a, (v, s) in sorted(p_shas.items())))
    if len(set(fed_shas.values())) > 1:
        warns.append("fleet: federation.json drift -> " + ", ".join(f"{a}={s}" for a, s in sorted(fed_shas.items())))
    status = "OK" if not fails and not warns else ("FAIL" if fails else "WARN")
    return {"status": f"{status}|mounted={len(mounted)}", "checked": len(mounted), "fails": fails, "warns": warns, "repos": repo_info}


def render_coherence_hub(repo: Path, *, local_only: bool = True, dep_entries: list[dict[str, str]] | None = None) -> str:
    """Render deterministic `<=150L` `.agents/docs/COHERENCE_HUB.md`."""
    pdir = _plugin_dir_for(repo)
    pver = str(_read_json(pdir / "plugin.json").get("version") or "0.0.0")
    dom, v_checks, h_cov = _read_json(pdir / "scripts" / "domains.json"), _introspect_verify_checks(pdir), _introspect_hooks_coverage(pdir)
    auto_res, hm = check_auto_blocks(repo), _read_json(repo / ".agents" / "host_matrix.json")
    deps = dep_entries if dep_entries is not None else (parse_deprecated_registry(repo / COHERENCE_HUB_REL) or _default_deprecated_entries(repo))
    fleet = audit_fleet_parity(repo, local_only=local_only)

    lines = [
        "# Ecosystem Coherence Hub (`COHERENCE_HUB.md`)", "",
        "> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`). Auto-synced by `plan_tool.py coherence sync`.", "",
        "## 1. SSOT_MAP (Authoritative Source -> Consumers)",
        "| Domain | Authoritative SSOT | Consumers |", "| :--- | :--- | :--- |",
        "| Domains & Checklists | `.agents/plugins/dynamic-fractal-planner/scripts/domains.json` | `complexity_triage.py`, `plan_tool.py`, `blueprint_engine.py`, `planner_pre_invocation.py` |",
        "| Verify Registry | `verify_universal.py:@verify_check` (`VERIFY_CHECKS`) | `verify_universal.py:run_static_checks`, `planner_stop.py`, CI |",
        "| Multi-Host Matrix | `.agents/host_matrix.json` (`strict_coverage=true`) | `host_context.py`, `verify_universal.py`, `plan_tool.py host-*` |",
        "| Triad Federation | `.agents/federation.json` | `repo_federation.py`, `self_evolve.py`, `verify_universal.py`, `coherence_engine.py` |",
        "| Profile Schema | `scripts/schema_migrations.py:migrate_profile` | `planner_state.py`, `blueprint_hub.py`, `complexity_triage.py` |",
        "| Living Docs Parity | `<!--AUTO:START:<id>:sha256=...-->` | `skills/*/SKILL.md`, `.agents/docs/EXTERNAL_AI_ONBOARDING.md` |",
        "| Token-Tax Governor | `scripts/token_governor.py` + `token_budget` | `planner_{session_start,pre_invocation,guard_files,stop}.py`, `verify_universal.py`, `plan_tool.py` |", "",
        "## 2. DEPRECATED_REGISTRY (`WARN -> GRACE -> TOMBSTONE`)",
        "<!-- Format: DEP|<id>|<stage>|<path_or_surface>|<replacement>|owner=<owner>|expires_ver=<semver> -->",
    ]
    for e in deps:
        lines.append(f"DEP|{e['id']}|{e['stage']}|{e['path']}|{e['replacement']}|owner={e['owner']}|expires_ver={e['expires_ver']}")
    if not deps:
        lines.append("DEP|none|TOMBSTONE|cli:none|none|owner=dfp|expires_ver=0.0.0")
    lines.extend([
        "", "## 3. COVERAGE_MATRIX",
        f"COV|plugin=v{pver}|triage_domains={len(dom.get('triage_domains') or {})}|bp_domains={len(dom.get('blueprint_domains') or {})}|verify_checks={len(v_checks)}|hooks={h_cov['disk']}(unwired={len(h_cov['unwired'])})",
        f"COV|host_matrix_strict={str(bool(hm.get('strict_coverage'))).lower()}|host_entries={len(hm.get('entries') or [])}|auto_blocks={auto_res['checked']}(drifted={len(auto_res['drifted'])})|deprecated={len(deps)}",
        "", "## 4. CROSS_REPO_PARITY", f"PARITY|{fleet['status']}",
    ])
    for alias, info in sorted((fleet.get("repos") or {}).items()):
        if info.get("mounted"):
            lines.append(f"REPO|{alias}|plugin=v{info.get('plugin_ver') or '?'}({info.get('plugin_sha') or '-'})|profile=v{info.get('profile_schema')}/v{info.get('profile_plugin_ver') or '?'}|strict={str(bool(info.get('host_matrix_strict'))).lower()}|fed={info.get('fed_sha') or '-'}")
    import meta_governance as _mg, token_governor as _tg  # noqa: E402
    wf_rows, anc, tok_cfg = _mg.parse_wontfix_registry(repo / COHERENCE_HUB_REL), _mg.verify_living_anchors(repo), _tg.resolve_token_budget_config(_read_json(repo / ".agents" / "planner_profile.json"))
    lines.extend(["", "## 5. CONSTITUTION_AND_META_GOV", f"CONSTITUTION|T0=immutable_canary|T1=ssot_parity|T2=observe_telemetry|anchors={anc['total_anchors']}(drifted={anc['drifted_count']})|plane2_cap={_mg.PLANE2_MAX_PER_WEEK}/wk", f"TOKEN_BUDGET|ss<={tok_cfg['session_start_max_chars']}c|pre<={tok_cfg['pre_invocation_max_chars']}c|rep<={tok_cfg['pre_invocation_repeat_max_chars']}c|dedup={str(bool(tok_cfg['session_dedup_enabled'])).lower()}|ratchet={str(bool(tok_cfg['ratchet_enabled'])).lower()}"])
    lines.extend(f"WONTFIX|{r['id']}|{r['reason']}|owner={r['owner']}|expires_ver={r['expires_ver']}" for r in wf_rows)
    return "\n".join(lines[:150]) + "\n"


def run_coherence_check(repo: Path, *, local_only: bool = True, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Strictly read-only ecosystem coherence check (<=200ms)."""
    t0, pdir = time.monotonic(), _plugin_dir_for(repo)
    pver = str(_read_json(pdir / "plugin.json").get("version") or "0.0.0")
    fails, warns = [], []
    dom = _read_json(pdir / "scripts" / "domains.json")
    if not dom.get("triage_domains") or not dom.get("blueprint_domains"):
        fails.append("COHERENCE|Missing or invalid `scripts/domains.json` SSOT.")
    v_checks = _introspect_verify_checks(pdir)
    if not v_checks:
        fails.append("COHERENCE|`verify_universal.py` has 0 `@verify_check` registrations.")
    h_cov = _introspect_hooks_coverage(pdir)
    if h_cov["unwired"]:
        warns.append(f"COHERENCE|Unwired hook file(s) not in `hooks.json`: {', '.join(h_cov['unwired'])}")
    auto_res = check_auto_blocks(repo)
    fails.extend(f"COHERENCE|AUTO doc drift `{d}` -> run `plan_tool.py coherence sync`." for d in auto_res["drifted"])

    hub_path = repo / COHERENCE_HUB_REL
    deps = parse_deprecated_registry(hub_path)
    if hub_path.is_file():
        n_lines = len(hub_path.read_text(encoding="utf-8", errors="ignore").splitlines())
        if n_lines > 150:
            fails.append(f"COHERENCE|`{COHERENCE_HUB_REL}` exceeds 150L cap ({n_lines}L).")
        df, dw = _audit_deprecated_entries(repo, deps, pver)
        fails.extend(df)
        warns.extend(dw)
    elif (repo / ".agents" / "docs").is_dir():
        warns.append(f"COHERENCE|Missing `{COHERENCE_HUB_REL}` -> run `plan_tool.py coherence sync`.")

    hm = _read_json(repo / ".agents" / "host_matrix.json")
    if hm and not hm.get("strict_coverage"):
        warns.append("COHERENCE|`.agents/host_matrix.json` has `strict_coverage=false`.")
    prof = profile if isinstance(profile, dict) else _read_json(repo / ".agents" / "planner_profile.json")
    if isinstance(prof, dict) and prof:
        p_pv = str(prof.get("plugin_version") or "").strip()
        if p_pv and pver and p_pv != pver and not (repo / "plugin.json").is_file():
            warns.append(f"COHERENCE|planner_profile.json plugin_version={p_pv} != plugin.json v{pver}.")

    fleet = audit_fleet_parity(repo, local_only=local_only)
    fails.extend(fleet["fails"])
    warns.extend(fleet["warns"])
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
    stats = {
        "ssot": 7, "domains": len(dom.get("triage_domains") or {}) + len(dom.get("blueprint_domains") or {}),
        "verify_checks": len(v_checks), "auto_checked": auto_res["checked"],
        "auto_drifted": len(auto_res["drifted"]), "deprecated": len(deps), "fleet": fleet["status"],
    }
    return {
        "ok": not fails, "elapsed_ms": elapsed_ms, "fails": fails, "warns": warns, "stats": stats,
        "summary": f"COHERENCE|{'OK' if not fails else f'FAIL({len(fails)})'}|warn={len(warns)}|checks={len(v_checks)}|auto={auto_res['checked']}|ms={elapsed_ms}",
    }


def format_session_start_digest(repo: Path, profile: dict[str, Any] | None = None) -> str:
    """Return `<=40c` SessionStart coherence digest (read-only)."""
    res = run_coherence_check(repo, local_only=True, profile=profile)
    st = res["stats"]
    if res["ok"] and not res["warns"]:
        out = f"COHERENCE|OK|s={st['ssot']}|a={st['auto_checked']}|d={st['deprecated']}"
    elif res["ok"]:
        out = f"COHERENCE|WARN({len(res['warns'])})|a={st['auto_checked']}|d={st['deprecated']}"
    else:
        out = f"COHERENCE|DRIFT({len(res['fails'])})|w={len(res['warns'])}"
    return out[:40]


def sync_coherence(repo: Path, *, docs: bool = True, hub: bool = True, local_only: bool = True) -> dict[str, Any]:
    touched = sync_auto_blocks(repo) if docs else []
    if hub and ((repo / ".agents" / "docs").is_dir() or (repo / ".agents" / "planner_profile.json").is_file()):
        hub_path = repo / COHERENCE_HUB_REL
        hub_path.parent.mkdir(parents=True, exist_ok=True)
        content = render_coherence_hub(repo, local_only=local_only)
        if not hub_path.is_file() or hub_path.read_text(encoding="utf-8", errors="ignore") != content:
            tmp = hub_path.with_name(f".{hub_path.name}.tmp.{os.getpid()}")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, hub_path)
            touched.append(COHERENCE_HUB_REL)
    return {"ok": True, "touched": touched}


def deprecate_surface(repo: Path, *, dep_id: str, stage: str, path: str, replacement: str, owner: str, expires_ver: str, apply: bool = False) -> dict[str, Any]:
    st = stage.strip().upper()
    if st not in VALID_DEP_STAGES:
        raise ValueError(f"Invalid stage `{stage}`. Use WARN|GRACE|TOMBSTONE.")
    if not owner.strip() or not expires_ver.strip():
        raise ValueError("`owner` and `expires_ver` are required for deprecation entries.")
    hub_path = repo / COHERENCE_HUB_REL
    entries = parse_deprecated_registry(hub_path) or _default_deprecated_entries(repo)
    rec = {"id": dep_id.strip(), "stage": st, "path": path.strip(), "replacement": replacement.strip(), "owner": owner.strip(), "expires_ver": expires_ver.strip()}
    entries = [e for e in entries if e["id"] != rec["id"]] + [rec]
    if apply:
        hub_path.parent.mkdir(parents=True, exist_ok=True)
        hub_path.write_text(render_coherence_hub(repo, local_only=True, dep_entries=entries), encoding="utf-8")
    return {"ok": True, "applied": apply, "entry": rec, "total": len(entries)}


def build_cold_audit_prompt(repo: Path) -> str:
    import meta_governance  # noqa: E402
    return meta_governance.build_plane2_meta_audit_prompt(repo, trigger="manual_prompt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Static Ecosystem Coherence & Meta-Governance Engine (DFP v2.22.5).")
    parser.add_argument("--workspace", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_chk = sub.add_parser("check")
    p_chk.add_argument("--local", action="store_true")
    p_chk.add_argument("--json", action="store_true")
    p_sync = sub.add_parser("sync")
    for fl in ("--docs", "--hub", "--fleet", "--json"):
        p_sync.add_argument(fl, action="store_true")
    sub.add_parser("audit").add_argument("--json", action="store_true")
    sub.add_parser("gov-audit").add_argument("--json", action="store_true")
    sub.add_parser("canary").add_argument("--json", action="store_true")
    p_dep = sub.add_parser("deprecate")
    for fl in ("--id", "--path", "--replacement", "--owner", "--expires-ver"):
        p_dep.add_argument(fl, required=True)
    p_dep.add_argument("--stage", required=True, choices=["WARN", "GRACE", "TOMBSTONE"])
    p_dep.add_argument("--apply", action="store_true")
    p_dep.add_argument("--json", action="store_true")
    sub.add_parser("fleet").add_argument("--json", action="store_true")
    sub.add_parser("prompt")

    args = parser.parse_args(argv)
    repo = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    if args.cmd in ("check", "audit"):
        res = run_coherence_check(repo, local_only=bool(getattr(args, "local", False)) if args.cmd == "check" else False)
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else "\n".join([res["summary"], *(f"  [WARN] {w}" for w in res["warns"]), *(f"  [FAIL] {f}" for f in res["fails"])]))
        return 0 if res["ok"] else 1
    if args.cmd == "gov-audit":
        import meta_governance  # noqa: E402
        res = meta_governance.run_gov_audit(repo)
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else "\n".join([res["summary"], *(f"  [WARN] {w}" for w in res["warns"]), *(f"  [FAIL] {f}" for f in res["fails"])]))
        return 0 if res["ok"] else 1
    if args.cmd == "canary":
        import meta_governance  # noqa: E402
        res = meta_governance.run_t0_mutation_canary(repo)
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else f"T0_CANARY|{'OK' if res['ok'] else 'FAIL'}|{res['caught']}/{res['total']}|kill_rate={res['kill_rate']:.2f}|ms={res['elapsed_ms']}")
        return 0 if res["ok"] else 1
    if args.cmd == "sync":
        res = sync_coherence(repo, docs=(args.docs or not args.hub), hub=(args.hub or not args.docs), local_only=(not args.fleet))
        print(json.dumps(res, indent=2) if args.json else f"COHERENCE_SYNC|touched={','.join(res['touched']) or 'none'}")
        return 0
    if args.cmd == "deprecate":
        res = deprecate_surface(repo, dep_id=args.id, stage=args.stage, path=args.path, replacement=args.replacement, owner=args.owner, expires_ver=args.expires_ver, apply=bool(args.apply))
        print(json.dumps(res, indent=2) if args.json else f"DEP_UPDATED|{res['entry']['id']}|{res['entry']['stage']}|applied={res['applied']}")
        return 0
    if args.cmd == "fleet":
        res = audit_fleet_parity(repo, local_only=False)
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else "\n".join([f"COHERENCE_FLEET|{res['status']}", *(f"  [WARN] {w}" for w in res["warns"]), *(f"  [FAIL] {f}" for f in res["fails"])]))
        return 0 if not res["fails"] else 1
    if args.cmd == "prompt":
        print(build_cold_audit_prompt(repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
