#!/usr/bin/env python3
"""Universal Plans Hub, Pre-Resumption Hygiene, Compaction & Blueprint Matching (`blueprint_hub.py`).

Split from `blueprint_engine.py` (`W56.1`) with full backwards-compatible re-exports.
Zero third-party dependencies (Python stdlib only).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from schema_migrations import migrate_profile, resolve_blueprint_config  # noqa: E402

HUB_AUTO_START = "<!-- HUB:AUTO_START -->"
HUB_AUTO_END = "<!-- HUB:AUTO_END -->"

_HUB_SKIP_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "obj", "bin", "archive",
    "__pycache__", ".venv", "venv", ".agents", ".aistudio", ".playwright-cli",
})

_MATCH_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "when", "what", "where",
    "para", "com", "que", "uma", "uns", "umas", "dos", "das", "nos", "nas", "pelo", "pela",
    "add", "fix", "update", "create", "edit", "make", "new", "novo", "nova", "criar",
    "atualizar", "corrigir", "blueprint", "plan", "plano",
})


def _status_bucket(status: str) -> str:
    st = (status or "").strip().upper()
    if st.startswith("DONE") or st.startswith("COMPLETE"):
        return "done"
    if st.startswith("OBSERVING"):
        return "observing"
    if st.startswith("DRIFTED"):
        return "drifted"
    if st.startswith("SUPERSEDED"):
        return "superseded"
    if st.startswith("DEFERRED"):
        return "deferred"
    if st.startswith("PARTIAL"):
        return "partial"
    return "active"


def extract_blueprint_governance_meta(path: Path) -> dict[str, Any]:
    """Extract governance directives (`SUPERSEDED_BY|`, `CONFLICTS_WITH|`, `CONFLICT_RESOLVED|`, `HYGIENE_NOTE|`, stubs) from a blueprint."""
    p = Path(path)
    if not p.is_file():
        return {"superseded_by": [], "open_conflicts": [], "resolved_conflicts": [], "hygiene_notes": [], "stubs": []}
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    superseded_by = [ln.split("|", 1)[1].strip() for ln in lines if ln.startswith("SUPERSEDED_BY|") and "|" in ln]
    open_conflicts: list[str] = []
    resolved_conflicts: list[str] = []
    for ln in lines:
        if ln.startswith("CONFLICTS_WITH|"):
            body = ln.split("|", 1)[1].strip()
            if re.search(r"\bRESOLVED\b", body, re.IGNORECASE):
                resolved_conflicts.append(body)
            else:
                open_conflicts.append(body)
        elif ln.startswith("CONFLICT_RESOLVED|"):
            resolved_conflicts.append(ln.split("|", 1)[1].strip())
    hygiene_notes = [ln.split("|", 1)[1].strip() for ln in lines if ln.startswith("HYGIENE_NOTE|") and "|" in ln]
    stubs = [ln.split("|", 1)[1].strip() for ln in lines if ln.startswith("STUB|") and "|" in ln]
    return {
        "superseded_by": superseded_by,
        "open_conflicts": open_conflicts,
        "resolved_conflicts": resolved_conflicts,
        "hygiene_notes": hygiene_notes,
        "stubs": stubs,
    }


def _norm_rel(raw: str) -> str:
    s = str(raw or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _load_profile_if_none(repo: Path, profile: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(profile, dict):
        return migrate_profile(profile)[0] if profile else {}
    p = repo / ".agents" / "planner_profile.json"
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(loaded, dict):
                return migrate_profile(loaded)[0]
        except Exception:
            pass
    return {}


def scan_oversized_code_files(repo: Path, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return sorted list of workspace code files exceeding `code_weight_limits.warn_lines_all` (default 2000L)."""
    prof = _load_profile_if_none(repo, profile)
    cw = prof.get("code_weight_limits") if isinstance(prof.get("code_weight_limits"), dict) else {}
    limit = int(cw.get("warn_lines_all") or 2000)
    exts = {str(e).lower() for e in (prof.get("code_extensions") or [".cs", ".py", ".ts", ".tsx", ".js", ".cpp", ".c", ".h"])}
    prefixes = tuple(_norm_rel(p) for p in (prof.get("code_path_prefixes") or []) if _norm_rel(p))
    found: list[dict[str, Any]] = []
    root = repo.resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in _HUB_SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            if Path(fname).suffix.lower() not in exts:
                continue
            full = Path(dirpath) / fname
            try:
                rel = full.relative_to(root).as_posix()
            except Exception:
                continue
            if prefixes and not rel.startswith(prefixes):
                continue
            try:
                with full.open("rb") as fh:
                    line_count = sum(1 for _ in fh)
            except Exception:
                continue
            if line_count > limit:
                found.append({"rel_path": rel, "lines": line_count, "limit": limit})
    found.sort(key=lambda item: (-item["lines"], item["rel_path"]))
    return found


def read_archived_ledger_entries(repo: Path, profile: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Parse `BP_ARCHIVED|...` index entries from `.agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md`."""
    import blueprint_engine  # noqa: E402

    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    ledger_rel = _norm_rel(cfg.get("archive_ledger") or ".agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md")
    ledger_path = repo / ledger_rel
    if not ledger_path.is_file():
        return []
    entries: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw in ledger_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = raw.strip()
        if not ln.startswith("BP_ARCHIVED|"):
            continue
        fields: dict[str, str] = {}
        for tok in blueprint_engine._split_pipe_fields(ln)[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k.strip()] = v.strip()
        bp_id = fields.get("id", "").strip()
        if bp_id and bp_id not in seen_ids:
            seen_ids.add(bp_id)
            entries.append(fields)
    return entries


def render_hub_auto_block(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    precomputed_items: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Render deterministic auto-synced inventory block for `PLANS_HUB.md` (single-pass when `precomputed_items` supplied)."""
    import blueprint_engine  # noqa: E402

    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    items = precomputed_items if precomputed_items is not None else blueprint_engine.list_blueprints(repo, profile=prof, include_details=True)
    archived = read_archived_ledger_entries(repo, profile=prof)
    buckets = {"done": 0, "partial": 0, "active": 0, "deferred": 0, "superseded": 0}
    open_conflicts_total = 0
    min_score = 100 if items else 0
    rows: list[str] = []

    for it in items:
        b = _status_bucket(it["status"])
        buckets[b] = buckets.get(b, 0) + 1
        min_score = min(min_score, int(it["score"]))
        gov = it.get("gov") if isinstance(it.get("gov"), dict) else extract_blueprint_governance_meta(repo / it["rel_path"])
        open_conflicts_total += len(gov["open_conflicts"])
        note_parts: list[str] = []
        if gov["open_conflicts"]:
            note_parts.append(f"OPEN_CONFLICT({len(gov['open_conflicts'])})")
        if gov["superseded_by"]:
            note_parts.append("superseded_by=" + "; ".join(gov["superseded_by"][:1])[:80])
        if gov["hygiene_notes"]:
            note_parts.append(gov["hygiene_notes"][0][:110])
        elif gov["resolved_conflicts"]:
            note_parts.append("resolved=" + gov["resolved_conflicts"][0][:100])
        note_str = (" ; ".join(note_parts) or "-").replace("|", "/")
        stub_count = len(gov["stubs"])
        rows.append(
            f"| `{it['id']}` | `{it['status']}` | `{it['score']}/100` | `{it['tier']}` | `{stub_count}` | {note_str} |"
        )

    monoliths = scan_oversized_code_files(repo, profile=prof)
    stats = {
        "blueprints_total": len(items),
        "archived_done": len(archived),
        "buckets": buckets,
        "min_score": min_score,
        "open_conflicts": open_conflicts_total,
        "monoliths_gt2000": len(monoliths),
    }

    arch_suffix = f"|archived_done={len(archived)}" if archived else ""
    lines = [
        HUB_AUTO_START,
        (
            f"HUB_STATS|blueprints={len(items)} "
            f"(done={buckets['done']}, partial={buckets['partial']}, active={buckets['active']}, "
            f"deferred={buckets['deferred']}, superseded={buckets['superseded']})"
            f"{arch_suffix}|"
            f"min_score={min_score}/100|open_conflicts={open_conflicts_total}|monoliths_gt2000={len(monoliths)}"
        ),
        "",
        "| Blueprint ID | Status | Score | Tier | H2+ Stubs | Hygiene / Lineage Note |",
        "| :--- | :---: | :---: | :---: | :---: | :--- |",
        *(rows or ["| *(none)* | `-` | `-` | `-` | `0` | - |"]),
    ]
    if archived:
        ledger_rel = _norm_rel(cfg.get("archive_ledger") or ".agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md")
        tar_rel = _norm_rel(cfg.get("archive_tarball") or ".agents/docs/archive/blueprints_done.tar.gz")
        arch_ids = ", ".join(f"`{a['id']}`" for a in archived)
        lines.extend([
            "",
            f"ARCHIVED_DONE|count={len(archived)}|ledger=`{ledger_rel}`|tarball=`{tar_rel}`|ids={arch_ids}",
        ])
    if monoliths:
        lines.extend([
            "",
            "### Oversized Code Files (`>2,000L` Live Scan)",
            "| File | Lines | Cap |",
            "| :--- | ---: | ---: |",
        ])
        for m in monoliths:
            lines.append(f"| `{m['rel_path']}` | `{m['lines']}` | `{m['limit']}` |")
    if (repo / ".agents" / "docs" / "COHERENCE_HUB.md").is_file():
        lines.append("COHERENCE_HUB|.agents/docs/COHERENCE_HUB.md")
    lines.append(HUB_AUTO_END)
    return "\n".join(lines), stats


def _default_hub_template(auto_block: str) -> str:
    return (
        "# Central Plans & Blueprints Hub (`PLANS_HUB.md`)\n\n"
        "> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`).\n"
        "> POLICY|BLUEPRINT_FIRST_DEFAULT (`require_by_default=true`; skip ONLY on explicit user opt-out to save tokens or `T0`/read-only) + MANDATORY PRE-RESUMPTION HYGIENE (`plan_tool.py blueprint hygiene --strict` BEFORE resuming any paused plan).\n\n"
        "## 1. Active & Pending Work Queue\n"
        "- *(Curated priority queue — never overwritten by `hub --sync`.)*\n\n"
        "## 2. Resolved Conflicts & Superseded Directives\n"
        "- *(Curated anti-regression registry — never overwritten by `hub --sync`.)*\n\n"
        "## 3. Auto-Synced Live Inventory\n"
        f"{auto_block}\n"
    )


def sync_plans_hub(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    apply: bool = True,
    precomputed_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check or atomically update the `<!-- HUB:AUTO_START -->..<!-- HUB:AUTO_END -->` block in `PLANS_HUB.md`."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    hub_rel = _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
    hub_path = repo / hub_rel
    auto_block, stats = render_hub_auto_block(repo, profile=prof, precomputed_items=precomputed_items)

    if not hub_path.is_file():
        if apply:
            hub_path.parent.mkdir(parents=True, exist_ok=True)
            hub_path.write_text(_default_hub_template(auto_block), encoding="utf-8")
            return {
                "path": str(hub_path), "rel_path": hub_rel, "exists": True,
                "in_sync": True, "updated": True, "changed": True, "created": True, "stats": stats,
            }
        return {
            "path": str(hub_path), "rel_path": hub_rel, "exists": False,
            "in_sync": False, "updated": False, "changed": False, "created": False, "stats": stats,
        }

    existing = hub_path.read_text(encoding="utf-8", errors="ignore")
    s_idx = existing.find(HUB_AUTO_START)
    e_idx = existing.find(HUB_AUTO_END)
    if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
        current_block = existing[s_idx : e_idx + len(HUB_AUTO_END)]
        if current_block == auto_block:
            return {
                "path": str(hub_path), "rel_path": hub_rel, "exists": True,
                "in_sync": True, "updated": False, "changed": False, "created": False, "stats": stats,
            }
        new_text = existing[:s_idx] + auto_block + existing[e_idx + len(HUB_AUTO_END) :]
    else:
        new_text = existing.rstrip() + f"\n\n## Auto-Synced Live Inventory\n{auto_block}\n"

    if apply:
        hub_path.write_text(new_text, encoding="utf-8")
        return {
            "path": str(hub_path), "rel_path": hub_rel, "exists": True,
            "in_sync": True, "updated": True, "changed": True, "created": False, "stats": stats,
        }
    return {
        "path": str(hub_path), "rel_path": hub_rel, "exists": True,
        "in_sync": False, "updated": False, "changed": False, "created": False, "stats": stats,
    }


def hygiene_audit(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Run deterministic single-pass pre-resumption plan hygiene & conflict audit across blueprints, roadmaps, & `PLANS_HUB.md`."""
    import blueprint_engine  # noqa: E402

    t0 = time.monotonic()
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    hub_rel = _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
    explicit_hub_cfg = isinstance(prof.get("blueprint"), dict) and bool(prof["blueprint"].get("hub_file"))
    items = blueprint_engine.list_blueprints(repo, profile=prof, strict=True, include_details=True)
    issues: list[str] = []
    warnings: list[str] = []

    for it in items:
        lint_res = it["lint"]
        if not lint_res["passed"]:
            issues.append(f"LINT|{it['id']}: score={lint_res['score']}/100 ({'; '.join(lint_res['issues'][:2])})")
        gov = it["gov"]
        for oc in gov["open_conflicts"]:
            issues.append(f"CONFLICT|{it['id']}: Unresolved CONFLICTS_WITH|{oc} — resolve before executing.")
        if gov["superseded_by"]:
            st_upper = str(it["status"]).upper()
            if st_upper not in ("SUPERSEDED", "DONE"):
                whole_sup = [s for s in gov["superseded_by"] if not re.match(r"^(?:section|part|slice|item)\s*:", s, re.I)]
                if whole_sup:
                    issues.append(
                        f"STALE_STATUS|{it['id']}: declares SUPERSEDED_BY|{whole_sup[0][:60]} but `BP_META` status=`{it['status']}` (mark `SUPERSEDED` or `DONE`)."
                    )

    hub_check = sync_plans_hub(repo, profile=prof, apply=False, precomputed_items=items)
    if not hub_check["exists"]:
        msg = f"HUB_MISSING|Central Plans Hub missing at `{hub_rel}` — run `plan_tool.py blueprint hub --sync`."
        (issues if (strict or explicit_hub_cfg) else warnings).append(msg)
    elif not hub_check["in_sync"]:
        msg = f"HUB_DRIFT|`{hub_rel}` auto-inventory is out of sync with workspace blueprints/monoliths — run `plan_tool.py blueprint hub --sync`."
        (issues if strict else warnings).append(msg)

    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
    passed = (len(issues) == 0) and (not strict or len(warnings) == 0)
    st = hub_check["stats"]
    b = st["buckets"]
    counts = {
        "total": st["blueprints_total"],
        "archived_done": st.get("archived_done", 0),
        "done": b.get("done", 0),
        "partial": b.get("partial", 0),
        "active": b.get("active", 0),
        "deferred": b.get("deferred", 0),
        "superseded": b.get("superseded", 0),
    }
    return {
        "ok": passed, "passed": passed, "strict": strict, "elapsed_ms": elapsed_ms,
        "hub_rel": hub_rel, "hub_exists": hub_check["exists"], "hub_in_sync": hub_check["in_sync"],
        "counts": counts, "stats": st, "issues": issues, "warnings": warnings,
    }


def compact_done_blueprints(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    apply: bool = True,
    include_superseded: bool = False,
    keep_ids: list[str] | set[str] | None = None,
    sync_hub: bool = True,
) -> dict[str, Any]:
    """Losslessly archive `DONE` (and optionally `SUPERSEDED`) blueprints into cold `.tar.gz` + searchable ledger (`O(1)` file count)."""
    import blueprint_engine  # noqa: E402

    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    ledger_rel = _norm_rel(cfg.get("archive_ledger") or ".agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md")
    tar_rel = _norm_rel(cfg.get("archive_tarball") or ".agents/docs/archive/blueprints_done.tar.gz")
    ledger_path = repo / ledger_rel
    tar_path = repo / tar_rel
    keep_set = {k.strip().upper() for k in (keep_ids or ()) if k.strip()}

    items = blueprint_engine.list_blueprints(repo, profile=prof, strict=False, include_details=True)
    allowed_buckets = {"done", "superseded"} if include_superseded else {"done"}
    targets = [
        it for it in items
        if _status_bucket(it["status"]) in allowed_buckets
        and it["id"].strip().upper() not in keep_set
    ]

    if not targets:
        return {
            "applied": False, "compacted": [], "skipped": len(items),
            "ledger_rel": ledger_rel, "tarball_rel": tar_rel, "verified_sha256": True,
        }

    if not apply:
        return {
            "applied": False, "dry_run": True, "candidates": [t["id"] for t in targets],
            "count": len(targets), "ledger_rel": ledger_rel, "tarball_rel": tar_rel,
        }

    existing_members: dict[str, bytes] = {}
    if tar_path.is_file():
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    fh = tf.extractfile(member)
                    if fh is not None:
                        existing_members[member.name] = fh.read()

    new_records: list[dict[str, str]] = []
    expected_hashes: dict[str, str] = {}
    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for it in targets:
        src = repo / it["rel_path"]
        raw_bytes = src.read_bytes()
        sha = hashlib.sha256(raw_bytes).hexdigest()[:16]
        arc_name = src.name
        existing_members[arc_name] = raw_bytes
        expected_hashes[arc_name] = hashlib.sha256(raw_bytes).hexdigest()

        txt = raw_bytes.decode("utf-8", errors="ignore")
        nonempty = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        val = next((ln[6:].strip() for ln in nonempty if ln.startswith("VALUE|")), "")[:140].replace("|", "/")
        win = next((ln[7:].strip() for ln in nonempty if ln.startswith("WINNER|")), "")[:140].replace("|", "/")
        gov = it["gov"]
        note_bits: list[str] = []
        if gov["hygiene_notes"]:
            note_bits.append(gov["hygiene_notes"][0][:120])
        if gov["resolved_conflicts"]:
            note_bits.append("resolved=" + gov["resolved_conflicts"][0][:100])
        if gov["superseded_by"]:
            note_bits.append("superseded_by=" + gov["superseded_by"][0][:80])
        note = (" ; ".join(note_bits) or "-").replace("|", "/")
        import meta_governance as _mg  # noqa: E402
        anc_specs, fit_specs = _mg.extract_living_anchors_from_text(txt)
        anc_str = "; ".join(anc_specs).replace("|", "/")[:240] if anc_specs else ""
        fit_str = " && ".join(fit_specs).replace("|", "/")[:180] if fit_specs else ""
        rec_item = {
            "id": it["id"], "status": it["status"], "tier": it["tier"], "score": str(it["score"]),
            "archived_on": now_date, "sha256": sha, "file": arc_name, "value": val,
            "winner": win, "note": note, "src_path": str(src),
        }
        if anc_str:
            rec_item["living"] = "OBSERVING"
            rec_item["anchors"] = anc_str
        if fit_str:
            rec_item["fitness"] = fit_str
        new_records.append(rec_item)

    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_tar = tar_path.with_suffix(".tmp.tar.gz")
    with tarfile.open(tmp_tar, "w:gz") as tf:
        for name in sorted(existing_members):
            data = existing_members[name]
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(data))

    with tarfile.open(tmp_tar, "r:gz") as tf:
        for arc_name, want_sha in expected_hashes.items():
            fh = tf.extractfile(arc_name)
            if fh is None or hashlib.sha256(fh.read()).hexdigest() != want_sha:
                tmp_tar.unlink(missing_ok=True)
                raise RuntimeError(f"Compaction abort: SHA-256 readback mismatch for {arc_name}")
    tmp_tar.replace(tar_path)

    existing_ledger_entries = read_archived_ledger_entries(repo, profile=prof)
    by_id: dict[str, dict[str, str]] = {e["id"]: e for e in existing_ledger_entries if e.get("id")}
    for rec in new_records:
        by_id[rec["id"]] = rec

    ledger_lines = [
        "# Archived Completed Blueprints Ledger (`BLUEPRINTS_DONE_LEDGER.md`)",
        "",
        "> STYLE|symbolic-EN (`<=120L` bounded slice reads on demand; verbatim full `.md` preserved in `blueprints_done.tar.gz`).",
        "",
    ]
    for bp_id in sorted(by_id):
        e = by_id[bp_id]
        extra_bits = ""
        if e.get("living"):
            extra_bits += f"|living={e['living']}"
        if e.get("anchors"):
            extra_bits += f"|anchors={e['anchors']}"
        if e.get("fitness"):
            extra_bits += f"|fitness={e['fitness']}"
        ledger_lines.append(
            f"BP_ARCHIVED|id={bp_id}|status={e.get('status', 'DONE')}|tier={e.get('tier', 'T3')}|"
            f"score={e.get('score', '100')}|archived_on={e.get('archived_on', now_date)}|sha256={e.get('sha256', '-')}|"
            f"file={e.get('file', bp_id + '.md')}|note={e.get('note', '-')}|value={e.get('value', '-')}{extra_bits}"
        )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("\n".join(ledger_lines) + "\n", encoding="utf-8")

    for rec in new_records:
        Path(rec["src_path"]).unlink(missing_ok=True)

    hub_synced = False
    if sync_hub and (repo / _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")).is_file():
        sync_plans_hub(repo, profile=prof, apply=True)
        hub_synced = True

    return {
        "applied": True, "compacted": [r["id"] for r in new_records], "count": len(new_records),
        "total_archived": len(by_id), "ledger_rel": ledger_rel, "tarball_rel": tar_rel,
        "verified_sha256": True, "hub_synced": hub_synced,
    }


def _tokenize_match_text(text: str) -> set[str]:
    norm = (text or "").lower().replace("_", " ").replace("-", " ")
    return {t for t in re.findall(r"[a-z0-9]{3,}", norm) if t not in _MATCH_STOPWORDS}


def match_blueprints(
    prompt: str,
    repo: Path,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match prompt against full text of active/partial/draft non-archived blueprints (`overlap_ratio >= 0.40`)."""
    import blueprint_engine  # noqa: E402

    prof = _load_profile_if_none(repo, profile)
    cls = blueprint_engine.classify_blueprint_request(prompt, profile=prof)
    prompt_tokens = _tokenize_match_text(prompt)
    prompt_doms = set(cls.get("active_domains") or [])

    items = blueprint_engine.list_blueprints(repo, profile=prof, strict=False, include_details=False)
    matches: list[dict[str, Any]] = []
    for it in items:
        st_norm = str(it.get("status") or "").strip().upper()
        if st_norm in ("DONE", "ARCHIVED", "SUPERSEDED") or st_norm.startswith(("DONE", "COMPLETE", "ARCHIVED", "SUPERSEDED")):
            continue
        rel_norm = str(it.get("rel_path") or "").replace("\\", "/").lower()
        if "/archive/" in rel_norm:
            continue
        p = repo / it["rel_path"]
        txt = p.read_text(encoding="utf-8", errors="ignore").lower()
        id_tokens = _tokenize_match_text(it["id"])
        bp_tokens = id_tokens | _tokenize_match_text(txt)
        overlap = prompt_tokens & bp_tokens
        id_overlap = prompt_tokens & id_tokens
        dom_overlap = [d for d in prompt_doms if d in txt]
        overlap_ratio = (len(overlap) / len(prompt_tokens)) if prompt_tokens else 0.0
        min_hits_ok = len(overlap) >= 2 or (len(prompt_tokens) == 1 and len(id_overlap) >= 1)
        if prompt_tokens and overlap_ratio >= 0.40 and min_hits_ok:
            matches.append({
                "id": it["id"],
                "rel_path": it["rel_path"],
                "status": it["status"],
                "format": it["format"],
                "score": round(overlap_ratio, 3),
                "overlap_ratio": round(overlap_ratio, 3),
                "id_hits": len(id_overlap),
                "domain_hits": len(dom_overlap),
                "matched_tokens": sorted(overlap)[:8],
            })

    matches.sort(key=lambda m: (-m["overlap_ratio"], -m["id_hits"], -m["domain_hits"], m["id"]))
    action = "AMEND" if matches else "CREATE"
    return {
        "action": action,
        "recommended_format": cls["recommended_format"],
        "risk_tier": cls["risk_tier"],
        "door_type": cls["door_type"],
        "top_match": matches[0] if matches else None,
        "matches": matches[:5],
    }


def transition_blueprint(
    path: Path,
    *,
    status: str,
    note: str = "",
    superseded_by: str = "",
    sync_hub: bool = False,
    repo: Path | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically transition `BP_META` status + `HYGIENE_NOTE|`/`SUPERSEDED_BY|` and optionally sync `PLANS_HUB.md`."""
    import blueprint_engine  # noqa: E402

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Blueprint not found: {p}")
    new_status = status.strip().upper()
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    meta_idx = -1
    bp_id = p.stem
    for i, ln in enumerate(lines):
        if ln.strip().startswith("BP_META|"):
            meta_idx = i
            parts = blueprint_engine._split_pipe_fields(ln.strip())
            updated_parts: list[str] = [parts[0]]
            found_st = False
            for tok in parts[1:]:
                if tok.startswith("id="):
                    bp_id = tok[3:].strip()
                    updated_parts.append(tok)
                elif tok.startswith("status="):
                    updated_parts.append(f"status={new_status}")
                    found_st = True
                else:
                    updated_parts.append(tok)
            if not found_st:
                updated_parts.append(f"status={new_status}")
            lines[i] = "|".join(updated_parts)
            break
    if meta_idx == -1:
        raise ValueError(f"Missing `BP_META|` line in {p}")

    insert_at = meta_idx + 1
    if superseded_by.strip():
        sup_line = f"SUPERSEDED_BY|{superseded_by.strip()}"
        if not any(ln.strip().startswith("SUPERSEDED_BY|") for ln in lines):
            lines.insert(insert_at, sup_line)
            insert_at += 1
        else:
            lines = [sup_line if ln.strip().startswith("SUPERSEDED_BY|") else ln for ln in lines]

    if note.strip():
        note_line = f"HYGIENE_NOTE|{note.strip()}"
        if not any(ln.strip().startswith("HYGIENE_NOTE|") for ln in lines):
            lines.insert(insert_at, note_line)
        else:
            lines = [note_line if ln.strip().startswith("HYGIENE_NOTE|") else ln for ln in lines]

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hub_synced = False
    if sync_hub and repo is not None:
        sync_plans_hub(repo, profile=profile, apply=True)
        hub_synced = True
    return {"id": bp_id, "path": str(p), "status": new_status, "hub_synced": hub_synced}


def check_plans_hub_hygiene(
    repo: Path,
    profile: dict[str, Any] | None,
    fails: list[str],
    warns: list[str],
) -> None:
    """Fast static verifier hook: if `PLANS_HUB.md` exists, flag drift or unresolved blueprint conflicts."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    hub_rel = _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
    if not (repo / hub_rel).is_file():
        return
    res = hygiene_audit(repo, profile=prof, strict=False)
    strict_verify = bool(cfg.get("strict_verify", False))
    for iss in res["issues"]:
        (fails if strict_verify or iss.startswith("CONFLICT|") else warns).append(f"plan-hygiene: {iss}")
    for w in res["warnings"]:
        warns.append(f"plan-hygiene: {w}")
