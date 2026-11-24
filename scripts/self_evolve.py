#!/usr/bin/env python3
"""Autonomous Dual-Scope Self-Evolution Engine (Dynamic Fractal Planner v2.6.0).

Helps any AI agent automatically classify detected workflow, tooling, diagnostic,
guardrail, or UX/engineering improvements into:
  - `plugin-universal`: reusable across any repo -> implement in plugin + plugin sync/changelog.
  - `project-only`: specific to host app domain -> implement in host project + host sync ledger.
  - `dual-project+plugin`: universal engine in plugin + thin adapter/config/UI in host project ->
    implement BOTH in the same turn and update BOTH ledgers.

Zero third-party dependencies (Python stdlib only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from schema_migrations import compare_semver, parse_semver  # noqa: E402

SYNC_PRESERVE_NAMES = frozenset({
    ".git",
    ".last_good_backup",
    ".last_good_backup.tmp",
    ".metrics.jsonl",
    ".model_modes.json",
    ".bypass_gates",
    ".diagnostics",
    ".workspaces.json",
    ".hook_state.json",
    "__pycache__",
})

STANDALONE_HOST_EXCLUDES: tuple[str, ...] = (
    ".gitignore",
    "package.json",
    "AGENTS.md",
    ".agents/preview",
    ".agents/federation.json",
    ".agents/bridge_profile.json",
    ".agents/env_manifest.json",
    ".agents/host_matrix.json",
    ".agents/planner_profile.json",
)

UNIVERSAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("build/CI error learning & timelessness", re.compile(r"\b(?:error.?learn|build.?lessons|timeless|anti-?obsolescence|ci.?diag|diagnostic.?fetch|log.?digest)\b", re.I)),
    ("planner/hooks/triage/guardrails/verification", re.compile(r"\b(?:planner|hook|triage|verify_universal|plan_tool|guardrail|wiring.?gate|parity.?rule|diff.?rule|circuit.?breaker|eco.?mode|recon|sota.?blueprint|blueprint.?engine|blueprint)\b", re.I)),
    ("multi-AI memory/sync/handoff/self-evolution", re.compile(r"\b(?:external_ai_sync|handoff|project_history|saved_ideas|extra_requests|self.?evolv|dual.?scope|sync.?ledger|auto.?migrat)\b", re.I)),
    ("generic relay/bridge protocol client", re.compile(r"\b(?:relay.?client|bridge.?client|cloudflare.?worker|hmac.?wake|encrypt-?then-?mac|live.?bridge.?protocol)\b", re.I)),
    ("multi-host adaptive framework & baton", re.compile(r"\b(?:host.?context|host.?matrix|multi-?host|cross-?host|baton.?switch|host-?boot|host-?audit|host-?classify)\b", re.I)),
    ("reusable workflow efficiency tooling", re.compile(r"\b(?:universaliz|reusable.?script|workflow.?efficienc|quota.?sav|token.?diet|plugin)\b", re.I)),
)

PROJECT_SPECIFIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("audio/DSP/codec domain logic", re.compile(r"\b(?:cavern|hrir|brir|atmos|truehd|truehdd|e-?ac-?3|iamf|ambisonic|lgsr|vocal.?cortex|upmix|quickeq|autoeq|iso.?226|iso.?9613|dsp|ffmpeg|onnx)\b", re.I)),
    ("Android/UI/native app implementation", re.compile(r"\b(?:mainactivity|androidmanifest|androidstrings|oneui|biometricprompt|foregroundservice|keystore|appsettingsstore|\.cs\b|\.csproj\b|ndk|jni|apk)\b", re.I)),
    ("app product UI/business domain", re.compile(r"\b(?:screen|tab|button|modal|player|playlist|checkout|billing|database.?schema|migration|product.?copy)\b", re.I)),
)

PLUGIN_SYNC_HEADING = "## Plugin (`dynamic-fractal-planner`) Sync Entries (Newest First)"
HOST_SYNC_HEADING_RE = re.compile(r"^##\s+(?:Cavern Project|Project|Sync)\s+Entries.*$", re.MULTILINE)


def classify_improvement(
    description: str,
    files: list[str] | None = None,
    plugin_rel_prefix: str = ".agents/plugins/dynamic-fractal-planner",
) -> dict[str, Any]:
    """Classify an improvement into `plugin-universal`, `project-only`, or `dual-project+plugin`."""
    text = (description or "").strip()
    file_list = [f.replace("\\", "/").lstrip("./") for f in (files or []) if f]
    combined = text + "\n" + "\n".join(file_list)

    uni_hits: list[str] = []
    for label, pat in UNIVERSAL_PATTERNS:
        if pat.search(combined):
            uni_hits.append(label)

    proj_hits: list[str] = []
    for label, pat in PROJECT_SPECIFIC_PATTERNS:
        if pat.search(combined):
            proj_hits.append(label)

    touches_plugin = any(f.startswith(plugin_rel_prefix) for f in file_list)
    touches_project = any(not f.startswith(plugin_rel_prefix) for f in file_list)

    if touches_plugin:
        uni_hits.append("touches plugin directory")
    if touches_project:
        proj_hits.append("touches host project files")

    if uni_hits and proj_hits:
        scope = "dual-project+plugin"
        confidence = "high" if (len(uni_hits) + len(proj_hits)) >= 3 else "medium"
        actions = [
            "Implement reusable core in plugin (`scripts/` / `hooks/` / `rules/` / `tests/`)",
            "Add thin project wrapper/config/UI integration in host repo",
            "Log entry in BOTH host `EXTERNAL_AI_SYNC.md` (Project + Plugin sections) && plugin `.agents/docs/EXTERNAL_AI_SYNC.md`",
        ]
        ledgers = [
            ".agents/docs/EXTERNAL_AI_SYNC.md (Project + Plugin sections)",
            f"{plugin_rel_prefix}/.agents/docs/EXTERNAL_AI_SYNC.md",
            f"{plugin_rel_prefix}/CHANGELOG.md",
            ".agents/docs/PROJECT_HISTORY.md",
        ]
    elif uni_hits:
        scope = "plugin-universal"
        confidence = "high" if len(uni_hits) >= 2 else "medium"
        actions = [
            "Implement directly in plugin with zero host-project hardcoding",
            "Add regression test in plugin `tests/run_plugin_tests.py`",
            "Log entry in plugin `.agents/docs/EXTERNAL_AI_SYNC.md` && host `EXTERNAL_AI_SYNC.md` Plugin section",
        ]
        ledgers = [
            f"{plugin_rel_prefix}/.agents/docs/EXTERNAL_AI_SYNC.md",
            f"{plugin_rel_prefix}/CHANGELOG.md",
            ".agents/docs/EXTERNAL_AI_SYNC.md (Plugin section)",
            ".agents/docs/PROJECT_HISTORY.md",
        ]
    else:
        scope = "project-only"
        confidence = "high" if proj_hits else "low"
        actions = [
            "Implement inside host project without polluting universal plugin",
            "Log entry in host `.agents/docs/EXTERNAL_AI_SYNC.md` (Project section) && `PROJECT_HISTORY.md`",
        ]
        ledgers = [
            ".agents/docs/EXTERNAL_AI_SYNC.md (Project section)",
            ".agents/docs/PROJECT_HISTORY.md",
        ]

    return {
        "scope": scope,
        "confidence": confidence,
        "universal_signals": uni_hits,
        "project_signals": proj_hits,
        "recommended_actions": actions,
        "required_ledgers": ledgers,
    }


def _prepend_under_heading(md_path: Path, heading_re: re.Pattern[str], entry_block: str, fallback_heading: str) -> bool:
    if not md_path.is_file():
        md_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            f"# External AI Sync Ledger (`{md_path.name}`)\n\n"
            f"> STYLE|lossless symbolic telegraphic EN ONLY.\n\n"
            f"{fallback_heading}\n\n"
            f"{entry_block}\n"
        )
        md_path.write_text(content, encoding="utf-8")
        return True

    text = md_path.read_text(encoding="utf-8", errors="ignore")
    m = heading_re.search(text)
    if m:
        insert_at = m.end()
        updated = text[:insert_at] + "\n\n" + entry_block.strip() + "\n" + text[insert_at:].lstrip("\n")
    else:
        updated = text.rstrip() + f"\n\n{fallback_heading}\n\n{entry_block.strip()}\n"
    md_path.write_text(updated, encoding="utf-8")
    return True


def record_sync_entry(
    repo_root: Path,
    *,
    scope: str,
    title: str,
    summary: str,
    files: list[str],
    commit: str = "pending-push",
    agent: str = "AI",
    plugin_rel: str = ".agents/plugins/dynamic-fractal-planner",
) -> list[str]:
    """Prepend a telegraphic EN sync entry into the appropriate host and/or plugin sync ledgers."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    files_str = ", ".join(f"`{f}`" for f in files) if files else "`-`"
    block = "\n".join(
        [
            f"### {date_str} | `{commit}` | `{agent}` | {title}",
            f"- `Scope`: `{scope}`",
            f"- `Summary`: {summary}",
            f"- `Files`: {files_str}",
        ]
    )

    touched: list[str] = []
    host_sync = repo_root / ".agents" / "docs" / "EXTERNAL_AI_SYNC.md"
    host_hist = repo_root / ".agents" / "docs" / "PROJECT_HISTORY.md"
    plugin_sync = repo_root / plugin_rel / ".agents" / "docs" / "EXTERNAL_AI_SYNC.md"

    if scope in ("project-only", "project", "dual-project+plugin", "dual"):
        _prepend_under_heading(
            host_sync,
            HOST_SYNC_HEADING_RE,
            block,
            "## Project Sync Entries (Newest First)",
        )
        touched.append(str(host_sync.relative_to(repo_root)))
        if host_hist.is_file() and not _is_standalone_plugin_root(repo_root):
            hist_line = f"- {date_str}|`{title}`|{summary} ({files_str})"
            if _prepend_under_version_history(host_hist, hist_line):
                touched.append(str(host_hist.relative_to(repo_root)))

    if scope in ("plugin-universal", "plugin", "dual-project+plugin", "dual"):
        _prepend_under_heading(
            host_sync,
            re.compile(r"^##\s+Plugin\b.*Sync Entries.*$", re.MULTILINE),
            block,
            PLUGIN_SYNC_HEADING,
        )
        if str(host_sync.relative_to(repo_root)) not in touched:
            touched.append(str(host_sync.relative_to(repo_root)))

        _prepend_under_heading(
            plugin_sync,
            re.compile(r"^##\s+(?:Plugin\b.*)?Sync Entries.*$", re.MULTILINE),
            block,
            "## Sync Entries (Newest First)",
        )
        touched.append(str(plugin_sync.relative_to(repo_root)))

    return touched


def _read_manifest_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.json"
    if not manifest.is_file():
        return ""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="ignore"))
        return str(data.get("version") or "").strip()
    except Exception:
        return ""


_KIND_RANK: dict[str, int] = {"standalone": 3, "embedded": 2, "global": 1}
_RELEASE_LINE_RE = re.compile(r"(`Release`\s*:\s*`?v?)(\d+\.\d+\.\d+)(`?)")
_AGENTS_VER_RE = re.compile(r"(dynamic-fractal-planner\s+v)(\d+\.\d+\.\d+)")


def _is_standalone_plugin_root(root: Path) -> bool:
    """Return True when root is the standalone plugin repo (has plugin.json + package.json or .agents/preview)."""
    return (root / "plugin.json").is_file() and (
        (root / "package.json").is_file() or (root / ".agents" / "preview").exists()
    )


def _is_rel_excluded(rel_posix: str, *, include_standalone_excludes: bool = True) -> bool:
    """Return True if rel_posix belongs to runtime state, bytecode, or standalone host-only files."""
    rel = rel_posix.replace("\\", "/").strip("/")
    if not rel:
        return True
    if any(part in SYNC_PRESERVE_NAMES for part in rel.split("/")):
        return True
    if rel.endswith((".pyc", ".pyo")):
        return True
    if include_standalone_excludes:
        for excl in STANDALONE_HOST_EXCLUDES:
            if rel == excl or rel.startswith(excl + "/"):
                return True
    return False


def _iter_canonical_plugin_files(root: Path) -> list[Path]:
    """Return sorted canonical plugin file Paths inside root (excluding runtime state and standalone host files)."""
    if not root.is_dir():
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        cur = Path(dirpath)
        rel_dir = "" if cur == root else cur.relative_to(root).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames
            if not _is_rel_excluded(f"{rel_dir}/{d}" if rel_dir else d, include_standalone_excludes=True)
        )
        for fn in sorted(filenames):
            f_rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if _is_rel_excluded(f_rel, include_standalone_excludes=True):
                continue
            p = cur / fn
            if p.is_file() and not p.is_symlink():
                files.append(p)
    files.sort(key=lambda p: p.relative_to(root).as_posix())
    return files


def _tree_fingerprint(root: Path) -> tuple[str, float]:
    """Compute deterministic (sha256_hex, max_mtime) over canonical plugin files in root."""
    files = _iter_canonical_plugin_files(root)
    if not files:
        return ("", 0.0)
    h = hashlib.sha256()
    max_mtime = 0.0
    for p in files:
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(data)).encode("ascii"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
        mt = p.stat().st_mtime
        if mt > max_mtime:
            max_mtime = mt
    return (h.hexdigest(), max_mtime)


def _sync_plugin_tree(src: Path, dst: Path, *, snapshot_backup: bool = True) -> list[str]:
    """Mirror ONLY canonical plugin files from src to dst, preserving runtime state and standalone host files."""
    dst.mkdir(parents=True, exist_ok=True)
    if snapshot_backup and (dst / "plugin.json").is_file():
        tmp_bak = dst / ".last_good_backup.tmp"
        bak = dst / ".last_good_backup"
        if tmp_bak.exists():
            shutil.rmtree(tmp_bak)
        ignore = shutil.ignore_patterns(*SYNC_PRESERVE_NAMES, "*.pyc", "*.pyo")
        shutil.copytree(dst, tmp_bak, ignore=ignore)
        if bak.exists():
            shutil.rmtree(bak)
        tmp_bak.rename(bak)

    src_files = _iter_canonical_plugin_files(src)
    src_map = {p.relative_to(src).as_posix(): p for p in src_files}

    for dp in _iter_canonical_plugin_files(dst):
        rel = dp.relative_to(dst).as_posix()
        if rel not in src_map:
            dp.unlink()

    for dirpath, _dirnames, _filenames in os.walk(dst, topdown=False, followlinks=False):
        cur = Path(dirpath)
        if cur == dst:
            continue
        if _is_rel_excluded(cur.relative_to(dst).as_posix(), include_standalone_excludes=True):
            continue
        try:
            if not any(cur.iterdir()):
                cur.rmdir()
        except OSError:
            pass

    copied: list[str] = []
    for rel, sp in src_map.items():
        tp = dst / rel
        tp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, tp)
        copied.append(rel)

    for rel_glob in ("install.sh", "hooks/*.py", "scripts/*.py", "tests/*.py"):
        for p in dst.glob(rel_glob):
            try:
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass
    for manifest_name in ("plugin.json", "hooks.json"):
        mp = dst / manifest_name
        if mp.is_file():
            json.loads(mp.read_text(encoding="utf-8"))
    return sorted(copied)


def _copy_plugin_tree(src: Path, dst: Path, *, snapshot_backup: bool = True) -> list[str]:
    """Copy plugin tree from src to dst preserving runtime state and optional .last_good_backup."""
    return _sync_plugin_tree(src, dst, snapshot_backup=snapshot_backup)


def resolve_global_plugin_dir(global_dir: str | Path | None = None) -> Path:
    if global_dir:
        return Path(global_dir).expanduser().resolve()
    gemini_home = os.environ.get("GEMINI_HOME", "").strip()
    base = Path(gemini_home).expanduser() if gemini_home else (Path.home() / ".gemini")
    return (base / "config" / "plugins" / "dynamic-fractal-planner").resolve()


def sync_plugin_versions(
    repo_root: Path,
    *,
    global_dir: str | Path | None = None,
    plugin_rel: str = ".agents/plugins/dynamic-fractal-planner",
    apply: bool = False,
) -> dict[str, Any]:
    """Compare embedded repo plugin vs global plugin install; higher semver ALWAYS wins (never downgrades)."""
    embedded_dir = (repo_root / plugin_rel).resolve()
    glob_dir = resolve_global_plugin_dir(global_dir)

    emb_ver = _read_manifest_version(embedded_dir)
    glob_ver = _read_manifest_version(glob_dir)

    same_dir = embedded_dir == glob_dir
    result: dict[str, Any] = {
        "embedded_dir": str(embedded_dir),
        "global_dir": str(glob_dir),
        "embedded_version": emb_ver or None,
        "global_version": glob_ver or None,
        "direction": "in-sync",
        "applied": False,
        "copied": [],
        "message": "",
    }

    if same_dir:
        if apply and emb_ver and (repo_root / ".agents" / "docs" / "PROJECT_HISTORY.md").is_file() and not _is_standalone_plugin_root(repo_root):
            _sync_host_plugin_history(repo_root, emb_ver, plugin_dir=embedded_dir, apply=True)
        result["message"] = f"Embedded and global plugin point to the same directory (v{emb_ver or '?'})."
        return result

    if not emb_ver and not glob_ver:
        result["direction"] = "missing"
        result["message"] = "Neither embedded nor global plugin.json found."
        return result

    if emb_ver and not glob_ver:
        result["direction"] = "embedded->global"
        src, dst = embedded_dir, glob_dir
        result["message"] = f"Embedded v{emb_ver} ready to install to global dir."
    elif glob_ver and not emb_ver:
        result["direction"] = "global-only"
        result["message"] = f"Global plugin v{glob_ver} active; repo has no embedded copy."
        return result
    else:
        cmp = compare_semver(emb_ver, glob_ver)
        if cmp == 0:
            emb_fp, emb_mt = _tree_fingerprint(embedded_dir)
            glob_fp, glob_mt = _tree_fingerprint(glob_dir)
            if emb_fp == glob_fp:
                if apply and emb_ver and (repo_root / ".agents" / "docs" / "PROJECT_HISTORY.md").is_file() and not _is_standalone_plugin_root(repo_root):
                    _sync_host_plugin_history(repo_root, emb_ver, plugin_dir=embedded_dir, apply=True)
                result["direction"] = "in-sync"
                result["message"] = f"Embedded and global plugin are in sync at v{emb_ver}."
                return result
            if emb_mt >= glob_mt:
                result["direction"] = "embedded->global"
                src, dst = embedded_dir, glob_dir
                result["message"] = (
                    f"Embedded and global share v{emb_ver} with content drift; "
                    "newer mtime propagates embedded->global."
                )
            else:
                result["direction"] = "global->embedded"
                src, dst = glob_dir, embedded_dir
                result["message"] = (
                    f"Global and embedded share v{glob_ver} with content drift; "
                    "newer mtime propagates global->embedded."
                )
        elif cmp > 0:
            result["direction"] = "embedded->global"
            src, dst = embedded_dir, glob_dir
            result["message"] = f"Embedded v{emb_ver} > Global v{glob_ver}; higher version propagates embedded->global."
        else:
            result["direction"] = "global->embedded"
            src, dst = glob_dir, embedded_dir
            result["message"] = f"Global v{glob_ver} > Embedded v{emb_ver}; higher version propagates global->embedded."

    if apply:
        copied = _sync_plugin_tree(src, dst, snapshot_backup=(dst == glob_dir))
        result["applied"] = True
        result["copied"] = copied
        if result["direction"] == "embedded->global":
            result["global_version"] = emb_ver
            synced_ver = emb_ver
        elif result["direction"] == "global->embedded":
            result["embedded_version"] = glob_ver
            synced_ver = glob_ver
        else:
            synced_ver = emb_ver or glob_ver
        if synced_ver and (repo_root / ".agents" / "docs" / "PROJECT_HISTORY.md").is_file() and not _is_standalone_plugin_root(repo_root):
            _sync_host_plugin_history(repo_root, synced_ver, plugin_dir=src, apply=True)
        result["message"] = f"Synced {result['direction']} cleanly ({len(copied)} canonical files)."
    return result


def resolve_workspaces_registry_path(global_dir: str | Path | None = None) -> Path:
    """Return path to the global `.workspaces.json` registry (respecting `PLANNER_WORKSPACES_FILE`)."""
    override = os.environ.get("PLANNER_WORKSPACES_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (resolve_global_plugin_dir(global_dir) / ".workspaces.json").resolve()


def _is_temp_workspace_path(p: Path) -> bool:
    if os.environ.get("PLANNER_WORKSPACES_FILE", "").strip():
        return False
    for seg in p.parts:
        low = seg.lower()
        if low in ("tmp", "temp") or low.startswith(("tmp", "temp", "pytest")):
            return True
    return False


def _load_registered_workspaces(reg_path: Path) -> list[str]:
    if not reg_path.is_file():
        return []
    try:
        raw = json.loads(reg_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    items = raw.get("workspaces", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    out: list[str] = []
    seen: set[str] = set()
    dirty = False
    for item in items:
        p_str = item.get("path", "") if isinstance(item, dict) else str(item or "")
        p_str = p_str.strip()
        if not p_str:
            dirty = True
            continue
        rp = Path(p_str).expanduser().resolve()
        if not rp.is_dir() or _is_temp_workspace_path(rp):
            dirty = True
            continue
        resolved = str(rp)
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
        else:
            dirty = True
    if dirty:
        try:
            reg_path.write_text(json.dumps({"schema": 1, "workspaces": out}, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return out


def register_workspace(repo_root: Path, *, global_dir: str | Path | None = None) -> bool:
    """Register `repo_root` in `.workspaces.json`; no-op during plugin tests unless `PLANNER_WORKSPACES_FILE` is set."""
    if os.environ.get("PLANNER_RUNNING_PLUGIN_TESTS") == "1" and not os.environ.get("PLANNER_WORKSPACES_FILE", "").strip():
        return False
    repo_resolved = Path(repo_root).expanduser().resolve()
    if not repo_resolved.is_dir() or _is_temp_workspace_path(repo_resolved):
        return False
    reg_path = resolve_workspaces_registry_path(global_dir)
    entries = _load_registered_workspaces(reg_path)
    repo_str = str(repo_resolved)
    if repo_str in entries:
        return False
    entries.append(repo_str)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({"schema": 1, "workspaces": entries}, indent=2) + "\n", encoding="utf-8")
    return True


def _is_temp_or_test_dirname(name: str) -> bool:
    lower = name.lower()
    if lower.startswith(".") or "pytest" in lower or lower in ("tmp", "temp", "test", "tests"):
        return True
    return bool(re.match(r"^(?:tmp|temp|test)[_-]", lower))


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _discover_fleet_targets(
    repo_root: Path,
    *,
    global_dir: str | Path | None = None,
    plugin_rel: str = ".agents/plugins/dynamic-fractal-planner",
    extra_repos: list[str] | None = None,
) -> list[dict[str, Any]]:
    repo_resolved = Path(repo_root).expanduser().resolve()
    glob_dir = resolve_global_plugin_dir(global_dir)
    reg_path = resolve_workspaces_registry_path(global_dir)

    candidates: list[Path] = []
    seen_cands: set[Path] = set()

    def _add_cand(p: Path) -> None:
        rp = p.expanduser().resolve()
        if rp not in seen_cands:
            seen_cands.add(rp)
            candidates.append(rp)

    _add_cand(repo_resolved)
    for ws_str in _load_registered_workspaces(reg_path):
        _add_cand(Path(ws_str))

    fed = _read_json_dict(repo_resolved / ".agents" / "federation.json")
    repos_map = fed.get("repos")
    if isinstance(repos_map, dict):
        for entry in repos_map.values():
            if isinstance(entry, dict) and str(entry.get("termuxPath") or "").strip():
                _add_cand(Path(str(entry["termuxPath"]).strip()))

    profile = _read_json_dict(repo_resolved / ".agents" / "planner_profile.json")
    pe = profile.get("plugin_evolution")
    if isinstance(pe, dict) and str(pe.get("canonical_repo") or "").strip():
        raw_canon = Path(str(pe["canonical_repo"]).strip()).expanduser()
        _add_cand(raw_canon if raw_canon.is_absolute() else (repo_resolved / raw_canon))

    explicit_set: set[Path] = set()
    for er in (extra_repos or []):
        if str(er or "").strip():
            rp = Path(str(er).strip()).expanduser().resolve()
            explicit_set.add(rp)
            _add_cand(rp)

    if not os.environ.get("PLANNER_RUNNING_PLUGIN_TESTS") and repo_resolved.parent.is_dir():
        try:
            for sib in sorted(repo_resolved.parent.iterdir()):
                if sib.is_dir() and not sib.is_symlink() and not _is_temp_or_test_dirname(sib.name):
                    _add_cand(sib)
        except OSError:
            pass

    targets_by_plugin_dir: dict[Path, dict[str, Any]] = {}

    def _record_target(kind: str, plugin_dir: Path, repo_dir: Path | None) -> None:
        p_res = plugin_dir.resolve()
        existing = targets_by_plugin_dir.get(p_res)
        if existing and _KIND_RANK[existing["kind"]] >= _KIND_RANK[kind]:
            return
        fp, mt = _tree_fingerprint(p_res)
        targets_by_plugin_dir[p_res] = {
            "kind": kind,
            "plugin_dir": p_res,
            "repo_dir": repo_dir.resolve() if repo_dir else None,
            "version": _read_manifest_version(p_res),
            "fingerprint": fp,
            "max_mtime": mt,
        }

    for cand in candidates:
        if not cand.is_dir():
            continue
        if _is_standalone_plugin_root(cand):
            _record_target("standalone", cand, cand)
            continue
        emb = (cand / plugin_rel).resolve()
        if (emb / "plugin.json").is_file() or cand in explicit_set:
            _record_target("embedded", emb, cand)

    _record_target("global", glob_dir, None)
    return list(targets_by_plugin_dir.values())


def _refresh_plugin_handoff_release(handoff_path: Path, target_ver: str) -> bool:
    if not handoff_path.is_file() or not target_ver:
        return False
    text = handoff_path.read_text(encoding="utf-8", errors="ignore")

    def _repl(m: re.Match[str]) -> str:
        if compare_semver(target_ver, m.group(2)) > 0:
            return f"{m.group(1)}{target_ver}{m.group(3)}"
        return m.group(0)

    updated = _RELEASE_LINE_RE.sub(_repl, text)
    if updated != text:
        handoff_path.write_text(updated, encoding="utf-8")
        return True
    return False


def _prepend_under_version_history(hist_path: Path, entry_line: str) -> bool:
    text = hist_path.read_text(encoding="utf-8", errors="ignore")
    heading_re = re.compile(
        r"^##\s+3\.\s+Version History[^\n]*\n(?:[ \t]*<!--[^\n]*-->[ \t]*\n)*",
        re.MULTILINE,
    )
    m = heading_re.search(text)
    if m:
        prefix = text[:m.end()]
        rest = text[m.end():].lstrip("\n")
        updated = f"{prefix}\n{entry_line.strip()}\n\n{rest}" if rest else f"{prefix}\n{entry_line.strip()}\n"
    else:
        updated = text.rstrip() + f"\n\n## 3. Version History (Newest First)\n\n{entry_line.strip()}\n"
    if updated == text:
        return False
    hist_path.write_text(updated, encoding="utf-8")
    return True


def _sync_host_plugin_history(
    repo_dir: Path,
    target_ver: str,
    plugin_dir: Path | None = None,
    *,
    apply: bool = True,
) -> list[str]:
    hist_path = repo_dir / ".agents" / "docs" / "PROJECT_HISTORY.md"
    if not target_ver or not hist_path.is_file() or _is_standalone_plugin_root(repo_dir):
        return []
    hist_text = hist_path.read_text(encoding="utf-8", errors="ignore")
    if f"v{target_ver}" in hist_text:
        return []

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = f"Synced dynamic-fractal-planner v{target_ver} into host repository."
    plug_dir = plugin_dir or (repo_dir / ".agents" / "plugins" / "dynamic-fractal-planner")
    changelog = plug_dir / "CHANGELOG.md"
    if changelog.is_file():
        pat = re.compile(rf"^-\s+(\d{{4}}-\d{{2}}-\d{{2}})\|v{re.escape(target_ver)}\|(.+)$")
        for raw_line in changelog.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = pat.match(raw_line.strip())
            if m:
                date_str = m.group(1)
                summary = m.group(2).strip()
                break

    entry_line = f"- `Plugin v{target_ver} [x]` ({date_str}): {summary}"
    if apply:
        _prepend_under_version_history(hist_path, entry_line)
    return [".agents/docs/PROJECT_HISTORY.md"]


def _update_host_metadata(
    repo_dir: Path,
    target_ver: str,
    *,
    apply: bool,
    plugin_dir: Path | None = None,
) -> list[str]:
    touched: list[str] = []
    if not target_ver:
        return touched

    prof_path = repo_dir / ".agents" / "planner_profile.json"
    if prof_path.is_file():
        try:
            prof = json.loads(prof_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            prof = None
        if isinstance(prof, dict):
            cur_pv = str(prof.get("plugin_version") or "").strip()
            if not cur_pv or compare_semver(target_ver, cur_pv) > 0:
                touched.append(".agents/planner_profile.json")
                if apply:
                    prof["plugin_version"] = target_ver
                    prof_path.write_text(json.dumps(prof, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    agents_path = repo_dir / "AGENTS.md"
    if agents_path.is_file():
        text = agents_path.read_text(encoding="utf-8", errors="ignore")

        def _repl_agents(m: re.Match[str]) -> str:
            if compare_semver(target_ver, m.group(2)) > 0:
                return f"{m.group(1)}{target_ver}"
            return m.group(0)

        updated = _AGENTS_VER_RE.sub(_repl_agents, text)
        if updated != text:
            touched.append("AGENTS.md")
            if apply:
                agents_path.write_text(updated, encoding="utf-8")

    touched.extend(_sync_host_plugin_history(repo_dir, target_ver, plugin_dir=plugin_dir, apply=apply))
    return touched


def _run_git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _git_dirty_paths(repo_dir: Path) -> set[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain=v1", "-z", "-uall"],
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout:
        return set()
    tokens = proc.stdout.split(b"\0")
    paths: set[str] = set()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        status_xy = tok[:2].decode("ascii", errors="ignore")
        path1 = tok[3:].decode("utf-8", errors="replace").replace("\\", "/")
        if path1:
            paths.add(path1)
        if status_xy[0] in ("R", "C") and i < len(tokens):
            path2 = tokens[i].decode("utf-8", errors="replace").replace("\\", "/")
            i += 1
            if path2:
                paths.add(path2)
    return paths


def _push_git_repo(
    repo_dir: Path,
    *,
    kind: str,
    plugin_rel: str,
    target_ver: str,
    commit_msg: str,
    touched_host_files: set[str],
    pre_dirty: set[str],
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "repo_dir": str(repo_dir),
        "kind": kind,
        "branch": "",
        "status": "skipped",
        "staged": [],
        "commit": None,
        "message": "",
    }
    if not (repo_dir / ".git").exists():
        res["status"] = "not_git"
        res["message"] = "Not a git repository."
        return res

    br_proc = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    branch = br_proc.stdout.strip()
    if br_proc.returncode != 0 or not branch or branch == "HEAD":
        res["status"] = "detached_head"
        res["message"] = "Detached HEAD or branch detection failed."
        return res
    res["branch"] = branch

    rem_proc = _run_git(repo_dir, "remote", "get-url", "origin")
    if rem_proc.returncode != 0:
        res["status"] = "no_origin"
        res["message"] = "No origin remote configured."
        return res

    fetch_proc = _run_git(repo_dir, "fetch", "origin", "--quiet")
    fetch_ok = fetch_proc.returncode == 0

    has_remote_branch = _run_git(repo_dir, "rev-parse", "--verify", "--quiet", f"origin/{branch}").returncode == 0
    if has_remote_branch:
        ahead_proc = _run_git(repo_dir, "rev-list", "--count", f"HEAD..origin/{branch}")
        if ahead_proc.returncode == 0 and int((ahead_proc.stdout or "0").strip() or "0") > 0:
            res["status"] = "remote_ahead"
            res["message"] = f"origin/{branch} has unpulled commits; skipped."
            return res

    dirty_now = _git_dirty_paths(repo_dir)
    if kind == "standalone":
        to_stage = sorted(
            p for p in dirty_now
            if (not _is_rel_excluded(p, include_standalone_excludes=True)) or p.startswith(".agents/docs/")
        )
    else:
        plugin_rel_norm = plugin_rel.replace("\\", "/").strip("/")
        prefix = plugin_rel_norm + "/"
        stage_set: set[str] = set()
        for p in dirty_now:
            if p == plugin_rel_norm:
                stage_set.add(p)
            elif p.startswith(prefix):
                if not _is_rel_excluded(p[len(prefix):], include_standalone_excludes=False):
                    stage_set.add(p)
        if ".agents/planner_profile.json" in dirty_now:
            stage_set.add(".agents/planner_profile.json")
        for opt_path in ("AGENTS.md", ".agents/docs/EXTERNAL_AI_SYNC.md", ".agents/docs/PROJECT_HISTORY.md"):
            if opt_path in touched_host_files and opt_path not in pre_dirty and opt_path in dirty_now:
                stage_set.add(opt_path)
        to_stage = sorted(stage_set)

    for idx in range(0, len(to_stage), 100):
        add_proc = _run_git(repo_dir, "add", "-A", "--", *to_stage[idx : idx + 100])
        if add_proc.returncode != 0:
            res["status"] = "error"
            res["message"] = f"git add failed: {(add_proc.stderr or add_proc.stdout).strip()}"
            return res
    res["staged"] = to_stage

    committed = False
    if _run_git(repo_dir, "diff", "--cached", "--quiet").returncode != 0:
        msg = commit_msg.strip() or f"chore(plugin): sync dynamic-fractal-planner v{target_ver} [skip ci]"
        cm_proc = _run_git(repo_dir, "commit", "-m", msg)
        if cm_proc.returncode != 0:
            res["status"] = "error"
            res["message"] = f"git commit failed: {(cm_proc.stderr or cm_proc.stdout).strip()}"
            return res
        committed = True

    sha_proc = _run_git(repo_dir, "rev-parse", "--short", "HEAD")
    cur_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None

    needs_push = committed or not has_remote_branch
    if not needs_push and has_remote_branch:
        local_ahead = _run_git(repo_dir, "rev-list", "--count", f"origin/{branch}..HEAD")
        needs_push = local_ahead.returncode == 0 and int((local_ahead.stdout or "0").strip() or "0") > 0

    if not needs_push:
        res["status"] = "up-to-date"
        res["message"] = "No plugin-scoped changes to push."
        return res

    push_proc = _run_git(repo_dir, "push", "origin", branch)
    if push_proc.returncode != 0:
        err_txt = (push_proc.stderr or push_proc.stdout or fetch_proc.stderr or "").strip()
        if committed or not fetch_ok:
            res["status"] = "committed_offline"
            res["commit"] = cur_sha
            res["message"] = f"Committed locally ({cur_sha or 'HEAD'}); push deferred: {err_txt}"
            return res
        res["status"] = "error"
        res["message"] = f"git push failed: {err_txt}"
        return res

    res["status"] = "pushed"
    res["commit"] = cur_sha
    res["message"] = f"Pushed to origin/{branch} ({res['commit'] or 'HEAD'})."
    return res


def sync_plugin_fleet(
    repo_root: Path,
    *,
    apply: bool = False,
    push: bool = False,
    run_tests: bool = True,
    commit_msg: str = "",
    global_dir: str | Path | None = None,
    plugin_rel: str = ".agents/plugins/dynamic-fractal-planner",
    extra_repos: list[str] | None = None,
) -> dict[str, Any]:
    """Elect highest-semver/newest plugin copy across fleet and sync (optionally test, commit, & push)."""
    targets = _discover_fleet_targets(
        repo_root,
        global_dir=global_dir,
        plugin_rel=plugin_rel,
        extra_repos=extra_repos,
    )
    valid_sources = [t for t in targets if t["version"] and (t["plugin_dir"] / "plugin.json").is_file()]
    if not valid_sources:
        return {
            "authoritative": None,
            "apply": bool(apply),
            "push": bool(push),
            "tests_ran": False,
            "tests_passed": None,
            "targets": [],
            "pushed": [],
            "ok": False,
            "message": "No valid plugin installation found across fleet.",
        }

    auth = max(
        valid_sources,
        key=lambda t: (parse_semver(t["version"]), t["max_mtime"], _KIND_RANK[t["kind"]]),
    )
    auth_dir: Path = auth["plugin_dir"]
    auth_ver: str = auth["version"]

    pre_dirty_map: dict[Path, set[str]] = {}
    if push:
        for t in targets:
            rdir = t["repo_dir"]
            if rdir and rdir not in pre_dirty_map and (rdir / ".git").exists():
                pre_dirty_map[rdir] = _git_dirty_paths(rdir)

    auth_meta_updated: list[str] = []
    if apply and _refresh_plugin_handoff_release(auth_dir / ".agents" / "docs" / "HANDOFF.md", auth_ver):
        auth_meta_updated.append(".agents/docs/HANDOFF.md")
        auth["fingerprint"], auth["max_mtime"] = _tree_fingerprint(auth_dir)

    auth_fp: str = auth["fingerprint"]
    target_reports: list[dict[str, Any]] = []
    touched_by_repo: dict[Path, set[str]] = {}

    for t in targets:
        t_dir: Path = t["plugin_dir"]
        t_repo: Path | None = t["repo_dir"]
        copied: list[str] = []
        meta_updated: list[str] = []
        is_auth = t_dir == auth_dir

        if is_auth:
            meta_updated.extend(auth_meta_updated)
            if t["kind"] == "embedded" and t_repo:
                meta_updated.extend(_update_host_metadata(t_repo, auth_ver, apply=apply, plugin_dir=auth_dir))
                if apply and meta_updated:
                    touched_by_repo.setdefault(t_repo, set()).update(meta_updated)
            status = "authoritative"
            ver_after = auth_ver
        else:
            needs_tree = t["fingerprint"] != auth_fp or not (t_dir / "plugin.json").is_file()
            if needs_tree and apply:
                copied = _sync_plugin_tree(auth_dir, t_dir, snapshot_backup=(t["kind"] == "global"))
                _refresh_plugin_handoff_release(t_dir / ".agents" / "docs" / "HANDOFF.md", auth_ver)
                t["fingerprint"], t["max_mtime"] = _tree_fingerprint(t_dir)
                ver_after = _read_manifest_version(t_dir) or auth_ver
            else:
                ver_after = t["version"] or None

            if t["kind"] == "embedded" and t_repo:
                meta_updated.extend(_update_host_metadata(t_repo, auth_ver, apply=apply, plugin_dir=auth_dir))
                if apply and meta_updated:
                    touched_by_repo.setdefault(t_repo, set()).update(meta_updated)

            if not needs_tree and not meta_updated:
                status = "in-sync"
            elif apply:
                status = "synced" if needs_tree else "metadata-updated"
            else:
                status = "would-sync"

        target_reports.append({
            "kind": t["kind"],
            "plugin_dir": str(t_dir),
            "repo_dir": str(t_repo) if t_repo else None,
            "version_before": t["version"] or None,
            "version_after": ver_after,
            "status": status,
            "copied": copied,
            "metadata_updated": meta_updated,
        })

    tests_ran = False
    tests_passed: bool | None = None
    pushed_reports: list[dict[str, Any]] = []
    ok = True
    message = f"Fleet authoritative source: {auth['kind']} v{auth_ver} ({auth_dir})."

    if push:
        test_script = auth_dir / "tests" / "run_plugin_tests.py"
        if run_tests and test_script.is_file():
            tests_ran = True
            t_proc = subprocess.run(
                [sys.executable, "-B", str(test_script), "--fast"],
                cwd=str(auth_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            tests_passed = t_proc.returncode == 0
            if not tests_passed:
                ok = False
                message = f"Push aborted: plugin fast tests failed on {auth_dir}."
                return {
                    "authoritative": {
                        "kind": auth["kind"],
                        "plugin_dir": str(auth_dir),
                        "repo_dir": str(auth["repo_dir"]) if auth["repo_dir"] else None,
                        "version": auth_ver,
                        "fingerprint": auth_fp,
                        "max_mtime": auth["max_mtime"],
                    },
                    "apply": bool(apply),
                    "push": bool(push),
                    "tests_ran": tests_ran,
                    "tests_passed": tests_passed,
                    "targets": target_reports,
                    "pushed": pushed_reports,
                    "ok": ok,
                    "message": message,
                }

        seen_git_repos: set[Path] = set()
        for t in targets:
            rdir = t["repo_dir"]
            if not rdir or rdir in seen_git_repos:
                continue
            seen_git_repos.add(rdir)
            if not (rdir / ".git").exists():
                continue
            p_res = _push_git_repo(
                rdir,
                kind=t["kind"],
                plugin_rel=plugin_rel,
                target_ver=auth_ver,
                commit_msg=commit_msg,
                touched_host_files=touched_by_repo.get(rdir, set()),
                pre_dirty=pre_dirty_map.get(rdir, set()),
            )
            pushed_reports.append(p_res)
            if p_res["status"] == "error":
                ok = False

    return {
        "authoritative": {
            "kind": auth["kind"],
            "plugin_dir": str(auth_dir),
            "repo_dir": str(auth["repo_dir"]) if auth["repo_dir"] else None,
            "version": auth_ver,
            "fingerprint": auth_fp,
            "max_mtime": auth["max_mtime"],
        },
        "apply": bool(apply),
        "push": bool(push),
        "tests_ran": tests_ran,
        "tests_passed": tests_passed,
        "targets": target_reports,
        "pushed": pushed_reports,
        "ok": ok,
        "message": message,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous Dual-Scope Self-Evolution Classifier, Ledger & Higher-Version Sync.")
    parser.add_argument("--workspace", default="", help="Workspace root override")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cls = sub.add_parser("classify", help="Classify an improvement as project-only, plugin-universal, or dual-project+plugin.")
    p_cls.add_argument("--desc", required=True, help="Improvement description")
    p_cls.add_argument("--files", nargs="*", default=[], help="Optional candidate file paths")
    p_cls.add_argument("--json", action="store_true", help="Output JSON")

    p_sync = sub.add_parser("sync", help="Prepend a telegraphic sync entry to host and/or plugin EXTERNAL_AI_SYNC.md.")
    p_sync.add_argument("--scope", choices=["project", "plugin", "dual"], required=True)
    p_sync.add_argument("--title", required=True)
    p_sync.add_argument("--summary", required=True)
    p_sync.add_argument("--files", nargs="*", default=[])
    p_sync.add_argument("--commit", default="pending-push")
    p_sync.add_argument("--agent", default="AI")

    p_psync = sub.add_parser("sync-plugin", help="Compare embedded vs global plugin semver and propagate higher version (never downgrades).")
    p_psync.add_argument("--apply", action="store_true", help="Apply higher-version sync (default: check-only)")
    p_psync.add_argument("--global-dir", default="", help="Override global plugin directory")
    p_psync.add_argument("--plugin-rel", default=".agents/plugins/dynamic-fractal-planner", help="Repo-relative embedded plugin dir")
    p_psync.add_argument("--json", action="store_true", help="Output JSON")

    p_all = sub.add_parser("sync-all", help="Sync plugin across standalone, embedded, and global fleet (with optional test/commit/push).")
    p_all.add_argument("--apply", action="store_true", help="Apply fleet sync (default: dry-run check)")
    p_all.add_argument("--push", action="store_true", help="Run fast plugin tests, commit plugin-scoped paths, and push clean git repos")
    p_all.add_argument("--no-test", action="store_true", help="Skip running tests/run_plugin_tests.py --fast before push")
    p_all.add_argument("--commit", default="", help="Custom git commit message")
    p_all.add_argument("--global-dir", default="", help="Override global plugin directory")
    p_all.add_argument("--plugin-rel", default=".agents/plugins/dynamic-fractal-planner", help="Repo-relative embedded plugin dir")
    p_all.add_argument("--repo", action="append", default=[], help="Additional repo path to include (repeatable)")
    p_all.add_argument("--json", action="store_true", help="Output JSON")

    p_reg = sub.add_parser("register-workspace", help="Register workspace root in .workspaces.json for fleet sync.")
    p_reg.add_argument("--global-dir", default="", help="Override global plugin directory")
    p_reg.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()

    if args.cmd == "classify":
        res = classify_improvement(args.desc, args.files)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"EVOLVE_SCOPE|{res['scope']}|conf={res['confidence']}")
            if res["universal_signals"]:
                print("  UNIVERSAL: " + " ; ".join(res["universal_signals"]))
            if res["project_signals"]:
                print("  PROJECT: " + " ; ".join(res["project_signals"]))
            print("  LEDGERS: " + " | ".join(res["required_ledgers"]))
        return 0

    if args.cmd == "sync":
        touched = record_sync_entry(
            ws,
            scope=args.scope,
            title=args.title,
            summary=args.summary,
            files=args.files,
            commit=args.commit,
            agent=args.agent,
        )
        print("EVOLVE_SYNC_OK|" + ",".join(touched))
        return 0

    if args.cmd == "sync-plugin":
        res = sync_plugin_versions(
            ws,
            global_dir=args.global_dir or None,
            plugin_rel=args.plugin_rel,
            apply=bool(args.apply),
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"PLUGIN_SYNC|dir={res['direction']}|applied={res['applied']}|"
                f"embedded={res['embedded_version'] or '-'}|global={res['global_version'] or '-'}|{res['message']}"
            )
        return 0

    if args.cmd == "sync-all":
        res = sync_plugin_fleet(
            ws,
            apply=bool(args.apply or args.push),
            push=bool(args.push),
            run_tests=not bool(args.no_test),
            commit_msg=args.commit,
            global_dir=args.global_dir or None,
            plugin_rel=args.plugin_rel,
            extra_repos=args.repo or None,
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            auth = res.get("authoritative") or {}
            print(
                f"FLEET_SYNC|ok={res['ok']}|auth={auth.get('kind', '-')}:v{auth.get('version', '-')}|"
                f"targets={len(res['targets'])}|pushed={len(res['pushed'])}|{res['message']}"
            )
            for t in res["targets"]:
                print(f"  TARGET|{t['kind']}|{t['status']}|{t['version_before'] or '-'}->{t['version_after'] or '-'}|{t['plugin_dir']}")
            for p in res["pushed"]:
                print(f"  PUSH|{p['kind']}|{p['status']}|{p['branch'] or '-'}|{p['commit'] or '-'}|{p['message']}")
        return 0 if res.get("ok", True) else 1

    if args.cmd == "register-workspace":
        reg_path = resolve_workspaces_registry_path(args.global_dir or None)
        ok = register_workspace(ws, global_dir=args.global_dir or None)
        if args.json:
            print(json.dumps({"registered": ok, "workspace": str(ws), "registry": str(reg_path)}, indent=2, ensure_ascii=False))
        else:
            print(f"WORKSPACE_REGISTER|registered={ok}|workspace={ws}|registry={reg_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
