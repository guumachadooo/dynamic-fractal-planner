#!/usr/bin/env python3
"""Deterministic Multi-Wave DAG Epoch Scheduler, `[LEASE:...]` Guard & O(1) Workflow Compiler."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
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

from host_context import detect_host  # noqa: E402
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
_LEASE_TAG_RE = re.compile(r"\[LEASE:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([A-Za-z0-9_-]+)(?::sub=([^\]]+))?\]")
_SUB_BULLET_RE = re.compile(r"^[-*]\s+`?([A-Za-z]+\d+(?:\.\d+)*[a-z]?)`?\s*:")
_AFTER_DEP_RE = re.compile(r"\b(?:after|depends\s+on|blocked\s+by)\s+`?([A-Za-z]+\d+(?:\.\d+)*[a-z]?)`?", re.I)


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


def infer_legacy_wave_meta(wave: dict[str, Any]) -> dict[str, Any]:
    """Best-effort deterministic write-set & lock inference for unannotated `SLICE|` lines."""
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
    deps = [m.group(1).strip() for m in _AFTER_DEP_RE.finditer(blob) if m.group(1).strip() != wave.get("id")]
    return {"deps": list(dict.fromkeys(deps)), "writes": writes, "reads": [], "locks": sorted(set(locks)), "tier": "L2", "inferred": True}


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

    del_cfg = resolve_delegation_config(profile)
    wo_cfg = del_cfg.get("wave_orchestration") if isinstance(del_cfg.get("wave_orchestration"), dict) else {}
    host_id = str(detect_host(repo).get("host_id") or "local-ide")
    termux_cap = int(parsed["policy"].get("max_width_termux") or wo_cfg.get("max_width_termux") or 2)
    local_cap = int(parsed["policy"].get("max_width_local") or wo_cfg.get("max_width_local") or 4)
    eff_width = min(max_width or termux_cap, termux_cap, 2) if host_id == "antigravity-termux" else ((max_width or 1) if host_id == "ai-studio-cloud" else min(max_width or local_cap, 8))
    eff_width = max(1, eff_width)

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
        mu, lu = meta_map[u], set(meta_map[u]["locks"]) & AUTHORING_EXCLUSIVE_LOCKS
        for v in active_list[i + 1 :]:
            mv, lv = meta_map[v], set(meta_map[v]["locks"]) & AUTHORING_EXCLUSIVE_LOCKS
            if (
                (lu & lv)
                or any(globs_overlap(wu, wv) for wu in mu["writes"] for wv in mv["writes"])
                or any(globs_overlap(ru, wv) for ru in mu["reads"] for wv in mv["writes"])
                or any(globs_overlap(rv, wu) for rv in mv["reads"] for wu in mu["writes"])
            ):
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
        ep_waves, probes, seen_p, barrier_locks = [], [], set(), set()
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
        epochs_out.append({"epoch_id": f"E{k}", "waves": ep_waves, "barrier_probes": probes, "barrier_locks": sorted(barrier_locks)})

    return {
        "bp_id": bp_id, "bp_path": str(p), "mode": req_mode, "host_id": host_id, "max_width": eff_width,
        "total_waves": len(active_waves), "done_skipped": len(parsed["waves"]) - len(active_waves),
        "total_epochs": len(epochs_out), "warnings": issues, "epochs": epochs_out,
    }


def emit_workflow_script(schedule: dict[str, Any], bp_path: Path, repo: Path, out_path: Path, artifact_dir: Path | None = None) -> Path:
    """Emit self-contained O(1) root-context `run_workflow` script for Antigravity."""
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    art = str((artifact_dir or out.parent.parent).expanduser().resolve())
    plan_cli = str((_SCRIPTS_DIR / "plan_tool.py").resolve())
    script = f'''#!/usr/bin/env python3
"""Auto-generated O(1) Multi-Wave Epoch Workflow (`{schedule["bp_id"]}`, mode={schedule["mode"]})."""
import json, pathlib, secrets, shlex
SCHEDULE = {json.dumps(schedule, ensure_ascii=False, indent=2)}
BP_ID, REPO_ROOT, ART_DIR, PLAN_CLI = SCHEDULE["bp_id"], {json.dumps(str(Path(repo).expanduser().resolve()))}, {json.dumps(art)}, {json.dumps(plan_cli)}
RECEIPT_SCHEMA = {{"type": "object", "properties": {{"slice_id": {{"type": "string"}}, "status": {{"type": "string", "enum": ["ok", "failed"]}}, "files_modified": {{"type": "array", "items": {{"type": "string"}}}}, "l10n_deltas": {{"type": "array", "items": {{"type": "object"}}}}, "ledger_note": {{"type": "string"}}, "verify_needed": {{"type": "boolean"}}, "sha256": {{"type": "string"}}, "err": {{"type": "string"}}}}, "required": ["slice_id", "status", "files_modified", "err"]}}
ENV_PREFIX = f"export PLANNER_ARTIFACT_DIR={{shlex.quote(ART_DIR)}} CAVERN_ARTIFACT_DIR={{shlex.quote(ART_DIR)}} PLANNER_WORKSPACE_ROOT={{shlex.quote(REPO_ROOT)}}"

async def run_wave_captain(epoch_id, wave, nonce):
    lease = f"[LEASE:{{BP_ID}}:{{epoch_id}}:{{wave['id']}}:{{nonce}}]"
    prompt = (f"{{lease}}\\nWave-Captain `{{BP_ID}}` `{{wave['id']}}` ({{epoch_id}}). Scope: {{wave['scope']}}. Outcome: {{wave['io']}}. "
              f"Owned writes ONLY: {{', '.join(wave.get('writes') or ['RO'])}}. FORBIDDEN: out-of-lease edits, hotspots, heavy builds/tests, `git commit`/`push`. "
              f"May nest <=2 `invoke_subagent` (`flash_lite`/`flash`) leaves w/ `{{lease}}:sub=<glob>`. Return <=600B JSON receipt.")
    try:
        return await agent(prompt, schema=RECEIPT_SCHEMA, role=f"{{wave['id']}} Captain", type_name="self", workspace="inherit", model=wave.get("model", "flash"))
    except Exception as exc:
        return {{"slice_id": wave["id"], "status": "failed", "files_modified": [], "l10n_deltas": [], "ledger_note": "", "verify_needed": False, "sha256": "", "err": str(exc)[:180]}}

async def run_epoch(epoch):
    ek, nonces = epoch["epoch_id"], {{w["id"]: secrets.token_hex(6) for w in epoch["waves"]}}
    phase(f"Epoch {{ek}} ({{len(epoch['waves'])}} wave(s))")
    scratch = pathlib.Path(ART_DIR) / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    meta_f, rec_f = scratch / f"epoch_meta_{{BP_ID}}_{{ek}}.json", scratch / f"epoch_receipts_{{BP_ID}}_{{ek}}.json"
    meta_f.write_text(json.dumps({{"epoch_id": ek, "waves": epoch["waves"], "nonces": nonces, "probes": epoch["barrier_probes"]}}), encoding="utf-8")
    await call_tool("run_command", CommandLine=f"{{ENV_PREFIX}} && python3 -B {{shlex.quote(PLAN_CLI)}} --workspace {{shlex.quote(REPO_ROOT)}} blueprint epoch-begin --bp {{shlex.quote(BP_ID)}} --epoch {{shlex.quote(ek)}} --meta-file {{shlex.quote(str(meta_f))}}", WaitMsBeforeAsync=10000, toolSummary=f"Begin {{ek}}")
    receipts = await parallel(epoch["waves"], lambda w: run_wave_captain(ek, w, nonces[w["id"]]))
    rec_f.write_text(json.dumps(receipts, ensure_ascii=False), encoding="utf-8")
    out_b = await call_tool("run_command", CommandLine=f"{{ENV_PREFIX}} && python3 -B {{shlex.quote(PLAN_CLI)}} --workspace {{shlex.quote(REPO_ROOT)}} blueprint epoch-barrier --bp {{shlex.quote(BP_ID)}} --epoch {{shlex.quote(ek)}} --receipts-file {{shlex.quote(str(rec_f))}} --meta-file {{shlex.quote(str(meta_f))}}", WaitMsBeforeAsync=10000, toolSummary=f"Verify {{ek}}")
    bars = [ln for ln in str(out_b).splitlines() if ln.startswith("BARRIER|")]
    log(bars[0] if bars else f"BARRIER|{{ek}}|done={{len(receipts)}}")
    return {{"epoch_id": ek, "receipts": receipts}}

results = await pipeline(SCHEDULE["epochs"], run_epoch)
log(f"WORKFLOW_DONE|{{BP_ID}}|mode={{SCHEDULE['mode']}}|epochs={{len(results)}}")
'''
    out.write_text(script, encoding="utf-8")
    return out


def emit_subagent_prompts(schedule: dict[str, Any]) -> str:
    """Emit concise D0 -> D1 Wave-Captain -> D2 Leaf fallback prompts when `run_workflow` is unavailable."""
    lines = [f"SUBAGENT_TREE|bp={schedule['bp_id']}|mode={schedule['mode']}|epochs={schedule['total_epochs']}"]
    for ep in schedule["epochs"]:
        ek = ep["epoch_id"]
        lines.append(f"EPOCH|{ek}|probes={len(ep['barrier_probes'])}")
        for w in ep["waves"]:
            nonce = secrets.token_hex(4)
            lines.append(
                f"  D1_CAPTAIN|model={w['model']}|[LEASE:{schedule['bp_id']}:{ek}:{w['id']}:{nonce}]"
                f"|writes={','.join(w['writes'] or ['RO'])}|EnableSubagentTools=true -> nest<=2 D2(flash_lite/flash)"
            )
    return "\n".join(lines)


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
            for k in [k for k, v in leases.items() if float(v.get("expires_at") or 0.0) < now]:
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
    with _locked_lease_store(r) as store:
        for w in waves_meta:
            wid = str(w["id"])
            nonce = (nonces or {}).get(wid) or secrets.token_hex(6)
            store["leases"][f"{bp_id}:{epoch_id}:{wid}"] = {
                "bp_id": bp_id, "epoch_id": epoch_id, "wave_id": wid, "nonce": nonce,
                "writes": [_norm_rel(x) for x in (w.get("writes") or [])],
                "reads": [_norm_rel(x) for x in (w.get("reads") or [])],
                "locks": [str(x).lower() for x in (w.get("locks") or [])],
                "conv_ids": [], "sub_leases": {}, "created_at": now, "expires_at": now + max(60, int(ttl_secs)),
            }
            out_leases[wid] = f"[LEASE:{bp_id}:{epoch_id}:{wid}:{nonce}]"
        store["epochs"][f"{bp_id}:{epoch_id}"] = {"started_at": now, "waves": [w["id"] for w in waves_meta]}

    (ep_meta_dir / f"{bp_id}__{epoch_id}.json").write_text(
        json.dumps({"bp_id": bp_id, "epoch_id": epoch_id, "baseline_hashes": baseline_hashes,
                    "untracked_pre": untracked_pre, "backup_map": backup_map, "waves": waves_meta}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"bp_id": bp_id, "epoch_id": epoch_id, "leases": out_leases, "preexisting_dirty": len(baseline_hashes)}


def extract_lease_tag(text: str) -> dict[str, Any] | None:
    m = _LEASE_TAG_RE.search(text or "")
    if not m:
        return None
    return {
        "bp_id": m.group(1), "epoch_id": m.group(2), "wave_id": m.group(3), "nonce": m.group(4),
        "sub_globs": [_norm_rel(x) for x in _split_csv(m.group(5) or "") if x.strip()],
    }


def bind_lease_conversation(
    repo: Path, conv_id: str, bp_id: str, epoch_id: str, wave_id: str, nonce: str, sub_globs: list[str] | None = None,
) -> bool:
    """Bind runtime conversationId (D1 Captain or D2 Leaf) to its active lease when nonce matches."""
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
        return {k: v for k, v in leases.items() if isinstance(v, dict) and float(v.get("expires_at") or 0.0) >= now}
    except Exception:
        return {}


def has_active_parallel_leases(repo: Path) -> bool:
    return sum(1 for v in _load_active_leases(repo).values() if v.get("writes")) >= 2


def is_parallel_wave_worker(repo: Path, conv_id: str) -> tuple[str, str, str] | None:
    leases = _load_active_leases(repo)
    if sum(1 for v in leases.values() if v.get("writes")) < 2:
        return None
    cid = str(conv_id or "").strip()
    for v in leases.values():
        if cid and cid in (v.get("conv_ids") or []):
            return str(v.get("bp_id", "")), str(v.get("epoch_id", "")), str(v.get("wave_id", ""))
    return None


def check_file_lease(repo: Path, conv_id: str, rel_path: str, profile: dict[str, Any] | None = None) -> str | None:
    """Enforce active parallel write-set leases (<1ms no-op when fewer than 2 active writing waves)."""
    leases = _load_active_leases(repo)
    writing = [v for v in leases.values() if v.get("writes")]
    if len(writing) < 2:
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
        if not _matches_any(rel, allowed):
            return f"LEASE DENY|`{rel}` is outside owned write-set for wave `{owned.get('wave_id')}` (allowed={','.join(allowed) or 'RO'})."
        if _matches_any(rel, hotspots) and not (set(owned.get("locks") or []) & AUTHORING_EXCLUSIVE_LOCKS):
            return f"LEASE DENY|Shared hotspot `{rel}` locked during parallel epoch `{owned.get('epoch_id')}`; emit receipt delta for Epoch Barrier."
        return None
    if _matches_any(rel, hotspots) or any(_matches_any(rel, v.get("writes") or []) for v in writing):
        return f"LEASE DENY|Unbound conversation `{cid[:12] or 'unknown'}` cannot edit `{rel}` while parallel epoch leases are active; pass `[LEASE:...]`."
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
    """Audit epoch deltas vs pre-epoch baseline, rollback failed/rogue writes, verify once, & checkpoint."""
    r = Path(repo).expanduser().resolve()
    ep_dir = get_ephemeral_dir(r)
    ep_file = ep_dir / "epochs" / f"{bp_id}__{epoch_id}.json"
    ep_doc = json.loads(ep_file.read_text(encoding="utf-8")) if ep_file.is_file() else {}
    baseline_hashes, backup_map = ep_doc.get("baseline_hashes") or {}, ep_doc.get("backup_map") or {}
    writes_by_wave = {str(w["id"]): list(w.get("writes") or []) for w in (ep_doc.get("waves") or [])}

    ok_ids = {str(rec.get("slice_id")) for rec in receipts if str(rec.get("status")) == "ok"}
    failed_ids = {str(rec.get("slice_id")) for rec in receipts if str(rec.get("status")) != "ok"}
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

    probe_results, probes_ok = [], True
    if run_probes and probes and not rogue_writes and ok_ids:
        seen_cmd, env = set(), dict(os.environ)
        env.setdefault("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1")
        env.setdefault("DOTNET_GCHeapHardLimit", "20000000")
        env.setdefault("DOTNET_EnableDiagnostics", "0")
        for c in (x.strip() for x in probes if x.strip()):
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
        try:
            hook_data = json.loads(hook_file.read_text(encoding="utf-8")) if hook_file.is_file() else {}
        except Exception:
            hook_data = {}
        hook_data.update({"needs_verify": not probes_ok, "needs_build": not probes_ok, "last_build_failed": not probes_ok})
        edited = list(hook_data.get("code_files_edited") or [])
        edited.extend(rel for rel in kept_changes if rel not in edited)
        hook_data["code_files_edited"] = edited[:100]
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text(json.dumps(hook_data, ensure_ascii=False, indent=2), encoding="utf-8")

    cp_dir = ep_dir / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    cp_data = load_checkpoint(r, bp_id) or {"bp_id": bp_id, "waves": {}}
    if probes_ok and not rogue_writes:
        for wid in sorted(ok_ids):
            cp_data["waves"][wid] = {"status": "done", "epoch": epoch_id, "ts": int(time.time())}
        (cp_dir / f"{bp_id}.json").write_text(json.dumps(cp_data, ensure_ascii=False, indent=2), encoding="utf-8")

    with _locked_lease_store(r) as store:
        store["epochs"].pop(f"{bp_id}:{epoch_id}", None)
        for k in [k for k in store.get("leases", {}) if k.startswith(f"{bp_id}:{epoch_id}:")]:
            store["leases"].pop(k, None)

    return {
        "bp_id": bp_id, "epoch_id": epoch_id, "ok_waves": sorted(ok_ids), "failed_waves": sorted(failed_ids),
        "kept_changes": kept_changes, "rolled_back": rolled_back, "rogue_writes": rogue_writes,
        "probes_ok": probes_ok, "probe_results": probe_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic Multi-Wave Blueprint Scheduler & Lease Engine.")
    parser.add_argument("--workspace", default="", help="Workspace root override.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sch = sub.add_parser("schedule")
    p_sch.add_argument("blueprint_path")
    p_sch.add_argument("--mode", choices=["seq", "parallel"], default="parallel")
    p_sch.add_argument("--max-width", type=int, default=0)
    p_sch.add_argument("--emit-workflow", default="")
    p_sch.add_argument("--emit-prompts", action="store_true")
    p_sch.add_argument("--ignore-checkpoints", action="store_true")
    p_sch.add_argument("--json", action="store_true")
    p_eb = sub.add_parser("epoch-begin")
    for fl, req in (("--bp", True), ("--epoch", True), ("--meta-file", True)):
        p_eb.add_argument(fl, required=req)
    p_eb.add_argument("--ttl", type=int, default=3600)
    p_bar = sub.add_parser("epoch-barrier")
    for fl, req in (("--bp", True), ("--epoch", True), ("--receipts-file", True)):
        p_bar.add_argument(fl, required=req)
    p_bar.add_argument("--meta-file", default="")
    p_bar.add_argument("--skip-probes", action="store_true")
    p_bar.add_argument("--json", action="store_true")
    ls_sub = sub.add_parser("lease").add_subparsers(dest="lease_cmd", required=True)
    ls_sub.add_parser("status")
    p_lb = ls_sub.add_parser("bind")
    p_lb.add_argument("--conv-id", required=True)
    p_lb.add_argument("--tag", required=True)
    p_lr = ls_sub.add_parser("release")
    p_lr.add_argument("--bp", default="")
    p_lr.add_argument("--epoch", default="")
    cp_sub = sub.add_parser("checkpoint").add_subparsers(dest="cp_cmd", required=True)
    cp_sub.add_parser("show").add_argument("bp_id")
    cp_sub.add_parser("clear").add_argument("bp_id")

    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    prof_path = ws / ".agents" / "planner_profile.json"
    try:
        profile = json.loads(prof_path.read_text(encoding="utf-8", errors="ignore")) if prof_path.is_file() else {}
    except Exception:
        profile = {}

    if args.cmd == "schedule":
        bp_p = Path(args.blueprint_path).expanduser()
        bp_p = bp_p if bp_p.is_absolute() else (ws / bp_p).resolve()
        try:
            res = schedule_blueprint(bp_p, ws, mode=args.mode, max_width=(args.max_width or None), profile=profile, ignore_checkpoints=bool(args.ignore_checkpoints))
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.emit_workflow:
            art_env = os.environ.get("PLANNER_ARTIFACT_DIR", "").strip()
            art_dir = Path(art_env) if art_env else Path(args.emit_workflow).expanduser().resolve().parent.parent
            res["workflow_path"] = str(emit_workflow_script(res, bp_p, ws, Path(args.emit_workflow), artifact_dir=art_dir))
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

    if args.cmd == "epoch-begin":
        meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8"))
        print(json.dumps(epoch_begin(ws, args.bp, args.epoch, meta.get("waves") or [], nonces=meta.get("nonces"), ttl_secs=args.ttl), ensure_ascii=False))
        return 0

    if args.cmd == "epoch-barrier":
        rec_raw = json.loads(Path(args.receipts_file).read_text(encoding="utf-8"))
        receipts = rec_raw if isinstance(rec_raw, list) else [rec_raw]
        meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8")) if args.meta_file and Path(args.meta_file).is_file() else {}
        art_env = os.environ.get("PLANNER_ARTIFACT_DIR", "").strip()
        res = epoch_barrier(ws, args.bp, args.epoch, receipts, probes=meta.get("probes") or [], run_probes=(not args.skip_probes), artifact_dir=(Path(art_env) if art_env else None))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"BARRIER|{res['epoch_id']}|ok={','.join(res['ok_waves']) or 'none'}|fail={','.join(res['failed_waves']) or 'none'}|rollback={len(res['rolled_back'])}|rogue={len(res['rogue_writes'])}|probes_ok={res['probes_ok']}")
        return 0 if (res["probes_ok"] and not res["rogue_writes"] and not res["failed_waves"]) else 1

    if args.cmd == "lease":
        if args.lease_cmd == "status":
            print(json.dumps(_load_active_leases(ws), ensure_ascii=False, indent=2))
            return 0
        if args.lease_cmd == "bind":
            parsed_tag = extract_lease_tag(args.tag)
            if not parsed_tag:
                print("ERROR: invalid [LEASE:...] tag", file=sys.stderr)
                return 1
            ok = bind_lease_conversation(ws, args.conv_id, parsed_tag["bp_id"], parsed_tag["epoch_id"], parsed_tag["wave_id"], parsed_tag["nonce"], parsed_tag["sub_globs"])
            print(json.dumps({"bound": ok}))
            return 0 if ok else 1
        with _locked_lease_store(ws) as store:
            for k in [k for k in store.get("leases", {}) if (not args.bp or k.startswith(f"{args.bp}:")) and (not args.epoch or f":{args.epoch}:" in k)]:
                store["leases"].pop(k, None)
        print("RELEASED")
        return 0

    if args.cmd == "checkpoint":
        if args.cp_cmd == "show":
            print(json.dumps(load_checkpoint(ws, args.bp_id), ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"cleared": clear_checkpoint(ws, args.bp_id)}))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
