#!/usr/bin/env python3
"""Universal Adversarial QA Gate, Frozen Regression Contracts & Cold-Context Destroyer Prompt Engine.

Part of `dynamic-fractal-planner` (`v2.20.0+`). Pure Python stdlib (zero external deps).
Provides:
1. Frozen "1 bug = 1 permanent automated test first" regression contract schema & offline verifier.
2. Blocking QA Gate verdict evaluator (`CRITICAL=0, HIGH=0, GEOM=0, EXIT_CRASH_NEW=0, P95<=12ms, CONTRACTS=0`).
3. Isolated Cold-Context Adversarial QA subagent prompt builder (zero builder rationale / chat leakage).
4. CLI (`plan_tool.py qa {status|verify-contracts|gate|prompt|add-contract}`).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = SCRIPTS_DIR.parent / "hooks"
for _p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from planner_state import load_project_profile  # noqa: E402
from schema_migrations import DEFAULT_ADVERSARIAL_QA_CONFIG, resolve_adversarial_qa_config  # noqa: E402

GEOM_DEFECT_CATEGORIES = frozenset({
    "ui_screen_overflow",
    "ui_parent_horizontal_clip",
    "ui_sibling_overlap",
    "ui_zero_size_visible",
    "ui_zero_size_button",
    "ui_zero_size_interactive",
    "ui_button_text_ellipsized",
    "ui_text_hard_clipped",
    "ui_crushed_touch_target",
    "ui_wcag_contrast_fail",
    "ui_visual_regression_ssim",
    "reflow_layout_defect",
})

EXIT_CRASH_CATEGORIES = frozenset({
    "exit_info_crash",
    "exit_info_anr",
    "exit_info_lmkd_fg",
    "external_watchdog_anr",
    "external_watchdog_pid_restart",
    "external_watchdog_crash",
    "soak_fatal_crash",
})

VALID_CONTRACT_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM"})
VALID_CONTRACT_MODES = frozenset({"offline", "live", "hybrid"})
VALID_CONTRACT_STATUSES = frozenset({"frozen", "waived"})
VALID_OFFLINE_CHECK_KINDS = frozenset({
    "file_exists",
    "file_contains_regex",
    "file_forbids_regex",
    "json_key_equals",
})
CONTRACT_ID_RE = re.compile(r"^REG-[A-Z0-9][A-Z0-9_-]{2,64}$")


def resolve_contracts_path(repo: Path, profile: dict[str, Any] | None = None, explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else (repo / p)
    cfg = resolve_adversarial_qa_config(profile)
    rel = str(cfg.get("contracts_file") or DEFAULT_ADVERSARIAL_QA_CONFIG["contracts_file"])
    p = Path(rel)
    return p if p.is_absolute() else (repo / p)


def load_contracts_registry(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load and parse a frozen regression contracts JSON registry."""
    if not path.is_file():
        return None, [f"Contracts registry not found: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [f"Invalid JSON in contracts registry `{path}`: {exc}"]
    if not isinstance(data, dict):
        return None, [f"Contracts registry `{path}` root must be a JSON object"]
    return data, []


def validate_contracts_registry(data: dict[str, Any]) -> list[str]:
    """Validate schema, immutability, anti-tampering, and 1-bug=1-test invariants."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Registry root must be an object"]
    ver = data.get("version")
    if ver != 1:
        errors.append(f"Unsupported contracts schema version: {ver!r} (expected 1)")
    contracts = data.get("contracts")
    if not isinstance(contracts, list):
        errors.append("`contracts` must be a list")
        return errors

    seen_ids: set[str] = set()
    for idx, item in enumerate(contracts):
        prefix = f"contracts[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: entry must be a JSON object")
            continue
        cid = str(item.get("id") or "").strip()
        if not cid or not CONTRACT_ID_RE.match(cid):
            errors.append(f"{prefix}: invalid or missing `id` ({cid!r}); must match REG-[A-Z0-9_-]+")
        elif cid in seen_ids:
            errors.append(f"{prefix}: duplicate contract id `{cid}`")
        else:
            seen_ids.add(cid)

        for req_k in ("bug_ref", "title", "domain", "test_ref"):
            if not str(item.get(req_k) or "").strip():
                errors.append(f"{cid or prefix}: missing required non-empty `{req_k}` (1-bug=1-test rule)")

        sev = str(item.get("severity") or "").upper()
        if sev not in VALID_CONTRACT_SEVERITIES:
            errors.append(f"{cid or prefix}: invalid severity `{sev}`")

        mode = str(item.get("mode") or "").lower()
        if mode not in VALID_CONTRACT_MODES:
            errors.append(f"{cid or prefix}: invalid mode `{mode}` (expected offline|live|hybrid)")

        status = str(item.get("status") or "").lower()
        if status not in VALID_CONTRACT_STATUSES:
            errors.append(
                f"{cid or prefix}: invalid status `{status}` (silently disabling frozen contracts is forbidden; use `frozen` or documented `waived`)"
            )
        elif status == "waived" and len(str(item.get("waiver_reason") or "").strip()) < 10:
            errors.append(f"{cid or prefix}: `status=waived` requires a substantive `waiver_reason` (>=10 chars)")

        if item.get("immutable") is False and status != "waived":
            errors.append(f"{cid or prefix}: frozen contracts must keep `immutable: true`")

        offline_checks = item.get("offline_checks")
        live_check = item.get("live_check")
        has_off = isinstance(offline_checks, list) and len(offline_checks) > 0
        has_live = isinstance(live_check, dict) and bool(live_check.get("kind"))

        if mode == "offline" and not has_off:
            errors.append(f"{cid or prefix}: mode=offline requires >=1 `offline_checks`")
        if mode == "live" and not has_live:
            errors.append(f"{cid or prefix}: mode=live requires `live_check` descriptor")
        if mode == "hybrid" and not (has_off and has_live):
            errors.append(f"{cid or prefix}: mode=hybrid requires both `offline_checks` and `live_check`")

        if isinstance(offline_checks, list):
            for c_idx, chk in enumerate(offline_checks):
                if not isinstance(chk, dict):
                    errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: must be an object")
                    continue
                kind = str(chk.get("kind") or "")
                if kind not in VALID_OFFLINE_CHECK_KINDS:
                    errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: unknown kind `{kind}`")
                    continue
                if not str(chk.get("path") or "").strip():
                    errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: missing `path`")
                if kind in ("file_contains_regex", "file_forbids_regex"):
                    pat = chk.get("pattern")
                    if not isinstance(pat, str) or not pat:
                        errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: missing regex `pattern`")
                    else:
                        try:
                            re.compile(pat, re.MULTILINE)
                        except re.error as exc:
                            errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: invalid regex: {exc}")
                elif kind == "json_key_equals":
                    if not str(chk.get("key") or "").strip():
                        errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: missing `key`")
                    if "expected" not in chk:
                        errors.append(f"{cid or prefix}.offline_checks[{c_idx}]: missing `expected`")
    return errors


def _lookup_dotted_json_key(obj: Any, dotted_key: str) -> tuple[bool, Any]:
    cur = obj
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def evaluate_offline_contracts(repo: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Deterministically execute all offline frozen regression contracts against `repo`."""
    schema_errors = validate_contracts_registry(data)
    contracts = data.get("contracts") if isinstance(data, dict) else []
    if not isinstance(contracts, list):
        contracts = []

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    waived_count = 0
    live_only_count = 0

    for item in contracts:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "REG-UNKNOWN")
        status = str(item.get("status") or "frozen").lower()
        sev = str(item.get("severity") or "HIGH").upper()
        title = str(item.get("title") or cid)
        mode = str(item.get("mode") or "offline").lower()
        if status == "waived":
            waived_count += 1
            results.append({"id": cid, "status": "waived", "mode": mode, "severity": sev, "detail": item.get("waiver_reason", "")})
            continue

        checks = item.get("offline_checks") or []
        if not checks:
            live_only_count += 1
            results.append({"id": cid, "status": "live_only", "mode": mode, "severity": sev, "detail": "evaluated on live target"})
            continue

        check_errors: list[str] = []
        for chk in checks:
            if not isinstance(chk, dict):
                continue
            kind = str(chk.get("kind") or "")
            rel = str(chk.get("path") or "").replace("\\", "/")
            full = repo / rel
            if kind == "file_exists":
                if not full.is_file():
                    check_errors.append(f"file missing: `{rel}`")
                continue
            if not full.is_file():
                check_errors.append(f"file missing for `{kind}`: `{rel}`")
                continue
            raw_text = full.read_text(encoding="utf-8", errors="ignore")
            if kind == "file_contains_regex":
                pat = str(chk.get("pattern") or "")
                flags = re.MULTILINE | (re.DOTALL if chk.get("dotall") else 0)
                if not re.search(pat, raw_text, flags):
                    msg = str(chk.get("message") or f"`{rel}` missing required pattern `{pat}`")
                    check_errors.append(msg)
            elif kind == "file_forbids_regex":
                pat = str(chk.get("pattern") or "")
                flags = re.MULTILINE | (re.DOTALL if chk.get("dotall") else 0)
                if re.search(pat, raw_text, flags):
                    msg = str(chk.get("message") or f"`{rel}` matched forbidden pattern `{pat}`")
                    check_errors.append(msg)
            elif kind == "json_key_equals":
                try:
                    parsed = json.loads(raw_text)
                except Exception as exc:
                    check_errors.append(f"`{rel}` invalid JSON: {exc}")
                    continue
                found, actual = _lookup_dotted_json_key(parsed, str(chk.get("key") or ""))
                expected = chk.get("expected")
                if not found or actual != expected:
                    check_errors.append(
                        f"`{rel}` key `{chk.get('key')}` expected {expected!r}, got {actual!r}"
                    )

        if check_errors:
            failed_count += 1
            entry = {
                "id": cid,
                "status": "FAIL",
                "mode": mode,
                "severity": sev,
                "title": title,
                "bug_ref": str(item.get("bug_ref") or ""),
                "test_ref": str(item.get("test_ref") or ""),
                "errors": check_errors,
            }
            results.append(entry)
            failures.append(entry)
        else:
            passed_count += 1
            results.append({"id": cid, "status": "PASS", "mode": mode, "severity": sev, "title": title})

    ok = (len(schema_errors) == 0) and (failed_count == 0)
    return {
        "ok": ok,
        "schemaErrors": schema_errors,
        "total": len(contracts),
        "offlineChecked": passed_count + failed_count,
        "passed": passed_count,
        "failed": failed_count,
        "waived": waived_count,
        "liveOnly": live_only_count,
        "results": results,
        "failures": failures,
    }


def evaluate_qa_gate(
    report: dict[str, Any] | None = None,
    contracts_eval: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a QA run report + contracts result against blocking Adversarial QA Gate thresholds.

    Default blocking policy:
      CRITICAL == 0, HIGH == 0, GEOM == 0, EXIT_CRASH_NEW == 0, P95 <= 12.0 ms, CONTRACTS_FAIL == 0.
    Historical archive findings (`origin == 'historical_archive'`) are strictly non-blocking.
    """
    cfg = copy.deepcopy(DEFAULT_ADVERSARIAL_QA_CONFIG)
    if isinstance(thresholds, dict):
        for k, v in thresholds.items():
            if v is not None:
                cfg[k] = v

    rep = report if isinstance(report, dict) else {}
    findings = rep.get("findings") if isinstance(rep.get("findings"), list) else []
    active = [f for f in findings if isinstance(f, dict) and str(f.get("origin") or "live_triggered") != "historical_archive"]

    crit_count = sum(1 for f in active if str(f.get("severity") or "").upper() == "CRITICAL")
    high_count = sum(1 for f in active if str(f.get("severity") or "").upper() == "HIGH")
    geom_count = sum(
        1 for f in active
        if str(f.get("category") or "") in GEOM_DEFECT_CATEGORIES
        and str(f.get("severity") or "").upper() in ("CRITICAL", "HIGH")
    )
    exit_crash_from_findings = sum(1 for f in active if str(f.get("category") or "") in EXIT_CRASH_CATEGORIES)

    q2 = rep.get("q2GateSummary") if isinstance(rep.get("q2GateSummary"), dict) else {}
    exit_crash_from_q2 = int(q2.get("exitInfoNewFatal") or 0)
    exit_crash_new = max(exit_crash_from_findings, exit_crash_from_q2)

    p95_val = 0.0
    cpu_p95 = q2.get("frameCpuP95Ms")
    raw_p95 = q2.get("frameP95Ms")
    total_frames = int(q2.get("totalFrames") or 0)
    if isinstance(cpu_p95, (int, float)) and float(cpu_p95) > 0:
        p95_val = float(cpu_p95)
    elif isinstance(raw_p95, (int, float)) and float(raw_p95) > 0:
        p95_val = float(raw_p95)
    frame_sample_valid = (total_frames >= 10) or (p95_val > 0 and "totalFrames" not in q2)

    q3 = rep.get("q3SoakSummary") if isinstance(rep.get("q3SoakSummary"), dict) else {}
    soak_ran = int(q3.get("cycles") or 0) > 0
    soak_failed = bool(soak_ran and (q3.get("passed") is False))

    q4 = rep.get("q4GateSummary") if isinstance(rep.get("q4GateSummary"), dict) else {}
    contracts_failed = int(q4.get("failed") or 0)
    contracts_passed = int(q4.get("passed") or 0)
    contracts_total = int(q4.get("total") or 0)
    schema_errors: list[str] = list(q4.get("schemaErrors") or [])

    if isinstance(contracts_eval, dict):
        contracts_failed = max(contracts_failed, int(contracts_eval.get("failed") or 0))
        if contracts_total == 0:
            contracts_passed = int(contracts_eval.get("passed") or 0)
            contracts_total = int(contracts_eval.get("offlineChecked") or contracts_eval.get("total") or 0)
        for err in contracts_eval.get("schemaErrors") or []:
            if err not in schema_errors:
                schema_errors.append(str(err))

    # Also count live regression_contract_failed findings if not already reflected
    reg_findings = sum(1 for f in active if str(f.get("category") or "") == "regression_contract_failed")
    contracts_failed = max(contracts_failed, reg_findings)

    max_crit = int(cfg.get("max_critical", 0))
    max_high = int(cfg.get("max_high", 0))
    max_geom = int(cfg.get("max_geom", 0))
    max_exit = int(cfg.get("max_exit_crash_new", 0))
    max_p95 = float(cfg.get("max_frame_p95_ms", 12.0))
    max_c_fail = int(cfg.get("max_contracts_failed", 0))

    reasons: list[str] = []
    if rep and rep.get("ok") is False:
        reasons.append(f"runner_failed:{rep.get('error', 'unknown')}")
    if crit_count > max_crit:
        reasons.append(f"CRITICAL={crit_count}>{max_crit}")
    if high_count > max_high:
        reasons.append(f"HIGH={high_count}>{max_high}")
    if geom_count > max_geom:
        reasons.append(f"GEOM={geom_count}>{max_geom}")
    if exit_crash_new > max_exit:
        reasons.append(f"EXIT_CRASH_NEW={exit_crash_new}>{max_exit}")
    if frame_sample_valid and p95_val > max_p95:
        reasons.append(f"P95={p95_val:.2f}ms>{max_p95:.1f}ms")
    if soak_failed and bool(cfg.get("require_soak_pass", True)):
        reasons.append("SOAK_V11=FAIL")
    if schema_errors:
        reasons.append(f"CONTRACT_SCHEMA_ERRORS={len(schema_errors)}")
    if contracts_failed > max_c_fail:
        reasons.append(f"CONTRACTS_FAIL={contracts_failed}>{max_c_fail}")

    passed = len(reasons) == 0
    verdict = "PASS" if passed else "FAIL"
    summary_line = (
        f"QA_GATE|VERDICT={verdict}|CRIT={crit_count}|HIGH={high_count}|GEOM={geom_count}|"
        f"EXIT_CRASH_NEW={exit_crash_new}|P95={p95_val:.1f}ms|"
        f"CONTRACTS={contracts_passed}/{contracts_total}(fail={contracts_failed})"
        + (f"|REASONS={','.join(reasons)}" if reasons else "|REASONS=none")
    )
    return {
        "ok": passed,
        "verdict": verdict,
        "metrics": {
            "critical": crit_count,
            "high": high_count,
            "geom": geom_count,
            "exitCrashNew": exit_crash_new,
            "frameP95Ms": round(p95_val, 2),
            "soakFailed": soak_failed,
            "contractsPassed": contracts_passed,
            "contractsTotal": contracts_total,
            "contractsFailed": contracts_failed,
            "schemaErrorCount": len(schema_errors),
        },
        "thresholds": {
            "maxCritical": max_crit,
            "maxHigh": max_high,
            "maxGeom": max_geom,
            "maxExitCrashNew": max_exit,
            "maxFrameP95Ms": max_p95,
            "maxContractsFailed": max_c_fail,
        },
        "reasons": reasons,
        "summaryLine": summary_line,
    }


def build_cold_context_qa_prompt(
    goal: str,
    changed_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    profile: dict[str, Any] | None = None,
    contracts_count: int = 0,
) -> dict[str, Any]:
    """Generate a strictly isolated Cold-Context Adversarial QA Destroyer prompt.

    Deliberately excludes builder rationale, chat history, or self-justification to eliminate
    author confirmation bias. Provides ONLY acceptance criteria, changed paths, frozen contract
    commands, and blocking gate thresholds.
    """
    cfg = resolve_adversarial_qa_config(profile)
    clean_goal = str(goal or "").strip() or "Verify changed product flows without regressions."
    files = [str(f).replace("\\", "/").strip() for f in (changed_files or []) if str(f).strip()]
    acc = [str(a).strip() for a in (acceptance_criteria or []) if str(a).strip()]
    gate_cmd = str(cfg.get("gate_command") or "").strip()
    contracts_file = str(cfg.get("contracts_file") or DEFAULT_ADVERSARIAL_QA_CONFIG["contracts_file"])
    model_tier = str(cfg.get("destroyer_model_tier") or "flash").strip() or "flash"

    thresh_str = (
        f"CRITICAL<={int(cfg.get('max_critical', 0))}, "
        f"HIGH<={int(cfg.get('max_high', 0))}, "
        f"GEOM<={int(cfg.get('max_geom', 0))}, "
        f"EXIT_CRASH_NEW<={int(cfg.get('max_exit_crash_new', 0))}, "
        f"P95<={float(cfg.get('max_frame_p95_ms', 12.0)):.1f}ms, "
        f"CONTRACTS_FAIL<={int(cfg.get('max_contracts_failed', 0))}"
    )

    lines = [
        "ROLE: COLD_CONTEXT_ADVERSARIAL_QA_DESTROYER (fresh 1-shot isolated subagent; zero builder context).",
        "MANDATE: Assume hidden defects exist. Never trust builder summaries; prove everything via deterministic tools & evidence.",
        f"TARGET_SPEC: {clean_goal}",
    ]
    if acc:
        lines.append("ACCEPTANCE_CRITERIA:")
        for idx, item in enumerate(acc[:8], 1):
            lines.append(f"  {idx}. {item}")
    if files:
        lines.append("CHANGED_FILES: " + ", ".join(files[:20]))
    lines.append(
        f"FROZEN_CONTRACTS: `{contracts_file}` ({contracts_count} registered). "
        "ENFORCE 1-bug=1-test-first: every newly discovered defect MUST be recorded as a permanent contract before fixing."
    )
    if gate_cmd:
        lines.append(f"EXEC_GATE_CMD: `{gate_cmd}`")
    lines.append(f"BLOCKING_THRESHOLDS: {thresh_str}")
    lines.append(
        "OUTPUT_FORMAT (<=20L): Line 1 MUST be exact `QA_GATE|VERDICT=PASS|FAIL|CRIT=..|HIGH=..|GEOM=..|EXIT_CRASH_NEW=..|P95=..ms|CONTRACTS=..`; "
        "follow with compact repro lines ONLY for failures (`[SEV] category @ file/vector -> exact repro`)."
    )
    return {
        "isolated": True,
        "stripBuilderHistory": True,
        "recommendedModelTier": model_tier,
        "thresholds": thresh_str,
        "gateCommand": gate_cmd,
        "contractsFile": contracts_file,
        "prompt": "\n".join(lines),
    }


def add_regression_contract(
    path: Path,
    *,
    contract_id: str,
    bug_ref: str,
    title: str,
    domain: str,
    test_ref: str,
    severity: str = "HIGH",
    mode: str = "offline",
    check_file: str = "",
    must_match: str = "",
    forbid_match: str = "",
    live_kind: str = "",
) -> tuple[bool, str]:
    """Append a new frozen 1-bug=1-test contract to `regression_contracts.json`."""
    if path.is_file():
        data, errs = load_contracts_registry(path)
        if errs or data is None:
            return False, "; ".join(errs)
    else:
        data = {
            "version": 1,
            "policy": "1-bug=1-permanent-automated-test-first",
            "contracts": [],
        }

    cid = contract_id.strip().upper()
    if any(isinstance(c, dict) and c.get("id") == cid for c in data.get("contracts") or []):
        return False, f"Contract `{cid}` already exists (frozen contracts are immutable)"

    offline_checks: list[dict[str, Any]] = []
    if check_file and must_match:
        offline_checks.append({
            "kind": "file_contains_regex",
            "path": check_file.replace("\\", "/"),
            "pattern": must_match,
        })
    if check_file and forbid_match:
        offline_checks.append({
            "kind": "file_forbids_regex",
            "path": check_file.replace("\\", "/"),
            "pattern": forbid_match,
        })
    if check_file and not must_match and not forbid_match:
        offline_checks.append({
            "kind": "file_exists",
            "path": check_file.replace("\\", "/"),
        })

    entry: dict[str, Any] = {
        "id": cid,
        "bug_ref": bug_ref.strip(),
        "title": title.strip(),
        "domain": domain.strip(),
        "severity": severity.strip().upper(),
        "status": "frozen",
        "immutable": True,
        "mode": mode.strip().lower(),
        "test_ref": test_ref.strip(),
        "added_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "offline_checks": offline_checks,
    }
    if live_kind:
        entry["live_check"] = {"kind": live_kind.strip()}

    candidate = copy.deepcopy(data)
    candidate["contracts"].append(entry)
    val_errs = validate_contracts_registry(candidate)
    if val_errs:
        return False, "; ".join(val_errs)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, f"Added frozen contract `{cid}` to `{path}`"


def check_adversarial_qa_contracts(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
) -> None:
    """Static guardrail hook called by `verify_universal.py` when a contracts registry exists."""
    cfg = resolve_adversarial_qa_config(profile)
    cpath = resolve_contracts_path(repo, profile)
    if not cpath.is_file():
        return
    data, load_errs = load_contracts_registry(cpath)
    if load_errs or data is None:
        for e in load_errs:
            fails.append(f"qa_contracts: {e}")
        return
    ev = evaluate_offline_contracts(repo, data)
    for err in ev.get("schemaErrors") or []:
        fails.append(f"qa_contracts schema: {err}")
    strict = bool(cfg.get("strict_verify", True))
    sink = fails if strict else warns
    for fail_item in ev.get("failures") or []:
        errs_s = "; ".join(fail_item.get("errors") or [])
        sink.append(f"qa_contract FAIL `{fail_item['id']}` ({fail_item.get('bug_ref', '')}): {errs_s}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qa_gate.py", description="Universal Adversarial QA Gate & Frozen Regression Contracts.")
    parser.add_argument("--workspace", default=None, help="Workspace root override.")
    sub = parser.add_subparsers(dest="qa_cmd", required=True)

    p_st = sub.add_parser("status", help="Show QA Gate config and frozen regression contract counts.")
    p_st.add_argument("--contracts", default="")
    p_st.add_argument("--json", action="store_true")

    p_vc = sub.add_parser("verify-contracts", help="Validate & run all offline frozen regression contracts.")
    p_vc.add_argument("--contracts", default="")
    p_vc.add_argument("--strict", action="store_true")
    p_vc.add_argument("--json", action="store_true")

    p_gt = sub.add_parser("gate", help="Evaluate a QA JSON report + offline contracts against blocking thresholds.")
    p_gt.add_argument("--report", default="", help="Path to JSON QA report (e.g. cavern_bug_hunter.py --out-json).")
    p_gt.add_argument("--contracts", default="", help="Optional path to regression_contracts.json.")
    p_gt.add_argument("--no-contracts", action="store_true", help="Skip offline contracts evaluation.")
    p_gt.add_argument("--json", action="store_true")

    p_pr = sub.add_parser("prompt", help="Generate isolated Cold-Context Adversarial QA Destroyer subagent prompt.")
    p_pr.add_argument("--goal", required=True)
    p_pr.add_argument("--files", nargs="*", default=[])
    p_pr.add_argument("--acceptance", action="append", default=[])
    p_pr.add_argument("--contracts", default="")
    p_pr.add_argument("--json", action="store_true")

    p_add = sub.add_parser("add-contract", help="Append a new frozen 1-bug=1-test regression contract.")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--bug", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--domain", default="software")
    p_add.add_argument("--severity", choices=("CRITICAL", "HIGH", "MEDIUM"), default="HIGH")
    p_add.add_argument("--mode", choices=("offline", "live", "hybrid"), default="offline")
    p_add.add_argument("--test-ref", required=True)
    p_add.add_argument("--file", default="")
    p_add.add_argument("--must-match", default="")
    p_add.add_argument("--forbid-match", default="")
    p_add.add_argument("--live-kind", default="")
    p_add.add_argument("--contracts", default="")

    args = parser.parse_args(argv)
    ws_root = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    profile = load_project_profile(ws_root)
    cfg = resolve_adversarial_qa_config(profile)

    if args.qa_cmd == "status":
        cpath = resolve_contracts_path(ws_root, profile, args.contracts)
        present = cpath.is_file()
        ev: dict[str, Any] = {}
        if present:
            data, errs = load_contracts_registry(cpath)
            ev = evaluate_offline_contracts(ws_root, data) if (data and not errs) else {"ok": False, "schemaErrors": errs}
        out = {
            "enabled": bool(cfg.get("enabled", True)),
            "contractsFile": str(cpath),
            "contractsPresent": present,
            "thresholds": {
                "maxCritical": cfg["max_critical"],
                "maxHigh": cfg["max_high"],
                "maxGeom": cfg["max_geom"],
                "maxExitCrashNew": cfg["max_exit_crash_new"],
                "maxFrameP95Ms": cfg["max_frame_p95_ms"],
                "maxContractsFailed": cfg["max_contracts_failed"],
            },
            "gateCommand": cfg.get("gate_command", ""),
            "contractsSummary": ev,
        }
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print(
                f"QA_STATUS|enabled={out['enabled']}|contracts={ev.get('passed', 0)}/{ev.get('offlineChecked', 0)} "
                f"(total={ev.get('total', 0)}, fail={ev.get('failed', 0)})|"
                f"CRIT<={cfg['max_critical']}|HIGH<={cfg['max_high']}|GEOM<={cfg['max_geom']}|"
                f"EXIT<={cfg['max_exit_crash_new']}|P95<={cfg['max_frame_p95_ms']}ms"
            )
        return 0

    if args.qa_cmd == "verify-contracts":
        cpath = resolve_contracts_path(ws_root, profile, args.contracts)
        data, errs = load_contracts_registry(cpath)
        if errs or data is None:
            out = {"ok": False, "schemaErrors": errs, "failed": 0, "total": 0}
        else:
            out = evaluate_offline_contracts(ws_root, data)
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            status_tag = "PASS" if out["ok"] else "FAIL"
            print(
                f"QA_CONTRACTS|{status_tag}|total={out.get('total', 0)}|offline_pass={out.get('passed', 0)}|"
                f"fail={out.get('failed', 0)}|live_only={out.get('liveOnly', 0)}|waived={out.get('waived', 0)}"
            )
            for err in out.get("schemaErrors") or []:
                print(f"  [SCHEMA] {err}")
            for f in out.get("failures") or []:
                print(f"  [FAIL] {f['id']} ({f.get('bug_ref', '')}): {'; '.join(f.get('errors') or [])}")
        return 0 if out["ok"] else 2

    if args.qa_cmd == "gate":
        rep: dict[str, Any] = {}
        if args.report:
            rp = Path(args.report)
            if not rp.is_file():
                print(f"QA_GATE|VERDICT=FAIL|REASONS=report_missing:{rp}")
                return 2
            rep = json.loads(rp.read_text(encoding="utf-8"))
        c_eval: dict[str, Any] | None = None
        if not args.no_contracts:
            cpath = resolve_contracts_path(ws_root, profile, args.contracts)
            if cpath.is_file():
                data, errs = load_contracts_registry(cpath)
                c_eval = evaluate_offline_contracts(ws_root, data) if (data and not errs) else {"ok": False, "schemaErrors": errs, "failed": 1}
        verdict = evaluate_qa_gate(rep, c_eval, cfg)
        if args.json:
            print(json.dumps(verdict, indent=2, ensure_ascii=False))
        else:
            print(verdict["summaryLine"])
        return 0 if verdict["ok"] else 2

    if args.qa_cmd == "prompt":
        cpath = resolve_contracts_path(ws_root, profile, args.contracts)
        c_count = 0
        if cpath.is_file():
            data, errs = load_contracts_registry(cpath)
            if data and not errs:
                c_count = len(data.get("contracts") or [])
        built = build_cold_context_qa_prompt(
            args.goal,
            changed_files=args.files,
            acceptance_criteria=args.acceptance,
            profile=profile,
            contracts_count=c_count,
        )
        if args.json:
            print(json.dumps(built, indent=2, ensure_ascii=False))
        else:
            print(built["prompt"])
        return 0

    if args.qa_cmd == "add-contract":
        cpath = resolve_contracts_path(ws_root, profile, args.contracts)
        ok, msg = add_regression_contract(
            cpath,
            contract_id=args.id,
            bug_ref=args.bug,
            title=args.title,
            domain=args.domain,
            test_ref=args.test_ref,
            severity=args.severity,
            mode=args.mode,
            check_file=args.file,
            must_match=args.must_match,
            forbid_match=args.forbid_match,
            live_kind=args.live_kind,
        )
        print(msg)
        return 0 if ok else 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
