#!/usr/bin/env python3
"""Pure-Python chained schema migration engine for `.agents/planner_profile.json`."""

from __future__ import annotations

import copy
from typing import Any, Callable

CURRENT_SCHEMA_VERSION = 4
CURRENT_DOCS_FORMAT_VERSION = 1

DEFAULT_MEMORY_DOCS = [
    ".agents/docs/PROJECT_HISTORY.md",
    ".agents/docs/SAVED_IDEAS.md",
    "AGENTS.md",
]

DEFAULT_READ_BUDGET = {
    "max_full_read_lines": 300,
    "max_slice_lines": 250,
}

DEFAULT_SECRET_GUARD = {
    "enabled": True,
}

DEFAULT_BLAST_RADIUS = {
    "simple_tier_max_files": 3,
}

DEFAULT_CODE_WEIGHT_LIMITS = {
    "enabled": True,
    "warn_lines": 1500,
    "warn_lines_all": 2000,
}

DEFAULT_MEMORY_WEIGHT_LIMITS = {
    "project_history_max_lines": 150,
    "project_history_max_kb": 28,
    "external_sync_max_kb": 48,
}

MigratorFn = Callable[[dict[str, Any]], dict[str, Any]]


def parse_semver(raw: Any) -> tuple[int, int, int]:
    """Parse a semantic version string like '2.9.0' or 'v2.9.0-r1' into (major, minor, patch)."""
    text = str(raw or "").strip().lstrip("vV")
    if not text:
        return (0, 0, 0)
    core = text.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    nums: list[int] = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def compare_semver(a: Any, b: Any) -> int:
    """Compare two semver strings: return -1 if a < b, 0 if equal, 1 if a > b."""
    pa, pb = parse_semver(a), parse_semver(b)
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Losslessly migrate a v1 profile (missing or schema_version=1) to schema v2."""
    out = copy.deepcopy(data)
    out["schema_version"] = 2

    if "build_rules" not in out or out.get("build_rules") is None:
        rules: list[dict[str, Any]] = []
        verify_cmd = str(out.get("verify_command") or "").strip()
        verify_non_code = str(out.get("verify_non_code_command") or "").strip()
        build_exts = [str(e) for e in (out.get("build_extensions") or []) if str(e).strip()]

        if verify_cmd:
            rule: dict[str, Any] = {
                "label": "verify_command",
                "command": verify_cmd,
                "run_on_force": True,
            }
            if build_exts:
                rule["trigger_extensions"] = build_exts
            rules.append(rule)

        if verify_non_code and verify_non_code != verify_cmd:
            rules.append({
                "label": "verify_non_code_command",
                "command": verify_non_code,
                "run_on_force": False,
                "run_without_build": True,
            })

        out["build_rules"] = rules

    raw_mf = out.get("memory_feedback")
    mf = copy.deepcopy(raw_mf) if isinstance(raw_mf, dict) else {}
    mf.setdefault("enabled", True)
    mf.setdefault("min_code_files", 2)
    if not isinstance(mf.get("docs"), list) or not mf.get("docs"):
        mf["docs"] = list(DEFAULT_MEMORY_DOCS)
    out["memory_feedback"] = mf

    return out


def migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Losslessly migrate a v2 profile to schema v3."""
    out = copy.deepcopy(data)
    out["schema_version"] = 3

    mode = str(out.get("planner_mode") or "").strip().lower()
    if mode not in ("lean", "strict", "auto"):
        out["planner_mode"] = "lean"
    else:
        out["planner_mode"] = mode

    raw_rb = out.get("read_budget")
    rb = copy.deepcopy(raw_rb) if isinstance(raw_rb, dict) else {}
    rb.setdefault("max_full_read_lines", DEFAULT_READ_BUDGET["max_full_read_lines"])
    rb.setdefault("max_slice_lines", DEFAULT_READ_BUDGET["max_slice_lines"])
    out["read_budget"] = rb

    raw_sg = out.get("secret_guard")
    sg = copy.deepcopy(raw_sg) if isinstance(raw_sg, dict) else {}
    sg.setdefault("enabled", DEFAULT_SECRET_GUARD["enabled"])
    out["secret_guard"] = sg

    raw_br = out.get("blast_radius")
    br = copy.deepcopy(raw_br) if isinstance(raw_br, dict) else {}
    br.setdefault("simple_tier_max_files", DEFAULT_BLAST_RADIUS["simple_tier_max_files"])
    out["blast_radius"] = br

    return out


def migrate_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    """Losslessly migrate a v3 profile to schema v4 (universal code/memory bloat limits)."""
    out = copy.deepcopy(data)
    out["schema_version"] = 4

    raw_cw = out.get("code_weight_limits")
    cw = copy.deepcopy(raw_cw) if isinstance(raw_cw, dict) else {}
    cw.setdefault("enabled", DEFAULT_CODE_WEIGHT_LIMITS["enabled"])
    cw.setdefault("warn_lines", DEFAULT_CODE_WEIGHT_LIMITS["warn_lines"])
    cw.setdefault("warn_lines_all", DEFAULT_CODE_WEIGHT_LIMITS["warn_lines_all"])
    out["code_weight_limits"] = cw

    raw_mw = out.get("memory_weight_limits")
    mw = copy.deepcopy(raw_mw) if isinstance(raw_mw, dict) else {}
    mw.setdefault("project_history_max_lines", DEFAULT_MEMORY_WEIGHT_LIMITS["project_history_max_lines"])
    mw.setdefault("project_history_max_kb", DEFAULT_MEMORY_WEIGHT_LIMITS["project_history_max_kb"])
    mw.setdefault("external_sync_max_kb", DEFAULT_MEMORY_WEIGHT_LIMITS["external_sync_max_kb"])
    out["memory_weight_limits"] = mw

    return out


MIGRATORS: dict[int, MigratorFn] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
    3: migrate_v3_to_v4,
}


def migrate_profile(
    data: dict[str, Any],
    migrators: dict[int, MigratorFn] | None = None,
    target_version: int | None = None,
) -> tuple[dict[str, Any], int, list[str]]:
    """Deterministically apply chained migrations from the profile's schema_version.

    Returns `(migrated_data, resolved_schema_version, warnings)`.
    Never mutates `data` in place. Emits a non-fatal warning if the profile's
    `schema_version` exceeds `target_version` (`CURRENT_SCHEMA_VERSION`).
    """
    if not isinstance(data, dict):
        return {}, 1, ["Invalid planner_profile.json root (expected object); using defaults."]

    out = copy.deepcopy(data)
    warnings: list[str] = []
    active_migrators = MIGRATORS if migrators is None else migrators
    target = CURRENT_SCHEMA_VERSION if target_version is None else int(target_version)

    raw_ver = out.get("schema_version")
    if raw_ver is None:
        ver = 1
    elif isinstance(raw_ver, bool):
        warnings.append(f"Invalid boolean schema_version {raw_ver!r}; treating as v1.")
        ver = 1
    else:
        try:
            ver = int(raw_ver)
        except (TypeError, ValueError):
            warnings.append(f"Unparseable schema_version {raw_ver!r}; treating as v1.")
            ver = 1
        if ver < 1:
            warnings.append(f"Non-positive schema_version {ver}; treating as v1.")
            ver = 1

    if ver > target:
        warnings.append(
            f"Profile schema_version {ver} is newer than supported v{target}; "
            "keeping fields intact without downgrading."
        )
        return out, ver, warnings

    while ver < target:
        migrator = active_migrators.get(ver)
        if migrator is None:
            warnings.append(
                f"Missing schema migrator for v{ver} -> v{ver + 1}; stopping at v{ver}."
            )
            break
        out = migrator(out)
        ver += 1
        out["schema_version"] = ver

    return out, ver, warnings
