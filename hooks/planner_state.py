#!/usr/bin/env python3
"""Shared state & declarative condition helpers for the universal Dynamic Fractal Planner plugin."""

from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from model_mode_cache import (  # noqa: E402
    lookup_model_mode,
    normalize_model_key,
    pin_native_plan_model,
    prepare_unseen_classification,
)
from schema_migrations import (  # noqa: E402
    CURRENT_DOCS_FORMAT_VERSION,
    CURRENT_SCHEMA_VERSION,
    DEFAULT_BLAST_RADIUS,
    DEFAULT_CODE_WEIGHT_LIMITS,
    DEFAULT_DELEGATION_CONFIG,
    DEFAULT_MEMORY_WEIGHT_LIMITS,
    DEFAULT_READ_BUDGET,
    DEFAULT_SECRET_GUARD,
    compare_semver,
    migrate_profile,
)

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
    "tsconfig.json",
    "vite.config.ts",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "pubspec.yaml",
    "Makefile",
    "AGENTS.md",
    "GEMINI.md",
}

PROJECT_SOURCE_EXTS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".csproj", ".sln",
    ".vb", ".fs", ".java", ".kt", ".kts", ".swift", ".dart", ".rs", ".go", ".c", ".cpp",
    ".cc", ".h", ".hpp", ".rb", ".php", ".sql", ".sh", ".html", ".css", ".scss",
}

PROJECT_SCAN_SKIP_DIRS = {
    ".git", ".agents", "node_modules", "dist", "build", "target",
    "bin", "obj", "__pycache__", ".venv", "venv",
}

PROJECT_HISTORY_TEMPLATE = """# Project History & Pitfalls (`PROJECT_HISTORY.md`)

> RULE|chronological archive ONLY (`SessionStart` injects live git/versions/queue) -> `!static("Current State"|"Current Version")`.
> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:line`, `symbol()`, invariants).

---

## 1. Env & Stack
<!-- ENV|runtime/build/test cmds & key constraints -->

## 2. Pitfalls & Invariants
<!-- FORMAT: - PITFALL|<path:sym> -> <root-cause/constraint> -> <safe fix> -->

## 3. Version History (Newest First)
<!-- RULE: PREPEND 1-3L entries directly below (newest first). !static("Current State"). -->
<!-- FORMAT: - YYYY-MM-DD|<tag>|<scope/file:sym> <change> -> <effect> | verified:<cmd> -->
"""

SAVED_IDEAS_TEMPLATE = """# Saved Ideas & Prototypes (`SAVED_IDEAS.md`)

> Deferred ideas/designs/algos/snippets for later revisit.
> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || !`, verbatim `path:sym`/snippets).
<!-- FORMAT: - IDEA|<name> -> <why deferred> | <key mechanism/snippet> -->

---
"""

EXTRA_REQUESTS_TEMPLATE = """# Extra Request Queue (`/btw` Threads)

> GATE|check pre-finish, `git commit` & `git push`: implement pending items -> move to `## Completed Requests` -> restore `(No pending requests.)`.
> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/filler`, ops `-> | &&`, verbatim `path:sym`). Format: `- REQ|<scope> -> <goal/constraint>`.

## Pending Extra Requests
(No pending requests.)

## Completed Requests
(No completed requests recorded yet.)
"""

DOCS_STYLE_MARKER = "STYLE|lossless symbolic telegraphic EN"
HANDOFF_FORMAT_MARKER = f"fmt={CURRENT_DOCS_FORMAT_VERSION}"
_LEGACY_HISTORY_PLACEHOLDERS = {
    "- (document runtime/build/test commands and key environments here.)",
    "- (record non-obvious bugs, edge cases, and invariants so future sessions never repeat them.)",
}


def is_bypass_enabled() -> bool:
    """Return True when the emergency `.bypass_gates` kill-switch exists in plugin root."""
    return (resolve_plugin_root() / ".bypass_gates").exists()


def resolve_planner_mode_info(
    profile: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    hook_state: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Resolve `(effective_mode, source, model_name)` for `lean`, `strict`, and `auto`.

    Precedence:
      1. `PLANNER_MODE` env (`lean`|`strict`)
      2. `profile["planner_mode"]` (`lean`|`strict`)
      3. When `auto`: missing/`auto` -> safe `lean`; active native-plan turn -> safe `lean`;
         exact lookup via `model_mode_cache` (`profile.auto_mode.overrides` > cache > seed);
         unseen -> safe `lean` (`auto:unseen`, never regex-guessed).
    """
    raw_model = str((payload or {}).get("modelName") or "").strip()
    env_mode = os.environ.get("PLANNER_MODE", "").strip().lower()
    if env_mode in ("lean", "strict"):
        return env_mode, "env", raw_model

    if env_mode == "auto":
        configured = "auto"
    else:
        p_mode = str((profile or {}).get("planner_mode") or "").strip().lower()
        configured = p_mode if p_mode in ("lean", "strict", "auto") else "auto"

    if configured in ("lean", "strict"):
        return configured, "profile", raw_model

    if not raw_model or raw_model.lower() == "auto":
        return "lean", "auto:no_model", raw_model

    if isinstance(hook_state, dict):
        turn_sig = str(hook_state.get("turn_sig") or "")
        np_sig = str(hook_state.get("native_plan_turn_sig") or "")
        np_ts = float(hook_state.get("native_plan_active_ts") or 0.0)
        if (turn_sig and np_sig == turn_sig) or (not turn_sig and np_ts > 0.0 and (time.time() - np_ts) <= 600.0):
            return "lean", "auto:native_plan_turn", raw_model

    mode, src, _ = lookup_model_mode(raw_model, profile=profile)
    if mode in ("lean", "strict"):
        return mode, src, raw_model

    return "lean", "auto:unseen", raw_model


def get_planner_mode(
    profile: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    hook_state: dict[str, Any] | None = None,
) -> str:
    """Return active planner mode (`lean` or `strict`), resolving `auto` via `payload['modelName']`."""
    mode, _, _ = resolve_planner_mode_info(profile, payload, hook_state=hook_state)
    return mode


_ECO_MODE_RE = re.compile(
    r"cota\s+acabando|quota\s+acabando|vai\s+r[aá]pido|modo\s+econ[oô]mico|economiza\s+cota|low\s+quota|save\s+quota",
    re.IGNORECASE,
)


def is_eco_mode_prompt(text: str) -> bool:
    return bool(_ECO_MODE_RE.search(text or ""))


def record_telemetry(event: str, meta: dict[str, Any] | None = None) -> None:
    """Append compact telemetry event to `.metrics.jsonl` (safe no-op during normal plugin tests)."""
    if os.environ.get("PLANNER_DISABLE_TELEMETRY") == "1":
        return
    metrics_env = os.environ.get("PLANNER_METRICS_FILE", "").strip()
    if not metrics_env and os.environ.get("PLANNER_RUNNING_PLUGIN_TESTS") == "1":
        return
    target = Path(metrics_env) if metrics_env else (resolve_plugin_root() / ".metrics.jsonl")
    try:
        import time

        entry: dict[str, Any] = {"ts": int(time.time()), "ev": event}
        if meta:
            entry.update(meta)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


_CONTROL_WORDS = frozenset({
    "if", "for", "foreach", "while", "switch", "catch", "using", "lock",
    "return", "throw", "new", "sizeof", "typeof", "nameof", "await",
    "else", "when", "try", "finally", "do", "case", "yield", "checked",
    "unchecked", "fixed", "super", "this",
})

_SYM_MD_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_SYM_PY_RE = re.compile(r"^\s*(?:async\s+)?(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_SYM_TYPE_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:public|private|protected|internal|abstract|sealed|static|partial|open|data|final|pub(?:\([^)]*\))?|\s)*(?:class|struct|interface|enum|record|trait|impl)\s+([A-Za-z_$][A-Za-z0-9_$]*)")
_SYM_CS_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|async|partial|extern|new|unsafe|readonly|suspend|synchronized|final)\s+)+"
    r"(?:[\w.<>\[\]?,\s]+?\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_SYM_KT_FUN_RE = re.compile(r"^\s*(?:(?:public|private|protected|internal|override|open|suspend|inline|operator)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SYM_JS_FN_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_SYM_JS_ARROW_RE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
_SYM_JS_METHOD_RE = re.compile(r"^\s*(?:(?:public|private|protected|static|async|override|readonly|get|set)\s+)*([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^;]*\)\s*(?::\s*[\w.<>\[\]|&\s]+)?\s*\{")
_SYM_GO_RE = re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SYM_RS_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")
_SYM_SH_FN_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{")


def extract_symbol_map(lines: list[str], suffix: str, max_entries: int = 80) -> list[str]:
    """Extract compact `L{no}|{symbol}` map from source/markdown lines in pure Python."""
    ext = (suffix or "").lower()
    out: list[str] = []

    for idx, line in enumerate(lines, start=1):
        s = line.rstrip()
        if not s.strip():
            continue
        if ext == ".md":
            m = _SYM_MD_RE.match(s)
            if m:
                out.append(f"L{idx}|{m.group(1)} {m.group(2)[:55]}")
            continue
        if ext == ".py":
            m = _SYM_PY_RE.match(s)
            if m:
                kind, name = m.group(1), m.group(2)
                out.append(f"L{idx}|class {name}" if kind == "class" else f"L{idx}|{name}()")
            continue
        if ext == ".sh":
            m = _SYM_SH_FN_RE.match(s)
            if m:
                out.append(f"L{idx}|{m.group(1)}()")
            continue
        if ext == ".go":
            m = _SYM_TYPE_RE.match(s) or _SYM_GO_RE.match(s)
            if m:
                txt = s.strip()
                out.append(f"L{idx}|{m.group(1)}" if txt.startswith("type ") else f"L{idx}|{m.group(1)}()")
            continue
        if ext == ".rs":
            m = _SYM_RS_FN_RE.match(s)
            if m:
                out.append(f"L{idx}|{m.group(1)}()")
                continue
            m = _SYM_TYPE_RE.match(s)
            if m:
                out.append(f"L{idx}|{m.group(1)}")
            continue

        m_type = _SYM_TYPE_RE.match(s)
        if m_type and not s.lstrip().startswith(("//", "*", "/*")):
            out.append(f"L{idx}|class {m_type.group(1)}")
            continue

        if ext in (".kt", ".kts"):
            m = _SYM_KT_FUN_RE.match(s)
            if m and m.group(1) not in _CONTROL_WORDS:
                out.append(f"L{idx}|{m.group(1)}()")
            continue

        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            m = _SYM_JS_FN_RE.match(s) or _SYM_JS_ARROW_RE.match(s) or _SYM_JS_METHOD_RE.match(s)
            if m and m.group(1) not in _CONTROL_WORDS:
                out.append(f"L{idx}|{m.group(1)}()")
            continue

        # C#, Java, C/C++, Dart, Swift, PHP fallback
        stripped = s.lstrip()
        if stripped.startswith(("//", "/*", "*", "#", "[")):
            continue
        m_cs = _SYM_CS_METHOD_RE.match(s)
        if m_cs and "=" not in s.split("(", 1)[0]:
            name = m_cs.group(1)
            if name not in _CONTROL_WORDS:
                out.append(f"L{idx}|{name}()")

    if len(out) > max_entries:
        keep = out[: max_entries - 1]
        keep.append(f"...(+{len(out) - (max_entries - 1)} more symbols)")
        return keep
    return out


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub token (ghp_*)", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("Google/Firebase API key (AIza*)", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("API secret key (sk-*)", re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("Private key PEM block", re.compile(r"-----BEGIN\s+(?:(?:RSA|EC|OPENSSH)\s+)?PRIVATE\s+KEY-----")),
)
_SECRET_DEF_HINT_RE = re.compile(r"re\.compile|\{\d+,\}|\[A-Za-z0-9|SECRET_PATTERNS")
_SECRET_EXTS = {".keystore", ".jks", ".p12", ".pfx", ".pem"}


def scan_secrets_and_conflicts(text: str, rel_path: str = "", check_filename: bool = True) -> list[str]:
    """Detect real secrets, secret keyfiles, and git merge-conflict markers."""
    hits: list[str] = []
    norm = (rel_path or "").replace("\\", "/")
    if check_filename and norm:
        fname = Path(norm).name.lower()
        if fname == ".env" or (fname.startswith(".env.") and not fname.endswith((".example", ".sample", ".template", ".dist"))):
            hits.append(f"Sensitive `.env` path `{norm}` blocked")
        elif Path(fname).suffix in _SECRET_EXTS:
            hits.append(f"Sensitive key/cert path `{norm}` blocked")

    lines = (text or "").splitlines()
    has_left = any(re.match(r"^<{7}(?:\s|$)", ln) for ln in lines)
    has_right = any(re.match(r"^>{7}(?:\s|$)", ln) for ln in lines)
    has_eq_conflict = has_left or has_right

    for idx, ln in enumerate(lines, start=1):
        if re.match(r"^<{7}(?:\s|$)", ln) or re.match(r"^>{7}(?:\s|$)", ln) or (has_eq_conflict and re.match(r"^={7}$", ln)):
            hits.append(f"L{idx}: Git merge-conflict marker detected")
            continue
        if _SECRET_DEF_HINT_RE.search(ln):
            continue
        for label, pat in _SECRET_PATTERNS:
            if pat.search(ln):
                hits.append(f"L{idx}: Possible {label} detected")
                break
    return hits


def resolve_plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def detect_doc_mcps(
    profile: dict[str, Any] | None = None,
    plugins_dir: Path | None = None,
) -> dict[str, bool]:
    """Detect installed documentation MCP plugins (`google-developer-knowledge`, `context7`).

    Respects optional `profile["doc_lookup"]`:
      - `enabled: false` disables all doc MCP hints
      - `gdk: true|false` / `context7: true|false` explicitly overrides auto-detection
      - `auto_detect: false` skips filesystem probing
    """
    cfg = (profile or {}).get("doc_lookup")
    cfg_dict = cfg if isinstance(cfg, dict) else {}
    if cfg_dict.get("enabled") is False:
        return {"gdk": False, "context7": False}

    auto_detect = cfg_dict.get("auto_detect", True) is not False
    if plugins_dir is not None:
        base = Path(plugins_dir).expanduser()
    else:
        env_dir = os.environ.get("PLANNER_PLUGINS_DIR", "").strip()
        base = Path(env_dir).expanduser() if env_dir else resolve_plugin_root().parent

    def _has_plugin(slug: str) -> bool:
        if not auto_detect or not base.is_dir():
            return False
        pdir = base / slug
        return (pdir / "mcp_config.json").is_file() or (pdir / "plugin.json").is_file()

    gdk = cfg_dict["gdk"] if isinstance(cfg_dict.get("gdk"), bool) else _has_plugin("google-developer-knowledge")
    c7 = cfg_dict["context7"] if isinstance(cfg_dict.get("context7"), bool) else _has_plugin("context7")
    return {"gdk": bool(gdk), "context7": bool(c7)}


def format_doc_mcp_hint(targets: list[str], available: dict[str, bool]) -> str:
    """Format a compact 1-line routing hint for matched & installed doc MCPs."""
    parts: list[str] = []
    if "gdk" in targets and available.get("gdk"):
        parts.append("GDK(`google-developer-knowledge`: `search_documents`/`answer_query`)")
    if "context7" in targets and available.get("context7"):
        parts.append("Context7(`context7`: `resolve-library-id`->`query-docs`, 1 concept/call)")
    if not parts:
        return ""
    return "DOC_LOOKUP|" + " + ".join(parts)


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
                if rel in (".agents", ".diagnostics", ".last_good_backup", ".git") or rel.startswith(
                    (".agents/", ".diagnostics/", ".last_good_backup/", ".git/")
                ):
                    return rel, False
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


def unique_edited_files(hook_state: dict[str, Any]) -> list[str]:
    """Return deduplicated list of edited code + plugin files in a turn."""
    raw_plugin = [str(p) for p in (hook_state.get("plugin_files_edited") or []) if p]
    plugin_set = set(raw_plugin)
    code = [str(c) for c in (hook_state.get("code_files_edited") or []) if c and c not in plugin_set]
    return code + [f"plugin:{p}" for p in raw_plugin]


def build_carried_task_context(
    prev_state: dict[str, Any] | None,
    prev_plan_state: dict[str, Any] | None,
    now: float,
    ttl_secs: float = 1800.0,
) -> dict[str, Any]:
    """Build or preserve compact multi-turn `task_context` for follow-up continuity."""
    if not isinstance(prev_state, dict) or not prev_state:
        return {}

    prev_plan = prev_plan_state if isinstance(prev_plan_state, dict) else {}
    lifecycle = str(prev_plan.get("lifecycle") or "")
    if prev_state.get("post_commit_nudge") and lifecycle != "awaiting_user":
        return {}

    prev_triage = prev_state.get("triage") if isinstance(prev_state.get("triage"), dict) else {}
    prev_tier = int(prev_triage.get("tier") or 0)
    prev_edited = len(unique_edited_files(prev_state))
    build_failed = bool(prev_state.get("last_build_failed"))

    if prev_tier >= 2 or prev_edited >= 3:
        status = "proposed" if (prev_edited == 0 or lifecycle == "awaiting_user") else "in_progress"
        ts = float(prev_state.get("last_activity_ts") or prev_state.get("turn_started_ts") or now)
        if ts <= 0.0 or (now - ts) > ttl_secs:
            return {}
        return {
            "turn_id": int(prev_state.get("turn_id") or 0),
            "tier": max(prev_tier, 2 if prev_edited >= 3 else prev_tier),
            "depth_level": str(prev_triage.get("depth_level") or "low"),
            "depth_signals": list(prev_triage.get("depth_signals") or [])[:8],
            "detected_domains": list(prev_triage.get("detected_domains") or []),
            "doc_lookup_targets": list(prev_triage.get("doc_lookup_targets") or []),
            "file_refs": list(prev_triage.get("file_refs") or [])[:12],
            "status": status,
            "build_failed": build_failed,
            "ts": ts,
            "ttl_secs": ttl_secs,
        }

    # Preserve prior fresh task_context across intervening Q&A turns (tier <= 1, no edits)
    if prev_tier <= 1 and prev_edited == 0:
        prev_ctx = prev_state.get("task_context")
        if isinstance(prev_ctx, dict) and int(prev_ctx.get("tier") or 0) >= 2:
            ctx_ts = float(prev_ctx.get("ts") or 0.0)
            ctx_ttl = float(prev_ctx.get("ttl_secs") or ttl_secs)
            if ctx_ts > 0.0 and (now - ctx_ts) <= ctx_ttl:
                return dict(prev_ctx)

    return {}


def resolve_extra_requests_rel(profile: dict[str, Any]) -> str:
    return (profile.get("extra_requests_file") or DEFAULT_EXTRA_REQUESTS_FILE).strip()


_PENDING_HEADING_ALTS = r"(?:Pending Extra Requests|Pedidos Extras Pendentes|Extra Requests|Pending|Queue)"
_COMPLETED_HEADING_ALTS = r"(?:Completed Requests|Conclu[ií]dos|Completed)"
_PENDING_SEC_RE = re.compile(
    rf"^#{{2,3}}\s+{_PENDING_HEADING_ALTS}(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#{{1,3}}\s+|\Z)",
    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_PENDING_SEC_H1_RE = re.compile(
    rf"^#\s+{_PENDING_HEADING_ALTS}(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#\s+|\Z)",
    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_COMPLETED_SEC_RE = re.compile(
    rf"^#{{1,3}}\s+{_COMPLETED_HEADING_ALTS}(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#{{1,3}}\s+|\Z)",
    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_PENDING_HEADING_LINE_RE = re.compile(rf"^#{{1,3}}\s+{_PENDING_HEADING_ALTS}(?:\s*:)?\s*$", re.IGNORECASE)
_COMPLETED_HEADING_LINE_RE = re.compile(rf"^#{{1,3}}\s+{_COMPLETED_HEADING_ALTS}(?:\s*:)?\s*$", re.IGNORECASE)


def _extract_pending_extra_items(text: str) -> list[str]:
    m = _PENDING_SEC_RE.search(text) or _PENDING_SEC_H1_RE.search(text)
    if not m:
        return []
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
    return pending


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
    return rel, _extract_pending_extra_items(text)


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
    scratch_dir = (gemini_dir / "antigravity" / "scratch").resolve()
    plugin_root = resolve_plugin_root().resolve()
    disallowed = {
        Path("/"),
        Path("/tmp"),
        Path("/tmp").resolve(),
        Path("/var/tmp"),
        Path("/var/tmp").resolve(),
        home,
        gemini_dir,
        scratch_dir,
        plugin_root,
    }
    in_scratch_project = scratch_dir in r.parents
    if r in disallowed or plugin_root in r.parents or (gemini_dir in r.parents and not in_scratch_project):
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
    has_tsconfig = (repo / "tsconfig.json").is_file()
    pkg_has_typecheck = False
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
                if "typecheck" in scripts:
                    pkg_has_typecheck = True
                    rules.append({
                        "label": "npm run typecheck",
                        "command": "npm run typecheck",
                        "trigger_extensions": [".ts", ".tsx"],
                        "run_on_force": True,
                    })
                if "lint" in scripts:
                    rules.append({
                        "label": "npm run lint",
                        "command": "npm run lint",
                        "trigger_extensions": [".js", ".jsx", ".ts", ".tsx"],
                    })
                if "build" in scripts:
                    rules.append({
                        "label": "npm run build",
                        "command": "npm run build",
                        "trigger_extensions": [".js", ".jsx", ".ts", ".tsx"],
                    })
        except Exception:
            pass
    if has_tsconfig and not pkg_has_typecheck:
        rules.append({
            "label": "tsc --noEmit",
            "command": "npx tsc --noEmit",
            "trigger_extensions": [".ts", ".tsx"],
            "run_on_force": True,
        })
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
    if (repo / "pubspec.yaml").is_file():
        rules.append({
            "label": "flutter test",
            "command": "flutter test",
            "trigger_extensions": [".dart"],
            "run_on_force": True,
        })
    if (repo / "build.gradle").is_file() or (repo / "build.gradle.kts").is_file() or (repo / "gradlew").is_file():
        g_cmd = "./gradlew test" if (repo / "gradlew").is_file() else "gradle test"
        rules.append({
            "label": "gradle test",
            "command": g_cmd,
            "trigger_extensions": [".kt", ".kts", ".java"],
            "run_on_force": True,
        })
    if any(repo.glob("*.sln")) or any(repo.glob("*.csproj")) or any(repo.glob("*/*.csproj")) or any(repo.glob("*/*/*.csproj")):
        termux_dotnet = Path("/root/.dotnet/dotnet")
        if termux_dotnet.is_file():
            rules.append({
                "label": "dotnet build",
                "command": "/root/.dotnet/dotnet build -m:1 /p:UseSharedCompilation=false --nologo -v:minimal",
                "trigger_extensions": [".cs", ".csproj"],
                "run_on_force": True,
                "env": {
                    "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
                    "DOTNET_GCHeapHardLimit": "20000000",
                    "DOTNET_EnableDiagnostics": "0",
                },
            })
        else:
            rules.append({
                "label": "dotnet build",
                "command": "dotnet build --nologo -v:minimal",
                "trigger_extensions": [".cs", ".csproj"],
                "run_on_force": True,
            })
    return rules


def get_managed_project_docs(repo: Path, profile: dict[str, Any] | None = None) -> list[str]:
    """Return existing plugin-managed `.md` docs in `repo` (excluding `.agents/docs/archive/`)."""
    candidates: list[str] = [
        ".agents/docs/PROJECT_HISTORY.md",
        ".agents/docs/SAVED_IDEAS.md",
        ".agents/docs/EXTRA_REQUESTS.md",
        ".agents/docs/HANDOFF.md",
    ]

    def _add_candidate(raw_rel: Any, allow_root_md: bool = False) -> None:
        if not isinstance(raw_rel, str):
            return
        rel = raw_rel.strip().replace("\\", "/").lstrip("./")
        if not rel or rel.startswith("/") or ".." in rel.split("/") or not rel.lower().endswith(".md"):
            return
        if rel.startswith(".agents/docs/archive/"):
            return
        if not allow_root_md and not rel.startswith(".agents/docs/"):
            return
        if rel not in candidates:
            candidates.append(rel)

    if isinstance(profile, dict):
        _add_candidate(profile.get("extra_requests_file"))
        sess = profile.get("session")
        if isinstance(sess, dict):
            for p in sess.get("extra_docs") or []:
                _add_candidate(p)
        mf = profile.get("memory_feedback")
        if isinstance(mf, dict):
            for p in mf.get("docs") or []:
                _add_candidate(p)
        dm = profile.get("docs_migration")
        if isinstance(dm, dict):
            for p in dm.get("extra_paths") or []:
                _add_candidate(p, allow_root_md=True)
        if not isinstance(dm, dict) or dm.get("include_root_agent_docs", True) is not False:
            for root_doc in ("AGENTS.md", "GEMINI.md"):
                _add_candidate(root_doc, allow_root_md=True)
    else:
        for root_doc in ("AGENTS.md", "GEMINI.md"):
            _add_candidate(root_doc, allow_root_md=True)

    return [rel for rel in candidates if (repo / rel).is_file()]


def doc_has_current_format(rel: str, text: str) -> bool:
    """Return True if a managed doc carries the current symbolic telegraphic EN format marker."""
    norm = (rel or "").replace("\\", "/")
    explicit = f"dfp:format={CURRENT_DOCS_FORMAT_VERSION}"
    if norm.endswith("/HANDOFF.md") or norm == "HANDOFF.md":
        return (HANDOFF_FORMAT_MARKER in text) or (DOCS_STYLE_MARKER in text) or (explicit in text)
    return (DOCS_STYLE_MARKER in text) or (explicit in text)


def is_unmodified_legacy_doc(rel: str, text: str, repo: Path | None = None) -> bool:
    """Return True ONLY when `text` is pure unmodified legacy template boilerplate (zero user content)."""
    norm = (rel or "").replace("\\", "/")
    if norm.endswith("/HANDOFF.md") or norm == "HANDOFF.md":
        return False

    if norm.endswith("/EXTRA_REQUESTS.md"):
        if _extract_pending_extra_items(text):
            return False
        for ln in text.splitlines():
            s = ln.strip()
            if re.match(r"^#{2,3}\s+", s) and not (
                _PENDING_HEADING_LINE_RE.match(s) or _COMPLETED_HEADING_LINE_RE.match(s)
            ):
                return False
        m_done = _COMPLETED_SEC_RE.search(text)
        if m_done:
            done_clean = re.sub(r"<!--.*?-->", "", m_done.group(1), flags=re.DOTALL)
            for ln in done_clean.splitlines():
                s = ln.strip()
                if s and not s.startswith("("):
                    return False
        outside = _PENDING_SEC_RE.sub("", text)
        if outside == text:
            outside = _PENDING_SEC_H1_RE.sub("", text)
        outside = _COMPLETED_SEC_RE.sub("", outside)
        outside = re.sub(r"<!--.*?-->", "", outside, flags=re.DOTALL)
        for ln in outside.splitlines():
            s = ln.strip()
            if not s:
                continue
            if not (s.startswith(("#", ">", "(")) or s in ("---", "***")):
                return False
        return True

    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    if norm.endswith("/SAVED_IDEAS.md"):
        for ln in stripped.splitlines():
            s = ln.strip()
            if not s:
                continue
            if not (s.startswith(("#", ">")) or s in ("---", "***")):
                return False
        return True

    if norm.endswith("/PROJECT_HISTORY.md"):
        for ln in stripped.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith(("#", ">")) or s in ("---", "***") or s.lower() in _LEGACY_HISTORY_PLACEHOLDERS:
                continue
            return False
        return True

    return False


def _template_for_managed_doc(rel: str) -> str | None:
    norm = (rel or "").replace("\\", "/")
    if norm.endswith("/PROJECT_HISTORY.md"):
        return PROJECT_HISTORY_TEMPLATE
    if norm.endswith("/SAVED_IDEAS.md"):
        return SAVED_IDEAS_TEMPLATE
    if norm.endswith("/EXTRA_REQUESTS.md"):
        return EXTRA_REQUESTS_TEMPLATE
    return None


def read_plugin_version(plugin_dir: Path | None = None) -> str:
    """Read semantic version from plugin.json (defaults to '0.0.0' if missing)."""
    root = plugin_dir if plugin_dir is not None else resolve_plugin_root()
    pjson = root / "plugin.json"
    if not pjson.is_file():
        return "0.0.0"
    try:
        data = json.loads(pjson.read_text(encoding="utf-8", errors="ignore"))
        return str(data.get("version") or "0.0.0").strip()
    except Exception:
        return "0.0.0"


def auto_upgrade_workspace(repo: Path) -> dict[str, Any]:
    """Retroactively upgrade outdated profile schema (v1..v4->v5), plugin_version stamp & untouched legacy templates (0 tokens)."""
    res: dict[str, Any] = {
        "profile_migrated": None,
        "plugin_upgraded": None,
        "plugin_downgrade_blocked": None,
        "boilerplate_upgraded": [],
        "docs_needing_ai_conversion": [],
        "docs_format_stamped": False,
        "current_docs_format": 0,
        "target_docs_format": CURRENT_DOCS_FORMAT_VERSION,
    }
    if not is_real_project_root(repo) or not (repo / ".agents").is_dir():
        return res

    profile_path = repo / ".agents" / "planner_profile.json"
    raw: dict[str, Any] = {}
    is_future_schema = False
    profile_dirty = False
    current_plugin_ver = read_plugin_version()

    if profile_path.is_file():
        try:
            loaded = json.loads(profile_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return res
        if not isinstance(loaded, dict):
            return res
        raw = loaded
        raw_ver = raw.get("schema_version")
        is_future_schema = isinstance(raw_ver, int) and not isinstance(raw_ver, bool) and raw_ver > CURRENT_SCHEMA_VERSION
        if not is_future_schema:
            migrated, resolved_ver, _ = migrate_profile(raw)
            if migrated != raw:
                old_v = raw_ver if (isinstance(raw_ver, int) and not isinstance(raw_ver, bool)) else 1
                res["profile_migrated"] = (old_v, resolved_ver)
                raw = migrated
                profile_dirty = True

            if current_plugin_ver and current_plugin_ver != "0.0.0":
                stored_pv = str(raw.get("plugin_version") or "").strip()
                cmp_pv = compare_semver(current_plugin_ver, stored_pv)
                if cmp_pv > 0:
                    res["plugin_upgraded"] = (stored_pv or "0.0.0", current_plugin_ver)
                    raw["plugin_version"] = current_plugin_ver
                    profile_dirty = True
                elif cmp_pv < 0 and stored_pv:
                    res["plugin_downgrade_blocked"] = (stored_pv, current_plugin_ver)

        doc_ver_raw = raw.get("docs_format_version")
        try:
            doc_ver = int(doc_ver_raw) if (doc_ver_raw is not None and not isinstance(doc_ver_raw, bool)) else 0
        except (TypeError, ValueError):
            doc_ver = 0
        res["current_docs_format"] = doc_ver
        if doc_ver >= CURRENT_DOCS_FORMAT_VERSION:
            if profile_dirty and not is_future_schema:
                profile_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return res

    managed = get_managed_project_docs(repo, raw)
    if not managed:
        if profile_dirty and profile_path.is_file() and not is_future_schema:
            profile_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return res

    for rel in managed:
        p = repo / rel
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if doc_has_current_format(rel, txt):
            continue
        tmpl = _template_for_managed_doc(rel)
        if tmpl is not None and is_unmodified_legacy_doc(rel, txt, repo=repo):
            p.write_text(tmpl, encoding="utf-8")
            res["boilerplate_upgraded"].append(rel)
        else:
            res["docs_needing_ai_conversion"].append(rel)

    if not res["docs_needing_ai_conversion"] and profile_path.is_file() and not is_future_schema:
        raw["docs_format_version"] = CURRENT_DOCS_FORMAT_VERSION
        profile_dirty = True
        res["docs_format_stamped"] = True
        res["current_docs_format"] = CURRENT_DOCS_FORMAT_VERSION

    if profile_dirty and profile_path.is_file() and not is_future_schema:
        profile_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return res


def mark_docs_migrated(repo: Path, force: bool = False) -> tuple[bool, str, list[str]]:
    """Validate that managed docs carry current format marker and stamp `docs_format_version`."""
    auto_upgrade_workspace(repo)
    profile = load_project_profile(repo)
    managed = get_managed_project_docs(repo, profile)
    remaining = [
        rel
        for rel in managed
        if not doc_has_current_format(rel, (repo / rel).read_text(encoding="utf-8", errors="ignore"))
    ]
    if remaining and not force:
        return False, "Docs still missing symbolic telegraphic EN format marker", remaining

    profile_path = repo / ".agents" / "planner_profile.json"
    if not profile_path.is_file():
        bootstrap_workspace(repo)
    raw = load_json(profile_path)
    raw_ver = raw.get("schema_version")
    if isinstance(raw_ver, int) and not isinstance(raw_ver, bool) and raw_ver > CURRENT_SCHEMA_VERSION:
        return False, f"Profile schema_version {raw_ver} > supported v{CURRENT_SCHEMA_VERSION}", []
    migrated, _, _ = migrate_profile(raw)
    migrated["docs_format_version"] = CURRENT_DOCS_FORMAT_VERSION
    profile_path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True, str(profile_path), managed


def bootstrap_workspace(
    repo: Path,
    name: str = "",
    force: bool = False,
    embed_plugin: bool = False,
) -> tuple[list[str], list[str]]:
    """Create missing `.agents/docs/*` and `.agents/planner_profile.json` (plus optional embedded plugin/host matrix)."""
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
        managed = get_managed_project_docs(repo, {})
        all_current = all(
            doc_has_current_format(rel, (repo / rel).read_text(encoding="utf-8", errors="ignore"))
            for rel in managed
        )
        profile = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "plugin_version": read_plugin_version(),
            "docs_format_version": CURRENT_DOCS_FORMAT_VERSION if all_current else 0,
            "project_name": name or repo.name,
            "planner_mode": "auto" if embed_plugin else "lean",
            "read_budget": dict(DEFAULT_READ_BUDGET),
            "secret_guard": dict(DEFAULT_SECRET_GUARD),
            "blast_radius": dict(DEFAULT_BLAST_RADIUS),
            "code_weight_limits": dict(DEFAULT_CODE_WEIGHT_LIMITS),
            "memory_weight_limits": dict(DEFAULT_MEMORY_WEIGHT_LIMITS),
            "file_budget_limits": {
                "enabled": True,
                "warn_files": 850,
                "fail_files": 920,
                "hard_platform_cap": 999,
            },
            "delegation": copy.deepcopy(DEFAULT_DELEGATION_CONFIG),
            "plugin_evolution": {
                "enabled": True,
                "plugin_path": ".agents/plugins/dynamic-fractal-planner",
                "plugin_sync_doc": ".agents/plugins/dynamic-fractal-planner/.agents/docs/EXTERNAL_AI_SYNC.md",
                "auto_sync_higher_version": True,
            },
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

    if embed_plugin:
        import self_evolve  # noqa: E402

        src_plugin = resolve_plugin_root()
        glob_plugin = self_evolve.resolve_global_plugin_dir()
        if (glob_plugin / "plugin.json").is_file() and compare_semver(
            read_plugin_version(glob_plugin), read_plugin_version(src_plugin)
        ) > 0:
            src_plugin = glob_plugin
        dst_plugin = agents_dir / "plugins" / "dynamic-fractal-planner"
        plugin_rel = ".agents/plugins/dynamic-fractal-planner"
        if src_plugin.resolve() != dst_plugin.resolve():
            if (dst_plugin / "plugin.json").is_file() and not force:
                skipped.append(plugin_rel)
            else:
                sync_fn = getattr(self_evolve, "_sync_plugin_tree", None) or getattr(self_evolve, "_copy_plugin_tree")
                sync_fn(src_plugin, dst_plugin, snapshot_backup=False)
                created.append(plugin_rel)

        host_matrix_template = {
            "schema": 1,
            "strict_coverage": False,
            "note": (
                "Declarative multi-host tool & integration matrix (`antigravity-termux` <-> `antigravity-local` "
                "<-> `ai-studio-cloud` <-> `ci-runner` <-> `local-ide`). Audited by `verify_universal.py` & `plan_tool.py host-audit`."
            ),
            "hosts": {
                "antigravity-termux": {
                    "label": "Antigravity (Termux PRoot)",
                    "ignore_cloud_env_alerts": True,
                    "boot_steps": [
                        "python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py host-boot",
                        "git status -sb",
                    ],
                },
                "antigravity-local": {
                    "label": "Antigravity (Local Workstation)",
                    "ignore_cloud_env_alerts": True,
                    "boot_steps": [
                        "python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py host-boot",
                        "git status -sb",
                    ],
                },
                "ai-studio-cloud": {
                    "label": "Google AI Studio Cloud Container",
                    "ignore_cloud_env_alerts": False,
                    "boot_steps": [
                        "python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py host-boot",
                    ],
                },
                "ci-runner": {
                    "label": "CI Runner",
                    "ignore_cloud_env_alerts": True,
                    "boot_steps": [
                        "python3 -B .agents/plugins/dynamic-fractal-planner/scripts/verify_universal.py --all",
                    ],
                },
                "local-ide": {
                    "label": "Generic Local IDE",
                    "ignore_cloud_env_alerts": True,
                    "boot_steps": [
                        "python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py host-boot",
                    ],
                },
            },
            "scan_globs": [
                ".agents/agent/*.py",
                ".agents/agent/*.mjs",
                ".agents/agent/*.sh",
                ".agents/scripts/*.py",
                ".agents/preview/*.mjs",
                ".agents/host_matrix.json",
            ],
            "ignore_globs": [
                "**/__pycache__/**",
                "**/test_*.py",
                "**/test_*.mjs",
                "**/*.test.mjs",
                "**/*_test.py",
            ],
            "entries": [
                {
                    "id": "host-matrix-config",
                    "category": "universal",
                    "paths": [".agents/host_matrix.json"],
                    "purpose": "Declarative multi-host capability, routing & auto-classification manifest.",
                    "per_host": {
                        "antigravity-termux": {"mode": "active", "how": "checked by verify_universal.py & host-boot"},
                        "antigravity-local": {"mode": "active", "how": "checked by verify_universal.py & host-boot"},
                        "ai-studio-cloud": {"mode": "active", "how": "checked by verify_universal.py & host-boot"},
                        "ci-runner": {"mode": "active", "how": "checked by verify_universal.py --all"},
                        "local-ide": {"mode": "active", "how": "checked by verify_universal.py"},
                    },
                }
            ],
        }
        _write_if_needed(agents_dir / "host_matrix.json", json.dumps(host_matrix_template, ensure_ascii=False, indent=2) + "\n")

        reg_fn = getattr(self_evolve, "register_workspace", None)
        if reg_fn is not None:
            reg_fn(repo)

    return created, skipped
