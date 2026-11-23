#!/usr/bin/env python3
"""Universal Dynamic Timeless Build-Error Learner (Dynamic Fractal Planner v2.6.0).

Ingests build/CI/commit errors from:
  - CI diagnostics JSON (`--from-ci-json <file>` or `--fetch-live-ci`)
  - Local build/compiler/checker logs (`--from-log <file|->`)
  - Explicit structural lesson recording (`--record ...`)

Enforces two strict invariants across any language/stack:
  1. Suitability filter: ignores transient/environmental/noise failures and deduplicates
     against existing lessons in `.agents/docs/BUILD_LESSONS.md`.
  2. Anti-obsolescence (version-agnostic) enforcement: rejects or abstracts ephemeral
     target versions (OS/API numbers, SDK/TFM versions, Action @vN pins, JDK/NDK/package
     versions, calendar dates) and requires a dynamic live-lookup instruction instead.

Zero third-party dependencies (Python stdlib only).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REPO = ""
DEFAULT_BRANCH = "diagnostics"
DEFAULT_CI_JSON_PATH = "ci/latest-build-diagnostics.json"
DEFAULT_LESSONS_REL = ".agents/docs/BUILD_LESSONS.md"
USER_AGENT = "DynamicFractalPlanner-ErrorLearner/2.6"

AUTO_START_MARKER = "<!-- AUTO-LEARNED-LESSONS:START -->"
AUTO_END_MARKER = "<!-- AUTO-LEARNED-LESSONS:END -->"
EMPTY_AUTO_PLACEHOLDER = "(No new uncatalogued structural lessons pending.)"

# Multi-ecosystem diagnostic extraction regexes
DIAG_LINE_RE = re.compile(
    r"(?::\s*(?:fatal\s+)?(?:error|warning)\b|\b(?:error|warning)\s+(?:[A-Z]{2,6}\d{3,5}|TS\d{4}|E\d{4})\b|::(?:error|warning)::)",
    re.IGNORECASE,
)
DIAG_CODE_RE = re.compile(r"\b(?:error|warning)\s+([A-Z]{2,6}\d{3,5}|TS\d{4}|E\d{4})\b", re.IGNORECASE)
TS_CODE_RE = re.compile(r"\berror\s+(TS\d{4})\s*:", re.IGNORECASE)
RUST_CODE_RE = re.compile(r"\berror\[(E\d{4})\]")
CLANG_WARN_RE = re.compile(r"\[-W([A-Za-z0-9_-]+)\]")
PY_CODE_RE = re.compile(r"\b([A-Z]\d{3,4})\b.*(?:ruff|flake8|mypy|pylint)", re.IGNORECASE)
ACTION_DEP_RE = re.compile(
    r"(?:Node\.js\s+\d+\s+actions\s+are\s+deprecated|is\s+deprecated.*actions/|actions/[A-Za-z0-9_.-]+@v\d+)",
    re.IGNORECASE,
)

# Transient / environmental / non-structural failures that MUST NOT pollute build docs
TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bHTTP\s*(?:429|500|502|503|504)\b", re.I),
    re.compile(r"\b(?:timed?\s*out|ETIMEDOUT|ECONNRESET|ECONNREFUSED|ENOTFOUND|Temporary failure in name resolution)\b", re.I),
    re.compile(r"\b(?:Unable to load the service index|NU1301|The remote name could not be resolved)\b", re.I),
    re.compile(r"\b(?:MSB3021|MSB3027|being used by another process|No space left on device|runner has received a shutdown signal)\b", re.I),
    re.compile(r"\b(?:The operation was canceled|rate limit exceeded|secondary rate limit|EAI_AGAIN)\b", re.I),
)

TRANSIENT_CODES = frozenset({"NU1301", "MSB3021", "MSB3027", "MSB4014", "GENERAL"})

# Anti-obsolescence (version-agnostic) violation patterns for rule/lookup text.
# Allows normative standards (ISO 226, ITU-R BS.1770, 16 KB ELF, RFC numbers,
# C# 8.0 for netstandard2.1, diagnostic codes like CA1416/CS0104/TS2322, and placeholders N/X.Y).
DATED_VERSION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "hardcoded Android OS version",
        re.compile(r"\bAndroid\s+(?:1[0-9]|2[0-9]|[3-9]\d)(?:\.\d+)?\b", re.I),
    ),
    (
        "hardcoded Android API level number",
        re.compile(r"\b(?:API[\s-]*|android)(?:2[1-9]|3[0-9]|4[0-9])(?:\.0)?\b", re.I),
    ),
    (
        "hardcoded .NET Android TFM version",
        re.compile(r"\bnet(?:6|7|8|9|1[0-9]|2[0-9])\.0-android\b", re.I),
    ),
    (
        "hardcoded .NET SDK major version",
        re.compile(r"\.NET\s+(?:6|7|8|9|1[0-9]|2[0-9])(?:\.\d+)?\b", re.I),
    ),
    (
        "hardcoded GitHub Action @vN version",
        re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@v\d+(?:\.\d+)*\b"),
    ),
    (
        "hardcoded JDK/Java version",
        re.compile(r"\b(?:Java|JDK)\s+(?:1[1-9]|2[0-9]|3[0-9])\b", re.I),
    ),
    (
        "hardcoded runner OS version",
        re.compile(r"\bubuntu-(?:2[0-9]|3[0-9])\.\d+\b", re.I),
    ),
    (
        "hardcoded third-party package version",
        re.compile(
            r"\b(?:libusb|onnxruntime|ffmpeg|truehdd|roslyn|ndk|node|python|typescript|gradle|kotlin)[\s/-]+v?(?:r?\d+\.\d+(?:\.\d+)*|r\d{2}[a-z]?)\b",
            re.I,
        ),
    ),
    (
        "hardcoded calendar year/date",
        re.compile(r"\b(?:as of|since|in|currently)\s+20[2-9]\d\b|\b20[2-9]\d-\d{2}-\d{2}\b", re.I),
    ),
)

# Version abstraction rules applied when auto-synthesizing from raw compiler/CI logs
SANITIZE_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"'Android'\s+\d+(?:\.\d+)?\s+and\s+later", re.I), "current target OS API"),
    (re.compile(r"'android'\s+\d+(?:\.\d+)?\s+and\s+later", re.I), "newer OS API level"),
    (re.compile(r"\$\(TargetPlatformVersion\)\s*'[^']+'"), "$(TargetPlatformVersion)"),
    (re.compile(r"android:targetSdkVersion\s*'?\d+'?"), "android:targetSdkVersion"),
    (re.compile(r"\bnet\d+\.\d+-android\b", re.I), "<active-android-tfm>"),
    (re.compile(r"\.NET\s+\d+(?:\.\d+)?", re.I), "active .NET SDK"),
    (re.compile(r"\bAndroid\s+(?:API\s+level\s+)?\d+(?:\.\d+)?\b", re.I), "current Android API level"),
    (re.compile(r"\bAPI[-\s]*\d+(?:\.\d+)?\b", re.I), "target API level"),
    (re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@v\d+(?:\.\d+)*\b"), r"\1@<current-major>"),
    (re.compile(r"\bNode\.js\s+\d+\b", re.I), "legacy Actions runtime"),
    (re.compile(r"\b(?:Java|JDK)\s+\d+\b", re.I), "active JDK"),
    (re.compile(r"\bubuntu-\d+\.\d+\b", re.I), "current CI runner OS"),
    (re.compile(r"\b20[2-9]\d-\d{2}-\d{2}\b"), "<date>"),
)

# Multi-stack family-level timeless synthesis templates for unseen diagnostic codes
FAMILY_TEMPLATES: dict[str, dict[str, str]] = {
    "CA": {
        "category": "code-analysis",
        "rule": "Follow Roslyn analyzer contract for `{code}` in the exact execution scope; avoid brittle suppressions unless cross-platform compatibility strictly requires a narrowly scoped `#pragma` w/ rationale.",
        "lookup": "Check current Microsoft .NET code-analysis rule `{code}` docs && live CI diagnostics before choosing the idiomatic fix.",
    },
    "CS": {
        "category": "csharp-compiler",
        "rule": "Resolve `{code}` at root cause: verify exact types, overloads, nullability, language level of target `.csproj`, & namespace qualifications instead of guessing signatures.",
        "lookup": "Inspect exact symbol declarations in repo + current C# compiler reference for `{code}` && validate via local build / CI diagnostics.",
    },
    "XA": {
        "category": "android-packaging",
        "rule": "Align `.NET Android` manifest, packaging items, & ACW/binding settings cleanly w/o duplicating SDK-default items or drifting from project W^X/targetSdk policy.",
        "lookup": "Check live official `.NET Android` build error/warning docs for `{code}` && live CI diagnostics.",
    },
    "APT": {
        "category": "android-packaging",
        "rule": "Ensure Android resources/assets follow `aapt2` naming, encoding, & packaging rules w/o reserved extensions or malformed XML.",
        "lookup": "Verify current Android `aapt2` / resource packaging rules in official Android developer docs && CI build log.",
    },
    "IL": {
        "category": "trimming-aot",
        "rule": "Preserve reflection/native interop metadata cleanly via explicit annotations or compatible linker settings so trimming/AOT never strips required members.",
        "lookup": "Check current `.NET` trimming/AOT diagnostics docs for `{code}` && project publish settings in `.csproj`.",
    },
    "SYSLIB": {
        "category": "dotnet-obsolescence",
        "rule": "Replace obsoleted `.NET` BCL APIs flagged by `{code}` w/ the current non-obsolete standard API rather than suppressing indefinitely.",
        "lookup": "Check live Microsoft Learn `SYSLIB` migration guide for `{code}` against the active project TFM.",
    },
    "NU": {
        "category": "nuget-deps",
        "rule": "Resolve package dependency/version constraints explicitly in project manifest without floating incompatible transitives or unverified feeds.",
        "lookup": "Inspect active package versions & official NuGet `{code}` guidance before updating references.",
    },
    "MSB": {
        "category": "msbuild-pipeline",
        "rule": "Fix MSBuild target/property configuration cleanly in `.csproj` or workflow without relying on deprecated SDK properties.",
        "lookup": "Verify current MSBuild / SDK property docs for `{code}` && inspect active workflow configuration.",
    },
    "NETSDK": {
        "category": "toolchain-ci",
        "rule": "Keep project TFM, workload, & SDK properties aligned w/ the installed build toolchain and current SDK requirements.",
        "lookup": "Verify live official `.NET` SDK docs for `{code}` && active runner SDK output (`dotnet --info`).",
    },
    "TS": {
        "category": "typescript-compiler",
        "rule": "Fix TypeScript `{code}` at the type contract level (narrow unions, align interfaces/generics, avoid unsafe `any` casts).",
        "lookup": "Inspect exact symbol types in repo + official TypeScript reference for `{code}` && run `tsc --noEmit`.",
    },
    "RUST": {
        "category": "rust-compiler",
        "rule": "Resolve Rust `{code}` cleanly respecting ownership, lifetimes, and trait bounds without unnecessary `unsafe`.",
        "lookup": "Run `rustc --explain {code}` && inspect exact trait/lifetime signatures in repo.",
    },
    "CLANG": {
        "category": "native-cc",
        "rule": "Fix native C/C++ compiler warning/error (`{code}`) cleanly under `-Wall -Wextra -Werror` while respecting minimum target ABI availability.",
        "lookup": "Inspect current NDK/Clang/GCC headers & build script configuration; verify via native compiler tests.",
    },
    "ACTIONS": {
        "category": "toolchain-ci",
        "rule": "Keep CI workflow steps on supported runner runtimes and non-deprecated action majors.",
        "lookup": "Check current official CI provider changelog & action repository releases before updating workflow `uses:` entries.",
    },
    "GENERIC": {
        "category": "build-process",
        "rule": "Resolve recurring structural diagnostic `{code}` at its root cause rather than silencing warnings globally.",
        "lookup": "Consult live official toolchain documentation for `{code}` && inspect exact failing build log.",
    },
}


@dataclass
class Lesson:
    lesson_id: str
    title: str
    category: str
    codes: list[str]
    keywords: list[str]
    root_cause: str
    rule: str
    lookup: str
    guard: str = "manual-review"


@dataclass
class DiagnosticItem:
    code: str
    severity: str  # error | warning
    layer: str
    raw_line: str


@dataclass
class LearningResult:
    known_matches: dict[str, list[DiagnosticItem]] = field(default_factory=dict)
    skipped_transient: list[DiagnosticItem] = field(default_factory=list)
    new_candidates: list[Lesson] = field(default_factory=list)
    already_covered_codes: set[str] = field(default_factory=set)


def find_repo_root(start_dir: Path | None = None) -> Path:
    base = (start_dir or Path.cwd()).expanduser().resolve()
    try:
        res = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except Exception:
        pass
    for cand in (base, *base.parents):
        if (cand / ".agents").is_dir() or (cand / ".git").exists():
            return cand
    return base


def detect_github_repo(repo_root: Path) -> str:
    """Detect GitHub owner/repo from planner_profile.json or git remote origin."""
    prof_file = repo_root / ".agents" / "planner_profile.json"
    if prof_file.is_file():
        try:
            prof = json.loads(prof_file.read_text(encoding="utf-8", errors="ignore"))
            for sec in ("error_learning", "diagnostics"):
                cfg = prof.get(sec)
                if isinstance(cfg, dict) and str(cfg.get("repo") or "").strip():
                    return str(cfg["repo"]).strip()
        except Exception:
            pass
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            url = res.stdout.strip()
            m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except Exception:
        pass
    return DEFAULT_REPO


def load_error_learning_config(repo_root: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "repo": "",
        "branch": DEFAULT_BRANCH,
        "ci_json_path": DEFAULT_CI_JSON_PATH,
        "lessons_file": DEFAULT_LESSONS_REL,
    }
    prof_file = repo_root / ".agents" / "planner_profile.json"
    if prof_file.is_file():
        try:
            prof = json.loads(prof_file.read_text(encoding="utf-8", errors="ignore"))
            for sec in ("diagnostics", "error_learning"):
                block = prof.get(sec)
                if isinstance(block, dict):
                    for k in ("repo", "branch", "ci_json_path", "lessons_file"):
                        if block.get(k):
                            cfg[k] = str(block[k]).strip()
        except Exception:
            pass
    if not cfg["repo"]:
        cfg["repo"] = detect_github_repo(repo_root)
    return cfg


def sanitize_timeless(text: str) -> str:
    """Abstract concrete OS/SDK/Action versions from raw diagnostic snippets."""
    cleaned = text or ""
    for pat, repl in SANITIZE_SUBS:
        cleaned = pat.sub(repl, cleaned)
    cleaned = re.sub(r"\[[^\]]+\.(?:csproj|targets)\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def lint_timelessness(text: str) -> list[str]:
    """Return list of anti-obsolescence violations found in a rule/lookup string."""
    violations: list[str] = []
    if not text:
        return violations
    for label, pat in DATED_VERSION_RULES:
        for m in pat.finditer(text):
            violations.append(f"{label}: `{m.group(0)}`")
    return violations


def parse_lessons(md_text: str) -> list[Lesson]:
    """Parse all `### \`BL-NNN\` | ...` entries from BUILD_LESSONS.md."""
    lessons: list[Lesson] = []
    header_re = re.compile(r"^###\s+`(BL-\d{3,})`\s*\|\s*(.+?)\s*$", re.MULTILINE)
    matches = list(header_re.finditer(md_text))
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
        block = md_text[start:end]

        def _field(name: str) -> str:
            fm = re.search(rf"^-\s+`{re.escape(name)}`:\s*(.+)$", block, re.MULTILINE)
            return fm.group(1).strip() if fm else ""

        raw_codes = _field("Codes").strip("`")
        codes = [c.strip().strip("`").upper() for c in raw_codes.split(",") if c.strip()]
        raw_kws = _field("Keywords").strip("`")
        kws = [k.strip().strip("`").lower() for k in raw_kws.split(",") if k.strip()]

        lessons.append(
            Lesson(
                lesson_id=m.group(1).strip(),
                title=m.group(2).strip(),
                category=_field("Category").strip("`"),
                codes=codes,
                keywords=kws,
                root_cause=_field("RootCause"),
                rule=_field("TimelessRule"),
                lookup=_field("DynamicLookup"),
                guard=_field("AutoGuard") or "manual-review",
            )
        )
    return lessons


def next_lesson_id(lessons: list[Lesson]) -> int:
    nums = []
    for l in lessons:
        m = re.match(r"^BL-(\d+)$", l.lesson_id)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def format_lesson_markdown(lesson: Lesson) -> str:
    codes_str = ", ".join(lesson.codes) if lesson.codes else "GENERAL"
    kws_str = ", ".join(lesson.keywords) if lesson.keywords else lesson.category
    return "\n".join(
        [
            f"### `{lesson.lesson_id}` | {lesson.title}",
            f"- `Category`: `{lesson.category}`",
            f"- `Codes`: `{codes_str}`",
            f"- `Keywords`: `{kws_str}`",
            f"- `RootCause`: {lesson.root_cause}",
            f"- `TimelessRule`: {lesson.rule}",
            f"- `DynamicLookup`: {lesson.lookup}",
            f"- `AutoGuard`: {lesson.guard}",
        ]
    )


def audit_lessons_file(path: Path) -> tuple[bool, list[str]]:
    """Validate BUILD_LESSONS.md for duplicates, required fields, and timelessness."""
    errors: list[str] = []
    if not path.is_file():
        return False, [f"Missing lessons file: {path}"]
    text = path.read_text(encoding="utf-8", errors="ignore")
    if AUTO_START_MARKER not in text or AUTO_END_MARKER not in text:
        errors.append("Missing AUTO-LEARNED-LESSONS start/end markers.")

    lessons = parse_lessons(text)
    if not lessons:
        errors.append("No `BL-NNN` lessons parsed.")
        return False, errors

    seen_ids: set[str] = set()
    for l in lessons:
        if l.lesson_id in seen_ids:
            errors.append(f"Duplicate lesson ID `{l.lesson_id}`.")
        seen_ids.add(l.lesson_id)
        if not l.category:
            errors.append(f"{l.lesson_id}: missing Category.")
        if not l.root_cause:
            errors.append(f"{l.lesson_id}: missing RootCause.")
        if not l.rule:
            errors.append(f"{l.lesson_id}: missing TimelessRule.")
        if not l.lookup:
            errors.append(f"{l.lesson_id}: missing DynamicLookup (dynamic verification guidance required).")

        for field_name, val in (
            ("Title", l.title),
            ("RootCause", l.root_cause),
            ("TimelessRule", l.rule),
            ("DynamicLookup", l.lookup),
        ):
            for v in lint_timelessness(val):
                errors.append(f"{l.lesson_id}.{field_name}: dated/version-pinned content -> {v}")

    return len(errors) == 0, errors


def extract_code_from_line(line: str) -> str:
    tm = TS_CODE_RE.search(line)
    if tm:
        return tm.group(1).upper()
    rm = RUST_CODE_RE.search(line)
    if rm:
        return rm.group(1).upper()
    m = DIAG_CODE_RE.search(line)
    if m:
        return m.group(1).upper()
    cm = CLANG_WARN_RE.search(line)
    if cm:
        return "-W" + cm.group(1)
    if ACTION_DEP_RE.search(line):
        return "ACTIONS_DEPRECATED"
    if "cmake" in line.lower() and ("error" in line.lower() or "could not find" in line.lower()):
        return "CMAKE_VAR"
    if "pt_load" in line.lower() or "16 kb" in line.lower():
        return "ELF_ALIGN"
    pm = PY_CODE_RE.search(line)
    if pm:
        return pm.group(1).upper()
    return "GENERAL"


def is_transient_diagnostic(item: DiagnosticItem) -> bool:
    if item.code.upper() in TRANSIENT_CODES and not ACTION_DEP_RE.search(item.raw_line):
        return True
    for pat in TRANSIENT_PATTERNS:
        if pat.search(item.raw_line):
            return True
    return False


def match_existing_lesson(item: DiagnosticItem, lessons: list[Lesson]) -> Lesson | None:
    code_up = item.code.upper()
    raw_low = item.raw_line.lower()

    code_hits = [l for l in lessons if code_up in l.codes]
    if len(code_hits) == 1:
        return code_hits[0]
    if len(code_hits) > 1:
        return max(code_hits, key=lambda l: sum(1 for kw in l.keywords if kw in raw_low))

    for l in lessons:
        for lc in l.codes:
            if lc == "SYSLIB" and code_up.startswith("SYSLIB"):
                return l
            if lc.startswith("-W") and code_up.startswith("-W"):
                return l

    best_kw_lesson: Lesson | None = None
    best_kw_score = 0
    for l in lessons:
        score = sum(1 for kw in l.keywords if len(kw) >= 4 and kw in raw_low)
        if score > best_kw_score:
            best_kw_score = score
            best_kw_lesson = l
    if best_kw_score >= 2:
        return best_kw_lesson

    return None


def _pick_family(code: str) -> str:
    up = code.upper()
    if up.startswith("-W"):
        return "CLANG"
    if up == "ACTIONS_DEPRECATED":
        return "ACTIONS"
    if re.match(r"^TS\d{4}$", up):
        return "TS"
    if re.match(r"^E\d{4}$", up):
        return "RUST"
    for pref in ("NETSDK", "SYSLIB", "MSB", "APT", "CA", "CS", "XA", "IL", "NU"):
        if up.startswith(pref):
            return pref
    return "GENERIC"


def _summarize_sample_reason(samples: list[DiagnosticItem]) -> str:
    if not samples:
        return "Recurring compiler/build diagnostic observed during verification."
    raw = samples[0].raw_line
    m = re.search(r"(?:error|warning)\s+(?:[A-Z]{2,6}\d{3,5}|TS\d{4}|E\d{4})\s*:\s*(.+)$", raw, re.I)
    snippet = m.group(1) if m else raw
    snippet = sanitize_timeless(snippet)
    if len(snippet) > 160:
        snippet = snippet[:157].rstrip() + "..."
    return snippet or "Structural build diagnostic encountered during compilation/packaging."


def synthesize_timeless_lesson(code: str, samples: list[DiagnosticItem], num_id: int) -> Lesson | None:
    fam = _pick_family(code)
    tpl = FAMILY_TEMPLATES.get(fam, FAMILY_TEMPLATES["GENERIC"])
    layers = sorted({s.layer for s in samples if s.layer})
    layer_tag = "/".join(layers[:2]) if layers else "build"
    sev = "error" if any(s.severity == "error" for s in samples) else "warning"

    reason_snippet = _summarize_sample_reason(samples)
    root_cause = sanitize_timeless(f"Recurring {sev} `{code}` ({layer_tag}): {reason_snippet}")
    rule = sanitize_timeless(tpl["rule"].format(code=code))
    lookup = sanitize_timeless(tpl["lookup"].format(code=code))

    for fld in (root_cause, rule, lookup):
        if lint_timelessness(fld):
            return None

    return Lesson(
        lesson_id=f"BL-{num_id:03d}",
        title=f"Auto-Learned {fam} Diagnostic `{code}`",
        category=tpl["category"],
        codes=[code.upper()],
        keywords=[code.lower(), tpl["category"]],
        root_cause=root_cause,
        rule=rule,
        lookup=lookup,
        guard=f"CI diagnostics (`{code}`) -> evaluate adding `diff_rule` if recurring",
    )


def load_diagnostics_from_ci_json(data: dict[str, Any]) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    for layer, lines in (data.get("errorsByLayer") or {}).items():
        for ln in lines or []:
            items.append(DiagnosticItem(code=extract_code_from_line(str(ln)), severity="error", layer=str(layer), raw_line=str(ln)))
    for layer, lines in (data.get("warningsByLayer") or {}).items():
        for ln in lines or []:
            items.append(DiagnosticItem(code=extract_code_from_line(str(ln)), severity="warning", layer=str(layer), raw_line=str(ln)))
    return items


def load_diagnostics_from_log_text(text: str, layer: str = "local-build") -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or re.match(r"^\d+\s+(?:Warning|Error)\(s\)$", ln, re.I):
            continue
        if not DIAG_LINE_RE.search(ln) and not ACTION_DEP_RE.search(ln) and not RUST_CODE_RE.search(ln):
            continue
        norm = re.sub(r"\s+", " ", ln)
        if norm in seen:
            continue
        seen.add(norm)
        sev = "error" if re.search(r"(?::\s*(?:fatal\s+)?error\b|\berror\s+(?:[A-Z]{2,6}\d{3,5}|TS\d{4})\b|\berror\[E\d{4}\]|::error::)", ln, re.I) else "warning"
        items.append(DiagnosticItem(code=extract_code_from_line(ln), severity=sev, layer=layer, raw_line=ln))
    return items


def fetch_live_ci_json(repo: str, branch: str, ci_json_path: str = DEFAULT_CI_JSON_PATH) -> dict[str, Any]:
    if not repo:
        raise ValueError("Missing GitHub owner/repo (pass --repo or set error_learning.repo in planner_profile.json).")
    url = f"https://api.github.com/repos/{repo}/contents/{ci_json_path.lstrip('/')}?ref={branch}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def analyze_diagnostics(items: list[DiagnosticItem], existing_lessons: list[Lesson]) -> LearningResult:
    res = LearningResult()
    unmatched_by_code: dict[str, list[DiagnosticItem]] = collections.defaultdict(list)

    for item in items:
        if is_transient_diagnostic(item):
            res.skipped_transient.append(item)
            continue
        matched = match_existing_lesson(item, existing_lessons)
        if matched is not None:
            res.known_matches.setdefault(matched.lesson_id, []).append(item)
            res.already_covered_codes.add(item.code.upper())
            continue
        unmatched_by_code[item.code.upper()].append(item)

    next_num = next_lesson_id(existing_lessons)
    for code in sorted(unmatched_by_code):
        samples = unmatched_by_code[code]
        lesson = synthesize_timeless_lesson(code, samples, next_num)
        if lesson is not None:
            res.new_candidates.append(lesson)
            next_num += 1
        else:
            res.skipped_transient.extend(samples)

    return res


def apply_new_lessons(lessons_path: Path, new_lessons: list[Lesson]) -> tuple[bool, str]:
    """Idempotently append validated timeless lessons into the managed section of BUILD_LESSONS.md."""
    if not new_lessons:
        return False, "No new structural lessons to add."
    text = lessons_path.read_text(encoding="utf-8")
    if AUTO_START_MARKER not in text or AUTO_END_MARKER not in text:
        raise RuntimeError(f"Missing `{AUTO_START_MARKER}` / `{AUTO_END_MARKER}` markers in {lessons_path}")

    existing = parse_lessons(text)
    existing_codes = {c for l in existing for c in l.codes}
    existing_ids = {l.lesson_id for l in existing}

    to_add: list[Lesson] = []
    next_num = next_lesson_id(existing)
    for cand in new_lessons:
        if cand.codes and all(c in existing_codes for c in cand.codes):
            continue
        if cand.lesson_id in existing_ids:
            cand = Lesson(
                lesson_id=f"BL-{next_num:03d}",
                title=cand.title,
                category=cand.category,
                codes=cand.codes,
                keywords=cand.keywords,
                root_cause=cand.root_cause,
                rule=cand.rule,
                lookup=cand.lookup,
                guard=cand.guard,
            )
            next_num += 1
        for fld_name, val in (("Title", cand.title), ("RootCause", cand.root_cause), ("TimelessRule", cand.rule), ("DynamicLookup", cand.lookup)):
            v = lint_timelessness(val)
            if v:
                raise ValueError(f"Refusing dated lesson `{cand.lesson_id}` ({fld_name}): {'; '.join(v)}")
        to_add.append(cand)
        existing_ids.add(cand.lesson_id)
        existing_codes.update(cand.codes)

    if not to_add:
        return False, "All candidate lessons already covered."

    before, rest = text.split(AUTO_START_MARKER, 1)
    middle, after = rest.split(AUTO_END_MARKER, 1)
    mid_clean = middle.strip()
    is_placeholder = (
        not mid_clean
        or mid_clean == EMPTY_AUTO_PLACEHOLDER
        or (mid_clean.startswith("(No new uncatalogued structural lessons") and mid_clean.endswith(")"))
    )
    if is_placeholder:
        chunks = [format_lesson_markdown(l) for l in to_add]
    else:
        chunks = [mid_clean] + [format_lesson_markdown(l) for l in to_add]

    updated = f"{before}{AUTO_START_MARKER}\n\n" + "\n\n".join(chunks) + f"\n\n{AUTO_END_MARKER}{after}"
    lessons_path.write_text(updated, encoding="utf-8")

    ok, audit_errs = audit_lessons_file(lessons_path)
    if not ok:
        lessons_path.write_text(text, encoding="utf-8")
        raise RuntimeError("Post-write timelessness audit failed; rolled back:\n" + "\n".join(audit_errs))

    ids = ", ".join(l.lesson_id for l in to_add)
    return True, f"Added {len(to_add)} timeless lesson(s) to {lessons_path.name}: {ids}"


def render_report_markdown(res: LearningResult, source_label: str, apply_hint: str = "") -> str:
    hint = apply_hint or "python3 -B .agents/scripts/error_learner.py --fetch-live-ci --apply"
    lines = [
        "# Dynamic Build Error Learner Report",
        "",
        f"- **Source:** `{source_label}`",
        f"- **Matched Existing Lessons:** `{sum(len(v) for v in res.known_matches.values())}` diagnostic(s) across `{len(res.known_matches)}` lesson(s)",
        f"- **Skipped Transient/Noise:** `{len(res.skipped_transient)}`",
        f"- **New Timeless Candidates:** `{len(res.new_candidates)}`",
        "",
    ]
    if res.known_matches:
        lines.append("## Matched Existing Lessons (Already Catalogued)")
        for lid in sorted(res.known_matches):
            items = res.known_matches[lid]
            codes = sorted({it.code for it in items})
            lines.append(f"- `{lid}`: {len(items)} occurrence(s) (`{', '.join(codes)}`)")
        lines.append("")

    if res.new_candidates:
        lines.append("## New Timeless Lesson Candidates (Ready for `--apply`)")
        lines.append(f"Run `{hint}` to merge into `.agents/docs/BUILD_LESSONS.md`.")
        lines.append("")
        for cand in res.new_candidates:
            lines.append(format_lesson_markdown(cand))
            lines.append("")
    else:
        lines.append("## New Timeless Lesson Candidates")
        lines.append("Zero uncatalogued structural build lessons detected — `.agents/docs/BUILD_LESSONS.md` is up to date.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(
    argv: list[str] | None = None,
    *,
    default_repo: str | None = None,
    default_branch: str | None = None,
    default_lessons_rel: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Universal dynamic version-agnostic build/commit error learner."
    )
    parser.add_argument("--workspace", help="Project workspace root override")
    parser.add_argument("--lessons-file", help="Path to BUILD_LESSONS.md")
    parser.add_argument("--audit-lessons", action="store_true", help="Audit BUILD_LESSONS.md for timelessness & integrity")
    parser.add_argument("--from-ci-json", help="Ingest CI build-diagnostics.json file")
    parser.add_argument("--fetch-live-ci", action="store_true", help="Fetch CI diagnostics JSON live from GitHub")
    parser.add_argument("--repo", default="", help="GitHub owner/repo (auto-detected from profile/git remote)")
    parser.add_argument("--branch", default="", help="Diagnostics branch (default from profile or 'diagnostics')")
    parser.add_argument("--ci-json-path", default="", help="Path inside diagnostics branch to CI JSON")
    parser.add_argument("--from-log", help="Ingest local compiler/build log file (use '-' for stdin)")
    parser.add_argument("--record", action="store_true", help="Record an explicit timeless structural lesson")
    parser.add_argument("--title", help="Title for --record")
    parser.add_argument("--category", default="build-process", help="Category for --record")
    parser.add_argument("--codes", default="", help="Comma-separated diagnostic codes for --record")
    parser.add_argument("--keywords", default="", help="Comma-separated keywords for --record")
    parser.add_argument("--root-cause", help="Timeless root cause for --record")
    parser.add_argument("--rule", help="Timeless rule for --record")
    parser.add_argument("--lookup", help="Dynamic lookup directive for --record")
    parser.add_argument("--guard", default="manual-review", help="AutoGuard reference for --record")
    parser.add_argument("--apply", action="store_true", help="Write newly learned timeless lessons into BUILD_LESSONS.md")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying BUILD_LESSONS.md")
    parser.add_argument("--report", help="Optional path to write Markdown learner report")
    args = parser.parse_args(argv)

    repo_root = find_repo_root(Path(args.workspace) if args.workspace else None)
    cfg = load_error_learning_config(repo_root)
    gh_repo = args.repo or default_repo or cfg["repo"]
    gh_branch = args.branch or default_branch or cfg["branch"]
    ci_json_path = args.ci_json_path or cfg["ci_json_path"]
    lessons_rel = default_lessons_rel or cfg["lessons_file"] or DEFAULT_LESSONS_REL
    lessons_path = Path(args.lessons_file).expanduser().resolve() if args.lessons_file else (repo_root / lessons_rel)

    if args.audit_lessons:
        ok, errs = audit_lessons_file(lessons_path)
        lessons = parse_lessons(lessons_path.read_text(encoding="utf-8", errors="ignore")) if lessons_path.is_file() else []
        if ok:
            print(f"[error_learner|AUDIT_PASS|lessons={len(lessons)}|timeless=OK|file={lessons_path.name}]")
            return 0
        print(f"[error_learner|AUDIT_FAIL|errors={len(errs)}]", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if not lessons_path.is_file():
        print(f"ERROR: lessons file not found: {lessons_path}", file=sys.stderr)
        return 1

    existing = parse_lessons(lessons_path.read_text(encoding="utf-8"))

    if args.record:
        missing = [k for k in ("title", "root_cause", "rule", "lookup") if not getattr(args, k)]
        if missing:
            print(f"ERROR: --record requires --title, --root-cause, --rule, and --lookup (missing: {', '.join(missing)}).", file=sys.stderr)
            return 2
        for fld_name, val in (
            ("title", args.title),
            ("root_cause", args.root_cause),
            ("rule", args.rule),
            ("lookup", args.lookup),
        ):
            v = lint_timelessness(val)
            if v:
                print(
                    f"TIMELESSNESS_REJECT|{fld_name} contains dated/version-pinned wording: {'; '.join(v)}\n"
                    "Rewrite using version-agnostic principles + dynamic live-lookup guidance.",
                    file=sys.stderr,
                )
                return 1
        lesson = Lesson(
            lesson_id=f"BL-{next_lesson_id(existing):03d}",
            title=args.title.strip(),
            category=args.category.strip(),
            codes=[c.strip().upper() for c in args.codes.split(",") if c.strip()] or ["STRUCTURAL"],
            keywords=[k.strip().lower() for k in args.keywords.split(",") if k.strip()] or [args.category.strip().lower()],
            root_cause=args.root_cause.strip(),
            rule=args.rule.strip(),
            lookup=args.lookup.strip(),
            guard=args.guard.strip() or "manual-review",
        )
        if args.apply and not args.dry_run:
            changed, msg = apply_new_lessons(lessons_path, [lesson])
            print(f"[error_learner|RECORD|changed={changed}] {msg}")
        else:
            print("[error_learner|RECORD_DRY_RUN]\n" + format_lesson_markdown(lesson))
        return 0

    items: list[DiagnosticItem] = []
    source_label = ""
    if args.from_ci_json:
        src = Path(args.from_ci_json).expanduser()
        data = json.loads(src.read_text(encoding="utf-8"))
        items = load_diagnostics_from_ci_json(data)
        source_label = str(src.name)
    elif args.fetch_live_ci:
        try:
            data = fetch_live_ci_json(gh_repo, gh_branch, ci_json_path=ci_json_path)
            items = load_diagnostics_from_ci_json(data)
            source_label = f"{gh_repo}@{gh_branch}:{ci_json_path}"
        except Exception as exc:
            print(f"[error_learner|FETCH_ERR] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    elif args.from_log:
        if args.from_log == "-":
            raw_text = sys.stdin.read()
            source_label = "stdin"
        else:
            p = Path(args.from_log).expanduser()
            raw_text = p.read_text(encoding="utf-8", errors="ignore")
            source_label = p.name
        items = load_diagnostics_from_log_text(raw_text)
    else:
        parser.print_help()
        return 0

    result = analyze_diagnostics(items, existing)
    report_md = render_report_markdown(result, source_label)

    if args.report:
        rp = Path(args.report).expanduser()
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(report_md, encoding="utf-8")

    known_count = sum(len(v) for v in result.known_matches.values())
    print(
        f"[error_learner|src={source_label}|total={len(items)}|known={known_count}"
        f"|skipped={len(result.skipped_transient)}|new={len(result.new_candidates)}]"
    )
    if result.known_matches:
        matched_summary = ", ".join(f"{k}({len(v)})" for k, v in sorted(result.known_matches.items()))
        print(f"  KNOWN: {matched_summary}")

    if args.apply and not args.dry_run:
        changed, msg = apply_new_lessons(lessons_path, result.new_candidates)
        print(f"  APPLY: changed={changed} | {msg}")
    elif result.new_candidates:
        print("  NEW_CANDIDATES (dry-run; pass --apply to record):")
        for cand in result.new_candidates:
            print(f"    - {cand.lesson_id} | {cand.title} | codes={','.join(cand.codes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
