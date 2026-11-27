#!/usr/bin/env python3
"""Zero-Accumulation Token-Tax & Side-Effect Overhead Auto-Governor (DFP v2.24.0, stdlib-only).

Prevents silent token bloat and multi-turn post-completion ceremony creep across waves without
weakening any T0/T1 safety or bug-prevention guardrails:
  - Always-loaded & boot governance doc byte/char ratchet (verify + shift-left pre-disk DENY).
  - Live in-process hook payload benchmark (SessionStart + PreInvocation T0/T2/T3 & repeat-turn dedup).
  - 1-shot batched post-finish closeout (`plan_tool.py closeout`) with <=6L compact PASS receipt.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPTS_DIR.parent
_HOOKS_DIR = _PLUGIN_ROOT / "hooks"
for _p in (str(_SCRIPTS_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schema_migrations import resolve_token_budget_config  # noqa: E402

_HOOK_MOD_CACHE: dict[str, Any] = {}


def _norm(p: str) -> str:
    s = str(p or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def estimate_tokens(chars: int) -> int:
    """Fast deterministic token estimate (~4 chars/token)."""
    c = max(0, int(chars))
    return (c + 3) // 4


def _git_head_char_count(repo: Path, rel: str) -> int | None:
    if not (repo / ".git").exists():
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            return len(res.stdout)
    except Exception:
        pass
    return None


def _resolved_doc_limits(profile: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    cfg = resolve_token_budget_config(profile)
    caps = {_norm(k): int(v) for k, v in (cfg.get("doc_caps") or {}).items() if _norm(k) and int(v) > 0}
    bases = {_norm(k): int(v) for k, v in (cfg.get("doc_baselines") or {}).items() if _norm(k) and int(v) > 0}
    return caps, bases


def check_doc_token_ratchet(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
    *,
    files: list[str] | None = None,
    scan_all: bool = False,
) -> dict[str, Any]:
    """Enforce zero-accumulation char/token caps on always-loaded & boot governance docs (read-only)."""
    cfg = resolve_token_budget_config(profile)
    if not cfg.get("enabled", True):
        return {"enabled": False, "checked": 0, "total_chars": 0, "total_tokens": 0, "docs": {}}

    ratchet = bool(cfg.get("ratchet_enabled", False))
    caps, baselines = _resolved_doc_limits(profile)
    tracked = sorted(set(caps.keys()) | set(baselines.keys()))
    touched = {_norm(f) for f in (files or [])}

    docs_report: dict[str, dict[str, Any]] = {}
    total_chars = 0
    checked = 0

    for rel in tracked:
        full = repo / rel
        if not full.is_file():
            continue
        try:
            cur_chars = len(full.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        checked += 1
        total_chars += cur_chars
        target_cap = caps.get(rel, baselines.get(rel, 16000))
        in_base = rel in baselines
        effective_cap = baselines[rel] if in_base else target_cap

        if ratchet and not scan_all:
            head_chars = _git_head_char_count(repo, rel)
            if head_chars is not None and head_chars > target_cap:
                effective_cap = min(effective_cap, head_chars)

        over_effective = cur_chars > effective_cap
        over_target = cur_chars > target_cap
        docs_report[rel] = {
            "chars": cur_chars,
            "tokens": estimate_tokens(cur_chars),
            "target_cap": target_cap,
            "effective_cap": effective_cap,
            "over_cap": over_effective,
            "over_target": over_target,
        }

        if over_effective:
            delta = cur_chars - effective_cap
            tok_delta = estimate_tokens(delta)
            msg = (
                f"TOKEN_RATCHET|DOC_BLOAT|`{rel}` {cur_chars}c > cap {effective_cap}c "
                f"(+{delta}c / ~{tok_delta} tok) -> FIX: compress/archive stale prose before adding rules."
            )
            if ratchet:
                fails.append(msg)
            else:
                warns.append(msg)
        elif over_target and (not scan_all) and (rel in touched):
            warns.append(
                f"TOKEN_RATCHET|SLIM_ON_TOUCH|`{rel}` {cur_chars}c/{effective_cap}c (target <={target_cap}c) -> "
                f"FIX: trim redundant text while editing."
            )

    return {
        "enabled": True,
        "ratchet_enabled": ratchet,
        "checked": checked,
        "total_chars": total_chars,
        "total_tokens": estimate_tokens(total_chars),
        "docs": docs_report,
    }


def check_shift_left_doc_token_ratchet(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    """Shift-left DENY before disk write if an edit inflates an always-loaded doc over its cap/baseline."""
    cfg = resolve_token_budget_config(profile)
    if not cfg.get("enabled", True) or not cfg.get("ratchet_enabled", False):
        return None
    try:
        from planner_state import normalize_rel_path  # noqa: E402

        rel, inside = normalize_rel_path(raw_path, repo)
    except Exception:
        rel, inside = _norm(raw_path), True
    if not inside or not rel:
        return None

    caps, baselines = _resolved_doc_limits(profile)
    if rel not in caps and rel not in baselines:
        return None

    full = repo / rel
    orig_chars = len(full.read_text(encoding="utf-8", errors="ignore")) if full.is_file() else 0

    if tool_name in ("edit_file", "replace_file_content"):
        target = str(args.get("Target") or args.get("TargetContent") or "")
        replacement = str(args.get("Replacement") or args.get("ReplacementContent") or "")
        projected = max(0, orig_chars - len(target) + len(replacement))
    elif tool_name in ("write_file", "write_to_file"):
        content = str(args.get("Content") or args.get("CodeContent") or "")
        projected = len(content)
    else:
        return None

    target_cap = caps.get(rel, baselines.get(rel, 16000))
    if rel in baselines:
        cap = min(baselines[rel], orig_chars) if orig_chars > target_cap else baselines[rel]
    elif orig_chars > target_cap:
        cap = orig_chars
    else:
        cap = target_cap

    if projected > cap and projected > orig_chars:
        delta = projected - cap
        return (
            f"TOKEN_RATCHET|DOC_BLOAT|`{rel}` projected {projected}c > cap {cap}c "
            f"(+{delta}c / ~{estimate_tokens(delta)} tok) -> "
            f"FIX: compress or archive stale text before adding new rules to always-loaded `{rel}`."
        )
    return None


def _load_hook_mod(hook_name: str) -> Any:
    path = _HOOKS_DIR / hook_name
    key = str(path.resolve())
    if key not in _HOOK_MOD_CACHE:
        spec = importlib.util.spec_from_file_location(f"_tgov_{path.stem}", str(path))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _HOOK_MOD_CACHE[key] = mod
    return _HOOK_MOD_CACHE[key]


@contextlib.contextmanager
def _sim_env(env_extra: dict[str, str], stdin_text: str):
    old_env = os.environ.copy()
    old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        os.environ.update(env_extra)
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = out_buf
        sys.stderr = err_buf
        yield out_buf, err_buf
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
        os.environ.clear()
        os.environ.update(old_env)


def _invoke_hook_inproc(hook_name: str, payload: dict[str, Any], art_dir: Path) -> str:
    mod = _load_hook_mod(hook_name)
    env = {
        "PLANNER_DISABLE_TELEMETRY": "1",
        "PLANNER_ARTIFACT_DIR": str(art_dir),
        "PLANNER_SLOW_HOOK_MS": "999999",
    }
    old_argv = sys.argv
    try:
        sys.argv = [str(_HOOKS_DIR / hook_name)]
        with _sim_env(env, json.dumps(payload, ensure_ascii=False)) as (out_buf, _):
            try:
                mod.main()
            except SystemExit:
                pass
            raw = out_buf.getvalue().strip()
    finally:
        sys.argv = old_argv
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        steps = parsed.get("injectSteps") or []
        if steps and isinstance(steps[0], dict):
            return str(((steps[0].get("systemMessage") or {}).get("systemMessage")) or "")
    except Exception:
        pass
    return ""


def measure_hook_payloads(repo: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """Run fast isolated in-process hook payload benchmark (0 persistent side-effects)."""
    t0 = time.monotonic()
    cfg = resolve_token_budget_config(profile)
    with tempfile.TemporaryDirectory(prefix="dfp_tgov_") as tmp:
        art = Path(tmp) / "art"
        ss_msg = _invoke_hook_inproc(
            "planner_session_start.py",
            {"workspacePaths": [str(repo)], "artifactDirectoryPath": str(art)},
            art,
        )

        art_t0 = Path(tmp) / "art_t0"
        t0_msg = _invoke_hook_inproc(
            "planner_pre_invocation.py",
            {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": str(art_t0),
                "conversationId": "tgov-t0",
                "invocationNum": 1,
                "lastUserInput": "what is the current app version?",
            },
            art_t0,
        )

        art_t2 = Path(tmp) / "art_t2"
        t2_p1 = "implement a new spatial equalizer filter and add the toggle to settings screen"
        t2_p2 = "now adjust the spatial equalizer gain curve and update the settings toggle label"
        t2_m1 = _invoke_hook_inproc(
            "planner_pre_invocation.py",
            {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": str(art_t2),
                "conversationId": "tgov-t2",
                "invocationNum": 1,
                "lastUserInput": t2_p1,
            },
            art_t2,
        )
        t2_m2 = _invoke_hook_inproc(
            "planner_pre_invocation.py",
            {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": str(art_t2),
                "conversationId": "tgov-t2",
                "invocationNum": 2,
                "lastUserInput": t2_p2,
            },
            art_t2,
        )

        art_t3 = Path(tmp) / "art_t3"
        t3_p1 = "design a complete multi-wave neural spatial architecture with sota blueprint and parallel waves"
        t3_p2 = "continue the multi-wave neural spatial architecture refactor across dsp and audio pipelines"
        t3_m1 = _invoke_hook_inproc(
            "planner_pre_invocation.py",
            {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": str(art_t3),
                "conversationId": "tgov-t3",
                "invocationNum": 1,
                "lastUserInput": t3_p1,
            },
            art_t3,
        )
        t3_m2 = _invoke_hook_inproc(
            "planner_pre_invocation.py",
            {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": str(art_t3),
                "conversationId": "tgov-t3",
                "invocationNum": 2,
                "lastUserInput": t3_p2,
            },
            art_t3,
        )

    ss_c = len(ss_msg)
    t0_c = len(t0_msg)
    t2_1, t2_2 = len(t2_m1), len(t2_m2)
    t3_1, t3_2 = len(t3_m1), len(t3_m2)
    dedup_pct = round((1.0 - (t2_2 + t3_2) / max(1, t2_1 + t3_1)) * 100.0, 1)

    violations: list[str] = []
    ss_cap = int(cfg.get("session_start_max_chars") or 1200)
    pre_cap = int(cfg.get("pre_invocation_max_chars") or 1250)
    t0_cap = int(cfg.get("pre_invocation_t0_max_chars") if cfg.get("pre_invocation_t0_max_chars") is not None else 0)
    rep_cap = int(cfg.get("pre_invocation_repeat_max_chars") or 300)

    if ss_c > ss_cap:
        violations.append(f"TOKEN_BUDGET|SessionStart {ss_c}c > cap {ss_cap}c (+{ss_c - ss_cap}c)")
    if t0_c > t0_cap:
        violations.append(f"TOKEN_BUDGET|PreInvocation T0 {t0_c}c > cap {t0_cap}c")
    if t2_1 > pre_cap:
        violations.append(f"TOKEN_BUDGET|PreInvocation T2 turn1 {t2_1}c > cap {pre_cap}c")
    if t3_1 > pre_cap:
        violations.append(f"TOKEN_BUDGET|PreInvocation T3 turn1 {t3_1}c > cap {pre_cap}c")
    if cfg.get("session_dedup_enabled", True):
        if t2_2 > rep_cap:
            violations.append(f"TOKEN_BUDGET|PreInvocation T2 repeat {t2_2}c > cap {rep_cap}c (dedup regression)")
        if t3_2 > rep_cap:
            violations.append(f"TOKEN_BUDGET|PreInvocation T3 repeat {t3_2}c > cap {rep_cap}c (dedup regression)")

    return {
        "ok": not violations,
        "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        "session_start_chars": ss_c,
        "session_start_cap": ss_cap,
        "pre_t0_chars": t0_c,
        "pre_t2_turn1_chars": t2_1,
        "pre_t2_repeat_chars": t2_2,
        "pre_t3_turn1_chars": t3_1,
        "pre_t3_repeat_chars": t3_2,
        "pre_cap": pre_cap,
        "repeat_cap": rep_cap,
        "repeat_dedup_savings_pct": dedup_pct,
        "violations": violations,
    }


def check_token_overhead_budget(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
    *,
    files: list[str] | None = None,
    scan_all: bool = False,
    measure_hooks: bool = False,
) -> dict[str, Any]:
    """Static `@verify_check('token_overhead_budget')` entry point (strictly read-only)."""
    cfg = resolve_token_budget_config(profile)
    if not cfg.get("enabled", True):
        return {"ok": True, "enabled": False}

    # Validate budget thresholds sanity
    ss_cap = int(cfg.get("session_start_max_chars") or 1200)
    pre_cap = int(cfg.get("pre_invocation_max_chars") or 1250)
    rep_cap = int(cfg.get("pre_invocation_repeat_max_chars") or 300)
    if ss_cap > 1800 or pre_cap > 1500 or rep_cap > pre_cap:
        fails.append(
            f"TOKEN_BUDGET|Invalid caps (ss={ss_cap}<=1800, pre={pre_cap}<=1500, repeat={rep_cap}<=pre)."
        )

    doc_res = check_doc_token_ratchet(repo, profile, fails, warns, files=files, scan_all=scan_all)
    hook_res: dict[str, Any] | None = None
    if measure_hooks:
        hook_res = measure_hook_payloads(repo, profile)
        sink = fails if cfg.get("ratchet_enabled", False) else warns
        sink.extend(hook_res.get("violations") or [])
    return {"ok": not fails, "docs": doc_res, "hooks": hook_res}


def run_token_audit(
    repo: Path,
    *,
    profile: dict[str, Any] | None = None,
    include_hooks: bool = True,
) -> dict[str, Any]:
    """Run full read-only token-overhead & side-effect audit (`plan_tool.py token-audit`)."""
    t0 = time.monotonic()
    if profile is None:
        from planner_state import load_project_profile  # noqa: E402

        prof = load_project_profile(repo)
    else:
        prof = profile

    fails: list[str] = []
    warns: list[str] = []
    res = check_token_overhead_budget(repo, prof, fails, warns, scan_all=True, measure_hooks=include_hooks)
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)

    docs = res.get("docs") or {}
    hooks = res.get("hooks") or {}
    status = "OK" if not fails else f"FAIL({len(fails)})"
    hook_part = (
        f"|ss={hooks['session_start_chars']}/{hooks['session_start_cap']}c"
        f"|t0={hooks['pre_t0_chars']}c"
        f"|t2={hooks['pre_t2_turn1_chars']}->{hooks['pre_t2_repeat_chars']}c"
        f"|t3={hooks['pre_t3_turn1_chars']}->{hooks['pre_t3_repeat_chars']}c"
        f"|dedup={hooks['repeat_dedup_savings_pct']}%"
        if hooks
        else "|hooks=skipped"
    )
    summary = (
        f"TOKEN_AUDIT|{status}|docs={docs.get('checked', 0)}({docs.get('total_chars', 0)}c/~{docs.get('total_tokens', 0)}tok)"
        f"{hook_part}|ms={elapsed_ms}"
    )
    return {
        "ok": not fails,
        "read_only": True,
        "elapsed_ms": elapsed_ms,
        "docs": docs,
        "hooks": hooks,
        "fails": fails,
        "warns": warns,
        "summary": summary,
    }


def run_batched_closeout(
    repo: Path,
    *,
    summary: str = "",
    next_act: str = "",
    roadmap: str = "",
    wave: str = "",
    archives: list[str] | None = None,
    compound_note: str = "",
    do_sync: bool = True,
    do_verify: bool = True,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """1-shot batched post-finish closeout: sync + static verify + gov-audit + token-audit + handoff (`<=6L` PASS)."""
    t0 = time.monotonic()
    from planner_state import load_project_profile  # noqa: E402

    prof = profile if isinstance(profile, dict) else load_project_profile(repo)
    synced: list[str] = []
    if do_sync:
        try:
            import coherence_engine  # noqa: E402

            cs = coherence_engine.sync_coherence(repo, docs=True, hub=True, local_only=True)
            synced.extend(cs.get("touched") or [])
        except Exception:
            pass
        try:
            import blueprint_hub  # noqa: E402

            hs = blueprint_hub.sync_plans_hub(repo, profile=prof)
            if hs.get("changed"):
                synced.append(str(hs.get("rel_path") or ".agents/docs/PLANS_HUB.md"))
        except Exception:
            pass

    verify_fails: list[str] = []
    verify_warns: list[str] = []
    audited_files = 0
    if do_verify:
        import verify_universal  # noqa: E402

        ch, verify_fails, verify_warns = verify_universal.run_static_checks(repo, prof, scan_all=False)
        audited_files = len(ch)

    import meta_governance  # noqa: E402

    gov = meta_governance.run_gov_audit(repo, profile=prof)
    tok = run_token_audit(repo, profile=prof, include_hooks=False)

    handoff_written = False
    if summary or next_act:
        import plan_tool  # noqa: E402

        ns = argparse.Namespace(
            summary=summary,
            next=next_act,
            roadmap=roadmap,
            wave=wave,
            archive=list(archives or []),
            compound_note=compound_note,
            clear=False,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            plan_tool.cmd_handoff(ns, repo)
        handoff_written = True

    all_fails = list(verify_fails) + [f for f in gov.get("fails") or [] if f not in verify_fails] + [
        f for f in tok.get("fails") or [] if f not in verify_fails
    ]
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
    status = "OK" if not all_fails else f"FAIL({len(all_fails)})"
    receipt = [
        f"CLOSEOUT|{status}|files={audited_files}|sync={len(synced)}|handoff={'YES' if handoff_written else 'SKIP'}|ms={elapsed_ms}",
        gov["summary"],
        tok["summary"],
    ]
    if all_fails:
        receipt.extend(f"  [FAIL] {f}" for f in all_fails[:8])
    return {
        "ok": not all_fails,
        "elapsed_ms": elapsed_ms,
        "synced": synced,
        "audited_files": audited_files,
        "handoff_written": handoff_written,
        "fails": all_fails,
        "warns": verify_warns,
        "receipt_lines": receipt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Accumulation Token-Tax & Overhead Auto-Governor.")
    parser.add_argument("--workspace", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_aud = sub.add_parser("audit")
    p_aud.add_argument("--no-hooks", action="store_true")
    p_aud.add_argument("--json", action="store_true")

    p_cls = sub.add_parser("closeout")
    p_cls.add_argument("--summary", default="")
    p_cls.add_argument("--next", default="")
    p_cls.add_argument("--roadmap", default="")
    p_cls.add_argument("--wave", default="")
    p_cls.add_argument("--archive", action="append")
    p_cls.add_argument("--compound-note", default="")
    p_cls.add_argument("--no-sync", action="store_true")
    p_cls.add_argument("--no-verify", action="store_true")
    p_cls.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    repo = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    if args.cmd == "audit":
        res = run_token_audit(repo, include_hooks=(not args.no_hooks))
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(res["summary"])
            for w in res["warns"]:
                print(f"  [WARN] {w}")
            for f in res["fails"]:
                print(f"  [FAIL] {f}")
        return 0 if res["ok"] else 1
    if args.cmd == "closeout":
        res = run_batched_closeout(
            repo,
            summary=args.summary,
            next_act=args.next,
            roadmap=args.roadmap,
            wave=args.wave,
            archives=args.archive,
            compound_note=args.compound_note,
            do_sync=(not args.no_sync),
            do_verify=(not args.no_verify),
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print("\n".join(res["receipt_lines"]))
        return 0 if res["ok"] else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
