#!/usr/bin/env python3
"""Universal Multi-Host Context Detector, Tool Matrix Auditor & Zero-Talk Baton Engine.

Automatically identifies the active execution host (`antigravity-termux`, `antigravity-local`,
`ai-studio-cloud`, `ci-runner`, `local-ide`), resolves per-host tool routing from
`.agents/host_matrix.json`, detects cross-host baton handoffs (`HANDOFF.md` `host=` + optional
live bridge handoff), and classifies newly added scripts/configs before workflow rot.

Pure Python 3 stdlib only (zero third-party dependencies, <15 ms typical).
"""

from __future__ import annotations

import argparse
import calendar
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

VALID_CATEGORIES = frozenset({"universal", "ai-studio-only", "dual-adapted", "phone-daemon", "ignore"})
DEFAULT_HOST_MATRIX_REL = ".agents/host_matrix.json"
DEFAULT_BRIDGE_HANDOFF_PATH = Path("/root/.gemini/antigravity/ai_studio_bridge/LATEST_HANDOFF.md")
DEFAULT_RUNTIME_ENV_BRIDGE = Path("/tmp/.cavern_runtime_env.json")
DEFAULT_LOCAL_DOTNET_BIN = Path("/root/.dotnet/dotnet")
DEFAULT_TERMUX_DIR = Path("/data/data/com.termux")
DEFAULT_SDCARD_DIR = Path("/sdcard")

_KV_PAIR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^|\s]+)")
_UTC_LINE_RE = re.compile(r"\*\*UTC\*\*:\s*`([^`]+)`")


def _norm_rel(path_str: str) -> str:
    """Normalize slash separators and strip leading './' or '/' without stripping leading dot from '.agents'."""
    p = str(path_str).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _glob_matches(rel_posix: str, pattern: str) -> bool:
    """Match relative POSIX path against glob pattern, handling leading/nested `**/` cleanly."""
    norm = _norm_rel(rel_posix)
    pat = _norm_rel(pattern)
    if not pat:
        return False
    if fnmatch.fnmatch(norm, pat):
        return True
    if pat.startswith("**/"):
        sub = pat[3:]
        if fnmatch.fnmatch(norm, sub) or fnmatch.fnmatch(Path(norm).name, sub):
            return True
    if "/**/" in pat:
        collapsed = pat.replace("/**/", "/")
        if fnmatch.fnmatch(norm, collapsed):
            return True
    return False


def _default_probe_paths(probe_paths: dict[str, Path] | None = None) -> dict[str, Path]:
    p_map = dict(probe_paths or {})
    p_map.setdefault("termux_dir", DEFAULT_TERMUX_DIR)
    p_map.setdefault("sdcard_dir", DEFAULT_SDCARD_DIR)
    p_map.setdefault("local_dotnet_bin", DEFAULT_LOCAL_DOTNET_BIN)
    p_map.setdefault("gh_hosts_file", Path.home() / ".config" / "gh" / "hosts.yml")
    p_map.setdefault("omni_local_config", Path.home() / ".omni_agent" / "config.json")
    p_map.setdefault("runtime_env_bridge", DEFAULT_RUNTIME_ENV_BRIDGE)
    return p_map


def detect_host(
    repo: Path,
    env: Mapping[str, str] | None = None,
    *,
    probe_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Detect current execution host & local native capabilities without spawning slow subprocesses."""
    e = env if env is not None else os.environ
    probes = _default_probe_paths(probe_paths)

    repo_path = Path(repo)
    try:
        repo_resolved = repo_path.resolve()
    except OSError:
        repo_resolved = repo_path

    has_git = (repo_path / ".git").exists()
    gh_file = probes["gh_hosts_file"]
    has_gh_auth = bool(
        (gh_file.is_file() and gh_file.stat().st_size > 0)
        or str(e.get("GH_TOKEN") or "").strip()
        or str(e.get("GITHUB_TOKEN") or "").strip()
    )
    dotnet_bin = probes["local_dotnet_bin"]
    has_local_dotnet = dotnet_bin.is_file() or bool(shutil.which("dotnet"))
    has_sdcard = probes["sdcard_dir"].is_dir()
    has_local_omni_config = probes["omni_local_config"].is_file()
    has_runtime_env_bridge = probes["runtime_env_bridge"].is_file()

    override = str(e.get("PLANNER_HOST_OVERRIDE") or "").strip()
    antigravity_present = any(
        str(e.get(k) or "").strip()
        for k in ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_TRAJECTORY_ID")
    )
    termux_proot = bool(
        str(e.get("PROOT_L2S_DIR") or "").strip()
        or probes["termux_dir"].exists()
        or (has_sdcard and dotnet_bin.is_file())
    )
    ci_present = (
        str(e.get("GITHUB_ACTIONS") or "").strip().lower() == "true"
        or str(e.get("CI") or "").strip().lower() in ("1", "true", "yes")
    )
    ai_studio_env = any(str(e.get(k) or "").strip() for k in ("AI_STUDIO", "AISTUDIO", "GOOGLE_AI_STUDIO"))
    in_app_applet = str(repo_path).startswith("/app/applet") or str(repo_resolved).startswith("/app/applet")
    gitless_preview_shim = (not has_git) and (repo_path / ".agents" / "preview" / "server.mjs").is_file()

    if override:
        host_id = override
        reason = "env:PLANNER_HOST_OVERRIDE"
    elif antigravity_present:
        if termux_proot:
            host_id = "antigravity-termux"
            reason = "env:ANTIGRAVITY+termux_proot"
        else:
            host_id = "antigravity-local"
            reason = "env:ANTIGRAVITY"
    elif ci_present:
        host_id = "ci-runner"
        reason = "env:GITHUB_ACTIONS/CI"
    elif in_app_applet or has_runtime_env_bridge or ai_studio_env or gitless_preview_shim:
        host_id = "ai-studio-cloud"
        if in_app_applet:
            reason = "path:/app/applet"
        elif has_runtime_env_bridge:
            reason = "file:runtime_env_bridge"
        elif ai_studio_env:
            reason = "env:AI_STUDIO"
        else:
            reason = "gitless+preview_shim"
    else:
        host_id = "local-ide"
        reason = "fallback:local-ide"

    return {
        "host_id": host_id,
        "reason": reason,
        "is_cloud": host_id == "ai-studio-cloud",
        "is_antigravity": host_id.startswith("antigravity"),
        "capabilities": {
            "has_git": has_git,
            "has_gh_auth": has_gh_auth,
            "has_local_dotnet": has_local_dotnet,
            "has_sdcard": has_sdcard,
            "has_local_omni_config": has_local_omni_config,
            "has_runtime_env_bridge": has_runtime_env_bridge,
        },
    }


def _load_host_matrix_raw(repo: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = Path(repo) / DEFAULT_HOST_MATRIX_REL
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"{DEFAULT_HOST_MATRIX_REL}: root must be a JSON object."
        return data, None
    except Exception as exc:
        return None, f"{DEFAULT_HOST_MATRIX_REL}: invalid JSON ({exc})."


def load_host_matrix(repo: Path) -> dict[str, Any] | None:
    """Load `.agents/host_matrix.json` if present and valid."""
    data, err = _load_host_matrix_raw(repo)
    return None if err else data


def resolve_host_view(
    repo: Path,
    host_info: dict[str, Any],
    matrix: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve active tool groups, skipped/preserved infra, and boot guidance for the current host."""
    host_id = str(host_info.get("host_id") or "local-ide")
    is_cloud = bool(host_info.get("is_cloud"))
    if not isinstance(matrix, dict):
        return {
            "host_id": host_id,
            "matrix_present": False,
            "ignore_cloud_env_alerts": not is_cloud,
            "boot_steps": [],
            "use_universal": [],
            "use_adapted": [],
            "skip_preserve": [],
            "daemon_infra": [],
        }

    hosts_cfg = matrix.get("hosts") if isinstance(matrix.get("hosts"), dict) else {}
    host_spec = hosts_cfg.get(host_id)
    if not isinstance(host_spec, dict) and host_id.startswith("antigravity"):
        host_spec = hosts_cfg.get("antigravity-termux")
    if not isinstance(host_spec, dict):
        host_spec = hosts_cfg.get("default") or {}

    ignore_cloud = bool(host_spec.get("ignore_cloud_env_alerts", not is_cloud))
    boot_steps = [str(s) for s in (host_spec.get("boot_steps") or []) if str(s).strip()]

    use_universal: list[dict[str, Any]] = []
    use_adapted: list[dict[str, Any]] = []
    skip_preserve: list[dict[str, Any]] = []
    daemon_infra: list[dict[str, Any]] = []

    for entry in matrix.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("id") or "")
        cat = str(entry.get("category") or "universal")
        if cat == "ignore":
            continue
        per_host = entry.get("per_host") if isinstance(entry.get("per_host"), dict) else {}
        h_rule = per_host.get(host_id) or (per_host.get("antigravity-termux") if host_id.startswith("antigravity") else None) or per_host.get("default")
        item = {
            "id": eid,
            "category": cat,
            "purpose": str(entry.get("purpose") or ""),
            "paths": list(entry.get("paths") or []),
            "rule": h_rule,
        }
        if cat == "universal":
            use_universal.append(item)
        elif cat == "dual-adapted":
            use_adapted.append(item)
        elif cat == "ai-studio-only":
            if is_cloud:
                use_adapted.append(item)
            else:
                skip_preserve.append(item)
        elif cat == "phone-daemon":
            daemon_infra.append(item)

    return {
        "host_id": host_id,
        "matrix_present": True,
        "ignore_cloud_env_alerts": ignore_cloud,
        "boot_steps": boot_steps,
        "use_universal": use_universal,
        "use_adapted": use_adapted,
        "skip_preserve": skip_preserve,
        "daemon_infra": daemon_infra,
    }


def _parse_iso_utc(raw: str) -> int:
    try:
        st = time.strptime(raw.strip(), "%Y-%m-%dT%H:%M:%SZ")
        return int(calendar.timegm(st))
    except Exception:
        return 0


def detect_baton_state(
    repo: Path,
    host_id: str,
    bridge_handoff_path: Path | None = None,
) -> dict[str, Any]:
    """Inspect `HANDOFF.md` (`host=`) and optional live bridge `LATEST_HANDOFF.md` for zero-talk baton switches."""
    handoff_file = Path(repo) / ".agents" / "docs" / "HANDOFF.md"
    last_host = ""
    last_ts = 0
    last_head = ""
    wave = ""
    next_step = ""
    summary = ""
    handoff_mtime = 0.0

    if handoff_file.is_file():
        try:
            handoff_mtime = handoff_file.stat().st_mtime
            text = handoff_file.read_text(encoding="utf-8", errors="ignore")
            for raw_ln in text.splitlines()[:25]:
                ln = raw_ln.strip()
                if not ln:
                    continue
                if ln.startswith("ts=") or "|host=" in ln or "|head=" in ln:
                    kv = dict(_KV_PAIR_RE.findall(ln))
                    if kv.get("host"):
                        last_host = kv["host"]
                    if kv.get("ts") and kv["ts"].isdigit():
                        last_ts = int(kv["ts"])
                    if kv.get("head") or kv.get("last_code"):
                        last_head = kv.get("head") or kv.get("last_code") or ""
                elif ln.startswith("WAVE|"):
                    wave = ln[5:].strip()
                elif ln.startswith(("NEXT|", "next|")):
                    next_step = ln.split("|", 1)[1].strip()
                elif ln.startswith(("SUMMARY|", "summary|")):
                    summary = ln.split("|", 1)[1].strip()
        except Exception:
            pass

    switched = bool(last_host and last_host != host_id)

    bpath = bridge_handoff_path
    if bpath is None and host_id.startswith("antigravity"):
        bpath = DEFAULT_BRIDGE_HANDOFF_PATH

    bridge_ahead = False
    bridge_summary = ""
    bridge_utc_ts = 0
    bridge_mtime = 0.0
    if bpath is not None and bpath.is_file():
        try:
            bridge_mtime = bpath.stat().st_mtime
            btext = bpath.read_text(encoding="utf-8", errors="ignore")
            m_utc = _UTC_LINE_RE.search(btext)
            if m_utc:
                bridge_utc_ts = _parse_iso_utc(m_utc.group(1))
            for bln in btext.splitlines():
                s = bln.strip()
                if s and not s.startswith("#") and not s.startswith("- **"):
                    bridge_summary = s[:120]
                    break
            mtime_newer = bridge_mtime > (handoff_mtime + 5.0)
            ts_newer = (last_ts <= 0) or (bridge_utc_ts > last_ts) or (int(bridge_mtime) > last_ts + 5)
            bridge_ahead = bool(mtime_newer and ts_newer)
        except Exception:
            pass

    return {
        "handoff_exists": handoff_file.is_file(),
        "last_host": last_host,
        "current_host": host_id,
        "switched": switched,
        "from_host": last_host,
        "to_host": host_id,
        "last_ts": last_ts,
        "last_head": last_head,
        "wave": wave,
        "next": next_step,
        "summary": summary,
        "bridge_ahead": bridge_ahead,
        "bridge_path": str(bpath) if bpath is not None else "",
        "bridge_summary": bridge_summary,
    }


def _default_per_host(category: str) -> dict[str, Any]:
    if category == "ai-studio-only":
        return {
            "ai-studio-cloud": {"mode": "active", "how": "runs in AI Studio cloud container"},
            "antigravity-termux": {"mode": "skip_preserve", "how": "preserve compatibility; do not execute locally"},
            "ci-runner": {"mode": "test_only", "how": "unit-test if applicable"},
            "local-ide": {"mode": "optional", "how": "preserve compatibility"},
        }
    if category == "phone-daemon":
        return {
            "ai-studio-cloud": {"mode": "remote_target", "how": "communicate via Cloudflare relay or deploy worker"},
            "antigravity-termux": {"mode": "infra_managed", "how": "phone daemon / relay infra (not needed for local git/fs)"},
            "ci-runner": {"mode": "test_only", "how": "unit-test daemon/worker logic"},
            "local-ide": {"mode": "remote_target", "how": "optional remote target"},
        }
    if category == "dual-adapted":
        return {
            "ai-studio-cloud": {"mode": "cloud_route", "how": "uses tokens/relay/tarball hydration"},
            "antigravity-termux": {"mode": "local_adapted", "how": "prefers native git/gh/local scratch + loopback IPC"},
            "ci-runner": {"mode": "ci_native", "how": "runs tests/checks only"},
            "local-ide": {"mode": "local_adapted", "how": "prefers native git/fs; relay optional"},
        }
    return {
        "ai-studio-cloud": {"mode": "active", "how": "runs identically"},
        "antigravity-termux": {"mode": "active", "how": "runs identically"},
        "ci-runner": {"mode": "active", "how": "runs in CI parity checks"},
        "local-ide": {"mode": "active", "how": "runs identically"},
    }


def infer_tool_classification(repo: Path, rel_path: str) -> dict[str, Any]:
    """Heuristically classify a script/config path by filename & first 4 KB content."""
    rel = _norm_rel(rel_path)
    low = rel.lower()
    head = ""
    full = Path(repo) / rel
    if full.is_file():
        try:
            head = full.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
        except Exception:
            head = ""

    ai_studio_name = (
        "preview/",
        "server.mjs",
        "dashboard.mjs",
        "package.json",
        "live_sync_watcher",
        "flight_recorder",
        "env_manifest.json",
    )
    ai_studio_body = (
        "runtime_env_bridge",
        "/tmp/.cavern_runtime_env",
        "cavern_flight_recorder",
        "renderdashboardhtml",
        "expectedinaistudio",
        "startlivesyncwatcher",
    )
    daemon_name = (
        "daemon",
        "auto_pilot",
        "install_termux",
        "relay_worker",
    )
    daemon_body = (
        "threadinghttpserver",
        "termux-wake-lock",
        "hubcoordinator",
        "/local/health",
        "autopilot",
    )
    dual_name = (
        "omni_agent",
        "omni_protocol",
        "bridge_auth",
        "local_capabilities",
        "repo_federation",
        "federation.json",
        "bridge_profile.json",
        "github_direct",
        "deploy_worker",
        "hot_probe",
        "live_phone_bridge",
    )
    dual_body = (
        "inspect_capabilities",
        "resolve_connection",
        "push_workspace",
        "compile_hot_probe",
        "cavern_federation_root",
        "detect_runtime_host",
    )

    if any(tok in low for tok in ai_studio_name) or any(tok in head for tok in ai_studio_body):
        cat = "ai-studio-only"
        reason = "AI Studio preview/runtime-env/flight-recorder signal"
    elif any(tok in low for tok in daemon_name) or any(tok in head for tok in daemon_body):
        cat = "phone-daemon"
        reason = "Termux/Cloudflare background daemon or worker signal"
    elif any(tok in low for tok in dual_name) or any(tok in head for tok in dual_body):
        cat = "dual-adapted"
        reason = "Cross-host bridge/federation/cloud-API adapter signal"
    else:
        cat = "universal"
        reason = "Host-agnostic verification, build, or config tool"

    return {
        "category": cat,
        "reason": reason,
        "per_host": _default_per_host(cat),
    }


def _discover_scan_files(repo: Path, scan_globs: list[str], ignore_globs: list[str]) -> list[str]:
    root = Path(repo)
    matched: set[str] = set()
    for pat in scan_globs:
        clean_pat = _norm_rel(str(pat))
        if not clean_pat:
            continue
        for cand in root.glob(clean_pat):
            if not cand.is_file():
                continue
            try:
                rel = cand.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(_glob_matches(rel, ig) for ig in ignore_globs):
                continue
            matched.add(rel)
    return sorted(matched)


def audit_host_matrix(repo: Path) -> dict[str, Any]:
    """Validate `.agents/host_matrix.json` schema, check stale exact paths, and detect unclassified tools."""
    data, parse_err = _load_host_matrix_raw(repo)
    if data is None and parse_err is None:
        return {
            "present": False,
            "ok": True,
            "strict": False,
            "entry_count": 0,
            "unclassified": [],
            "stale": [],
            "schema_errors": [],
        }
    if parse_err is not None or data is None:
        return {
            "present": True,
            "ok": False,
            "strict": False,
            "entry_count": 0,
            "unclassified": [],
            "stale": [],
            "schema_errors": [parse_err or f"Invalid {DEFAULT_HOST_MATRIX_REL}"],
        }

    schema_errors: list[str] = []
    scan_globs = data.get("scan_globs")
    ignore_globs = data.get("ignore_globs") or []
    entries = data.get("entries")
    if not isinstance(scan_globs, list):
        schema_errors.append("`scan_globs` must be a list of glob strings.")
        scan_globs = []
    if not isinstance(ignore_globs, list):
        schema_errors.append("`ignore_globs` must be a list of glob strings.")
        ignore_globs = []
    if not isinstance(entries, list):
        schema_errors.append("`entries` must be a list of tool objects.")
        entries = []

    covered_patterns: list[str] = []
    stale: list[str] = []
    seen_ids: set[str] = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            schema_errors.append(f"entries[{idx}] must be an object.")
            continue
        eid = str(entry.get("id") or "").strip()
        cat = str(entry.get("category") or "").strip()
        paths = entry.get("paths")
        purpose = str(entry.get("purpose") or "").strip()
        per_host = entry.get("per_host")

        if not eid:
            schema_errors.append(f"entries[{idx}] missing non-empty `id`.")
        elif eid in seen_ids:
            schema_errors.append(f"Duplicate entry id `{eid}`.")
        else:
            seen_ids.add(eid)

        if cat not in VALID_CATEGORIES:
            schema_errors.append(f"entries[{idx}] (`{eid or '?'}`) invalid category `{cat}`.")
        if not isinstance(paths, list) or not paths:
            schema_errors.append(f"entries[{idx}] (`{eid or '?'}`) `paths` must be a non-empty list.")
            paths = []
        if not purpose:
            schema_errors.append(f"entries[{idx}] (`{eid or '?'}`) missing `purpose`.")
        if not isinstance(per_host, dict):
            schema_errors.append(f"entries[{idx}] (`{eid or '?'}`) `per_host` must be an object.")

        for raw_p in paths:
            p = _norm_rel(str(raw_p))
            if not p:
                continue
            covered_patterns.append(p)
            if not any(ch in p for ch in "*?["):
                if not (Path(repo) / p).exists():
                    stale.append(p)

    scanned = _discover_scan_files(repo, [str(g) for g in scan_globs], [str(g) for g in ignore_globs])
    unclassified: list[dict[str, str]] = []
    for rel in scanned:
        if any(rel == pat or _glob_matches(rel, pat) for pat in covered_patterns):
            continue
        inf = infer_tool_classification(repo, rel)
        unclassified.append(
            {
                "path": rel,
                "suggested_category": str(inf["category"]),
                "reason": str(inf["reason"]),
            }
        )

    ok = not schema_errors and not unclassified and not stale
    return {
        "present": True,
        "ok": ok,
        "strict": bool(data.get("strict_coverage")),
        "entry_count": len(entries),
        "scanned_count": len(scanned),
        "unclassified": unclassified,
        "stale": sorted(set(stale)),
        "schema_errors": schema_errors,
    }


def register_tool_entry(
    repo: Path,
    rel_path: str,
    *,
    category: str = "",
    purpose: str = "",
    entry_id: str = "",
    per_host: dict[str, Any] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Classify a file and optionally register/update it inside `.agents/host_matrix.json`."""
    rel = _norm_rel(rel_path)
    inf = infer_tool_classification(repo, rel)
    chosen_cat = (category or str(inf["category"])).strip()
    if chosen_cat not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category `{chosen_cat}`. Valid: {', '.join(sorted(VALID_CATEGORIES))}")

    slug = re.sub(r"[^a-z0-9]+", "-", Path(rel).stem.lower()).strip("-") or "tool"
    eid = (entry_id or slug).strip()
    chosen_purpose = (purpose or f"{chosen_cat} tool ({rel})").strip()
    chosen_per_host = per_host if isinstance(per_host, dict) else _default_per_host(chosen_cat)

    result: dict[str, Any] = {
        "path": rel,
        "id": eid,
        "category": chosen_cat,
        "purpose": chosen_purpose,
        "reason": inf["reason"],
        "per_host": chosen_per_host,
        "applied": False,
        "matrix_path": str(Path(repo) / DEFAULT_HOST_MATRIX_REL),
    }
    if not apply:
        return result

    matrix_path = Path(repo) / DEFAULT_HOST_MATRIX_REL
    data, err = _load_host_matrix_raw(repo)
    if err:
        raise ValueError(err)
    if data is None:
        data = {
            "schema": 1,
            "strict_coverage": False,
            "hosts": {
                "antigravity-termux": {"ignore_cloud_env_alerts": True, "boot_steps": ["plan_tool.py host-boot"]},
                "ai-studio-cloud": {"ignore_cloud_env_alerts": False, "boot_steps": ["flight_recorder.py recover", "plan_tool.py host-boot"]},
                "ci-runner": {"ignore_cloud_env_alerts": True, "boot_steps": ["verify_universal.py --all"]},
                "local-ide": {"ignore_cloud_env_alerts": True, "boot_steps": ["plan_tool.py host-boot"]},
            },
            "scan_globs": [".agents/scripts/*.py", ".agents/agent/*.py"],
            "ignore_globs": ["**/__pycache__/**", "**/test_*.py", "**/*.test.mjs", "**/*_test.py"],
            "entries": [],
        }

    entries = data.setdefault("entries", [])
    existing = next((e for e in entries if isinstance(e, dict) and e.get("id") == eid), None)
    if existing is not None:
        existing["category"] = chosen_cat
        existing["purpose"] = chosen_purpose
        paths = list(existing.get("paths") or [])
        if rel not in paths:
            paths.append(rel)
        existing["paths"] = paths
        existing["per_host"] = chosen_per_host
    else:
        entries.append(
            {
                "id": eid,
                "category": chosen_cat,
                "paths": [rel],
                "purpose": chosen_purpose,
                "per_host": chosen_per_host,
            }
        )

    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = matrix_path.with_name(f".{matrix_path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, matrix_path)
    result["applied"] = True
    return result


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)] + "..."


def format_session_start_lines(
    repo: Path,
    env: Mapping[str, str] | None = None,
    *,
    bridge_handoff_path: Path | None = None,
    probe_paths: dict[str, Path] | None = None,
) -> list[str]:
    """Return <=2 compact pipe-delimited lines for SessionStart (strictly token-budgeted)."""
    host = detect_host(repo, env=env, probe_paths=probe_paths)
    host_id = host["host_id"]
    matrix = load_host_matrix(repo)
    baton = detect_baton_state(repo, host_id, bridge_handoff_path=bridge_handoff_path)

    if matrix is None and not baton["switched"] and not baton["bridge_ahead"]:
        return []

    caps = host["capabilities"]
    out: list[str] = []

    if host["is_cloud"]:
        n_ent = len(matrix.get("entries") or []) if isinstance(matrix, dict) else 0
        line1 = f"HOST|id={host_id}|bridge=fr,live_mirror,federation|matrix={n_ent}"
    else:
        native_parts = []
        if caps["has_git"]:
            native_parts.append("git")
        if caps["has_gh_auth"]:
            native_parts.append("gh")
        if caps["has_local_dotnet"]:
            native_parts.append("dotnet")
        if caps["has_sdcard"]:
            native_parts.append("sdcard")
        native_s = ",".join(native_parts) or "fs"
        omni_s = "ipc_hot_exec_only" if (host["is_antigravity"] or caps["has_local_omni_config"]) else "optional"
        line1 = f"HOST|id={host_id}|native={native_s}|omni={omni_s}|ignore_cloud_env=1"
    out.append(_clip(line1, 140))

    b_parts: list[str] = []
    if baton["switched"]:
        tag = f"BATON_SWITCH|{baton['from_host']}->{baton['to_host']}"
        if baton["wave"]:
            tag += f"|wave={baton['wave'][:24]}"
        if baton["next"]:
            tag += f"|next={baton['next'][:45]}"
        b_parts.append(tag)
    if baton["bridge_ahead"]:
        bsum = f"|{baton['bridge_summary'][:50]}" if baton["bridge_summary"] else ""
        b_parts.append(f"BRIDGE_AHEAD|LIVE_HANDOFF_NEWER{bsum}")

    if b_parts:
        out.append(_clip(" ; ".join(b_parts), 110))
    return out


def run_host_boot(
    repo: Path,
    *,
    as_json: bool = False,
    env: Mapping[str, str] | None = None,
    probe_paths: dict[str, Path] | None = None,
    bridge_handoff_path: Path | None = None,
) -> tuple[int, str]:
    """Unified 1-shot boot routine for any host (AI Studio cloud vs Antigravity local vs CI)."""
    repo_root = Path(repo).resolve()
    host = detect_host(repo_root, env=env, probe_paths=probe_paths)
    matrix = load_host_matrix(repo_root)
    view = resolve_host_view(repo_root, host, matrix)
    baton = detect_baton_state(repo_root, host["host_id"], bridge_handoff_path=bridge_handoff_path)
    audit = audit_host_matrix(repo_root)

    fr_recover: dict[str, Any] | None = None
    cloud_caps: dict[str, Any] | None = None

    if host["is_cloud"]:
        fr_script = repo_root / ".agents" / "scripts" / "flight_recorder.py"
        if fr_script.is_file():
            try:
                res = subprocess.run(
                    [sys.executable, "-B", str(fr_script), "recover", "--json"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                fr_recover = {
                    "returncode": res.returncode,
                    "output": (res.stdout or res.stderr).strip()[:600],
                }
            except Exception as exc:
                fr_recover = {"returncode": 1, "error": str(exc)}

        lcap_script = repo_root / ".agents" / "agent" / "local_capabilities.py"
        if lcap_script.is_file():
            try:
                agent_dir = str(lcap_script.parent)
                if agent_dir not in sys.path:
                    sys.path.insert(0, agent_dir)
                import local_capabilities as lcap  # noqa: E402

                cloud_caps = lcap.inspect_capabilities()
            except Exception as exc:
                cloud_caps = {"ok": False, "error": str(exc)}

    payload = {
        "ok": bool(audit.get("ok", True)),
        "host": host,
        "view": {
            "matrix_present": view["matrix_present"],
            "ignore_cloud_env_alerts": view["ignore_cloud_env_alerts"],
            "boot_steps": view["boot_steps"],
            "universal_ids": [e["id"] for e in view["use_universal"]],
            "adapted_ids": [e["id"] for e in view["use_adapted"]],
            "skipped_preserved_ids": [e["id"] for e in view["skip_preserve"]],
            "daemon_infra_ids": [e["id"] for e in view["daemon_infra"]],
        },
        "baton": baton,
        "matrix_audit": audit,
        "flight_recorder_recover": fr_recover,
        "cloud_capabilities": cloud_caps,
    }

    if as_json:
        return (0 if payload["ok"] else 1), json.dumps(payload, indent=2, ensure_ascii=False)

    caps = host["capabilities"]
    lines = [
        f"HOST_BOOT|id={host['host_id']}|reason={host['reason']}|ignore_cloud_env={int(view['ignore_cloud_env_alerts'])}",
        f"CAPABILITIES|git={int(caps['has_git'])}|gh={int(caps['has_gh_auth'])}|dotnet={int(caps['has_local_dotnet'])}|sdcard={int(caps['has_sdcard'])}|omni_cfg={int(caps['has_local_omni_config'])}|rt_env={int(caps['has_runtime_env_bridge'])}",
    ]
    if view["matrix_present"]:
        lines.append(
            f"TOOLS|universal={','.join(payload['view']['universal_ids']) or '-'}"
            f"|adapted={','.join(payload['view']['adapted_ids']) or '-'}"
            f"|skip_preserve={','.join(payload['view']['skipped_preserved_ids']) or '-'}"
        )
    if baton["switched"]:
        lines.append(f"BATON_SWITCH|{baton['from_host']}->{baton['to_host']}|wave={baton['wave'] or '-'}|next={baton['next'] or '-'}")
    elif baton["handoff_exists"]:
        lines.append(f"BATON|host={baton['last_host'] or host['host_id']}|wave={baton['wave'] or '-'}|next={baton['next'] or '-'}")
    if baton["bridge_ahead"]:
        lines.append(f"BRIDGE_AHEAD|{baton['bridge_path']}|{baton['bridge_summary']}")
    if fr_recover is not None:
        lines.append(f"FLIGHT_RECORDER|rc={fr_recover.get('returncode', '?')}|{str(fr_recover.get('output') or fr_recover.get('error') or '')[:140]}")
    if cloud_caps and cloud_caps.get("alert"):
        lines.append(f"CLOUD_ALERT|{cloud_caps['alert']}")
    if audit["present"]:
        lines.append(
            f"MATRIX_AUDIT|ok={audit['ok']}|entries={audit['entry_count']}|unclassified={len(audit['unclassified'])}|stale={len(audit['stale'])}|schema_errors={len(audit['schema_errors'])}"
        )
        for u in audit["unclassified"][:5]:
            lines.append(f"  UNCLASSIFIED|{u['path']} -> suggest={u['suggested_category']} ({u['reason']})")
        for s in audit["stale"][:5]:
            lines.append(f"  STALE|{s}")
        for se in audit["schema_errors"][:5]:
            lines.append(f"  SCHEMA_ERR|{se}")

    return (0 if payload["ok"] else 1), "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Universal Multi-Host Context & Tool Matrix Manager.")
    parser.add_argument("--workspace", default="", help="Workspace root override")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for cmd_name in ("boot", "status", "audit"):
        sp = sub.add_parser(cmd_name)
        sp.add_argument("--json", action="store_true")

    p_cls = sub.add_parser("classify", help="Classify and optionally register a tool in .agents/host_matrix.json")
    p_cls.add_argument("path", help="Workspace-relative file path")
    p_cls.add_argument("--category", default="", choices=("", *sorted(VALID_CATEGORIES)))
    p_cls.add_argument("--purpose", default="")
    p_cls.add_argument("--id", dest="entry_id", default="")
    p_cls.add_argument("--apply", action="store_true")
    p_cls.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()

    if args.cmd == "boot":
        rc, out = run_host_boot(ws, as_json=bool(args.json))
        print(out)
        return rc
    if args.cmd == "status":
        host = detect_host(ws)
        matrix = load_host_matrix(ws)
        view = resolve_host_view(ws, host, matrix)
        baton = detect_baton_state(ws, host["host_id"])
        data = {"ok": True, "host": host, "view": view, "baton": baton}
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            for ln in format_session_start_lines(ws):
                print(ln)
            if not matrix:
                print(f"HOST|id={host['host_id']}|matrix=none")
        return 0
    if args.cmd == "audit":
        audit = audit_host_matrix(ws)
        if args.json:
            print(json.dumps(audit, indent=2, ensure_ascii=False))
        else:
            print(
                f"MATRIX_AUDIT|present={audit['present']}|ok={audit['ok']}|entries={audit['entry_count']}|"
                f"unclassified={len(audit['unclassified'])}|stale={len(audit['stale'])}|errors={len(audit['schema_errors'])}"
            )
            for u in audit["unclassified"]:
                print(f"  UNCLASSIFIED|{u['path']} -> suggest={u['suggested_category']} ({u['reason']})")
            for s in audit["stale"]:
                print(f"  STALE|{s}")
            for se in audit["schema_errors"]:
                print(f"  SCHEMA_ERR|{se}")
        return 0 if audit["ok"] else 1
    if args.cmd == "classify":
        res = register_tool_entry(
            ws,
            args.path,
            category=args.category,
            purpose=args.purpose,
            entry_id=args.entry_id,
            apply=bool(args.apply),
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"HOST_CLASSIFY|path={res['path']}|id={res['id']}|cat={res['category']}|applied={res['applied']}|reason={res['reason']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
