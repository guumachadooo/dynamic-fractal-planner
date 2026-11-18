#!/usr/bin/env python3
"""Shared state & declarative condition helpers for the universal Dynamic Fractal Planner plugin."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from schema_migrations import CURRENT_SCHEMA_VERSION, migrate_profile  # noqa: E402

DEFAULT_IGNORED_PREFIXES = (
    ".git/", ".agents/", "node_modules/", "dist/", "build/", "target/",
    "bin/", "obj/", "__pycache__/", ".venv/", "venv/", "docs/",
)
DEFAULT_DOC_NAMES = {"agents.md", "gemini.md", "claude.md", "readme.md", "changelog.md", "license", "license.md"}
DEFAULT_CODE_EXTS = (
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".csproj",
    ".vb", ".fs", ".java", ".kt", ".kts", ".swift", ".rs", ".go", ".c",
    ".cpp", ".cc", ".h", ".hpp", ".rb", ".php", ".sql", ".sh", ".yml",
    ".yaml", ".toml", ".html", ".css", ".scss", ".xml", ".json", ".axml", ".resx",
)
DEFAULT_BUILD_EXTS = (
    ".cs", ".csproj", ".fs", ".vb", ".kt", ".kts", ".java", ".swift",
    ".rs", ".go", ".c", ".cpp", ".cc", ".ts", ".tsx",
)
DEFAULT_EXTRA_REQUESTS_FILE = ".agents/docs/EXTRA_REQUESTS.md"
DEFAULT_MEMORY_DOCS = (
    ".agents/docs/PROJECT_HISTORY.md",
    ".agents/docs/SAVED_IDEAS.md",
    "AGENTS.md",
    "GEMINI.md",
    "CHANGELOG.md",
)

BOOTSTRAP_REQUIRED_FILES = (
    ".agents/docs/PROJECT_HISTORY.md",
    ".agents/docs/SAVED_IDEAS.md",
    ".agents/docs/EXTRA_REQUESTS.md",
    ".agents/planner_profile.json",
)

PROJECT_ROOT_MARKERS = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Makefile",
    "AGENTS.md",
    "GEMINI.md",
}

PROJECT_SOURCE_EXTS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".csproj", ".sln",
    ".vb", ".fs", ".java", ".kt", ".kts", ".swift", ".rs", ".go", ".c", ".cpp",
    ".cc", ".h", ".hpp", ".rb", ".php", ".sql", ".sh", ".html", ".css", ".scss",
}

PROJECT_SCAN_SKIP_DIRS = {
    ".git", ".agents", "node_modules", "dist", "build", "target",
    "bin", "obj", "__pycache__", ".venv", "venv",
}

PROJECT_HISTORY_TEMPLATE = """# Project History & Engineering Pitfalls (`PROJECT_HISTORY.md`)

> **Chronological Archive Only:** Live workspace state, git status, and pending extra requests are injected dynamically at startup by `SessionStart`. **Never write `"Current State"` or `"Current Version"` headers in this static file.**
>
> **How to Update:** Use Section 2 for reusable pitfalls/constraints and **prepend** concise 1–4 line milestone entries directly below the comment in Section 3 (newest first).

---

## 1. Environment & Stack Overview
- (Document runtime/build/test commands and key environments here.)

## 2. Technical Lessons & Known Pitfalls
- (Record non-obvious bugs, edge cases, and invariants so future sessions never repeat them.)

## 3. Version History (Newest First)
<!-- MAINTENANCE RULE: Always PREPEND new entries immediately below this comment (newest first). Never label any entry as "Current State". -->
"""

SAVED_IDEAS_TEMPLATE = """# Saved Ideas & Archived Prototypes (`SAVED_IDEAS.md`)

> Stores promising ideas, designs, algorithms, or snippets that were deferred or replaced temporarily, so they can be revisited cleanly later.

---
"""

EXTRA_REQUESTS_TEMPLATE = """# Extra Request Queue (Parallel `/btw` Threads)

> **Instructions for the Primary Agent:**
> Before finishing any task, running `git commit`, or `git push`, read this file.
> - If items exist under **Pending Extra Requests** (other than `(No pending requests.)`), plan and implement them first, then move them to **Completed Requests** restoring `(No pending requests.)`.

## Pending Extra Requests
(No pending requests.)

## Completed Requests
(No completed requests recorded yet.)
"""


def resolve_plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_plugin_path(path_str: str) -> tuple[str, bool]:
    """Return `(plugin_relative_posix_path, True)` if `path_str` targets the plugin directory."""
    if not path_str:
        return "", False

    candidates: list[Path] = []
    for base in (
        resolve_plugin_root(),
        Path.home() / ".gemini" / "config" / "plugins" / "dynamic-fractal-planner",
    ):
        for fn in (lambda p: p.expanduser().resolve(), lambda p: Path(os.path.abspath(os.path.expanduser(str(p))))):
            try:
                c = fn(base)
                if c not in candidates:
                    candidates.append(c)
            except Exception:
                pass

    p_variants: list[Path] = []
    for fn in (lambda s: Path(s).expanduser().resolve(), lambda s: Path(os.path.abspath(os.path.expanduser(s)))):
        try:
            pv = fn(path_str)
            if pv not in p_variants:
                p_variants.append(pv)
        except Exception:
            pass

    for pv in p_variants:
        for root in candidates:
            try:
                rel = pv.relative_to(root).as_posix()
                return rel, True
            except ValueError:
                continue
    return path_str.replace("\\", "/"), False


def resolve_repo_root(payload: dict[str, Any]) -> Path:
    ws_paths = payload.get("workspacePaths") or []
    if ws_paths:
        cand = Path(ws_paths[0]).expanduser().resolve()
        if cand.exists():
            return cand
    cwd = (payload.get("cwd") or "").strip()
    if cwd:
        cand = Path(cwd).expanduser().resolve()
        if cand.exists():
            return cand
    return Path.cwd().resolve()


def resolve_artifact_dir(payload: dict[str, Any]) -> Path:
    raw = (payload.get("artifactDirectoryPath") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    conv_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(payload.get("conversationId") or "default")) or "default"
    return Path(f"/tmp/.agy_planner_{conv_id}")


def prompt_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()[:16]


def get_paths(payload: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    art = resolve_artifact_dir(payload)
    scratch = art / "scratch"
    return art, art / "TASK_PLAN.md", scratch / "plan_state.json", scratch / ".hook_state.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_project_profile_with_warnings(repo: Path) -> tuple[dict[str, Any], list[str]]:
    """Load `.agents/planner_profile.json` and migrate older schemas purely in-memory."""
    profile_path = repo / ".agents" / "planner_profile.json"
    if not profile_path.is_file():
        return {}, []
    raw = load_json(profile_path)
    if not raw and profile_path.read_text(encoding="utf-8", errors="ignore").strip() != "{}":
        return {}, [f"Could not parse `{profile_path}` as a valid JSON object; using defaults."]
    migrated, _, warnings = migrate_profile(raw)
    return migrated, warnings


def load_project_profile(repo: Path, warnings_out: list[str] | None = None) -> dict[str, Any]:
    """Load and in-memory migrate `.agents/planner_profile.json` without mutating disk."""
    migrated, warnings = load_project_profile_with_warnings(repo)
    if warnings_out is not None and warnings:
        warnings_out.extend(warnings)
    return migrated


def eval_conditions(cond: dict[str, Any] | None, repo: Path | None = None) -> bool:
    """Evaluate declarative filesystem/environment conditions safely."""
    if not cond:
        return True

    def _expand(p: str) -> str:
        if repo and not p.startswith(("/", "~")):
            return str(repo / p)
        return str(Path(p).expanduser())

    if "exists_path" in cond:
        if not Path(_expand(str(cond["exists_path"]))).exists():
            return False
    if "not_exists_path" in cond:
        if Path(_expand(str(cond["not_exists_path"]))).exists():
            return False
    if "exists_glob" in cond:
        if not glob.glob(_expand(str(cond["exists_glob"]))):
            return False
    if "not_exists_glob" in cond:
        if glob.glob(_expand(str(cond["not_exists_glob"]))):
            return False
    return True


def normalize_rel_path(path_str: str, repo: Path) -> tuple[str, bool]:
    """Return (repo_relative_path, is_inside_repo)."""
    if not path_str:
        return "", False
    try:
        p_norm = str(Path(path_str).expanduser().resolve()).replace("\\", "/")
    except Exception:
        p_norm = path_str.replace("\\", "/")
    repo_norm = str(repo.resolve()).replace("\\", "/").rstrip("/") + "/"
    if p_norm.startswith(repo_norm):
        return p_norm[len(repo_norm) :].lstrip("./"), True
    raw = path_str.replace("\\", "/").lstrip("./")
    if not raw.startswith("/"):
        return raw, True
    return raw, False


def is_app_code_path(path_str: str, repo: Path, profile: dict[str, Any] | None = None) -> bool:
    rel, inside = normalize_rel_path(path_str, repo)
    if not inside or not rel:
        return False
    low = rel.lower()
    if Path(low).name in DEFAULT_DOC_NAMES:
        return False

    prof = profile if profile is not None else load_project_profile(repo)
    prefixes = prof.get("code_path_prefixes")
    exts = tuple(e.lower() for e in (prof.get("code_extensions") or DEFAULT_CODE_EXTS))

    if prefixes:
        norm_prefixes = tuple(p.replace("\\", "/").lstrip("./") for p in prefixes)
        if not rel.startswith(norm_prefixes):
            return False
        return low.endswith(exts)

    if low.startswith(DEFAULT_IGNORED_PREFIXES):
        return False
    return low.endswith(exts)


def is_verifiable_path(path_str: str, repo: Path, profile: dict[str, Any] | None = None) -> bool:
    if is_app_code_path(path_str, repo, profile):
        return True
    rel, inside = normalize_rel_path(path_str, repo)
    if not inside or not rel:
        return False
    low = rel.lower()
    if low.startswith((".agents/", ".github/workflows/")) and low.endswith((".py", ".json", ".sh", ".yml", ".yaml")):
        return True
    return False


def is_build_relevant_path(path_str: str, repo: Path, profile: dict[str, Any] | None = None) -> bool:
    prof = profile if profile is not None else load_project_profile(repo)
    if not prof.get("verify_command") and not prof.get("build_rules"):
        return False
    exts = tuple(e.lower() for e in (prof.get("build_extensions") or DEFAULT_BUILD_EXTS))
    return path_str.lower().endswith(exts)


def has_active_turn_plan(payload: dict[str, Any], hook_state: dict[str, Any]) -> bool:
    art, task_md, plan_json, _ = get_paths(payload)
    turn_ts = float(hook_state.get("turn_started_ts") or 0.0)
    cur_hash = prompt_hash(payload.get("lastUserInput") or "")
    turn_sig = hook_state.get("turn_sig")

    if turn_sig and hook_state.get("plan_turn_sig") == turn_sig:
        if plan_json.is_file() or task_md.is_file():
            return True

    state = load_json(plan_json)
    if state and cur_hash and state.get("prompt_hash") == cur_hash:
        return True

    if turn_ts > 0:
        if task_md.is_file() and task_md.stat().st_size >= 80 and task_md.stat().st_mtime >= turn_ts:
            return True
        if art.is_dir():
            for cand in art.glob("*plan*.md"):
                if cand.is_file() and cand.stat().st_size >= 80 and cand.stat().st_mtime >= turn_ts:
                    return True
    elif plan_json.is_file() or (task_md.is_file() and task_md.stat().st_size >= 80):
        return True

    return False


def resolve_extra_requests_rel(profile: dict[str, Any]) -> str:
    return (profile.get("extra_requests_file") or DEFAULT_EXTRA_REQUESTS_FILE).strip()


def get_pending_extra_requests(repo: Path, profile: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    prof = profile if profile is not None else load_project_profile(repo)
    rel = resolve_extra_requests_rel(prof)
    extra_file = repo / rel
    if not extra_file.is_file():
        return rel, []
    try:
        text = extra_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return rel, []

    heading_alts = r"(?:Pending Extra Requests|Pedidos Extras Pendentes|Extra Requests|Pending|Queue)"
    m = re.search(
        rf"^#{{2,3}}\s+{heading_alts}(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#{{1,3}}\s+|\Z)",
        text,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if not m:
        m = re.search(
            rf"^#\s+{heading_alts}(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#\s+|\Z)",
            text,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
    if not m:
        return rel, []

    pending: list[str] = []
    for ln in m.group(1).splitlines():
        s = ln.strip()
        sl = s.lower()
        if (
            not s
            or s.startswith("<!--")
            or sl.startswith(("(no pending", "(none", "(nenhum", "(empty", "(vazio", "- (no pending", "- (none"))
            or sl in ("none", "n/a", "-", "empty")
        ):
            continue
        pending.append(s)
    return rel, pending


def is_real_project_root(repo: Path) -> bool:
    """Return True only when `repo` looks like a real project directory (never /, /tmp, ~, or ~/.gemini/*)."""
    try:
        r = repo.expanduser().resolve()
    except Exception:
        return False
    if not r.is_dir():
        return False

    home = Path.home().resolve()
    gemini_dir = (home / ".gemini").resolve()
    disallowed = {
        Path("/"),
        Path("/tmp"),
        Path("/tmp").resolve(),
        Path("/var/tmp"),
        Path("/var/tmp").resolve(),
        home,
        gemini_dir,
    }
    if r in disallowed or gemini_dir in r.parents:
        return False

    if (r / ".git").exists():
        return True

    for marker in PROJECT_ROOT_MARKERS:
        if (r / marker).is_file():
            return True

    # Bounded breadth-first scan for project markers (.sln/.csproj) or source files at depth <= 2
    queue: list[tuple[Path, int]] = [(r, 0)]
    while queue:
        cur, depth = queue.pop(0)
        try:
            entries = list(os.scandir(cur))
        except Exception:
            continue
        for entry in entries[:250]:
            try:
                if entry.is_symlink():
                    continue
                name = entry.name
                if entry.is_file():
                    low = name.lower()
                    if low.endswith((".sln", ".csproj")) or Path(low).suffix in PROJECT_SOURCE_EXTS:
                        return True
                elif depth < 2 and entry.is_dir():
                    if not name.startswith(".") and name not in PROJECT_SCAN_SKIP_DIRS:
                        queue.append((Path(entry.path), depth + 1))
            except Exception:
                continue
    return False


def _detect_starter_build_rules(repo: Path) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    pkg_file = repo / "package.json"
    if pkg_file.is_file():
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8", errors="ignore") or "{}")
            scripts = pkg.get("scripts") if isinstance(pkg, dict) else {}
            if isinstance(scripts, dict):
                if "test" in scripts:
                    rules.append({
                        "label": "npm test",
                        "command": "npm test",
                        "trigger_extensions": [".js", ".jsx", ".ts", ".tsx"],
                        "run_on_force": True,
                    })
                if "build" in scripts:
                    rules.append({
                        "label": "npm run build",
                        "command": "npm run build",
                        "trigger_extensions": [".js", ".jsx", ".ts", ".tsx"],
                    })
        except Exception:
            pass
    if (repo / "pyproject.toml").is_file() or (repo / "pytest.ini").is_file() or (repo / "tests").is_dir():
        rules.append({
            "label": "pytest",
            "command": "pytest -q",
            "trigger_extensions": [".py"],
            "run_on_force": True,
        })
    if (repo / "Cargo.toml").is_file():
        rules.append({
            "label": "cargo test",
            "command": "cargo test",
            "trigger_extensions": [".rs"],
            "run_on_force": True,
        })
    if (repo / "go.mod").is_file():
        rules.append({
            "label": "go test",
            "command": "go test ./...",
            "trigger_extensions": [".go"],
            "run_on_force": True,
        })
    return rules


def bootstrap_workspace(repo: Path, name: str = "", force: bool = False) -> tuple[list[str], list[str]]:
    """Create missing `.agents/docs/*` and `.agents/planner_profile.json` without overwriting existing files."""
    agents_dir = repo / ".agents"
    docs_dir = agents_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    skipped: list[str] = []

    def _write_if_needed(path: Path, content: str) -> None:
        rel = path.relative_to(repo).as_posix()
        if path.exists() and not force:
            skipped.append(rel)
            return
        path.write_text(content, encoding="utf-8")
        created.append(rel)

    _write_if_needed(docs_dir / "PROJECT_HISTORY.md", PROJECT_HISTORY_TEMPLATE)
    _write_if_needed(docs_dir / "SAVED_IDEAS.md", SAVED_IDEAS_TEMPLATE)
    _write_if_needed(docs_dir / "EXTRA_REQUESTS.md", EXTRA_REQUESTS_TEMPLATE)

    profile_path = agents_dir / "planner_profile.json"
    if profile_path.exists() and not force:
        skipped.append(profile_path.relative_to(repo).as_posix())
    else:
        profile = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "project_name": name or repo.name,
            "extra_requests_file": ".agents/docs/EXTRA_REQUESTS.md",
            "memory_feedback": {
                "enabled": True,
                "min_code_files": 2,
                "docs": [
                    ".agents/docs/PROJECT_HISTORY.md",
                    ".agents/docs/SAVED_IDEAS.md",
                    "AGENTS.md",
                ],
            },
            "build_rules": _detect_starter_build_rules(repo),
            "domains": {},
        }
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created.append(profile_path.relative_to(repo).as_posix())

    return created, skipped
