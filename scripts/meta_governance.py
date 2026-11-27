#!/usr/bin/env python3
"""Reflexive Self-Evolving Meta-Governance Engine (DFP v2.22.5, W56.3, stdlib-only).

Provides:
  - W56.3.1: T0/T1/T2 Constitutional Registry, zero-disk-write T0 synthetic mutation-canary
    (`kill_rate == 1.00`, `!decay`), and observe-only `.metrics.jsonl` rule telemetry aggregation.
  - W56.3.2: Living Blueprints (`DONE -> OBSERVING` executable `CODE_ANCHORS` + `FITNESS|` AST/symbol
    drift detection) and strictly read-only `gov-audit` (`PROBATION -> PRUNED` proposals only on
    dead AST/file target or `FP > 10%` with `cooldown >= 3` epochs; never mutates files).
  - W56.3.3: Event-triggered Plane-2 Cold-Context `pro [L3]` Semantic & Meta-Governance Auditor
    (`<= 4/wk` weekly budget, excluding doc/bloat warns) mandating Plane-1 ratchet or timed `WONTFIX`.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPTS_DIR.parent
_HOOKS_DIR = _PLUGIN_ROOT / "hooks"
for _p in (str(_SCRIPTS_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schema_migrations import compare_semver, migrate_profile, resolve_blueprint_config  # noqa: E402

PLANE2_MAX_PER_WEEK = 4
DEFAULT_COOLDOWN_EPOCHS = 3
DEFAULT_FP_THRESHOLD = 0.10
_WEEK_SECS = 7 * 86400

CONSTITUTIONAL_TIERS: dict[str, dict[str, Any]] = {
    "T0": {
        "name": "Safety/Physics/Security Invariants",
        "immutable": True,
        "decay_allowed": False,
        "verify_via": "synthetic_mutation_canary",
    },
    "T1": {
        "name": "SSOT Parity Contracts",
        "immutable": False,
        "decay_allowed": False,
        "verify_via": "ssot_parity",
    },
    "T2": {
        "name": "Heuristic/Style Rules",
        "immutable": False,
        "decay_allowed": True,
        "verify_via": "observe_telemetry",
    },
}

VERIFY_CHECK_TIERS: dict[str, str] = {
    # T0: Safety, Security, Syntax, QA Contracts, File-Budget Hard Safety
    "secrets_and_conflicts": "T0",
    "universal_syntax": "T0",
    "csharp_syntax_deep": "T0",
    "balanced_brackets": "T0",
    "file_guards_state": "T0",
    "adversarial_qa_contracts": "T0",
    "file_budget": "T0",
    # T1: SSOT Parity Contracts & Governance Coherence
    "coherence_local": "T1",
    "meta_gov_t0_anchors": "T1",
    "host_matrix_coverage": "T1",
    "key_parity_rules": "T1",
    "version_sync": "T1",
    "plugin_sync_parity": "T1",
    "blueprint_quality": "T1",
    "build_lessons_timelessness": "T1",
    "forward_wiring": "T1",
    # T2: Heuristic / Style / Weight Rules
    "diff_rules": "T2",
    "orphan_calls": "T2",
    "memory_weight": "T2",
    "code_weight": "T2",
}

GUARD_RULE_TIERS: dict[str, str] = {
    "secrets_block": "T0",
    "file_guard": "T0",
    "destructive_write_shield": "T0",
    "shift_left_diff_fail": "T0",
    "ship_pre_flight": "T0",
    "qa_gate_stop": "T0",
    "lease_guard": "T1",
    "blueprint_first_gate": "T1",
    "plan_gate": "T1",
    "extra_requests_gate": "T1",
    "blast_radius_guard": "T2",
    "read_budget_guard": "T2",
    "polling_guard": "T2",
    "terminal_diet_rewrite": "T2",
}

_NON_BLOAT_GUARD_ANOMALIES = frozenset({
    "guard_deny_burst",
    "stop_gate_loop",
    "hook_exception",
    "circuit_breaker_suspect",
})
_WONTFIX_ROW_RE = re.compile(r"^WONTFIX\|([^|]+)\|([^|]+)\|owner=([^|]+)\|expires_ver=([^\s|]+)")
_ANCHOR_SYM_RE_TMPL = r"\b(?:class|struct|interface|enum|record|def|async\s+def|function|const|let|var|static|public|private|protected|internal|void|bool|int|float|double|string|Task)\b[^;\n{=]*\b%s\b"


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


def classify_rule_tier(rule_id: str) -> str:
    """Return constitutional tier (`T0`|`T1`|`T2`) for a verify check or guard rule."""
    rid = str(rule_id or "").strip()
    if rid in VERIFY_CHECK_TIERS:
        return VERIFY_CHECK_TIERS[rid]
    if rid in GUARD_RULE_TIERS:
        return GUARD_RULE_TIERS[rid]
    low = rid.lower()
    if any(k in low for k in ("secret", "security", "wx", "selinux", "oom", "safety", "syntax", "qa_frozen", "t0_")):
        return "T0"
    if any(k in low for k in ("ssot", "parity", "coherence", "schema", "host_matrix", "blueprint")):
        return "T1"
    return "T2"


def run_t0_mutation_canary(repo: Path | None = None) -> dict[str, Any]:
    """Run zero-disk-write synthetic fault-injection probes proving 100% T0 safety kill rate."""
    t0 = time.monotonic()
    probes: list[dict[str, Any]] = []

    def _run(pid: str, fn: Any) -> None:
        try:
            caught, detail = fn()
            probes.append({"id": pid, "caught": bool(caught), "detail": str(detail)})
        except Exception as exc:
            probes.append({"id": pid, "caught": False, "detail": f"exception:{type(exc).__name__}:{exc}"})

    def _p_secret() -> tuple[bool, str]:
        import planner_state  # noqa: E402

        tok = "ghp_" + "A1b2C3d4E5F6g7H8i9J01234"
        hits = planner_state.scan_secrets_and_conflicts(f"KEY={tok}\n", rel_path="src/demo.py")
        return bool(hits), f"hits={len(hits)}"

    def _p_conflict() -> tuple[bool, str]:
        import planner_state  # noqa: E402

        body = "<<<<<<< HEAD\na=1\n=======\na=2\n>>>>>>> main\n"
        hits = planner_state.scan_secrets_and_conflicts(body, rel_path="src/demo.py")
        return len(hits) >= 2, f"hits={len(hits)}"

    def _p_env_keyfile() -> tuple[bool, str]:
        import planner_state  # noqa: E402

        h1 = planner_state.scan_secrets_and_conflicts("X=1\n", rel_path=".env")
        h2 = planner_state.scan_secrets_and_conflicts("BIN", rel_path="app/release.keystore")
        return bool(h1 and h2), f"env={len(h1)},ks={len(h2)}"

    def _p_shlex_guard() -> tuple[bool, str]:
        import planner_guard_command as pgc  # noqa: E402

        wc_cmd = "wc -l tests/run_plugin_tests.py"
        py_cmd = "env FOO=1 python3 -B tests/run_plugin_tests.py"
        wc_out = pgc._inject_plugin_tests_fast(wc_cmd)
        py_out = pgc._inject_plugin_tests_fast(py_cmd)
        ship_hit = pgc._classify_ship_segment("python3 -B .agents/scripts/omni_agent.py ship --commit test")
        ok = (wc_out == wc_cmd) and ("--fast" in py_out) and bool(ship_hit)
        return ok, f"wc_ok={wc_out == wc_cmd},fast={'--fast' in py_out},ship={bool(ship_hit)}"

    def _p_qa_gate() -> tuple[bool, str]:
        import qa_gate  # noqa: E402

        verdict = qa_gate.evaluate_qa_gate(
            report={"findings": [{"severity": "CRITICAL", "category": "regression_contract_failed", "origin": "live_triggered"}]},
            contracts_eval={"failed": 1, "passed": 0, "total": 1, "schemaErrors": []},
        )
        caught = (verdict.get("ok") is False) and (verdict.get("verdict") == "FAIL")
        return caught, f"ok={verdict.get('ok')},verdict={verdict.get('verdict')}"

    def _p_t0_no_decay() -> tuple[bool, str]:
        synthetic_metrics = {
            "secrets_and_conflicts": {
                "rule": "secrets_and_conflicts",
                "tier": "T0",
                "hits": 0,
                "fp_overrides": 50,
                "fp_rate": 1.0,
                "epochs_observed": 99,
                "target_exists": False,
                "hook_ms_p95": 1.0,
            }
        }
        props = evaluate_rule_lifecycle_proposals(synthetic_metrics, repo or Path.cwd())
        t0_proposed = [p for p in props if p.get("tier") == "T0"]
        return len(t0_proposed) == 0, f"t0_proposed={len(t0_proposed)}"

    _run("T0-SECRETS-TOKEN", _p_secret)
    _run("T0-MERGE-CONFLICT", _p_conflict)
    _run("T0-ENV-KEYFILE", _p_env_keyfile)
    _run("T0-SHLEX-SHIP-GUARD", _p_shlex_guard)
    _run("T0-QA-CONTRACT-BLOCK", _p_qa_gate)
    _run("T0-ZERO-HIT-NO-DECAY", _p_t0_no_decay)

    caught_n = sum(1 for p in probes if p["caught"])
    total_n = len(probes)
    kill_rate = round((caught_n / total_n) if total_n else 0.0, 4)
    return {
        "ok": caught_n == total_n and total_n > 0,
        "caught": caught_n,
        "total": total_n,
        "fails": total_n - caught_n,
        "kill_rate": kill_rate,
        "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        "probes": probes,
    }


def _symbol_exists_in_file(path: Path, symbol: str) -> bool:
    if not path.is_file():
        return False
    sym = str(symbol or "").strip()
    if not sym or sym == "*":
        return True
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(txt)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == sym:
                    return True
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == sym:
                            return True
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == sym:
                    return True
            return False
        except SyntaxError:
            pass
    if re.search(_ANCHOR_SYM_RE_TMPL % re.escape(sym), txt):
        return True
    return re.search(rf"\b{re.escape(sym)}\s*\(", txt) is not None


def _check_target_exists(repo: Path, target: str) -> bool:
    raw = str(target or "").strip()
    if not raw:
        return True
    if ":" in raw and not raw.startswith(("cli:", "sym:")):
        fpath, sym = raw.split(":", 1)
        return _symbol_exists_in_file(repo / _norm(fpath), sym.strip())
    if raw.startswith(("cli:", "sym:")):
        return True
    return (repo / _norm(raw)).exists()


def _resolve_metrics_file(repo: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env_p = os.environ.get("PLANNER_METRICS_FILE", "").strip()
    if env_p:
        return Path(env_p).expanduser()
    return _plugin_dir_for(repo) / ".metrics.jsonl"


def summarize_rule_metrics(metrics_path: Path, repo: Path) -> dict[str, dict[str, Any]]:
    """Aggregate observe-only `rule_obs` rows from `.metrics.jsonl` (strictly read-only)."""
    if not metrics_path.is_file():
        return {}
    agg: dict[str, dict[str, Any]] = {}
    for raw in metrics_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("ev") != "rule_obs":
            continue
        rid = str(obj.get("rule") or "").strip()
        if not rid:
            continue
        tier = str(obj.get("tier") or classify_rule_tier(rid)).upper()
        if tier not in CONSTITUTIONAL_TIERS:
            tier = classify_rule_tier(rid)
        cur = agg.setdefault(
            rid,
            {
                "rule": rid,
                "tier": tier,
                "hits": 0,
                "fp_overrides": 0,
                "hook_ms_samples": [],
                "epochs": set(),
                "target": "",
                "target_exists": True,
            },
        )
        cur["hits"] += int(obj.get("hits") if obj.get("hits") is not None else 1)
        cur["fp_overrides"] += int(obj.get("fp_overrides") or 0)
        if obj.get("hook_ms") is not None:
            try:
                cur["hook_ms_samples"].append(float(obj["hook_ms"]))
            except Exception:
                pass
        ep = obj.get("epoch")
        if ep is not None and str(ep).strip():
            cur["epochs"].add(str(ep).strip())
        else:
            ts = int(obj.get("ts") or 0)
            cur["epochs"].add(str(ts // 86400) if ts > 0 else "0")
        if obj.get("target"):
            cur["target"] = str(obj["target"]).strip()
        if obj.get("target_exists") is False:
            cur["target_exists"] = False

    out: dict[str, dict[str, Any]] = {}
    for rid, d in agg.items():
        ms = sorted(d["hook_ms_samples"])
        p95 = round(ms[min(len(ms) - 1, int(0.95 * len(ms)))], 2) if ms else 0.0
        denom = max(1, d["hits"] + d["fp_overrides"]) if d["hits"] == 0 else d["hits"]
        fp_rate = round(d["fp_overrides"] / denom, 4) if d["fp_overrides"] > 0 else 0.0
        t_exists = bool(d["target_exists"])
        if t_exists and d["target"]:
            t_exists = _check_target_exists(repo, d["target"])
        out[rid] = {
            "rule": rid,
            "tier": d["tier"],
            "hits": d["hits"],
            "fp_overrides": d["fp_overrides"],
            "fp_rate": fp_rate,
            "hook_ms_p95": p95,
            "epochs_observed": len(d["epochs"]),
            "target": d["target"],
            "target_exists": t_exists,
        }
    return out


def evaluate_rule_lifecycle_proposals(
    metrics_summary: dict[str, dict[str, Any]],
    repo: Path,
    *,
    cooldown_epochs: int = DEFAULT_COOLDOWN_EPOCHS,
    fp_threshold: float = DEFAULT_FP_THRESHOLD,
) -> list[dict[str, Any]]:
    """Emit read-only `PROBATION -> PRUNED` proposals. NEVER proposes pruning `T0` rules."""
    proposals: list[dict[str, Any]] = []
    for rid, info in sorted(metrics_summary.items()):
        tier = str(info.get("tier") or classify_rule_tier(rid)).upper()
        if tier == "T0" or CONSTITUTIONAL_TIERS.get(tier, {}).get("immutable"):
            continue
        target_exists = info.get("target_exists", True)
        if target_exists and info.get("target"):
            target_exists = _check_target_exists(repo, str(info["target"]))
        fp_rate = float(info.get("fp_rate") or 0.0)
        epochs = int(info.get("epochs_observed") or 0)

        if not target_exists:
            stage = "PRUNED" if epochs >= cooldown_epochs else "PROBATION"
            proposals.append({
                "rule": rid,
                "tier": tier,
                "proposed_stage": stage,
                "reason": "missing_target_symbol",
                "target": info.get("target") or "-",
                "epochs_observed": epochs,
                "cooldown_epochs": cooldown_epochs,
                "fp_rate": fp_rate,
            })
        elif tier == "T2" and fp_rate > fp_threshold and epochs >= cooldown_epochs:
            proposals.append({
                "rule": rid,
                "tier": tier,
                "proposed_stage": "PROBATION",
                "reason": f"fp_rate>{fp_threshold:.2f} ({fp_rate:.2f}) over {epochs} epochs",
                "target": info.get("target") or "-",
                "epochs_observed": epochs,
                "cooldown_epochs": cooldown_epochs,
                "fp_rate": fp_rate,
            })
    return proposals


def parse_code_anchors_spec(spec: str) -> list[tuple[str, list[str]]]:
    """Parse `path/a.py:sym1,sym2; path/b.cs:ClassB` into `[(rel_path, [syms...]), ...]`."""
    anchors: list[tuple[str, list[str]]] = []
    for chunk in re.split(r"[;]+", str(spec or "")):
        c = chunk.strip().strip("`")
        if not c or c == "-":
            continue
        if ":" in c:
            raw_path, raw_syms = c.split(":", 1)
            syms = [s.strip().strip("`") for s in raw_syms.split(",") if s.strip().strip("`")]
        else:
            raw_path, syms = c, []
        rel = _norm(raw_path.strip().strip("`"))
        if rel:
            anchors.append((rel, syms))
    return anchors


def extract_living_anchors_from_text(text: str) -> tuple[list[str], list[str]]:
    """Extract raw `CODE_ANCHORS|` specs and `FITNESS|` lines from a blueprint markdown file."""
    anchors: list[str] = []
    fitness: list[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if s.startswith("CODE_ANCHORS|"):
            body = s.split("|", 1)[1].strip()
            if body:
                anchors.append(body)
        elif s.startswith("FITNESS|"):
            body = s.split("|", 1)[1].strip()
            if body:
                fitness.append(body)
    return anchors, fitness


def verify_living_anchors(repo: Path, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify executable `CODE_ANCHORS` across active/observing blueprints & archived `OBSERVING` ledger entries (read-only)."""
    import blueprint_hub  # noqa: E402

    prof = blueprint_hub._load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    bp_dir = repo / _norm(cfg.get("dir") or cfg.get("blueprints_dir") or ".agents/docs/blueprints")

    records: list[dict[str, Any]] = []
    drifted: list[dict[str, Any]] = []
    total_anchors = 0

    def _audit_entry(bp_id: str, status: str, source: str, specs: list[str]) -> None:
        nonlocal total_anchors
        missing: list[str] = []
        checked = 0
        for sp in specs:
            for rel_path, syms in parse_code_anchors_spec(sp):
                full = repo / rel_path
                if not syms:
                    checked += 1
                    total_anchors += 1
                    if not full.exists():
                        missing.append(f"{rel_path}:<missing_file>")
                else:
                    for sym in syms:
                        checked += 1
                        total_anchors += 1
                        if not _symbol_exists_in_file(full, sym):
                            missing.append(f"{rel_path}:{sym}")
        if checked == 0:
            return
        st = "DRIFTED" if missing else ("OBSERVING" if status in ("DONE", "OBSERVING") else status)
        rec = {"id": bp_id, "source": source, "status": st, "checked": checked, "missing": missing}
        records.append(rec)
        if missing:
            drifted.append(rec)

    if bp_dir.is_dir():
        for bp_file in sorted(bp_dir.glob("BP_*.md")):
            txt = bp_file.read_text(encoding="utf-8", errors="ignore")
            m_id = re.search(r"\bid=([^|\s]+)", txt)
            m_st = re.search(r"\bstatus=([^|\s]+)", txt)
            bp_id = m_id.group(1).strip() if m_id else bp_file.stem
            bp_st = (m_st.group(1).strip().upper() if m_st else "ACTIVE")
            specs, _ = extract_living_anchors_from_text(txt)
            if specs:
                _audit_entry(bp_id, bp_st, bp_file.relative_to(repo).as_posix(), specs)

    for archived in blueprint_hub.read_archived_ledger_entries(repo, profile=prof):
        anc = str(archived.get("anchors") or "").strip()
        if anc and anc != "-":
            st = str(archived.get("living") or archived.get("status") or "OBSERVING").strip().upper()
            _audit_entry(archived.get("id", "?"), st, "BLUEPRINTS_DONE_LEDGER.md", [anc])

    return {
        "ok": len(drifted) == 0,
        "blueprints_with_anchors": len(records),
        "total_anchors": total_anchors,
        "drifted_count": len(drifted),
        "records": records,
        "drifted": drifted,
    }


def parse_wontfix_registry(hub_path: Path) -> list[dict[str, str]]:
    if not hub_path.is_file():
        return []
    rows: list[dict[str, str]] = []
    for raw in hub_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = _WONTFIX_ROW_RE.match(raw.strip())
        if m:
            rows.append({
                "id": m.group(1).strip(),
                "reason": m.group(2).strip(),
                "owner": m.group(3).strip(),
                "expires_ver": m.group(4).strip(),
            })
    return rows


def _audit_wontfix(rows: list[dict[str, str]], cur_ver: str) -> tuple[list[str], list[str]]:
    fails, warns = [], []
    for r in rows:
        wid = r.get("id", "?")
        if not r.get("owner") or not r.get("expires_ver"):
            fails.append(f"META_GOV|WONTFIX `{wid}` missing owner or expires_ver.")
        elif cur_ver and compare_semver(cur_ver, r["expires_ver"]) >= 0:
            warns.append(f"META_GOV|WONTFIX `{wid}` expired at v{r['expires_ver']} (current v{cur_ver}) -> re-evaluate or ratchet.")
    return fails, warns


def _count_plane2_audits_7d(metrics_path: Path, now_ts: float | None = None) -> int:
    if not metrics_path.is_file():
        return 0
    now = float(now_ts if now_ts is not None else time.time())
    cutoff = now - _WEEK_SECS
    cnt = 0
    for raw in metrics_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and obj.get("ev") == "plane2_audit" and float(obj.get("ts") or 0) >= cutoff:
                cnt += 1
        except Exception:
            continue
    return cnt


def evaluate_plane2_trigger(
    repo: Path,
    *,
    profile: dict[str, Any] | None = None,
    metrics_file: Path | None = None,
    diag_file: Path | None = None,
    explicit_trigger: str = "",
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Determine if Plane-2 Cold-Context `pro [L3]` Meta-Auditor should fire (`<=4/wk`, zero doc/bloat noise)."""
    import coherence_engine  # noqa: E402
    import plugin_diagnostics  # noqa: E402

    pdir = _plugin_dir_for(repo)
    pver = str(_read_json(pdir / "plugin.json").get("version") or "0.0.0")
    prof = profile if isinstance(profile, dict) else _read_json(repo / ".agents" / "planner_profile.json")
    metrics_p = _resolve_metrics_file(repo, metrics_file)
    used_7d = _count_plane2_audits_7d(metrics_p, now_ts=now_ts)
    budget_ok = used_7d < PLANE2_MAX_PER_WEEK

    triggers: list[str] = []
    if explicit_trigger:
        triggers.append(explicit_trigger.strip())

    # 1. plugin_bump: profile plugin_version != embedded plugin.json version
    prof_pv = str(prof.get("plugin_version") or "").strip() if isinstance(prof, dict) else ""
    if prof_pv and pver and prof_pv != pver and not (repo / "plugin.json").is_file():
        triggers.append(f"plugin_bump:v{prof_pv}->v{pver}")

    # 2. new_subsystem: unwired hook or unclassified @verify_check
    h_cov = coherence_engine._introspect_hooks_coverage(pdir)
    if h_cov.get("unwired"):
        triggers.append(f"new_subsystem:unwired_hooks({len(h_cov['unwired'])})")
    v_checks = coherence_engine._introspect_verify_checks(pdir)
    unclassified = [c for c in v_checks if c not in VERIFY_CHECK_TIERS]
    if unclassified:
        triggers.append(f"new_subsystem:unclassified_checks({len(unclassified)})")

    # 3. blueprint_schema_upgrade / drifted living anchor
    anchors = verify_living_anchors(repo, profile=prof)
    if anchors["drifted_count"] > 0:
        triggers.append(f"living_anchor_drifted({anchors['drifted_count']})")

    # 4. >=2 non-bloat guard anomalies (strictly excluding injection_bloat / doc warnings per KILL|K5)
    anomalies = plugin_diagnostics.load_anomalies(diag_file) if diag_file is not None else plugin_diagnostics.load_anomalies()
    guard_anoms = [a for a in anomalies if str(a.get("kind") or "") in _NON_BLOAT_GUARD_ANOMALIES]
    if len(guard_anoms) >= 2:
        triggers.append(f"guard_anomalies({len(guard_anoms)})")

    should_fire = bool(triggers) and (budget_ok or explicit_trigger == "manual_prompt")
    return {
        "should_fire": should_fire,
        "triggers": triggers,
        "used_7d": used_7d,
        "max_per_week": PLANE2_MAX_PER_WEEK,
        "budget_exhausted": not budget_ok,
        "guard_anomalies_count": len(guard_anoms),
    }


def run_gov_audit(
    repo: Path,
    *,
    profile: dict[str, Any] | None = None,
    metrics_file: Path | None = None,
    diag_file: Path | None = None,
) -> dict[str, Any]:
    """Strictly read-only reflexive meta-governance audit (`plan_tool.py coherence gov-audit`)."""
    import coherence_engine  # noqa: E402

    t0 = time.monotonic()
    pdir = _plugin_dir_for(repo)
    pver = str(_read_json(pdir / "plugin.json").get("version") or "0.0.0")
    raw_prof = profile if isinstance(profile, dict) else _read_json(repo / ".agents" / "planner_profile.json")
    prof = migrate_profile(raw_prof)[0] if raw_prof else {}

    fails: list[str] = []
    warns: list[str] = []

    # 1. T0 Synthetic Mutation-Canary (KILL|K6: must be 100% kill rate)
    canary = run_t0_mutation_canary(repo)
    if not canary["ok"]:
        fails.append(f"META_GOV|T0 mutation-canary kill_rate={canary['kill_rate']} (<1.00): {canary['caught']}/{canary['total']}.")

    # 2. Constitutional Tier Coverage over all @verify_check registrations
    v_checks = coherence_engine._introspect_verify_checks(pdir)
    tier_counts = {"T0": 0, "T1": 0, "T2": 0}
    unclassified_checks: list[str] = []
    for cid in v_checks:
        if cid not in VERIFY_CHECK_TIERS:
            unclassified_checks.append(cid)
            tier_counts[classify_rule_tier(cid)] += 1
        else:
            tier_counts[VERIFY_CHECK_TIERS[cid]] += 1
    if unclassified_checks:
        warns.append(f"META_GOV|Unclassified @verify_check(s) in VERIFY_CHECK_TIERS: {', '.join(unclassified_checks)}")

    # 3. Observe-only telemetry -> read-only PROBATION/PRUNED proposals (T0 never pruned!)
    metrics_p = _resolve_metrics_file(repo, metrics_file)
    metrics_summary = summarize_rule_metrics(metrics_p, repo)
    proposals = evaluate_rule_lifecycle_proposals(metrics_summary, repo)
    t0_pruned = sum(1 for p in proposals if p.get("tier") == "T0")
    if t0_pruned > 0:
        fails.append(f"META_GOV|KILL K6 VIOLATION: {t0_pruned} T0 safety invariant(s) proposed for decay/pruning.")

    # 4. Living Blueprints (DONE -> OBSERVING executable CODE_ANCHORS)
    anchors = verify_living_anchors(repo, profile=prof)
    for dr in anchors["drifted"]:
        warns.append(f"LIVING_ANCHOR_DRIFT|{dr['id']}|missing={','.join(dr['missing'][:4])}")

    # 5. Governance Engine / Hub / Docs / Formats / Thresholds sanity check
    coh = coherence_engine.run_coherence_check(repo, local_only=True, profile=prof)
    fails.extend(coh["fails"])
    warns.extend(coh["warns"])
    bp_cfg = resolve_blueprint_config(prof)
    if not (int(bp_cfg.get("mini_max_lines") or 35) < int(bp_cfg.get("compact_max_lines") or 65) < int(bp_cfg.get("max_lines") or 350)):
        fails.append("META_GOV|Invalid blueprint format line caps (expected mini < compact < full).")
    fb = prof.get("file_budget_limits") if isinstance(prof.get("file_budget_limits"), dict) else {}
    if fb and not (int(fb.get("warn_files", 850)) < int(fb.get("fail_files", 920)) <= int(fb.get("hard_cap_files", 999))):
        fails.append("META_GOV|Invalid file_budget_limits thresholds (expected warn < fail <= cap).")

    # 6. Timed WONTFIX registry
    hub_path = repo / coherence_engine.COHERENCE_HUB_REL
    wontfix_rows = parse_wontfix_registry(hub_path)
    wf_f, wf_w = _audit_wontfix(wontfix_rows, pver)
    fails.extend(wf_f)
    warns.extend(wf_w)

    # 7. Plane-2 trigger & weekly budget snapshot
    plane2 = evaluate_plane2_trigger(repo, profile=prof, metrics_file=metrics_p, diag_file=diag_file)

    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
    status = "OK" if not fails else f"FAIL({len(fails)})"
    summary = (
        f"GOV_AUDIT|{status}|t0_canary={canary['caught']}/{canary['total']}({canary['kill_rate']:.2f})"
        f"|t0_pruned={t0_pruned}|tiers=T0:{tier_counts['T0']},T1:{tier_counts['T1']},T2:{tier_counts['T2']}"
        f"|anchors={anchors['total_anchors']}(drifted={anchors['drifted_count']})"
        f"|proposals={len(proposals)}|p2={plane2['used_7d']}/{plane2['max_per_week']}|ms={elapsed_ms}"
    )
    return {
        "ok": not fails,
        "read_only": True,
        "elapsed_ms": elapsed_ms,
        "t0_canary": canary,
        "t0_pruned": t0_pruned,
        "tiers": tier_counts,
        "unclassified_checks": unclassified_checks,
        "metrics_rules_observed": len(metrics_summary),
        "proposals": proposals,
        "living_anchors": anchors,
        "wontfix": wontfix_rows,
        "plane2": plane2,
        "fails": fails,
        "warns": warns,
        "summary": summary,
    }


def build_plane2_meta_audit_prompt(
    repo: Path,
    *,
    trigger: str = "manual_prompt",
    profile: dict[str, Any] | None = None,
    metrics_file: Path | None = None,
    diag_file: Path | None = None,
) -> str:
    """Emit Cold-Context `pro [L3]` Semantic & Meta-Governance Auditor prompt."""
    gov = run_gov_audit(repo, profile=profile, metrics_file=metrics_file, diag_file=diag_file)
    p2 = evaluate_plane2_trigger(
        repo,
        profile=profile,
        metrics_file=metrics_file,
        diag_file=diag_file,
        explicit_trigger=trigger,
    )
    trig_s = ",".join(p2["triggers"]) or trigger
    return (
        f"PLANE2_COLD_META_GOV_AUDIT|model=pro[L3]|trigger={trig_s}|budget={p2['used_7d']}/{p2['max_per_week']}/wk\n"
        f"SNAPSHOT|{gov['summary']}\n"
        "PROTOCOL|Zero drafting history. Inspect `COHERENCE_HUB.md`, `domains.json`, `verify_universal.py:@verify_check`, "
        "`meta_governance.py` (`T0-T2` Constitution, `CODE_ANCHORS`, `gov-audit`), `host_matrix.json`, & `federation.json`.\n"
        "MANDATE|Every confirmed finding `[DRIFT|SSOT|STALE|GAP|ANCHOR] @ path` MUST either:\n"
        "  1. Ratchet into a deterministic Plane-1 `@verify_check`/rule classified `T0` (immutable safety, `!decay`) | `T1` (SSOT) | `T2` (heuristic), OR\n"
        "  2. Be logged in `COHERENCE_HUB.md` as `WONTFIX|<id>|<reason>|owner=<owner>|expires_ver=<semver>` (no silent ignores)."
    )
