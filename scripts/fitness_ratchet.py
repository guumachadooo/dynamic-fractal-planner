#!/usr/bin/env python3
"""Zero-Accumulation Monolith Ratchet & Declarative Fitness Rules Engine (DFP v2.23.0, stdlib-only).

Provides:
  - Zero-Accumulation Monolith Ratchet (`check_code_weight_ratchet`, `check_shift_left_monolith_ratchet`):
    hard-fails any new file crossing `fail_lines` (default 2000L) AND hard-fails any `+1L` growth
    on an existing `>2000L` monolith (via `monolith_baseline` and/or git HEAD), while emitting
    actionable split-on-touch hints above `warn_lines` (default 1600L).
  - Declarative `fitness_rules` Engine (`check_fitness_rules`, `check_shift_left_fitness_rules`):
    evaluates multi-domain (`BL|UI|ARCH|DSP|AI`) architectural/structural fitness rules
    (`forbid_regex`, `max_matches_per_file`, `duplicate_capture_regex`) with per-file/global
    zero-accumulation baselines and inline `remediation_hint` + `lesson_id` links.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_IGNORE_PREFIXES = (".agents/", "Libraries/", "node_modules/")
DEFAULT_CODE_EXTS = (
    ".cs", ".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java",
    ".rs", ".go", ".c", ".cpp", ".h", ".swift",
)


def _norm_rel(path_str: str) -> str:
    s = str(path_str or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


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


def _rule_matches_path(rule: dict[str, Any], rel: str) -> bool:
    prefs: list[str] = []
    if rule.get("path_prefix"):
        prefs.append(_norm_rel(str(rule["path_prefix"])))
    for p in rule.get("path_prefixes") or []:
        ps = _norm_rel(str(p))
        if ps:
            prefs.append(ps)
    if prefs and not any(rel.startswith(p) for p in prefs):
        return False
    preg = str(rule.get("path_regex") or "").strip()
    if preg and not re.search(preg, rel):
        return False
    ex_reg = str(rule.get("exclude_path_regex") or "").strip()
    if ex_reg and re.search(ex_reg, rel):
        return False
    return True


def _git_head_line_count(repo: Path, rel: str) -> int | None:
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
            return len(res.stdout.splitlines())
    except Exception:
        pass
    return None


def resolve_code_weight_settings(profile: dict[str, Any]) -> dict[str, Any]:
    cw = profile.get("code_weight_limits") if isinstance(profile.get("code_weight_limits"), dict) else {}
    raw_base = cw.get("monolith_baseline")
    baseline: dict[str, int] = {}
    if isinstance(raw_base, dict):
        for k, v in raw_base.items():
            nk = _norm_rel(str(k))
            try:
                nv = int(v)
            except (TypeError, ValueError):
                continue
            if nk and nv > 0:
                baseline[nk] = nv
    ignore_prefixes = tuple(
        _norm_rel(str(p))
        for p in (cw.get("ignore_path_prefixes") or DEFAULT_IGNORE_PREFIXES)
        if str(p).strip()
    )
    allowed_exts = {
        str(e).lower()
        for e in (cw.get("extensions") or DEFAULT_CODE_EXTS)
        if str(e).strip()
    }
    return {
        "enabled": cw.get("enabled", True) is not False,
        "ratchet_enabled": bool(cw.get("ratchet_enabled", False)),
        "warn_lines": int(cw.get("warn_lines") or 1500),
        "warn_lines_all": int(cw.get("warn_lines_all") or 2000),
        "fail_lines": int(cw.get("fail_lines") or cw.get("warn_lines_all") or 2000),
        "split_target_lines": int(cw.get("split_target_lines") or 1800),
        "monolith_baseline": baseline,
        "ignore_prefixes": ignore_prefixes,
        "allowed_exts": allowed_exts,
    }


def check_code_weight_ratchet(
    repo: Path,
    files: list[str],
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
    *,
    scan_all: bool = False,
) -> None:
    """Enforce code_weight_limits: legacy warn-only OR hard-blocking Zero-Accumulation Monolith Ratchet."""
    cw = profile.get("code_weight_limits")
    if not isinstance(cw, dict) or not cw.get("enabled", True):
        return
    cfg = resolve_code_weight_settings(profile)
    if not cfg["enabled"]:
        return

    ratchet = cfg["ratchet_enabled"]
    warn_lines = cfg["warn_lines"]
    warn_lines_all = cfg["warn_lines_all"]
    fail_lines = cfg["fail_lines"]
    split_target = cfg["split_target_lines"]
    baseline = cfg["monolith_baseline"]
    ignore_prefixes = cfg["ignore_prefixes"]
    allowed_exts = cfg["allowed_exts"]

    if not ratchet:
        limit = warn_lines_all if scan_all else warn_lines
        if limit <= 0:
            return
        for rel in files:
            norm = _norm_rel(rel)
            if any(norm.startswith(pref) for pref in ignore_prefixes):
                continue
            if Path(norm).suffix.lower() not in allowed_exts:
                continue
            full = repo / norm
            if not full.is_file():
                continue
            try:
                n_lines = len(full.read_text(encoding="utf-8", errors="ignore").splitlines())
            except Exception:
                continue
            if n_lines > limit:
                warns.append(
                    f"{norm}: {n_lines}L (>{limit}L) — consider splitting into modular/partial files (`code_weight_limits`)."
                )
        return

    for rel in files:
        norm = _norm_rel(rel)
        if any(norm.startswith(pref) for pref in ignore_prefixes):
            continue
        if Path(norm).suffix.lower() not in allowed_exts:
            continue
        full = repo / norm
        if not full.is_file():
            continue
        try:
            n_lines = len(full.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            continue

        if fail_lines > 0 and n_lines > fail_lines:
            in_base = norm in baseline
            cap = baseline[norm] if in_base else fail_lines
            head_lines = _git_head_line_count(repo, norm) if not scan_all else None
            if head_lines is not None and head_lines > fail_lines:
                cap = min(cap, head_lines) if in_base else min(fail_lines, head_lines)
            if n_lines > cap:
                delta = n_lines - cap
                kind = "GROWN_MONOLITH" if in_base else "NEW_MONOLITH"
                fails.append(
                    f"MONOLITH_RATCHET|{kind}|{norm}: {n_lines}L > cap {cap}L (+{delta}L) -> "
                    f"FIX: split cohesive helpers/partials out of `{norm}` below {split_target}L before adding lines (ARCH-001)."
                )
            elif not scan_all:
                warns.append(
                    f"MONOLITH_RATCHET|SPLIT_ON_TOUCH|{norm}: {n_lines}L/{cap}L (>{fail_lines}L baseline) -> "
                    f"FIX: extract a focused partial/module toward <{split_target}L while touching this file (ARCH-001)."
                )
        elif not scan_all and warn_lines > 0 and n_lines > warn_lines:
            warns.append(
                f"MONOLITH_RATCHET|NEAR_CAP|{norm}: {n_lines}L (>{warn_lines}L warn, hard cap {fail_lines}L) -> "
                f"FIX: split into modular files before reaching {fail_lines}L (ARCH-001)."
            )


def check_shift_left_monolith_ratchet(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    """Block +1L growth on >2,000L monoliths (or new files crossing fail_lines) BEFORE disk write."""
    cw = profile.get("code_weight_limits")
    if not isinstance(cw, dict) or not cw.get("enabled", True) or not cw.get("ratchet_enabled", False):
        return None
    cfg = resolve_code_weight_settings(profile)
    fail_lines = cfg["fail_lines"]
    if fail_lines <= 0:
        return None

    try:
        from planner_state import normalize_rel_path  # noqa: E402

        rel, inside = normalize_rel_path(raw_path, repo)
    except Exception:
        rel, inside = _norm_rel(raw_path), True
    if not inside or not rel:
        return None
    if any(rel.startswith(pref) for pref in cfg["ignore_prefixes"]):
        return None
    if Path(rel).suffix.lower() not in cfg["allowed_exts"]:
        return None

    full = repo / rel
    orig_lines = len(full.read_text(encoding="utf-8", errors="ignore").splitlines()) if full.is_file() else 0

    if tool_name in ("edit_file", "replace_file_content"):
        target = args.get("Target") or args.get("TargetContent") or ""
        replacement = args.get("Replacement") or args.get("ReplacementContent") or ""
        delta = len(replacement.splitlines()) - len(target.splitlines())
        projected = max(0, orig_lines + delta)
    elif tool_name in ("write_file", "write_to_file"):
        content = args.get("Content") or args.get("CodeContent") or ""
        projected = len(content.splitlines())
    else:
        return None

    if projected <= fail_lines:
        return None

    baseline = cfg["monolith_baseline"]
    in_base = rel in baseline
    if in_base:
        cap = min(baseline[rel], orig_lines) if orig_lines > fail_lines else baseline[rel]
    elif orig_lines > fail_lines:
        cap = orig_lines
    else:
        cap = fail_lines

    if projected > cap:
        delta_over = projected - cap
        kind = "GROWN_MONOLITH" if (in_base or orig_lines > fail_lines) else "NEW_MONOLITH"
        return (
            f"MONOLITH_RATCHET|{kind}|`{rel}` projected {projected}L > cap {cap}L (+{delta_over}L) -> "
            f"FIX: split cohesive logic into a new partial/service file (<{cfg['split_target_lines']}L) instead of growing `{rel}` (ARCH-001)."
        )
    return None


def _extract_added_lines_from_edit(tool_name: str, args: dict[str, Any]) -> list[str]:
    if tool_name in ("edit_file", "replace_file_content"):
        target = args.get("Target") or args.get("TargetContent") or ""
        replacement = args.get("Replacement") or args.get("ReplacementContent") or ""
        tgt_counts = Counter(target.splitlines())
        added: list[str] = []
        for ln in replacement.splitlines():
            if tgt_counts.get(ln, 0) > 0:
                tgt_counts[ln] -= 1
            else:
                added.append(ln)
        return added
    content = args.get("Content") or args.get("CodeContent") or ""
    return content.splitlines()


def _format_fitness_msg(rule: dict[str, Any], detail: str) -> str:
    rid = str(rule.get("id") or "FITNESS-RULE").strip()
    lid = str(rule.get("lesson_id") or "").strip()
    tag = f"FITNESS|{rid}|{lid}" if lid else f"FITNESS|{rid}"
    msg = str(rule.get("message") or "Architectural fitness rule violated.").strip()
    hint = str(rule.get("remediation_hint") or "").strip()
    suffix = f" | FIX: {hint}" if hint else ""
    return f"[{tag}] {detail}: {msg}{suffix}"


def _count_regex_matches_in_file(
    path: Path,
    pattern: re.Pattern[str],
    *,
    strip_comments: bool = True,
    strip_q: bool = False,
) -> tuple[int, int]:
    """Return `(match_count, first_line_number)` for `pattern` in `path`."""
    count = 0
    first_ln = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return 0, 0
    for idx, raw in enumerate(lines, start=1):
        code = _strip_line_comment(raw) if strip_comments else raw
        if not code.strip():
            continue
        target = _strip_strings(code) if strip_q else code
        if pattern.search(target):
            count += 1
            if first_ln == 0:
                first_ln = idx
    return count, first_ln


def check_fitness_rules(
    repo: Path,
    files: list[str],
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
    *,
    scan_all: bool = False,
) -> None:
    """Evaluate declarative `profile.fitness_rules` across `files` with zero-accumulation baselines."""
    rules = profile.get("fitness_rules") or []
    if not isinstance(rules, list) or not rules:
        return

    norm_files = [_norm_rel(f) for f in files]
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled", True) is False:
            continue
        sev = str(rule.get("severity") or "fail").strip().lower()
        bucket = warns if sev == "warn" else fails
        strip_comm = bool(rule.get("strip_line_comments", True))
        strip_q = bool(rule.get("strip_strings", False))
        matching_files = [rel for rel in norm_files if _rule_matches_path(rule, rel) and (repo / rel).is_file()]
        if not matching_files:
            continue

        base_per_file: dict[str, int] = {}
        raw_bpf = rule.get("baseline_per_file")
        if isinstance(raw_bpf, dict):
            for k, v in raw_bpf.items():
                try:
                    base_per_file[_norm_rel(str(k))] = int(v)
                except (TypeError, ValueError):
                    pass

        forbid_pat_s = str(rule.get("forbid_regex") or "").strip()
        if forbid_pat_s:
            pat = re.compile(forbid_pat_s)
            default_cap = int(rule.get("baseline_max_matches") or 0)
            for rel in matching_files:
                cnt, first_ln = _count_regex_matches_in_file(
                    repo / rel, pat, strip_comments=strip_comm, strip_q=strip_q
                )
                cap = base_per_file.get(rel, default_cap)
                if cnt > cap:
                    bucket.append(_format_fitness_msg(rule, f"{rel}:{first_ln} ({cnt} > cap {cap})"))
            continue

        match_pat_s = str(rule.get("match_regex") or "").strip()
        max_per_file = rule.get("max_matches_per_file")
        if match_pat_s and max_per_file is not None:
            pat = re.compile(match_pat_s)
            default_cap = int(max_per_file)
            for rel in matching_files:
                cnt, first_ln = _count_regex_matches_in_file(
                    repo / rel, pat, strip_comments=strip_comm, strip_q=strip_q
                )
                cap = base_per_file.get(rel, default_cap)
                if cnt > cap:
                    bucket.append(_format_fitness_msg(rule, f"{rel}:{first_ln} ({cnt} > max {cap})"))
            continue

        dup_pat_s = str(rule.get("duplicate_capture_regex") or "").strip()
        if dup_pat_s:
            pat = re.compile(dup_pat_s)
            max_occ = int(rule.get("max_occurrences") or 1)
            scope = str(rule.get("scope") or "per_file").strip().lower()
            raw_bpk = rule.get("baseline_per_key")
            base_per_key: dict[str, int] = {}
            if isinstance(raw_bpk, dict):
                for k, v in raw_bpk.items():
                    try:
                        base_per_key[str(k)] = int(v)
                    except (TypeError, ValueError):
                        pass

            if scope == "cross_file":
                occ_map: dict[str, list[str]] = {}
                for rel in matching_files:
                    try:
                        lines = (repo / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
                    except Exception:
                        continue
                    for idx, raw in enumerate(lines, start=1):
                        code = _strip_line_comment(raw) if strip_comm else raw
                        target = _strip_strings(code) if strip_q else code
                        for m in pat.finditer(target):
                            key = m.group(1) if m.groups() else m.group(0)
                            if key:
                                occ_map.setdefault(key, []).append(f"{rel}:{idx}")
                for key, locs in sorted(occ_map.items()):
                    cap = base_per_key.get(key, max_occ)
                    if len(locs) > cap:
                        bucket.append(
                            _format_fitness_msg(rule, f"duplicate `{key}` ({len(locs)} > cap {cap} @ {', '.join(locs[:3])})")
                        )
            else:
                for rel in matching_files:
                    occ_map_f: dict[str, list[int]] = {}
                    try:
                        lines = (repo / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
                    except Exception:
                        continue
                    for idx, raw in enumerate(lines, start=1):
                        code = _strip_line_comment(raw) if strip_comm else raw
                        target = _strip_strings(code) if strip_q else code
                        for m in pat.finditer(target):
                            key = m.group(1) if m.groups() else m.group(0)
                            if key:
                                occ_map_f.setdefault(key, []).append(idx)
                    for key, lns in sorted(occ_map_f.items()):
                        cap = base_per_key.get(f"{rel}:{key}", base_per_key.get(key, max_occ))
                        if len(lns) > cap:
                            bucket.append(
                                _format_fitness_msg(rule, f"{rel}:{lns[0]} duplicate `{key}` ({len(lns)} > cap {cap})")
                            )


def check_shift_left_fitness_rules(
    tool_name: str,
    args: dict[str, Any],
    raw_path: str,
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    """Shift-left block newly added lines that violate `fail`-severity `forbid_regex` fitness rules."""
    rules = profile.get("fitness_rules") or []
    if not isinstance(rules, list) or not rules:
        return None
    try:
        from planner_state import normalize_rel_path  # noqa: E402

        rel, inside = normalize_rel_path(raw_path, repo)
    except Exception:
        rel, inside = _norm_rel(raw_path), True
    if not inside or not rel:
        return None

    added = _extract_added_lines_from_edit(tool_name, args)
    if not added:
        return None

    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled", True) is False:
            continue
        if str(rule.get("severity") or "fail").strip().lower() == "warn":
            continue
        forbid_pat_s = str(rule.get("forbid_regex") or "").strip()
        if not forbid_pat_s or not _rule_matches_path(rule, rel):
            continue
        pat = re.compile(forbid_pat_s)
        strip_comm = bool(rule.get("strip_line_comments", True))
        strip_q = bool(rule.get("strip_strings", False))
        for raw_ln in added:
            code = _strip_line_comment(raw_ln) if strip_comm else raw_ln
            if not code.strip():
                continue
            target = _strip_strings(code) if strip_q else code
            if pat.search(target):
                return "SHIFT-LEFT " + _format_fitness_msg(rule, f"`{rel}`")
    return None
