#!/usr/bin/env python3
"""Deterministic Multi-Wave DAG Epoch Scheduler, `[LEASE:...]` Guard & O(1) Workflow Compiler."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_HOOKS_DIR = _SCRIPTS_DIR.parent / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from host_context import detect_host  # noqa: E402
from planner_state import extract_symbol_map  # noqa: E402
from schema_migrations import resolve_delegation_config  # noqa: E402

VALID_TIERS = frozenset({"L0", "L1", "L2", "L3"})
_TIER_MODEL_MAP = {"L0": "flash_lite", "L1": "flash", "L2": "flash", "L3": "pro"}
_VALID_DAG_KEYS = frozenset({"deps", "writes", "reads", "locks", "tier"})
_VALID_MODES = frozenset({"seq", "parallel"})

AUTHORING_EXCLUSIVE_LOCKS = frozenset({
    "l10n", "settings_ssot", "csproj", "manifest", "device_ipc", "git_ship", "assembly_api", "__legacy_serial",
})
BARRIER_DEFERRED_LOCKS = frozenset({"proot_build", "heavy_tests", "verify_all", "ledgers"})
DEFAULT_SHARED_HOTSPOTS: tuple[str, ...] = (
    "**/AndroidStrings*.cs", ".agents/docs/HANDOFF.md", ".agents/docs/PLANS_HUB.md",
    ".agents/docs/PROJECT_HISTORY.md", ".agents/docs/EXTERNAL_AI_SYNC.md", "**/AppSettingsStore.cs",
    "*.csproj", "**/AndroidManifest.xml", ".agents/planner_profile.json",
)
_CODE_FILE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_./{}*-]+\.(?:cs|py|ts|tsx|js|jsx|kt|java|rs|go|cpp|c|h|swift|md|json|yml|yaml|csproj|xml|sh|axml|resx))",
    re.IGNORECASE,
)
_LEASE_TAG_RE = re.compile(
    r"\[LEASE:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_-]+)((?::(?:mode=(?:flat|captain)|sub=[^:\]]+))*)\]"
)
_SUB_BULLET_RE = re.compile(r"^[-*]\s+`?([A-Za-z]+\d+(?:\.\d+)*[a-z]?)`?\s*:")
_AFTER_DEP_RE = re.compile(r"\b(?:after|depends\s+on|blocked\s+by)\s+`?([A-Za-z]+\d+(?:\.\d+)*[a-z]?)`?", re.I)
_COMPLEX_MARKER_CORE = (
    r"(?:\[l3\]|\b(?:architecture(?:al)?|dsp|physics|math(?:s|ematics|ematical)?|"
    r"security|concurrency|concurrent|race(?:\s+condition)?|deadlock|sota|"
    r"root[\s-]+cause|forensic(?:s)?|deep[\s-]+audit)\b)"
)
_FORBIDDEN_CLAUSE_RE = re.compile(r"\bFORBIDDEN\s*:[^\n.;|]*", re.IGNORECASE)
_NEGATED_COMPLEX_RE = re.compile(
    rf"\b(?:no|not|non|without|never|avoid|skip)\b[\s-]+(?:(?:a|an|the|any)\s+)?{_COMPLEX_MARKER_CORE}"
    rf"(?:[\s,/&]+(?:or|and|nor)?[\s,/&]*{_COMPLEX_MARKER_CORE})*",
    re.IGNORECASE,
)
_POSITIVE_COMPLEX_RE = re.compile(_COMPLEX_MARKER_CORE, re.IGNORECASE)


def _split_pipe(line: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    in_bt, i, n = False, 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            in_bt = not in_bt
            buf.append(ch)
            i += 1
        elif ch == "|" and not in_bt:
            if i + 1 < n and line[i + 1] == "|":
                buf.append("||")
                i += 2
            else:
                parts.append("".join(buf).strip())
                buf = []
                i += 1
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf).strip())
    return parts


def _norm_rel(path_str: str) -> str:
    p = str(path_str or "").strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    pre, body, suf = pattern[: m.start()], m.group(1), pattern[m.end() :]
    out: list[str] = []
    for opt in body.split(","):
        out.extend(_expand_braces(pre + opt.strip() + suf))
    return out or [pattern]


def _split_csv(val: str) -> list[str]:
    out, buf, depth = [], [], 0
    for ch in str(val or ""):
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        if ch == "," and depth == 0:
            if (tok := "".join(buf).strip()):
                out.append(tok)
            buf = []
        else:
            buf.append(ch)
    if (tail := "".join(buf).strip()):
        out.append(tail)
    return out


def _is_dir_catchall(pat: str) -> str:
    """Return non-empty directory prefix if `pat` is a broad subtree catch-all like `dir/*` or `dir/**`."""
    for suf in ("/**/*", "/**", "/*"):
        if pat.endswith(suf):
            pre = pat[: -len(suf) + 1]
            return pre if pre.strip("/") else ""
    return pat if pat.endswith("/") and pat.strip("/") else ""


def globs_overlap(raw_a: str, raw_b: str) -> bool:
    """Return True if two repo-relative path/glob specs can touch the same physical file."""
    na, nb = _norm_rel(raw_a), _norm_rel(raw_b)
    if not na or not nb or na.upper() == "RO" or nb.upper() == "RO":
        return False
    for pa in _expand_braces(na):
        for pb in _expand_braces(nb):
            if pa == pb or fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa):
                return True
            if pa.startswith("**/") and (fnmatch.fnmatch(pb, pa[3:]) or fnmatch.fnmatch(Path(pb).name, pa[3:])):
                return True
            if pb.startswith("**/") and (fnmatch.fnmatch(pa, pb[3:]) or fnmatch.fnmatch(Path(pa).name, pb[3:])):
                return True
            da, db = _is_dir_catchall(pa), _is_dir_catchall(pb)
            if (da and pb.startswith(da)) or (db and pa.startswith(db)):
                return True
    return False


def _matches_any(rel_path: str, globs: list[str] | tuple[str, ...]) -> bool:
    rel = _norm_rel(rel_path)
    return any(globs_overlap(rel, g) for g in globs if g and _norm_rel(g).upper() != "RO")


def _parse_kv(fields: list[str]) -> dict[str, str]:
    return {k.strip().lower(): v.strip() for tok in fields if "=" in tok for k, v in [tok.split("=", 1)]}


def parse_blueprint_waves(source: Path | str) -> dict[str, Any]:
    """Parse `SLICE|`, child bullets, `SLICE_DAG|`, and `DAG_POLICY|` from a blueprint file or text."""
    if isinstance(source, Path) or ("\n" not in str(source) and Path(str(source)).is_file()):
        p = Path(source)
        text, bp_id = p.read_text(encoding="utf-8", errors="ignore"), p.stem
    else:
        text, bp_id = str(source or ""), "BP_INLINE"

    policy: dict[str, Any] = {"default_mode": "seq", "max_width_termux": 2, "max_width_local": 4, "barrier": "epoch"}
    waves, dag_records, in_h2, cur_wave = [], [], False, None
    for idx, raw_ln in enumerate(text.splitlines(), start=1):
        s = raw_ln.strip()
        if not s:
            continue
        if s.startswith("BP_META|"):
            bp_id = _parse_kv(_split_pipe(s)[1:]).get("id") or bp_id
        elif s.startswith("DAG_POLICY|"):
            pol = _parse_kv(_split_pipe(s)[1:])
            if pol.get("default_mode") in _VALID_MODES:
                policy["default_mode"] = pol["default_mode"]
            for k in ("max_width_termux", "max_width_local", "max_width"):
                if pol.get(k, "").isdigit():
                    policy[k] = max(1, int(pol[k]))
            if pol.get("barrier"):
                policy["barrier"] = pol["barrier"]
        elif re.match(r"^#{2,4}\s+H2\+", s, re.I):
            in_h2, cur_wave = True, None
        elif in_h2 and re.match(r"^#{1,3}\s+(?!H2\+)", s):
            in_h2 = False
        elif in_h2:
            continue
        elif s.startswith("SLICE|"):
            parts = _split_pipe(s)
            wid = parts[1].strip() if len(parts) > 1 else f"W{len(waves) + 1}"
            scope = parts[2].strip() if len(parts) > 2 else ""
            w_obj = {
                "id": wid, "order": len(waves), "line": idx, "scope": scope,
                "io": parts[3].strip() if len(parts) > 3 else "",
                "probe": parts[4].strip() if len(parts) > 4 else "",
                "is_done": bool(re.search(r"\[DONE(?:\b|\s|\|)", scope, re.I)), "bullets": [],
            }
            waves.append(w_obj)
            cur_wave = w_obj
        elif s.startswith("SLICE_DAG|"):
            parts = _split_pipe(s)
            dag_records.append({"id": parts[1].strip() if len(parts) > 1 else "", "line": idx, "kv": _parse_kv(parts[2:])})
        elif cur_wave is not None:
            if s.startswith("## "):
                cur_wave = None
            elif s.startswith(("- ", "* ")) or _SUB_BULLET_RE.match(s):
                cur_wave["bullets"].append(s)
    return {"bp_id": bp_id, "policy": policy, "waves": waves, "dag_records": dag_records}


def validate_slice_dag(source: Path | str) -> tuple[list[str], list[str], dict[str, Any]]:
    """Validate optional `SLICE_DAG|` declarations (syntax, unknown IDs, tiers & cycles)."""
    parsed = parse_blueprint_waves(source)
    dag_records = parsed["dag_records"]
    if not dag_records:
        return [], [], {"has_dag": False, "waves": len(parsed["waves"])}

    issues, blockers = [], []
    slice_ids = {w["id"] for w in parsed["waves"]}
    seen_dag: set[str] = set()
    adj: dict[str, list[str]] = {w["id"]: [] for w in parsed["waves"]}

    def _err(msg: str) -> None:
        issues.append(msg)
        blockers.append(msg)

    for rec in dag_records:
        wid, kv = rec["id"], rec["kv"]
        if not wid or wid not in slice_ids:
            _err(f"SLICE_DAG `{wid}` @L{rec['line']} does not match any `SLICE|` id.")
            continue
        if wid in seen_dag:
            _err(f"Duplicate `SLICE_DAG` for `{wid}` @L{rec['line']}.")
            continue
        seen_dag.add(wid)
        if (bad_keys := [k for k in kv if k not in _VALID_DAG_KEYS]):
            _err(f"SLICE_DAG `{wid}` unknown key(s): {','.join(bad_keys)}.")
        if (tier := kv.get("tier", "L2").upper()) not in VALID_TIERS:
            _err(f"SLICE_DAG `{wid}` invalid tier `{tier}` (use L0..L3).")
        raw_deps = kv.get("deps", "none")
        if raw_deps.lower() not in ("", "none", "null"):
            for dep in _split_csv(raw_deps):
                if dep == wid:
                    _err(f"SLICE_DAG `{wid}` self-dependency.")
                elif dep not in slice_ids:
                    _err(f"SLICE_DAG `{wid}` depends on unknown slice `{dep}`.")
                else:
                    adj[wid].append(dep)

    indeg = {u: 0 for u in slice_ids}
    rev: dict[str, list[str]] = {u: [] for u in slice_ids}
    for u, deps in adj.items():
        for d in deps:
            indeg[u] += 1
            rev[d].append(u)
    q = [u for u, deg in indeg.items() if deg == 0]
    vis = 0
    while q:
        cur = q.pop(0)
        vis += 1
        for nxt in rev[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)
    if vis < len(slice_ids):
        _err(f"DAG_CYCLE detected among slice(s): {','.join(sorted(u for u, d in indeg.items() if d > 0))}.")
    return issues, blockers, {"has_dag": True, "dag_count": len(dag_records), "waves": len(slice_ids)}


def is_termux_constrained(repo: Path | None = None, *, env: dict[str, str] | None = None) -> bool:
    """Return True when executing on Termux/PRoot (respecting explicit PLANNER_HOST_OVERRIDE)."""
    info = detect_host(Path(repo) if repo is not None else Path.cwd(), env=env)
    host_id = info.get("host_id")
    if info.get("reason") == "env:PLANNER_HOST_OVERRIDE":
        return host_id == "antigravity-termux"
    caps = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
    return bool(host_id == "antigravity-termux" or info.get("is_termux_proot") or caps.get("is_termux_proot"))


def _has_positive_complex_marker(text: str) -> bool:
    """Detect positive L3/complexity markers while ignoring negated clauses and FORBIDDEN blocks."""
    cleaned = _FORBIDDEN_CLAUSE_RE.sub(" ", str(text or ""))
    cleaned = _NEGATED_COMPLEX_RE.sub(" ", cleaned)
    return bool(_POSITIVE_COMPLEX_RE.search(cleaned))


def _fixed_line_ranges(
    start: int, end: int, step: int, first_syms: list[str] | None = None,
) -> list[tuple[int, int, list[str]]]:
    out: list[tuple[int, int, list[str]]] = []
    cur = start
    while cur <= end:
        nxt = min(end, cur + step - 1)
        out.append((cur, nxt, list(first_syms) if cur == start and first_syms else []))
        cur = nxt + 1
    return out


def _cap_chunks(chunks: list[tuple[int, int, list[str]]], max_chunks: int) -> list[tuple[int, int, list[str]]]:
    while len(chunks) > max_chunks:
        best_idx = min(range(len(chunks) - 1), key=lambda i: chunks[i + 1][1] - chunks[i][0])
        s1, _, syms1 = chunks[best_idx]
        _, e2, syms2 = chunks[best_idx + 1]
        chunks[best_idx : best_idx + 2] = [(s1, e2, list(dict.fromkeys([*syms1, *syms2])))]
    return chunks


def _partition_contiguous(items: list[Any], max_buckets: int) -> list[list[Any]]:
    n = min(max(1, max_buckets), len(items))
    q, rem = divmod(len(items), n)
    out, idx = [], 0
    for i in range(n):
        size = q + (1 if i < rem else 0)
        out.append(items[idx : idx + size])
        idx += size
    return out


def preslice_wave_inputs(
    repo: Path,
    writes: list[str],
    reads: list[str],
    *,
    max_slice_lines: int = 180,
    max_chunks_per_file: int = 6,
) -> list[dict[str, Any]]:
    """Preslice existing non-glob read/write targets into <=max_chunks_per_file symbol or line-range chunks."""
    repo_root = Path(repo).expanduser().resolve() if repo is not None else Path.cwd().resolve()
    max_lines = max(1, int(max_slice_lines))
    max_chunks = max(1, int(max_chunks_per_file))

    candidates: list[str] = []
    for raw in [*(reads or []), *(writes or [])]:
        rel = _norm_rel(raw)
        if not rel or rel.upper() in ("RO", "NONE", "NULL") or rel in candidates:
            continue
        if any(ch in rel for ch in "*?[{"):
            continue
        candidates.append(rel)

    slices: list[dict[str, Any]] = []
    for rel in candidates:
        fp = repo_root / rel
        if not fp.is_file():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        n_lines = len(lines)
        parsed_syms: list[tuple[int, str]] = []
        for entry in extract_symbol_map(lines, fp.suffix):
            if (m := re.match(r"^L(\d+)\|(.+)$", entry)):
                ln = max(1, min(int(m.group(1)), max(1, n_lines)))
                parsed_syms.append((ln, m.group(2).strip()))

        if n_lines <= 200:
            end_ln = max(1, n_lines)
            slices.append({
                "path": rel, "start_line": 1, "end_line": end_ln,
                "symbols": [sym for _, sym in parsed_syms], "label": f"{rel}:L1-{end_ln}",
            })
            continue

        if not parsed_syms:
            step = max(max_lines, (n_lines + max_chunks - 1) // max_chunks)
            raw_chunks = _fixed_line_ranges(1, n_lines, step)
        else:
            by_line: dict[int, list[str]] = {}
            for ln, sym in sorted(parsed_syms, key=lambda item: item[0]):
                by_line.setdefault(ln, [])
                if sym not in by_line[ln]:
                    by_line[ln].append(sym)
            grouped = sorted(by_line.items(), key=lambda item: item[0])
            intervals: list[tuple[int, int, list[str]]] = []
            if grouped[0][0] > 1:
                intervals.append((1, grouped[0][0] - 1, []))
            for idx, (ln, syms) in enumerate(grouped):
                end = (grouped[idx + 1][0] - 1) if idx + 1 < len(grouped) else n_lines
                if ln <= end:
                    intervals.append((ln, end, syms))
            subdivided: list[tuple[int, int, list[str]]] = []
            for s, e, syms in intervals:
                if (e - s + 1) > max_lines:
                    subdivided.extend(_fixed_line_ranges(s, e, max_lines, syms))
                else:
                    subdivided.append((s, e, syms))
            coalesced: list[tuple[int, int, list[str]]] = []
            for s, e, syms in subdivided:
                if coalesced and (e - coalesced[-1][0] + 1) <= max_lines:
                    ps, _, psyms = coalesced[-1]
                    coalesced[-1] = (ps, e, list(dict.fromkeys([*psyms, *syms])))
                else:
                    coalesced.append((s, e, syms))
            raw_chunks = _cap_chunks(coalesced, max_chunks)

        for s, e, syms in raw_chunks:
            slices.append({
                "path": rel, "start_line": s, "end_line": e,
                "symbols": syms, "label": f"{rel}:L{s}-{e}",
            })
    return slices


def infer_legacy_wave_meta(wave: dict[str, Any]) -> dict[str, Any]:
    """Best-effort deterministic write-set, lock & tier inference for unannotated `SLICE|` lines."""
    blob = " ".join([wave.get("scope", ""), wave.get("io", ""), wave.get("probe", ""), *wave.get("bullets", [])])
    writes: list[str] = []
    for m in _CODE_FILE_TOKEN_RE.finditer(blob):
        tok = _norm_rel(m.group(1))
        if tok and tok not in writes:
            writes.append(tok)
    locks: list[str] = []
    low = blob.lower()
    if any("androidstrings" in w.lower() for w in writes) or "l10n" in low or "4-lang" in low:
        locks.append("l10n")
    if any("appsettingsstore" in w.lower() for w in writes):
        locks.append("settings_ssot")
    if any(w.lower().endswith(".csproj") for w in writes):
        locks.append("csproj")
    if any("androidmanifest.xml" in w.lower() for w in writes):
        locks.append("manifest")
    if any(k in w.upper() for w in writes for k in ("HANDOFF", "PLANS_HUB", "PROJECT_HISTORY", "EXTERNAL_AI_SYNC")):
        locks.append("ledgers")
    if any(k in wave.get("probe", "").lower() for k in ("dotnet", "w46", "plugin_tests", "verify_universal", "check_android_cs")):
        locks.append("proot_build")
    if not writes:
        locks.append("__legacy_serial")
    if _has_positive_complex_marker(blob):
        tier = "L3"
    elif not writes:
        tier = "L0"
    elif len(writes) == 1 and "proot_build" not in locks and "ledgers" not in locks:
        tier = "L1"
    else:
        tier = "L2"
    deps = [m.group(1).strip() for m in _AFTER_DEP_RE.finditer(blob) if m.group(1).strip() != wave.get("id")]
    return {"deps": list(dict.fromkeys(deps)), "writes": writes, "reads": [], "locks": sorted(set(locks)), "tier": tier, "inferred": True}


def decompose_wave_to_cubicles(
    wave: dict[str, Any],
    meta: dict[str, Any],
    repo: Path,
    *,
    max_cubicles_per_wave: int = 8,
) -> list[dict[str, Any]]:
    """Decompose a wave into flat cubicles: split RO multi-reads/preslices to L0; keep writes intact."""
    wid = str(wave.get("id") or "W1")
    cap = max(1, int(max_cubicles_per_wave))
    writes = [_norm_rel(x) for x in (meta.get("writes") or []) if _norm_rel(x) and _norm_rel(x).upper() != "RO"]
    reads = [_norm_rel(x) for x in (meta.get("reads") or []) if _norm_rel(x) and _norm_rel(x).upper() not in ("", "NONE", "NULL")]
    locks = list(meta.get("locks") or [])
    blob = " ".join([wave.get("scope", ""), wave.get("io", ""), wave.get("probe", ""), *(wave.get("bullets") or [])])
    repo_path = Path(repo) if repo is not None else Path.cwd()
    preslices = preslice_wave_inputs(repo_path, writes, reads)

    def _cubicle(idx: int, tier: str, c_writes: list[str], c_reads: list[str], c_slices: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": f"{wid}.c{idx}", "parent_id": wid, "mode": "flat",
            "tier": tier, "model": _TIER_MODEL_MAP.get(tier, "flash"),
            "writes": c_writes, "reads": c_reads, "locks": locks, "slices": c_slices,
            "scope": wave.get("scope", ""), "io": wave.get("io", ""), "probe": wave.get("probe", ""),
        }

    if not writes:
        if len(preslices) > 1:
            return [
                _cubicle(idx, "L0", [], list(dict.fromkeys(sl["path"] for sl in grp)), grp)
                for idx, grp in enumerate(_partition_contiguous(preslices, cap), start=1)
            ]
        if len(reads) > 1:
            return [
                _cubicle(idx, "L0", [], r_grp, [sl for sl in preslices if sl["path"] in r_grp])
                for idx, r_grp in enumerate(_partition_contiguous(reads, cap), start=1)
            ]
        return [_cubicle(1, "L0", [], reads, preslices)]

    if meta.get("tier") == "L3" or _has_positive_complex_marker(blob):
        tier = "L3"
    elif meta.get("tier") == "L1" or (len(writes) == 1 and "proot_build" not in locks and "ledgers" not in locks):
        tier = "L1"
    else:
        tier = "L2"
    return [_cubicle(1, tier, writes, reads, preslices)]


def _resolve_wave_meta(parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    dag_by_id = {r["id"]: r["kv"] for r in parsed["dag_records"]}
    out: dict[str, dict[str, Any]] = {}
    for w in parsed["waves"]:
        wid = w["id"]
        if wid not in dag_by_id:
            out[wid] = infer_legacy_wave_meta(w)
            continue
        kv = dag_by_id[wid]
        deps_raw, writes_raw, reads_raw, locks_raw = kv.get("deps", "none"), kv.get("writes", "RO"), kv.get("reads", "none"), kv.get("locks", "none")
        tier = kv.get("tier", "L2").upper()
        out[wid] = {
            "deps": [] if deps_raw.lower() in ("", "none", "null") else _split_csv(deps_raw),
            "writes": [] if writes_raw.upper() in ("", "NONE", "RO") else [_norm_rel(x) for x in _split_csv(writes_raw)],
            "reads": [] if reads_raw.lower() in ("", "none", "null") else [_norm_rel(x) for x in _split_csv(reads_raw)],
            "locks": [] if locks_raw.lower() in ("", "none", "null") else sorted({x.lower() for x in _split_csv(locks_raw)}),
            "tier": tier if tier in VALID_TIERS else "L2",
            "inferred": False,
        }
    return out


def _git_common_dir(repo: Path) -> Path:
    if (override := os.environ.get("PLANNER_LEASE_DIR", "").strip()):
        p = Path(override).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    r = Path(repo).expanduser().resolve()
    if (r / ".git").exists():
        try:
            res = subprocess.run(["git", "-C", str(r), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=2, check=False)
            if res.returncode == 0 and res.stdout.strip():
                gd = Path(res.stdout.strip())
                return gd if gd.is_absolute() else (r / gd).resolve()
        except Exception:
            pass
    fallback = Path(f"/tmp/.agy_leases_{hashlib.sha256(str(r).encode('utf-8', errors='ignore')).hexdigest()[:12]}")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_ephemeral_dir(repo: Path) -> Path:
    d = _git_common_dir(repo) / "planner_ephemeral"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_checkpoint(repo: Path, bp_id: str) -> dict[str, Any]:
    cp = get_ephemeral_dir(repo) / "checkpoints" / f"{bp_id}.json"
    if not cp.is_file():
        return {}
    try:
        data = json.loads(cp.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clear_checkpoint(repo: Path, bp_id: str) -> bool:
    cp = get_ephemeral_dir(repo) / "checkpoints" / f"{bp_id}.json"
    if cp.is_file():
        cp.unlink()
        return True
    return False


def _resolve_eff_width(
    repo: Path, max_width: int | None = None, profile: dict[str, Any] | None = None, policy: dict[str, Any] | None = None,
) -> tuple[str, int]:
    wo_cfg = resolve_delegation_config(profile).get("wave_orchestration") or {}
    pol = policy or {}
    host_id = str(detect_host(repo).get("host_id") or "local-ide")
    if is_termux_constrained(repo):
        cap = int(pol.get("max_width_termux") or wo_cfg.get("max_width_termux") or 2)
        return host_id, max(1, min(max_width or cap, cap, 2))
    if host_id == "ai-studio-cloud":
        return host_id, max(1, int(max_width or 1))
    cap = int(pol.get("max_width_local") or wo_cfg.get("max_width_local") or 4)
    return host_id, max(1, min(max_width or cap, 8))


def _items_conflict(u: dict[str, Any], v: dict[str, Any]) -> bool:
    lu, lv = set(u.get("locks") or []) & AUTHORING_EXCLUSIVE_LOCKS, set(v.get("locks") or []) & AUTHORING_EXCLUSIVE_LOCKS
    if lu & lv:
        return True
    wu, wv, ru, rv = u.get("writes") or [], v.get("writes") or [], u.get("reads") or [], v.get("reads") or []
    return any(globs_overlap(a, b) for a in wu for b in wv) or any(globs_overlap(a, b) for a in ru for b in wv) or any(globs_overlap(a, b) for a in rv for b in wu)


def schedule_blueprint(
    bp_path: Path, repo: Path, *, mode: str = "parallel", max_width: int | None = None,
    profile: dict[str, Any] | None = None, ignore_checkpoints: bool = False,
) -> dict[str, Any]:
    """Compile a multi-wave blueprint into conflict-free sequential or Coffman-Graham parallel epochs."""
    p = Path(bp_path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Blueprint not found: {p}")
    parsed = parse_blueprint_waves(p)
    bp_id = parsed["bp_id"]
    issues, blockers, _ = validate_slice_dag(p)
    if blockers:
        raise ValueError("Invalid SLICE_DAG: " + " ; ".join(blockers))

    host_id, eff_width = _resolve_eff_width(repo, max_width=max_width, profile=profile, policy=parsed["policy"])

    cp = {} if ignore_checkpoints else load_checkpoint(repo, bp_id)
    cp_done = {k for k, v in (cp.get("waves") or {}).items() if isinstance(v, dict) and v.get("status") == "done"}
    meta_map = _resolve_wave_meta(parsed)
    active_waves = [w for w in parsed["waves"] if not w["is_done"] and w["id"] not in cp_done]
    active_ids = {w["id"] for w in active_waves}
    wave_by_id = {w["id"]: w for w in active_waves}

    deps_map, children, indeg = {}, {wid: [] for wid in active_ids}, {wid: 0 for wid in active_ids}
    for w in active_waves:
        wid = w["id"]
        d_list = [d for d in meta_map[wid]["deps"] if d in active_ids]
        deps_map[wid], indeg[wid] = d_list, len(d_list)
        for d in d_list:
            children[d].append(wid)

    q = sorted([wid for wid, deg in indeg.items() if deg == 0], key=lambda x: wave_by_id[x]["order"])
    topo_order, lvl = [], {wid: 0 for wid in active_ids}
    while q:
        u = q.pop(0)
        topo_order.append(u)
        for v in children[u]:
            lvl[v] = max(lvl[v], lvl[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
                q.sort(key=lambda x: wave_by_id[x]["order"])
    if len(topo_order) < len(active_ids):
        raise ValueError(f"DAG_CYCLE detected in `{bp_id}`.")

    crit_path = {wid: 1 for wid in active_ids}
    for u in reversed(topo_order):
        if children[u]:
            crit_path[u] = 1 + max(crit_path[v] for v in children[u])

    conflict_adj: dict[str, set[str]] = {wid: set() for wid in active_ids}
    active_list = [w["id"] for w in active_waves]
    for i, u in enumerate(active_list):
        for v in active_list[i + 1 :]:
            if _items_conflict(meta_map[u], meta_map[v]):
                conflict_adj[u].add(v)
                conflict_adj[v].add(u)

    req_mode = mode if mode in _VALID_MODES else "seq"
    epoch_buckets, epoch_of = [], {}
    if req_mode == "seq":
        for u in topo_order:
            epoch_of[u] = len(epoch_buckets)
            epoch_buckets.append([u])
    else:
        ordered = sorted(topo_order, key=lambda u: (-crit_path[u], lvl[u], -len(conflict_adj[u]), wave_by_id[u]["order"]))
        unscheduled = set(ordered)
        while unscheduled:
            u = next(x for x in ordered if x in unscheduled and all(d not in unscheduled for d in deps_map[x]))
            min_ep = 1 + max((epoch_of[d] for d in deps_map[u]), default=-1)
            for ek in range(min_ep, len(epoch_buckets)):
                if len(epoch_buckets[ek]) < eff_width and not any(v in conflict_adj[u] for v in epoch_buckets[ek]):
                    epoch_buckets[ek].append(u)
                    epoch_of[u] = ek
                    break
            else:
                epoch_of[u] = len(epoch_buckets)
                epoch_buckets.append([u])
            unscheduled.remove(u)

    epochs_out = []
    for k, wids in enumerate(epoch_buckets, start=1):
        ep_waves, ep_cubicles, probes, seen_p, barrier_locks = [], [], [], set(), set()
        for wid in sorted(wids, key=lambda x: wave_by_id[x]["order"]):
            w, m = wave_by_id[wid], meta_map[wid]
            probe_cmd = w["probe"].strip()
            if probe_cmd and probe_cmd not in seen_p and not probe_cmd.startswith("<FILL"):
                seen_p.add(probe_cmd)
                probes.append(probe_cmd)
            barrier_locks.update(set(m["locks"]) & BARRIER_DEFERRED_LOCKS)
            ep_waves.append({
                "id": wid, "scope": w["scope"], "io": w["io"], "probe": probe_cmd, "deps": deps_map[wid],
                "writes": m["writes"], "reads": m["reads"], "locks": m["locks"], "tier": m["tier"],
                "model": _TIER_MODEL_MAP.get(m["tier"], "flash"), "inferred": m["inferred"],
            })
            ep_cubicles.extend(decompose_wave_to_cubicles(w, m, repo))
        epochs_out.append({
            "epoch_id": f"E{k}", "waves": ep_waves, "cubicles": ep_cubicles,
            "barrier_probes": probes, "barrier_locks": sorted(barrier_locks),
        })

    return {
        "bp_id": bp_id, "bp_path": str(p), "mode": req_mode, "host_id": host_id, "max_width": eff_width,
        "total_waves": len(active_waves), "done_skipped": len(parsed["waves"]) - len(active_waves),
        "total_epochs": len(epochs_out), "warnings": issues, "epochs": epochs_out,
    }


def _render_flat_workflow_script(schedule: dict[str, Any], repo: Path, out_path: Path, artifact_dir: Path | None = None) -> Path:
    """Emit self-contained flat O(1) root-context `run_workflow` script (zero captains, zero call_tool)."""
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    r = Path(repo).expanduser().resolve()
    art = str((artifact_dir or out.parent.parent).expanduser().resolve())
    plan_cli = str((_SCRIPTS_DIR / "plan_tool.py").resolve())
    host_id = str(schedule.get("host_id") or detect_host(r).get("host_id") or "local-ide")
    raw_w = max(1, int(schedule.get("max_width") or 2))
    max_conc = min(raw_w, 2) if is_termux_constrained(r) else raw_w
    barrier_probes = {ep["epoch_id"]: list(ep.get("barrier_probes") or []) for ep in (schedule.get("epochs") or [])}
    wf_sched = {
        **schedule,
        "epochs": [
            {
                k: ([{ck: cv for ck, cv in item.items() if ck != "probe"} for item in v] if k in ("waves", "cubicles") else v)
                for k, v in ep.items()
                if k != "barrier_probes"
            }
            for ep in (schedule.get("epochs") or [])
        ],
    }
    script = f'''#!/usr/bin/env python3
"""Auto-generated Flat O(1) Epoch Workflow (`{schedule["bp_id"]}`, mode={schedule.get("mode", "parallel")})."""
import json, os, pathlib, secrets, subprocess, sys
SCHEDULE = {repr(wf_sched)}
BARRIER_PROBES = {repr(barrier_probes)}
BP_ID, REPO_ROOT, ART_DIR, PLAN_CLI = SCHEDULE["bp_id"], {json.dumps(str(r))}, {json.dumps(art)}, {json.dumps(plan_cli)}
HOST_ID = {json.dumps(host_id)}
MAX_CONCURRENCY = {max_conc}
RECEIPT_SCHEMA = {{"type": "object", "additionalProperties": False, "properties": {{"slice_id": {{"type": "string"}}, "status": {{"type": "string", "enum": ["ok", "failed"]}}, "files_modified": {{"type": "array", "items": {{"type": "string"}}}}, "err": {{"type": "string"}}, "summary": {{"type": "string"}}}}, "required": ["slice_id", "status", "files_modified", "err"]}}

def _compact_receipt(raw, sid):
    d = raw if isinstance(raw, dict) else {{}}
    out = {{"slice_id": str(d.get("slice_id") or sid)[:64], "status": "ok" if d.get("status") == "ok" else "failed", "files_modified": [str(x)[:120] for x in (d.get("files_modified") or [])[:16]], "err": str(d.get("err") or "")[:160], "summary": str(d.get("summary") or "")[:160]}}
    while len(json.dumps(out, ensure_ascii=False)) > 600:
        if out["summary"]: out["summary"] = out["summary"][:-32]
        elif out["err"]: out["err"] = out["err"][:-32]
        elif len(out["files_modified"]) > 1: out["files_modified"].pop()
        else: break
    return out

def _run_plan_cli_seq(subargs):
    env = {{**os.environ, "PLANNER_HOST_OVERRIDE": HOST_ID, "PLANNER_ARTIFACT_DIR": ART_DIR, "CAVERN_ARTIFACT_DIR": ART_DIR, "PLANNER_WORKSPACE_ROOT": REPO_ROOT}}
    return subprocess.run([sys.executable, "-B", PLAN_CLI, "--workspace", REPO_ROOT, "blueprint", *subargs], shell=False, env=env, capture_output=True, text=True, check=True).stdout

async def run_atomic_cubicle(ek, cubicle, nonce):
    cid = cubicle["id"]
    lease = f"[LEASE:{{BP_ID}}:{{ek}}:{{cid}}:{{nonce}}:mode=flat]"
    slices = [sl.get("label", "") for sl in (cubicle.get("slices") or []) if sl.get("label")][:6]
    sl_txt = f" Slices: {{', '.join(slices)}}." if slices else ""
    prompt = f"{{lease}}\\nFlat cubicle `{{BP_ID}}` `{{cid}}` ({{ek}}). Scope: {{cubicle.get('scope', '')}}. Outcome: {{cubicle.get('io', '')}}.{{sl_txt}}\\nOwned writes ONLY: {{', '.join(cubicle.get('writes') or ['RO'])}}. FORBIDDEN: nested subagents or heavy commands, out-of-lease writes, commits/pushes. Return <=600B JSON receipt via finish."
    try:
        res = await agent(prompt, schema=RECEIPT_SCHEMA, role=f"{{cid}} Cubicle", type_name="self", workspace="inherit", model=cubicle.get("model", "flash"))
        return _compact_receipt(res, cid)
    except Exception as exc:
        return _compact_receipt({{"slice_id": cid, "status": "failed", "files_modified": [], "err": str(exc)}}, cid)

async def run_bounded_parallel(items, fn, max_width=MAX_CONCURRENCY):
    w, out = max(1, int(max_width or 1)), []
    for i in range(0, len(items), w):
        out.extend(await parallel(items[i : i + w], fn))
    return out

async def run_epoch(epoch):
    ek = epoch["epoch_id"]
    cubicles = epoch.get("cubicles") or epoch.get("waves") or []
    nonces = {{c["id"]: secrets.token_hex(6) for c in cubicles}}
    probes = list(BARRIER_PROBES.get(ek) or [])
    phase(f"Epoch {{ek}} ({{len(cubicles)}} cubicle(s), max={{MAX_CONCURRENCY}})")
    scratch = pathlib.Path(ART_DIR) / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    meta_f, rec_f = scratch / f"epoch_meta_{{BP_ID}}_{{ek}}.json", scratch / f"epoch_receipts_{{BP_ID}}_{{ek}}.json"
    meta_f.write_text(json.dumps({{"epoch_id": ek, "waves": cubicles, "nonces": nonces, "probes": probes}}, ensure_ascii=False), encoding="utf-8")
    _run_plan_cli_seq(["epoch-begin", "--bp", BP_ID, "--epoch", ek, "--meta-file", str(meta_f), "--owner-pid", str(os.getpid())])
    receipts = await run_bounded_parallel(cubicles, lambda c: run_atomic_cubicle(ek, c, nonces[c["id"]]), MAX_CONCURRENCY)
    rec_f.write_text(json.dumps(receipts, ensure_ascii=False), encoding="utf-8")
    out_b = _run_plan_cli_seq(["epoch-barrier", "--bp", BP_ID, "--epoch", ek, "--receipts-file", str(rec_f), "--meta-file", str(meta_f)])
    bars = [ln for ln in str(out_b).splitlines() if ln.startswith("BARRIER|")]
    log(bars[0] if bars else f"BARRIER|{{ek}}|done={{len(receipts)}}")
    return {{"epoch_id": ek, "receipts": receipts}}

results = await pipeline(SCHEDULE["epochs"], run_epoch)
log(f"WORKFLOW_DONE|{{BP_ID}}|mode={{SCHEDULE.get('mode','parallel')}}|epochs={{len(results)}}")
'''
    out.write_text(script, encoding="utf-8")
    return out


def emit_workflow_script(schedule: dict[str, Any], bp_path: Path, repo: Path, out_path: Path, artifact_dir: Path | None = None) -> Path:
    """Emit self-contained flat O(1) root-context `run_workflow` script for Antigravity."""
    return _render_flat_workflow_script(schedule, repo, out_path, artifact_dir=artifact_dir)


def emit_fanout_workflow(schedule: dict[str, Any], repo: Path, out_path: Path | None = None, artifact_dir: Path | None = None) -> Path:
    """Emit flat O(1) root-context `run_workflow` script for a fanout schedule."""
    if out_path is None:
        return _render_flat_workflow_script(schedule, Path(schedule.get("repo_root") or Path.cwd()), repo, artifact_dir=artifact_dir)
    return _render_flat_workflow_script(schedule, repo, out_path, artifact_dir=artifact_dir)


def emit_subagent_prompts(schedule: dict[str, Any]) -> str:
    """Emit flat HORIZONTAL_CUBICLES fallback prompts when `run_workflow` is unavailable."""
    lines = [f"HORIZONTAL_CUBICLES|bp={schedule['bp_id']}|mode={schedule['mode']}|max_w={schedule.get('max_width', 1)}|epochs={schedule['total_epochs']}"]
    for ep in schedule["epochs"]:
        ek = ep["epoch_id"]
        cubicles = ep.get("cubicles") or ep["waves"]
        lines.append(f"EPOCH|{ek}|cubicles={len(cubicles)}|probes={len(ep['barrier_probes'])}")
        for c in cubicles:
            nonce = secrets.token_hex(4)
            lines.append(
                f"  CUBICLE|id={c['id']}|model={c.get('model', 'flash')}|writes={','.join(c.get('writes') or ['RO'])}"
                f"|EnableSubagentTools=false|[LEASE:{schedule['bp_id']}:{ek}:{c['id']}:{nonce}:mode=flat]"
            )
    return "\n".join(lines)


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _sign_probes(hmac_key: str, probes: list[str] | None) -> str:
    payload = json.dumps(list(probes or []), sort_keys=True).encode("utf-8")
    return hmac.new(hmac_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@contextmanager
def _locked_lease_store(repo: Path) -> Iterator[dict[str, Any]]:
    gdir = _git_common_dir(repo)
    gdir.mkdir(parents=True, exist_ok=True)
    lock_path, data_path = gdir / "planner_leases.lock", gdir / "planner_leases.json"
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            now = time.time()
            try:
                raw = json.loads(data_path.read_text(encoding="utf-8", errors="ignore")) if data_path.is_file() else {}
                store = raw if isinstance(raw, dict) else {}
            except Exception:
                store = {}
            leases = store.setdefault("leases", {})
            for k in [
                k for k, v in leases.items()
                if not isinstance(v, dict) or float(v.get("expires_at") or 0.0) < now or not _pid_alive(v.get("owner_pid"))
            ]:
                leases.pop(k, None)
            store.setdefault("epochs", {})
            yield store
            tmp = gdir / f".planner_leases_{os.getpid()}.tmp"
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, data_path)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while (chunk := f.read(65536)):
            h.update(chunk)
    return h.hexdigest()


def _git_status_files(repo: Path) -> tuple[dict[str, str], list[str]]:
    if not (repo / ".git").exists():
        return {}, []
    res = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "-uall"], capture_output=True, text=True, timeout=5, check=False)
    dirty_hashes, untracked = {}, []
    if res.returncode != 0:
        return dirty_hashes, untracked
    for ln in res.stdout.splitlines():
        if len(ln) < 4:
            continue
        xy, raw_p = ln[:2], ln[3:].strip()
        if " -> " in raw_p:
            raw_p = raw_p.split(" -> ", 1)[1].strip()
        rel = _norm_rel(raw_p)
        if rel:
            if xy == "??":
                untracked.append(rel)
            fp = repo / rel
            dirty_hashes[rel] = _sha256_file(fp) if fp.is_file() else "__DELETED__"
    return dirty_hashes, untracked


def epoch_begin(
    repo: Path, bp_id: str, epoch_id: str, waves_meta: list[dict[str, Any]],
    *, nonces: dict[str, str] | None = None, ttl_secs: int = 3600,
    owner_pid: int | None = None, probes: list[str] | None = None,
) -> dict[str, Any]:
    """Capture pre-epoch dirty-tree baseline + backups and register flock-protected write leases."""
    r = Path(repo).expanduser().resolve()
    ep_dir = get_ephemeral_dir(r)
    bkp_dir, ep_meta_dir = ep_dir / "backups" / f"{bp_id}__{epoch_id}", ep_dir / "epochs"
    bkp_dir.mkdir(parents=True, exist_ok=True)
    ep_meta_dir.mkdir(parents=True, exist_ok=True)
    baseline_hashes, untracked_pre = _git_status_files(r)
    backup_map: dict[str, str] = {}
    for rel, digest in baseline_hashes.items():
        if digest != "__DELETED__" and (r / rel).is_file():
            dst = bkp_dir / hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
            shutil.copy2(r / rel, dst)
            backup_map[rel] = str(dst)

    now, out_leases = time.time(), {}
    pid_val = int(owner_pid) if owner_pid is not None else None
    norm_probes = [str(x) for x in (probes or [])]
    hmac_key = secrets.token_hex(32)
    probes_sig = _sign_probes(hmac_key, norm_probes)
    with _locked_lease_store(r) as store:
        for w in waves_meta:
            wid = str(w["id"])
            nonce = (nonces or {}).get(wid) or secrets.token_hex(6)
            mode_val = str(w.get("mode") or "flat").strip().lower()
            if mode_val not in ("flat", "captain"):
                mode_val = "flat"
            store["leases"][f"{bp_id}:{epoch_id}:{wid}"] = {
                "bp_id": bp_id, "epoch_id": epoch_id, "wave_id": wid, "parent_id": str(w.get("parent_id") or wid),
                "nonce": nonce, "mode": mode_val, "owner_pid": pid_val,
                "writes": [_norm_rel(x) for x in (w.get("writes") or [])],
                "reads": [_norm_rel(x) for x in (w.get("reads") or [])],
                "locks": [str(x).lower() for x in (w.get("locks") or [])],
                "conv_ids": [], "sub_leases": {}, "created_at": now, "expires_at": now + max(60, int(ttl_secs)),
            }
            out_leases[wid] = f"[LEASE:{bp_id}:{epoch_id}:{wid}:{nonce}:mode={mode_val}]"
        store["epochs"][f"{bp_id}:{epoch_id}"] = {
            "started_at": now, "owner_pid": pid_val, "waves": [w["id"] for w in waves_meta],
            "hmac_key": hmac_key, "probes_sig": probes_sig,
        }

    (ep_meta_dir / f"{bp_id}__{epoch_id}.json").write_text(
        json.dumps({"bp_id": bp_id, "epoch_id": epoch_id, "baseline_hashes": baseline_hashes,
                    "untracked_pre": untracked_pre, "backup_map": backup_map,
                    "waves": waves_meta, "probes": norm_probes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"bp_id": bp_id, "epoch_id": epoch_id, "leases": out_leases, "preexisting_dirty": len(baseline_hashes)}


def extract_lease_tag(text: str) -> dict[str, Any] | None:
    m = _LEASE_TAG_RE.search(text or "")
    if not m:
        return None
    tail = m.group(5) or ""
    mode_val, sub_raw = "captain", ""
    for opt in re.finditer(r":(?:mode=(flat|captain)|sub=([^:\]]+))", tail):
        if opt.group(1):
            mode_val = opt.group(1)
        elif opt.group(2) is not None:
            sub_raw = opt.group(2)
    return {
        "bp_id": m.group(1), "epoch_id": m.group(2), "wave_id": m.group(3), "nonce": m.group(4),
        "mode": mode_val, "sub_globs": [_norm_rel(x) for x in _split_csv(sub_raw) if x.strip()],
    }


def bind_lease_conversation(
    repo: Path, conv_id: str, bp_id: str, epoch_id: str, wave_id: str, nonce: str,
    sub_globs: list[str] | None = None, mode: str | None = None,
) -> bool:
    """Bind runtime conversationId to its active lease when nonce matches."""
    cid = str(conv_id or "").strip()
    if not cid or cid == "default":
        return False
    with _locked_lease_store(Path(repo).expanduser().resolve()) as store:
        lease = store.get("leases", {}).get(f"{bp_id}:{epoch_id}:{wave_id}")
        if not isinstance(lease, dict) or str(lease.get("nonce")) != str(nonce):
            return False
        conv_ids = lease.setdefault("conv_ids", [])
        if cid not in conv_ids:
            conv_ids.append(cid)
        if mode is not None:
            m_val = str(mode).strip().lower()
            if m_val in ("flat", "captain"):
                lease["mode"] = m_val
        else:
            lease.setdefault("mode", "captain")
        if sub_globs:
            parent_writes = lease.get("writes") or []
            lease.setdefault("sub_leases", {})[cid] = [_norm_rel(sg) for sg in sub_globs if _matches_any(sg, parent_writes)]
        return True


def _load_active_leases(repo: Path) -> dict[str, dict[str, Any]]:
    data_path = _git_common_dir(repo) / "planner_leases.json"
    if not data_path.is_file():
        return {}
    try:
        raw = json.loads(data_path.read_text(encoding="utf-8", errors="ignore"))
        leases = raw.get("leases") if isinstance(raw, dict) else None
        if not isinstance(leases, dict) or not leases:
            return {}
        now = time.time()
        return {
            k: v for k, v in leases.items()
            if isinstance(v, dict) and float(v.get("expires_at") or 0.0) >= now and _pid_alive(v.get("owner_pid"))
        }
    except Exception:
        return {}


def has_active_parallel_leases(repo: Path) -> bool:
    return len(_load_active_leases(repo)) >= 1


def is_parallel_wave_worker(repo: Path, conv_id: str) -> tuple[str, str, str] | None:
    leases = _load_active_leases(repo)
    if not leases:
        return None
    cid = str(conv_id or "").strip()
    for v in leases.values():
        if cid and cid in (v.get("conv_ids") or []):
            return str(v.get("bp_id", "")), str(v.get("epoch_id", "")), str(v.get("wave_id", ""))
    return None


def is_flat_lease_worker(repo: Path, conv_id: str) -> bool:
    """Return True when bound active lease for `conv_id` has `mode == 'flat'`."""
    cid = str(conv_id or "").strip()
    if not cid:
        return False
    for v in _load_active_leases(repo).values():
        if cid in (v.get("conv_ids") or []):
            return str(v.get("mode") or "captain").strip().lower() == "flat"
    return False


def check_file_lease(repo: Path, conv_id: str, rel_path: str, profile: dict[str, Any] | None = None) -> str | None:
    """Enforce active write-set leases (including single-worker & RO leases)."""
    leases = _load_active_leases(repo)
    if not leases:
        return None
    wo_cfg = resolve_delegation_config(profile).get("wave_orchestration") or {}
    if not wo_cfg.get("enable_lease_guard", True):
        return None
    hotspots = tuple(wo_cfg.get("shared_hotspots") or DEFAULT_SHARED_HOTSPOTS)
    rel, cid = _norm_rel(rel_path), str(conv_id or "").strip()
    owned = next((v for v in leases.values() if cid and cid in (v.get("conv_ids") or [])), None)

    if owned is not None:
        sub_map = owned.get("sub_leases") or {}
        allowed = sub_map[cid] if cid in sub_map else (owned.get("writes") or [])
        if not allowed:
            return f"LEASE DENY|Read-only worker `{owned.get('wave_id')}` (`{owned.get('epoch_id')}`) cannot edit `{rel}`."
        if not _matches_any(rel, allowed):
            return f"LEASE DENY|`{rel}` is outside owned write-set for wave `{owned.get('wave_id')}` (allowed={','.join(allowed)})."
        if _matches_any(rel, hotspots) and not (set(owned.get("locks") or []) & AUTHORING_EXCLUSIVE_LOCKS):
            return f"LEASE DENY|Shared hotspot `{rel}` locked during epoch `{owned.get('epoch_id')}`; emit receipt delta for Epoch Barrier."
        return None
    writing = [v for v in leases.values() if v.get("writes")]
    if _matches_any(rel, hotspots) or any(_matches_any(rel, v.get("writes") or []) for v in writing):
        return f"LEASE DENY|Unbound conversation `{cid[:12] or 'unknown'}` cannot edit `{rel}` while epoch leases are active; pass `[LEASE:...]`."
    return None


def _restore_file_from_baseline(repo: Path, rel: str, baseline_hashes: dict[str, str], backup_map: dict[str, str]) -> None:
    fp, bkp = repo / rel, backup_map.get(rel)
    if bkp and Path(bkp).is_file():
        fp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bkp, fp)
        return
    if baseline_hashes.get(rel) == "__DELETED__":
        if fp.exists():
            fp.unlink()
        return
    if (repo / ".git").exists():
        res = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", rel], capture_output=True, text=True, timeout=2, check=False)
        if res.returncode == 0:
            subprocess.run(["git", "-C", str(repo), "checkout", "--", rel], capture_output=True, timeout=3, check=False)
            return
    if fp.is_file():
        fp.unlink()


def epoch_barrier(
    repo: Path, bp_id: str, epoch_id: str, receipts: list[dict[str, Any]],
    *, probes: list[str] | None = None, run_probes: bool = True, artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Audit epoch deltas vs baseline, rollback failed/sibling/rogue writes, verify sealed probes, & checkpoint."""
    r = Path(repo).expanduser().resolve()
    ep_dir = get_ephemeral_dir(r)
    ep_file = ep_dir / "epochs" / f"{bp_id}__{epoch_id}.json"
    ep_doc = json.loads(ep_file.read_text(encoding="utf-8")) if ep_file.is_file() else {}
    baseline_hashes, backup_map = ep_doc.get("baseline_hashes") or {}, ep_doc.get("backup_map") or {}
    writes_by_wave = {str(w["id"]): list(w.get("writes") or []) for w in (ep_doc.get("waves") or [])}

    with _locked_lease_store(r) as store:
        ep_rec = store.get("epochs", {}).get(f"{bp_id}:{epoch_id}")
        ep_leases = {k: v for k, v in (store.get("leases") or {}).items() if k.startswith(f"{bp_id}:{epoch_id}:")}

    parent_of: dict[str, str] = {str(w["id"]): str(w.get("parent_id") or w["id"]) for w in (ep_doc.get("waves") or [])}
    for l_rec in ep_leases.values():
        wid = str(l_rec.get("wave_id") or "")
        if wid:
            parent_of.setdefault(wid, str(l_rec.get("parent_id") or wid))
    for rec in receipts:
        sid = str(rec.get("slice_id"))
        parent_of.setdefault(sid, sid)

    raw_ok_ids = {str(rec.get("slice_id")) for rec in receipts if str(rec.get("status")) == "ok"}
    raw_failed_ids = {str(rec.get("slice_id")) for rec in receipts if str(rec.get("status")) != "ok"}
    bad_parents = {parent_of.get(wid, wid) for wid in raw_failed_ids} | {
        parent_of.get(str(w["id"]), str(w["id"]))
        for w in (ep_doc.get("waves") or [])
        if str(w["id"]) not in raw_ok_ids
    }
    failed_ids = set(raw_failed_ids) | {wid for wid, pid in parent_of.items() if pid in bad_parents}
    ok_ids = raw_ok_ids - failed_ids
    completed_parents = sorted({parent_of.get(wid, wid) for wid in ok_ids} - bad_parents)

    ok_globs = [g for wid in ok_ids for g in writes_by_wave.get(wid, [])]
    fail_globs = [g for wid in failed_ids for g in writes_by_wave.get(wid, [])]

    cur_hashes, _ = _git_status_files(r)
    changed_during = [rel for rel, dig in cur_hashes.items() if baseline_hashes.get(rel) != dig]
    changed_during.extend(rel for rel in baseline_hashes if rel not in cur_hashes and (r / rel).is_file())

    rolled_back, rogue_writes, kept_changes = [], [], []
    for rel in sorted(set(changed_during)):
        if _matches_any(rel, ok_globs):
            kept_changes.append(rel)
        elif _matches_any(rel, fail_globs):
            _restore_file_from_baseline(r, rel, baseline_hashes, backup_map)
            rolled_back.append(rel)
        else:
            _restore_file_from_baseline(r, rel, baseline_hashes, backup_map)
            rogue_writes.append(rel)

    norm_probes = [str(x) for x in (probes or [])]
    probe_results, probes_ok, tamper_error = [], True, ""
    if isinstance(ep_rec, dict) and (ep_rec.get("hmac_key") or ep_rec.get("probes_sig")):
        exp_sig = _sign_probes(str(ep_rec.get("hmac_key") or ""), norm_probes)
        if not ep_rec.get("hmac_key") or not hmac.compare_digest(exp_sig, str(ep_rec.get("probes_sig") or "")):
            probes_ok, tamper_error = False, "PROBE_SIG_MISMATCH"

    if run_probes and norm_probes and not rogue_writes and not tamper_error and ok_ids:
        seen_cmd, env = set(), dict(os.environ)
        env.setdefault("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1")
        env.setdefault("DOTNET_GCHeapHardLimit", "20000000")
        env.setdefault("DOTNET_EnableDiagnostics", "0")
        for c in (x.strip() for x in norm_probes if x.strip()):
            if c in seen_cmd:
                continue
            seen_cmd.add(c)
            p_res = subprocess.run(c, shell=True, cwd=str(r), env=env, capture_output=True, text=True, timeout=240, check=False)
            probe_results.append({"cmd": c, "returncode": p_res.returncode})
            if p_res.returncode != 0:
                probes_ok = False
                break

    if artifact_dir is not None:
        hook_file = Path(artifact_dir).expanduser().resolve() / "scratch" / ".hook_state.json"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        with hook_file.with_suffix(".lock").open("a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                raw_h = json.loads(hook_file.read_text(encoding="utf-8")) if hook_file.is_file() else {}
                hook_data = raw_h if isinstance(raw_h, dict) else {}
            except Exception:
                hook_data = {}
            try:
                can_clear = bool(probes_ok and not rogue_writes and not tamper_error and not (kept_changes and not probe_results))
                hook_data.update({"needs_verify": False, "needs_build": False, "last_build_failed": False} if can_clear else {
                    "needs_verify": True, "needs_build": True,
                    "last_build_failed": (not probes_ok) or bool(tamper_error) or bool(hook_data.get("last_build_failed", False)),
                })
                edited = list(hook_data.get("code_files_edited") or [])
                edited.extend(rel for rel in kept_changes if rel not in edited)
                hook_data["code_files_edited"] = edited[:100]
                tmp_h = hook_file.with_name(f".hook_state_{os.getpid()}.tmp")
                tmp_h.write_text(json.dumps(hook_data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_h, hook_file)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    cp_dir = ep_dir / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    cp_data = load_checkpoint(r, bp_id) or {"bp_id": bp_id, "waves": {}}
    if probes_ok and not rogue_writes and not tamper_error:
        for pid in completed_parents:
            cp_data["waves"][pid] = {"status": "done", "epoch": epoch_id, "ts": int(time.time())}
        (cp_dir / f"{bp_id}.json").write_text(json.dumps(cp_data, ensure_ascii=False, indent=2), encoding="utf-8")

    with _locked_lease_store(r) as store:
        store["epochs"].pop(f"{bp_id}:{epoch_id}", None)
        for k in [k for k in store.get("leases", {}) if k.startswith(f"{bp_id}:{epoch_id}:")]:
            store["leases"].pop(k, None)

    return {
        "bp_id": bp_id, "epoch_id": epoch_id, "ok_waves": sorted(ok_ids), "failed_waves": sorted(failed_ids),
        "kept_changes": kept_changes, "rolled_back": rolled_back, "rogue_writes": rogue_writes,
        "probes_ok": probes_ok, "probe_results": probe_results, "tamper_error": tamper_error,
    }


def compile_fanout_schedule(
    repo: Path, *, targets: list[str] | None = None, manifest: list[Any] | dict[str, Any] | Path | str | None = None,
    task: str = "", writes: list[str] | None = None, tier: str = "L1", max_width: int | None = None,
    profile: dict[str, Any] | None = None, bp_id: str | None = None,
) -> dict[str, Any]:
    """Compile arbitrary manifest or targets list into conflict-free bounded parallel fanout epochs."""
    r = Path(repo).expanduser().resolve()
    host_id, eff_width = _resolve_eff_width(r, max_width=max_width, profile=profile)
    raw_items, man_probes = [], []
    if manifest is not None and manifest != "":
        loaded: Any = manifest
        if isinstance(manifest, (Path, str)):
            s_man = str(manifest).strip()
            loaded = json.loads(s_man) if s_man.startswith(("{", "[")) else json.loads(((Path(s_man).expanduser()) if Path(s_man).expanduser().is_absolute() else (r / s_man).resolve()).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw_items = list(loaded.get("cubicles") or loaded.get("items") or loaded.get("targets") or loaded.get("waves") or [])
            bp_id, task = bp_id or (str(loaded["bp_id"]) if loaded.get("bp_id") else None), task or str(loaded.get("task") or "")
            man_probes = [str(p) for p in (loaded.get("probes") or []) if str(p).strip()]
        elif isinstance(loaded, list):
            raw_items = list(loaded)
    if not raw_items and targets:
        raw_items = [{"id": f"F{i}", "target": t, "reads": [t], "writes": list(writes or [])} for i, t in enumerate(targets, 1) if str(t).strip()]
    if not raw_items:
        raise ValueError("Fanout requires `--manifest` or `--targets`.")

    def_writes = [_norm_rel(x) for x in (writes or []) if _norm_rel(x) and _norm_rel(x).upper() not in ("RO", "NONE")]
    cubicles: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_items, 1):
        it = {"target": raw, "reads": [raw]} if isinstance(raw, str) else dict(raw)
        cid = str(it.get("id") or f"F{idx}")
        c_writes = [_norm_rel(x) for x in ((def_writes if it.get("writes") is None else it.get("writes")) or []) if _norm_rel(x) and _norm_rel(x).upper() not in ("RO", "NONE")]
        c_reads = [_norm_rel(x) for x in (it.get("reads") or ([it["target"]] if it.get("target") else [])) if _norm_rel(x) and _norm_rel(x).upper() not in ("NONE", "NULL")]
        c_locks = sorted({str(x).lower() for x in (it.get("locks") or []) if str(x).strip()})
        c_scope = str(it.get("scope") or it.get("task") or task or f"Process {', '.join(c_reads or c_writes or [cid])}")
        c_tier = str(it.get("tier") or (tier if c_writes else "L0")).upper()
        c_tier = "L3" if _has_positive_complex_marker(c_scope) else (c_tier if c_tier in VALID_TIERS else ("L0" if not c_writes else "L1"))
        c_model = str(it.get("model") or _TIER_MODEL_MAP.get(c_tier, "flash"))
        if c_model not in ("flash_lite", "flash", "pro"):
            c_model = _TIER_MODEL_MAP.get(c_tier, "flash")
        cubicles.append({
            "id": cid, "parent_id": str(it.get("parent_id") or cid), "mode": "flat", "tier": c_tier, "model": c_model,
            "writes": c_writes, "reads": c_reads, "locks": c_locks, "slices": preslice_wave_inputs(r, c_writes, c_reads),
            "scope": c_scope, "io": str(it.get("io") or "<=600B JSON receipt"), "probe": str(it.get("probe") or ""),
        })

    buckets: list[list[dict[str, Any]]] = []
    for c in cubicles:
        for bk in buckets:
            if len(bk) < eff_width and not any(_items_conflict(c, x) for x in bk):
                bk.append(c)
                break
        else:
            buckets.append([c])

    epochs_out = [
        {"epoch_id": f"E{k}", "waves": bk, "cubicles": bk, "barrier_probes": list(dict.fromkeys([*man_probes, *(c["probe"].strip() for c in bk if c["probe"].strip())])), "barrier_locks": sorted({lk for c in bk for lk in c["locks"] if lk in BARRIER_DEFERRED_LOCKS})}
        for k, bk in enumerate(buckets, 1)
    ]
    eff_bp = bp_id or f"FANOUT_{hashlib.sha256(('|'.join(c['id'] for c in cubicles) + task).encode()).hexdigest()[:8]}"
    return {
        "kind": "fanout", "bp_id": eff_bp, "repo_root": str(r), "mode": "parallel", "host_id": host_id,
        "max_width": eff_width, "total_waves": len(cubicles), "total_cubicles": len(cubicles), "total_epochs": len(epochs_out), "epochs": epochs_out,
    }


def compile_stage13_workflow(
    target: str | Path, repo: Path, out_path: Path | None = None, *,
    artifact_dir: Path | None = None, max_width: int | None = None, profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile bounded Stage 1 (flash) -> Python synth -> Stage 2 (blind pro [L3]) -> Python mask -> Stage 3 (cold pro [L3])."""
    from blueprint_engine import _build_subagent_topology, classify_blueprint_request  # noqa: E402

    r, t_str = Path(repo).expanduser().resolve(), str(target or "").strip()
    t_path = Path(t_str).expanduser() if t_str and "\n" not in t_str else None
    cand = (t_path if t_path.is_absolute() else (r / t_path)) if t_path is not None else None
    t_text = cand.read_text(encoding="utf-8", errors="ignore")[:6000] if cand is not None and cand.is_file() else t_str

    clf = classify_blueprint_request(t_text, profile=profile, repo_root=r)
    topo = clf.get("subagent_topology") or _build_subagent_topology(
        str(clf.get("recommended_format") or "full"), str(clf.get("door_type") or "1-way"),
        str(clf.get("risk_tier") or "T3"), list(clf.get("active_domains") or ["software"]), list(clf.get("morph_axes") or []),
    )
    host_id, max_conc = _resolve_eff_width(r, max_width=max_width, profile=profile)
    s1_count = max(1, min(3, int((topo.get("stage_1_scouts") or {}).get("count") or 2)))
    s2_count = max(2, min(3, int((topo.get("stage_2_blind_solvers") or {}).get("count") or 2)))
    axes = [a["axis"] for a in (clf.get("morph_axes") or []) if isinstance(a, dict) and a.get("axis")]
    for fb in ("Architecture/Boundary", "Failure/Invariant", "Cost/Latency"):
        if fb not in axes:
            axes.append(fb)
    scouts = [{"id": f"S1_{i}", "domains": clf.get("active_domains") or ["software"]} for i in range(1, s1_count + 1)]
    solvers = [{"id": f"S2_{i}", "axis": axes[i - 1]} for i in range(1, s2_count + 1)]
    art = str((artifact_dir or (Path(out_path).expanduser().resolve().parent.parent if out_path else r / ".agents")).expanduser().resolve())

    s1_schema = {"type": "object", "additionalProperties": False, "properties": {"scout_id": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}, "constraints": {"type": "array", "items": {"type": "string"}}, "oss_refs": {"type": "array", "items": {"type": "string"}}}, "required": ["scout_id", "facts", "constraints"]}
    s2_schema = {"type": "object", "additionalProperties": False, "properties": {"solver_id": {"type": "string"}, "axis": {"type": "string"}, "thesis": {"type": "string"}, "mechanism": {"type": "string"}, "triz_resolution": {"type": "string"}, "rejected_alts": {"type": "array", "items": {"type": "string"}}, "risks": {"type": "array", "items": {"type": "string"}}}, "required": ["solver_id", "axis", "thesis", "mechanism", "rejected_alts"]}
    s3_schema = {"type": "object", "additionalProperties": False, "properties": {"verdict": {"type": "string", "enum": ["pass", "revise", "kill"]}, "selected_option": {"type": "string"}, "premortem_p0": {"type": "array", "items": {"type": "string"}}, "kill_triggers": {"type": "array", "items": {"type": "string"}}, "required_fixes": {"type": "array", "items": {"type": "string"}}}, "required": ["verdict", "selected_option", "premortem_p0", "kill_triggers"]}

    script = f'''#!/usr/bin/env python3
"""Auto-generated Bounded Stage 1-3 Workflow (`{host_id}`, max_concurrency={max_conc})."""
import json, pathlib
TARGET_BRIEF, REPO_ROOT, ART_DIR = {json.dumps(t_text[:1800])}, {json.dumps(str(r))}, {json.dumps(art)}
HOST_ID = {json.dumps(host_id)}
MAX_CONCURRENCY = {max_conc}
SCOUTS, SOLVERS = {repr(scouts)}, {repr(solvers)}
STAGE1_SCHEMA, STAGE2_SCHEMA, STAGE3_SCHEMA = {repr(s1_schema)}, {repr(s2_schema)}, {repr(s3_schema)}

async def run_bounded_parallel(items, fn, max_width=MAX_CONCURRENCY):
    w, out = max(1, int(max_width or 1)), []
    for i in range(0, len(items), w): out.extend(await parallel(items[i : i + w], fn))
    return out

def synthesize_stage1(items):
    facts, cons, refs = [], [], []
    for it in (x for x in items if isinstance(x, dict)):
        for dst, key, cap in ((facts, "facts", 160), (cons, "constraints", 160), (refs, "oss_refs", 120)):
            for val in (it.get(key) or []):
                if str(val) not in dst: dst.append(str(val)[:cap])
    return {{"facts": facts[:12], "constraints": cons[:12], "oss_refs": refs[:8]}}

def mask_stage2_proposals(items):
    return [{{"option": f"Option {{chr(65 + idx)}}", "axis": str(d.get("axis") or "")[:80], "thesis": str(d.get("thesis") or "")[:240], "mechanism": str(d.get("mechanism") or "")[:400], "triz_resolution": str(d.get("triz_resolution") or "")[:240], "rejected_alts": [str(x)[:120] for x in (d.get("rejected_alts") or [])[:4]], "risks": [str(x)[:120] for x in (d.get("risks") or [])[:4]]}} for idx, it in enumerate(items) for d in [it if isinstance(it, dict) else {{}}]]

async def run_stage1_scout(sc):
    return await agent(f"Stage 1 Scout {{sc['id']}} (domains={{','.join(sc['domains'])}}). Brief: {{TARGET_BRIEF}}\\nFORBIDDEN: nested subagents, file edits, or heavy commands.", schema=STAGE1_SCHEMA, role=f"Scout {{sc['id']}}", type_name="self", workspace="inherit", model="flash")

async def run_stage2_solver(sv, synth):
    return await agent(f"[L3] Blind Stage 2 Solver {{sv['id']}} (axis={{sv['axis']}}; !see_peer).\\nBrief: {{TARGET_BRIEF}}\\nSynthesis: {{json.dumps(synth, ensure_ascii=False)}}\\nFORBIDDEN: nested subagents, file edits, or heavy commands.", schema=STAGE2_SCHEMA, role=f"Solver {{sv['id']}}", type_name="self", workspace="inherit", model="pro")

phase("Stage 1: Bounded Scouts (flash)")
synth = synthesize_stage1(await run_bounded_parallel(SCOUTS, run_stage1_scout, MAX_CONCURRENCY))
phase("Stage 2: Blind Solvers (pro [L3])")
masked = mask_stage2_proposals(await run_bounded_parallel(SOLVERS, lambda sv: run_stage2_solver(sv, synth), MAX_CONCURRENCY))
phase("Stage 3: Cold Red-Team (pro [L3])")
s3_out = await agent(f"[L3] Cold Red-Team Pre-Mortem (zero builder history). Constraints: {{json.dumps(synth['constraints'], ensure_ascii=False)}}\\nCandidates: {{json.dumps(masked, ensure_ascii=False)}}\\nFORBIDDEN: nested subagents, file edits, or heavy commands.", schema=STAGE3_SCHEMA, role="Cold Red-Team", type_name="self", workspace="inherit", model="pro")
out_d = pathlib.Path(ART_DIR) / "scratch"
out_d.mkdir(parents=True, exist_ok=True)
(out_d / "stage13_result.json").write_text(json.dumps({{"synthesis": synth, "masked": masked, "verdict": s3_out}}, ensure_ascii=False, indent=2), encoding="utf-8")
log("STAGE13_DONE|" + json.dumps(s3_out if isinstance(s3_out, dict) else {{"verdict": "unknown"}}, ensure_ascii=False))
'''
    res: dict[str, Any] = {
        "kind": "stage1-3", "target": t_str, "host_id": host_id, "max_width": max_conc, "stage1_count": s1_count,
        "stage2_count": s2_count, "stage3_count": 1, "topology": topo,
        "stage2_schema_bytes": len(json.dumps(s2_schema)), "stage3_schema_bytes": len(json.dumps(s3_schema)), "script": script,
    }
    if out_path is not None:
        out = Path(out_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(script, encoding="utf-8")
        res["workflow_path"] = str(out)
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic Multi-Wave Blueprint Scheduler & Lease Engine.")
    parser.add_argument("--workspace", default="", help="Workspace root override.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sch = sub.add_parser("schedule")
    p_sch.add_argument("blueprint_path")
    p_sch.add_argument("--mode", choices=["seq", "parallel"], default="parallel")
    p_sch.add_argument("--max-width", type=int, default=0)
    for fl in ("--emit-prompts", "--ignore-checkpoints", "--json"):
        p_sch.add_argument(fl, action="store_true")
    p_sch.add_argument("--emit-workflow", default="")
    p_fo = sub.add_parser("fanout")
    for fl in ("--manifest", "--task", "--emit-workflow"):
        p_fo.add_argument(fl, default="")
    for fl in ("--targets", "--writes"):
        p_fo.add_argument(fl, action="append", default=[])
    p_fo.add_argument("--tier", default="L1")
    p_fo.add_argument("--max-width", type=int, default=0)
    p_fo.add_argument("--json", action="store_true")
    p_s13 = sub.add_parser("stage1-3")
    p_s13.add_argument("target")
    p_s13.add_argument("--emit-workflow", default="")
    p_s13.add_argument("--json", action="store_true")
    p_eb = sub.add_parser("epoch-begin")
    for fl in ("--bp", "--epoch", "--meta-file"):
        p_eb.add_argument(fl, required=True)
    p_eb.add_argument("--ttl", type=int, default=3600)
    p_eb.add_argument("--owner-pid", type=int, default=None)
    p_bar = sub.add_parser("epoch-barrier")
    for fl in ("--bp", "--epoch", "--receipts-file"):
        p_bar.add_argument(fl, required=True)
    p_bar.add_argument("--meta-file", default="")
    for fl in ("--skip-probes", "--json"):
        p_bar.add_argument(fl, action="store_true")
    ls_sub = sub.add_parser("lease").add_subparsers(dest="lease_cmd", required=True)
    ls_sub.add_parser("status")
    p_lb = ls_sub.add_parser("bind")
    for fl in ("--conv-id", "--tag"):
        p_lb.add_argument(fl, required=True)
    p_lr = ls_sub.add_parser("release")
    for fl in ("--bp", "--epoch"):
        p_lr.add_argument(fl, default="")
    cp_sub = sub.add_parser("checkpoint").add_subparsers(dest="cp_cmd", required=True)
    for c_name in ("show", "clear"):
        cp_sub.add_parser(c_name).add_argument("bp_id")

    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    prof_path = ws / ".agents" / "planner_profile.json"
    try:
        profile = json.loads(prof_path.read_text(encoding="utf-8", errors="ignore")) if prof_path.is_file() else {}
    except Exception:
        profile = {}
    art_env = os.environ.get("PLANNER_ARTIFACT_DIR", "").strip()
    _art = lambda wf: Path(art_env) if art_env else (Path(wf).expanduser().resolve().parent.parent if wf else None)

    if args.cmd == "schedule":
        bp_p = Path(args.blueprint_path).expanduser()
        bp_p = bp_p if bp_p.is_absolute() else (ws / bp_p).resolve()
        try:
            res = schedule_blueprint(bp_p, ws, mode=args.mode, max_width=(args.max_width or None), profile=profile, ignore_checkpoints=bool(args.ignore_checkpoints))
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.emit_workflow:
            res["workflow_path"] = str(emit_workflow_script(res, bp_p, ws, Path(args.emit_workflow), artifact_dir=_art(args.emit_workflow)))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"SCHEDULE|{res['bp_id']}|mode={res['mode']}|host={res['host_id']}|max_w={res['max_width']}|waves={res['total_waves']}|epochs={res['total_epochs']}")
            for ep in res["epochs"]:
                waves_desc = ",".join(f"{w['id']}({w['tier']})" for w in ep["waves"])
                print(f"  {ep['epoch_id']}: [{waves_desc}] | probes={len(ep['barrier_probes'])}")
            if args.emit_prompts:
                print(emit_subagent_prompts(res))
        return 0

    if args.cmd == "fanout":
        flat_t = [x for tok in (args.targets or []) for x in _split_csv(tok)]
        flat_w = [x for tok in (args.writes or []) for x in _split_csv(tok)]
        try:
            res = compile_fanout_schedule(ws, targets=flat_t, manifest=(args.manifest or None), task=args.task, writes=flat_w, tier=args.tier, max_width=(args.max_width or None), profile=profile)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.emit_workflow:
            res["workflow_path"] = str(emit_fanout_workflow(res, ws, Path(args.emit_workflow), artifact_dir=_art(args.emit_workflow)))
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else f"FANOUT|{res['bp_id']}|host={res['host_id']}|max_w={res['max_width']}|cubicles={res['total_cubicles']}|epochs={res['total_epochs']}")
        return 0

    if args.cmd == "stage1-3":
        wf_p = Path(args.emit_workflow) if args.emit_workflow else None
        res = compile_stage13_workflow(args.target, ws, out_path=wf_p, artifact_dir=_art(args.emit_workflow), profile=profile)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else f"STAGE13|host={res['host_id']}|max_w={res['max_width']}|s1={res['stage1_count']}|s2={res['stage2_count']}|s3={res['stage3_count']}|wf={res.get('workflow_path', 'inline')}")
        return 0

    if args.cmd == "epoch-begin":
        meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8"))
        print(json.dumps(epoch_begin(ws, args.bp, args.epoch, meta.get("waves") or [], nonces=meta.get("nonces"), ttl_secs=args.ttl, owner_pid=args.owner_pid, probes=meta.get("probes") or []), ensure_ascii=False))
        return 0

    if args.cmd == "epoch-barrier":
        rec_raw = json.loads(Path(args.receipts_file).read_text(encoding="utf-8"))
        receipts = rec_raw if isinstance(rec_raw, list) else [rec_raw]
        meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8")) if args.meta_file and Path(args.meta_file).is_file() else {}
        res = epoch_barrier(ws, args.bp, args.epoch, receipts, probes=meta.get("probes") or [], run_probes=(not args.skip_probes), artifact_dir=(Path(art_env) if art_env else None))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            t_str = f"|tamper={res['tamper_error']}" if res.get("tamper_error") else ""
            print(f"BARRIER|{res['epoch_id']}|ok={','.join(res['ok_waves']) or 'none'}|fail={','.join(res['failed_waves']) or 'none'}|rollback={len(res['rolled_back'])}|rogue={len(res['rogue_writes'])}|probes_ok={res['probes_ok']}{t_str}")
        return 0 if (res["probes_ok"] and not res["rogue_writes"] and not res["failed_waves"] and not res.get("tamper_error")) else 1

    if args.cmd == "lease":
        if args.lease_cmd == "status":
            print(json.dumps(_load_active_leases(ws), ensure_ascii=False, indent=2))
            return 0
        if args.lease_cmd == "bind":
            parsed_tag = extract_lease_tag(args.tag)
            if not parsed_tag:
                print("ERROR: invalid [LEASE:...] tag", file=sys.stderr)
                return 1
            ok = bind_lease_conversation(ws, args.conv_id, parsed_tag["bp_id"], parsed_tag["epoch_id"], parsed_tag["wave_id"], parsed_tag["nonce"], parsed_tag["sub_globs"], mode=parsed_tag.get("mode"))
            print(json.dumps({"bound": ok}))
            return 0 if ok else 1
        with _locked_lease_store(ws) as store:
            for k in [k for k in store.get("leases", {}) if (not args.bp or k.startswith(f"{args.bp}:")) and (not args.epoch or f":{args.epoch}:" in k)]:
                store["leases"].pop(k, None)
        print("RELEASED")
        return 0

    if args.cmd == "checkpoint":
        print(json.dumps(load_checkpoint(ws, args.bp_id) if args.cp_cmd == "show" else {"cleared": clear_checkpoint(ws, args.bp_id)}, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
