#!/usr/bin/env python3
"""Universal static verifier, declarative guardrail engine, and build dispatcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = SCRIPTS_DIR.parent / "hooks"
for p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from log_digest import extract_unique_diagnostics  # noqa: E402
from planner_state import (  # noqa: E402
    DEFAULT_EXTRA_REQUESTS_FILE,
    eval_conditions,
    get_pending_extra_requests,
    is_plugin_path,
    load_project_profile_with_warnings,
    resolve_plugin_root,
    scan_secrets_and_conflicts,
)
from verify_wiring import (  # noqa: E402, F401
    BRACKET_EXTS,
    GENERIC_ORPHAN_NAMES,
    ORPHAN_CHECK_EXTS,
    _SETTING_IGNORE_NAMES,
    _WIRING_CONTROL_WORDS,
    _advance_literal_or_comment,
    _extract_public_symbols,
    _extract_setting_keys,
    _find_matching_delim,
    _get_diff_add_rem,
    _has_external_code_ref,
    _is_ident_ch,
    _is_test_path,
    _scan_brace_if_else,
    _skip_ws_comments,
    _strip_comments_only,
    check_balanced_brackets,
    check_forward_wiring,
    check_orphan_calls,
)

BINARY_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ttf", ".otf", ".apk", ".dll", ".exe", ".so", ".zip", ".gz", ".tar", ".mp3", ".flac", ".mp4", ".mkv", ".wav")


def resolve_repo_root(cli_ws: str | None = None) -> Path:
    if cli_ws:
        return Path(cli_ws).expanduser().resolve()
    env_ws = os.environ.get("PLANNER_WORKSPACE_ROOT", "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    cwd = Path.cwd()
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip())
    except Exception:
        pass
    return cwd


SKIP_DISCOVERY_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "bin", "obj", "dist", "build",
    "__pycache__", ".venv", "venv", ".idea", ".vs", ".gradle", ".cache",
})
DISCOVERY_EXTS = (
    ".cs", ".csproj", ".c", ".cpp", ".h", ".py", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".kt", ".kts", ".java", ".go", ".rs",
    ".json", ".xml", ".yml", ".yaml", ".sh",
)


def discover_workspace_files(repo: Path, profile: dict[str, Any] | None = None) -> list[str]:
    """Enumerate source/config files for `--all` or gitless container environments."""
    prefixes: tuple[str, ...] = ()
    if profile:
        raw_p = profile.get("code_path_prefixes") or []
        prefixes = tuple(str(p).replace("\\", "/").lstrip("./") for p in raw_p if str(p).strip())
    found: list[str] = []
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DISCOVERY_DIRS)
        rel_root = Path(root).relative_to(repo).as_posix()
        for fn in sorted(filenames):
            rel = fn if rel_root == "." else f"{rel_root}/{fn}"
            if rel.startswith(".aistudio/"):
                continue
            low = fn.lower()
            is_bp_md = low.endswith(".md") and (fn.startswith("BP_") or rel_root.endswith("/blueprints") or rel_root == "blueprints")
            if not low.endswith(DISCOVERY_EXTS) and fn != "BUILD_LESSONS.md" and not is_bp_md:
                continue
            if prefixes and not rel.startswith(prefixes) and not rel.startswith(".agents/"):
                continue
            found.append(rel)
    return found


def get_changed_files(repo: Path, profile: dict[str, Any] | None = None, scan_all: bool = False) -> list[str]:
    if scan_all or not (repo / ".git").exists():
        return discover_workspace_files(repo, profile)
    res = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if res.returncode != 0:
        return []
    files: list[str] = []
    for line in res.stdout.splitlines():
        if len(line) < 4:
            continue
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        path_part = path_part.strip('"').replace("\\", "/")
        if path_part:
            files.append(path_part)
    return sorted(set(files))


def get_added_lines(repo: Path, rel_path: str) -> list[tuple[int, str]]:
    if (repo / ".git").exists():
        res = subprocess.run(
            ["git", "-C", str(repo), "diff", "-U0", "HEAD", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        added: list[tuple[int, str]] = []
        if res.returncode == 0 and res.stdout.strip():
            cur_line = 0
            for ln in res.stdout.splitlines():
                if ln.startswith("@@"):
                    m = re.search(r"\+(\d+)", ln)
                    cur_line = int(m.group(1)) if m else 0
                elif ln.startswith("+") and not ln.startswith("+++"):
                    added.append((cur_line, ln[1:]))
                    cur_line += 1
            return added

        st = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if st.returncode == 0:
            return []

    full = repo / rel_path
    if full.is_file():
        return [(i, ln) for i, ln in enumerate(full.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1)]
    return []


def strip_line_comment(line: str) -> str:
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


def strip_strings(line: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', line)


def check_universal_syntax(repo: Path, files: list[str], fails: list[str]) -> None:
    has_bash = shutil.which("bash") is not None
    for rel in files:
        full = repo / rel
        if not full.is_file():
            continue
        if rel.endswith(".py"):
            try:
                py_compile.compile(str(full), doraise=True)
            except py_compile.PyCompileError as exc:
                fails.append(f"{rel}: Python syntax error: {exc.msg}")
        elif rel.endswith(".json"):
            try:
                json.loads(full.read_text(encoding="utf-8"))
            except Exception as exc:
                fails.append(f"{rel}: Invalid JSON: {exc}")
        elif rel.endswith(".sh") and has_bash:
            res = subprocess.run(["bash", "-n", str(full)], capture_output=True, text=True, timeout=5, check=False)
            if res.returncode != 0:
                fails.append(f"{rel}: Shell syntax error: {res.stderr.strip()}")


_CS_STMT_INDEX_ASSIGN_COMMA_RE = re.compile(
    r'^\s*[A-Za-z_][A-Za-z0-9_.]*\[[^\]]+\]\s*=\s*(?:\$?"(?:\\.|[^"\\])*"|\'[^\']*\'|[^;{}()]+),\s*$'
)


def check_csharp_syntax_deep(repo: Path, files: list[str], fails: list[str], warns: list[str]) -> None:
    """Fast Roslyn-free C# syntax heuristics catching high-frequency CI breaks (e.g. statement trailing comma CS1002)."""
    for rel in files:
        if not rel.lower().endswith(".cs"):
            continue
        full = repo / rel
        if not full.is_file():
            continue
        lines = full.read_text(encoding="utf-8", errors="ignore").splitlines()
        in_block_comment = False
        in_raw_str = False
        for idx, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if '"""' in stripped:
                if stripped.count('"""') % 2 == 1:
                    in_raw_str = not in_raw_str
                continue
            if in_raw_str:
                continue
            if in_block_comment:
                if "*/" in raw:
                    in_block_comment = False
                continue
            if "/*" in stripped and "*/" not in stripped:
                in_block_comment = True
                continue
            code = strip_line_comment(raw).rstrip()
            if not code.strip():
                continue
            # Catch dictionary/indexer assignment statements terminated with `,` instead of `;`
            # (valid ONLY inside `new() { ["k"] = "v", }` where LHS has NO target identifier before `[`).
            if _CS_STMT_INDEX_ASSIGN_COMMA_RE.match(code):
                prev_code = ""
                for back in range(idx - 2, max(-1, idx - 6), -1):
                    pc = strip_line_comment(lines[back]).strip()
                    if pc:
                        prev_code = pc
                        break
                if prev_code.endswith((";", "{", "}")) and not prev_code.endswith(("=> new() {", "new {", "new() {")):
                    fails.append(
                        f"{rel}:{idx}: C# statement indexer assignment ends with `,` instead of `;` (likely CS1002/CS1513)."
                    )


def check_file_guards_state(repo: Path, profile: dict[str, Any], fails: list[str], warns: list[str]) -> None:
    for g in profile.get("file_guards") or []:
        if not eval_conditions(g.get("conditions"), repo):
            continue
        rel = (g.get("path_suffix") or "").lstrip("./")
        must_pat = g.get("must_contain_regex") or ""
        if not rel or not must_pat:
            continue
        target = repo / rel
        if not target.is_file():
            continue
        txt = target.read_text(encoding="utf-8", errors="ignore")
        if not re.search(must_pat, txt):
            msg = f"{rel}: {g.get('reason') or 'File guard invariant violated.'}"
            if g.get("verify_severity") == "warn":
                warns.append(msg)
            else:
                fails.append(msg)


def check_version_sync(repo: Path, profile: dict[str, Any], warns: list[str]) -> None:
    vfiles = (profile.get("session") or {}).get("version_files") or []
    sync_vals: list[tuple[str, str]] = []
    for vf in vfiles:
        if not vf.get("sync_prefix"):
            continue
        p = repo / (vf.get("path") or "")
        pat = vf.get("regex") or ""
        if p.is_file() and pat:
            m = re.search(pat, p.read_text(encoding="utf-8", errors="ignore"))
            if m:
                sync_vals.append((vf.get("label") or vf.get("path"), m.group(1).strip()))
    if len(sync_vals) >= 2:
        prefixes = {lbl: ".".join(v.split(".")[:2]) for lbl, v in sync_vals}
        if len(set(prefixes.values())) > 1:
            desc = ", ".join(f"{lbl}={v}" for lbl, v in sync_vals)
            warns.append(f"Version prefix mismatch: {desc}")


def _rule_matches_path(rule: dict[str, Any], rel: str) -> bool:
    prefs: list[str] = []
    if rule.get("path_prefix"):
        prefs.append(str(rule["path_prefix"]).replace("\\", "/").lstrip("./"))
    for p in rule.get("path_prefixes") or []:
        ps = str(p).replace("\\", "/").lstrip("./")
        if ps:
            prefs.append(ps)
    if prefs and not any(rel.startswith(p) for p in prefs):
        return False
    preg = rule.get("path_regex") or ""
    if preg and not re.search(preg, rel):
        return False
    return True


def check_diff_rules(repo: Path, files: list[str], profile: dict[str, Any], fails: list[str], warns: list[str]) -> None:
    rules = profile.get("diff_rules") or []
    if not rules:
        return

    for rel in files:
        matching_rules = [r for r in rules if _rule_matches_path(r, rel)]
        if not matching_rules:
            continue

        added = get_added_lines(repo, rel)
        if not added:
            continue
        full = repo / rel
        all_lines = full.read_text(encoding="utf-8", errors="ignore").splitlines() if full.is_file() else []

        for r in matching_rules:
            sev = (r.get("severity") or "fail").lower()
            bucket = warns if sev == "warn" else fails
            msg_tpl = r.get("message") or "Diff rule violation."
            strip_comm = r.get("strip_line_comments", True)
            strip_q = r.get("strip_strings", True)

            for line_no, raw in added:
                code = strip_line_comment(raw) if strip_comm else raw
                if not code.strip():
                    continue
                target_txt = strip_strings(code) if strip_q else code

                forbid = r.get("forbid_regex") or ""
                if forbid and re.search(forbid, target_txt):
                    bucket.append(f"{rel}:{line_no}: {msg_tpl}")

                trig = r.get("trigger_regex") or ""
                if trig and re.search(trig, target_txt) and all_lines:
                    before = int(r.get("window_before", 5))
                    after = int(r.get("window_after", 13))
                    start = max(0, line_no - 1 - before)
                    end = min(len(all_lines), line_no + after)
                    window = "\n".join(all_lines[start:end])
                    req_if = r.get("require_if_any_regex") or ""
                    must_win = r.get("must_match_window_regex") or ""
                    if (not req_if or re.search(req_if, window)) and must_win and not re.search(must_win, window):
                        bucket.append(f"{rel}:{line_no}: {msg_tpl}")


def check_key_parity_rules(repo: Path, files: list[str], profile: dict[str, Any], fails: list[str], warns: list[str]) -> None:
    for r in profile.get("key_parity_rules") or []:
        rel = (r.get("path") or "").replace("\\", "/").lstrip("./")
        if not rel:
            continue
        full = repo / rel
        if not full.is_file():
            continue
        text = full.read_text(encoding="utf-8", errors="ignore")
        target_rel = (r.get("target_path") or "").replace("\\", "/").lstrip("./") or rel
        target_full = repo / target_rel
        target_text = target_full.read_text(encoding="utf-8", errors="ignore") if target_full.is_file() else text

        if rel in files or target_rel in files:
            sec_pat = r.get("target_section_regex") or ""
            m_sec = re.search(sec_pat, target_text, flags=re.MULTILINE | re.DOTALL) if target_text else None
            if not m_sec:
                fails.append(f"{target_rel}: Could not locate target section for key parity rule.")
            else:
                target_block = m_sec.group(1) if m_sec.groups() else m_sec.group(0)
                key_pat = r.get("added_key_regex") or ""
                tpl = r.get("target_key_template") or "{key}"
                sev = (r.get("severity") or "fail").lower()
                bucket = warns if sev == "warn" else fails
                msg_tpl = r.get("message") or "Added key `{key}` missing from target locale/block."

                for line_no, raw in get_added_lines(repo, rel):
                    m_k = re.search(key_pat, raw)
                    if not m_k:
                        continue
                    key = m_k.group(1)
                    needle = tpl.replace("{key}", key)
                    if needle not in target_block:
                        bucket.append(f"{rel}:{line_no}: {msg_tpl.replace('{key}', key)}")

        ref_pat = r.get("ref_regex") or ""
        if not ref_pat:
            continue
        src_sec_pat = r.get("source_section_regex") or ""
        src_block = text
        if src_sec_pat:
            m_src = re.search(src_sec_pat, text, flags=re.MULTILINE | re.DOTALL)
            if m_src:
                src_block = m_src.group(1) if m_src.groups() else m_src.group(0)
        src_key_pat = r.get("source_key_regex") or r.get("added_key_regex") or ""
        if not src_key_pat:
            continue
        known_keys = {m.group(1) for m in re.finditer(src_key_pat, src_block, flags=re.MULTILINE) if m.groups()}

        ref_prefs = [str(p).replace("\\", "/").lstrip("./") for p in (r.get("ref_path_prefixes") or []) if str(p).strip()]
        ref_preg = r.get("ref_path_regex") or ""
        ref_sev = (r.get("ref_severity") or r.get("severity") or "fail").lower()
        ref_bucket = warns if ref_sev == "warn" else fails
        ref_msg_tpl = r.get("ref_message") or "Referenced key `{key}` missing from `{table}`."
        strip_comm = r.get("strip_line_comments", True)
        skip_rels = {rel, target_rel}

        for f_rel in files:
            if f_rel in skip_rels or not (repo / f_rel).is_file():
                continue
            if ref_prefs and not any(f_rel.startswith(p) for p in ref_prefs):
                continue
            if ref_preg and not re.search(ref_preg, f_rel):
                continue
            if not ref_prefs and not ref_preg and Path(f_rel).suffix.lower() not in ORPHAN_CHECK_EXTS:
                continue
            for line_no, raw in get_added_lines(repo, f_rel):
                code = strip_line_comment(raw) if strip_comm else raw
                if not code.strip():
                    continue
                for m_ref in re.finditer(ref_pat, code):
                    key = m_ref.group(1) if m_ref.groups() else m_ref.group(0)
                    if not key or any(c in key for c in ("{", "$", "+")):
                        continue
                    if key not in known_keys:
                        msg = ref_msg_tpl.replace("{key}", key).replace("{table}", rel)
                        ref_bucket.append(f"{f_rel}:{line_no}: {msg}")


def run_build_rules(
    repo: Path,
    changed: list[str],
    profile: dict[str, Any],
    force_build: bool,
    fails: list[str],
    build_requested: bool = True,
) -> bool:
    rules = profile.get("build_rules") or []
    if not rules:
        return False

    ran_any = False
    seen_cmds: set[str] = set()
    upstream_failed = False
    cascade_halted = False
    global_seen: set[str] = set()
    total_dups = 0
    layer_statuses: list[tuple[str, str, int]] = []

    for r in rules:
        if not eval_conditions(r.get("conditions"), repo):
            continue
        run_without_build = bool(r.get("run_without_build"))
        if run_without_build and build_requested:
            continue
        if not run_without_build and not build_requested:
            continue

        exts = tuple(e.lower() for e in (r.get("trigger_extensions") or []))
        prefs = tuple((p.replace("\\", "/") for p in (r.get("trigger_path_prefixes") or [])))
        shadow_cmd = (r.get("on_upstream_fail_command") or "").strip()
        use_shadow = bool(
            upstream_failed
            and shadow_cmd
            and build_requested
            and (r.get("trigger_on_upstream_fail", True) is not False)
        )

        if not use_shadow:
            if run_without_build:
                matched = True
                if changed and (exts or prefs):
                    matched = any(
                        (not exts or f.lower().endswith(exts)) and (not prefs or f.startswith(prefs))
                        for f in changed
                    )
            else:
                matched = force_build and bool(r.get("run_on_force", True))
                if not matched:
                    for f in changed:
                        if exts and not f.lower().endswith(exts):
                            continue
                        if prefs and not f.startswith(prefs):
                            continue
                        matched = True
                        break
            if not matched:
                continue

        cmd = shadow_cmd if use_shadow else (r.get("command") or "").strip()
        label = r.get("label") or cmd
        if cascade_halted and not use_shadow:
            layer_statuses.append((label, "SKIPPED_UPSTREAM", 0))
            print(f"Skipping `{label}` (upstream failed).")
            continue

        if not cmd or cmd in seen_cmds:
            continue
        seen_cmds.add(cmd)
        ran_any = True

        env = os.environ.copy()
        for k, v in (r.get("env") or {}).items():
            env[str(k)] = str(v)

        print(f"Building `{label}`" + (" [shadow]" if use_shadow else "") + "...")
        res = subprocess.run(cmd, shell=True, cwd=str(repo), env=env, capture_output=True, text=True, timeout=240, check=False)
        if res.returncode != 0:
            upstream_failed = True
            if r.get("continue_on_error") is False:
                cascade_halted = True
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            uniq, dups = extract_unique_diagnostics(combined, global_seen=global_seen)
            total_dups += dups
            st = "SHADOW_FAIL" if use_shadow else "FAIL"
            layer_statuses.append((label, st, len(uniq)))
            if uniq:
                preview = "\n".join(uniq[:25])
                extra = f"\n...(+{len(uniq) - 25} more unique)" if len(uniq) > 25 else ""
                fails.append(f"Build `{label}` failed (exit {res.returncode}, {len(uniq)} unique error(s)):\n{preview}{extra}")
            elif dups > 0:
                fails.append(f"Build `{label}` failed (exit {res.returncode}; {dups} duplicate error(s) already reported upstream).")
            else:
                tail = "\n".join(combined.splitlines()[-25:])
                fails.append(f"Build `{label}` failed (exit {res.returncode}):\n{tail}")
        else:
            st = "SHADOW_PASS" if use_shadow else "PASS"
            layer_statuses.append((label, st, 0))
            print(f"  [OK] {label}" + (" [shadow]" if use_shadow else ""))

    if ran_any and layer_statuses:
        l_parts = [f"{lbl}:{st}({cnt})" if cnt else f"{lbl}:{st}" for lbl, st, cnt in layer_statuses]
        print(f"BUILD_SUMMARY|layers={','.join(l_parts)}|unique_errors={len(global_seen)}|deduped={total_dups}")
        if upstream_failed:
            el_cmd = f"python3 -B {SCRIPTS_DIR / 'plan_tool.py'} --workspace {repo} learn --from-log - --apply"
            print(f"LEARN_HINT|new recurring build error? pipe output -> `{el_cmd}`")
    return ran_any


def run_legacy_verify_command(repo: Path, profile: dict[str, Any], build: bool, fails: list[str]) -> bool:
    if profile.get("build_rules") or profile.get("diff_rules") or profile.get("key_parity_rules"):
        # Project already uses declarative rules; only invoke extra verify_command if explicitly marked always
        if not profile.get("always_run_verify_command"):
            return False
    cmd = (profile.get("verify_command") or "").strip() if build else (
        profile.get("verify_non_code_command") or profile.get("verify_command") or ""
    ).strip()
    if not cmd:
        return False
    print(f"Running project verifier: `{cmd}`")
    res = subprocess.run(cmd, shell=True, cwd=str(repo), capture_output=True, text=True, timeout=240, check=False)
    if res.stdout.strip():
        print(res.stdout.rstrip())
    if res.stderr.strip():
        print(res.stderr.rstrip(), file=sys.stderr)
    if res.returncode != 0:
        fails.append(f"Project verifier `{cmd}` failed (exit {res.returncode}).")
    return True


def check_secrets_and_conflicts_files(repo: Path, files: list[str], profile: dict[str, Any], fails: list[str]) -> None:
    sg = profile.get("secret_guard") or {}
    if sg.get("enabled") is False:
        return
    for rel in files:
        if rel.lower().endswith(BINARY_EXTS):
            continue
        full = repo / rel
        if not full.is_file():
            continue
        txt = full.read_text(encoding="utf-8", errors="ignore")
        for hit in scan_secrets_and_conflicts(txt, rel_path=rel, check_filename=True):
            fails.append(f"{rel}: {hit}")


def check_memory_weight(repo: Path, profile: dict[str, Any], warns: list[str]) -> None:
    mw = profile.get("memory_weight_limits") if isinstance(profile.get("memory_weight_limits"), dict) else {}
    hist_max_lines = int(mw.get("project_history_max_lines") or 150)
    hist_max_kb = float(mw.get("project_history_max_kb") or 28.0)
    sync_max_kb = float(mw.get("external_sync_max_kb") or 48.0)

    hist = repo / ".agents" / "docs" / "PROJECT_HISTORY.md"
    if hist.is_file():
        hist_txt = hist.read_text(encoding="utf-8", errors="ignore")
        n_lines = len(hist_txt.splitlines())
        hist_kb = len(hist_txt.encode("utf-8")) / 1024.0
        if n_lines > hist_max_lines:
            warns.append(f"`.agents/docs/PROJECT_HISTORY.md` has {n_lines}L (>{hist_max_lines}L); archive older milestones to `.agents/docs/archive/`.")
        elif hist_kb > hist_max_kb:
            warns.append(f"`.agents/docs/PROJECT_HISTORY.md` is {hist_kb:.1f} KB (>{hist_max_kb:.0f} KB despite {n_lines}L); archive older milestones to `.agents/docs/archive/`.")

    sync_doc = repo / ".agents" / "docs" / "EXTERNAL_AI_SYNC.md"
    if sync_doc.is_file():
        sync_kb = sync_doc.stat().st_size / 1024.0
        if sync_kb > sync_max_kb:
            warns.append(f"`.agents/docs/EXTERNAL_AI_SYNC.md` is {sync_kb:.1f} KB (>{sync_max_kb:.0f} KB); archive older sync entries to `.agents/docs/archive/`.")

    extra_rel = (profile.get("extra_requests_file") or DEFAULT_EXTRA_REQUESTS_FILE).strip()
    extra_p = repo / extra_rel
    if extra_p.is_file():
        txt = extra_p.read_text(encoding="utf-8", errors="ignore")
        m = re.search(
            r"^#{1,3}\s+(?:Completed Requests|Conclu[ií]dos|Completed)(?:\s*:)?[ \t]*\r?\n(.*?)(?=^#{1,3}\s+|\Z)",
            txt,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if m:
            items = [
                ln.strip()
                for ln in m.group(1).splitlines()
                if ln.strip() and not ln.strip().lower().startswith(("(no completed", "(none", "<!--"))
            ]
            if len(items) > 15:
                warns.append(f"`{extra_rel}` has {len(items)} completed items (>15); archive old entries to `.agents/docs/archive/`.")


def check_code_weight(
    repo: Path,
    files: list[str],
    profile: dict[str, Any],
    warns: list[str],
    *,
    fails: list[str] | None = None,
    scan_all: bool = False,
) -> None:
    """Enforce `code_weight_limits` (legacy warn-only OR hard-blocking Zero-Accumulation Monolith Ratchet)."""
    try:
        import fitness_ratchet  # noqa: E402
    except Exception:
        return
    fail_sink = fails if fails is not None else warns
    fitness_ratchet.check_code_weight_ratchet(repo, files, profile, fail_sink, warns, scan_all=scan_all)


def check_fitness_rules(
    repo: Path,
    files: list[str],
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
    *,
    scan_all: bool = False,
) -> None:
    """Evaluate declarative multi-domain `profile.fitness_rules` with zero-accumulation baselines."""
    try:
        import fitness_ratchet  # noqa: E402
    except Exception:
        return
    fitness_ratchet.check_fitness_rules(repo, files, profile, fails, warns, scan_all=scan_all)


def check_build_lessons_timelessness(repo: Path, files: list[str], fails: list[str]) -> None:
    """Enforce timeless anti-obsolescence rules whenever any BUILD_LESSONS.md is modified."""
    lesson_files = [f for f in files if Path(f).name == "BUILD_LESSONS.md" and (repo / f).is_file()]
    if not lesson_files:
        return
    try:
        import error_learner  # noqa: E402
    except Exception:
        return
    for rel in lesson_files:
        ok, errs = error_learner.audit_lessons_file(repo / rel)
        if not ok:
            for e in errs[:8]:
                fails.append(f"{rel}: timelessness audit failed -> {e}")


_PARITY_PRESERVE_NAMES = frozenset({
    ".git", ".last_good_backup", ".last_good_backup.tmp", ".metrics.jsonl",
    ".model_modes.json", ".bypass_gates", ".diagnostics", ".workspaces.json",
    ".hook_state.json", "__pycache__",
})
_PARITY_HOST_EXCLUDES = (
    ".gitignore", "package.json", "AGENTS.md", ".github", ".agents/preview",
    ".agents/federation.json", ".agents/bridge_profile.json", ".agents/env_manifest.json",
    ".agents/host_matrix.json", ".agents/planner_profile.json",
)


def _inline_plugin_tree_sha256(root: Path) -> str:
    if not root.is_dir():
        return ""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        cur = Path(dirpath)
        rel_dir = "" if cur == root else cur.relative_to(root).as_posix()
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            d_rel = f"{rel_dir}/{d}" if rel_dir else d
            if any(p in _PARITY_PRESERVE_NAMES for p in d_rel.split("/")):
                continue
            if any(d_rel == ex or d_rel.startswith(ex + "/") for ex in _PARITY_HOST_EXCLUDES):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for fn in sorted(filenames):
            f_rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if any(p in _PARITY_PRESERVE_NAMES for p in f_rel.split("/")) or f_rel.endswith((".pyc", ".pyo")):
                continue
            if any(f_rel == ex or f_rel.startswith(ex + "/") for ex in _PARITY_HOST_EXCLUDES):
                continue
            p = cur / fn
            if p.is_file() and not p.is_symlink():
                files.append(p)
    if not files:
        return ""
    files.sort(key=lambda p: p.relative_to(root).as_posix())
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(data)).encode("ascii"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def _compute_plugin_tree_sha256(plugin_dir: Path) -> str:
    try:
        import self_evolve  # noqa: E402

        fn = getattr(self_evolve, "compute_plugin_tree_sha256", None)
        if callable(fn):
            res = fn(plugin_dir)
            if isinstance(res, tuple) and len(res) >= 2:
                return str(res[1] or "")
            return str(res or "")
        fp_fn = getattr(self_evolve, "_tree_fingerprint", None)
        if callable(fp_fn):
            return str(fp_fn(plugin_dir)[0] or "")
    except Exception:
        pass
    return _inline_plugin_tree_sha256(plugin_dir)


def _read_plugin_ver_sha(plugin_dir: Path) -> tuple[str, str]:
    ver = ""
    mp = plugin_dir / "plugin.json"
    if mp.is_file():
        try:
            ver = str(json.loads(mp.read_text(encoding="utf-8", errors="ignore")).get("version") or "").strip()
        except Exception:
            ver = ""
    return ver, _compute_plugin_tree_sha256(plugin_dir)


def _is_isolated_temp_repo(repo: Path) -> bool:
    try:
        rp = repo.resolve()
        if str(rp).startswith("/tmp/cavern-federation"):
            return False
        for t in {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}:
            if rp == t or t in rp.parents:
                return True
    except Exception:
        pass
    return False


def _collect_mounted_plugin_peers(
    repo: Path,
    local_dir: Path,
    *,
    global_dir: Path | None = None,
    sibling_dirs: list[Path] | None = None,
) -> list[tuple[str, Path]]:
    peers: list[tuple[str, Path]] = []
    seen_res: set[Path] = {local_dir.resolve()}
    is_tmp = _is_isolated_temp_repo(repo)

    def _add(label: str, cand: Path) -> None:
        if not cand.is_dir():
            return
        p_dir = cand if (cand / "plugin.json").is_file() else (cand / ".agents" / "plugins" / "dynamic-fractal-planner")
        if not (p_dir / "plugin.json").is_file():
            return
        try:
            rp = p_dir.resolve()
        except Exception:
            return
        if rp in seen_res:
            return
        seen_res.add(rp)
        peers.append((label, rp))

    ghome = os.environ.get("GEMINI_HOME", "").strip()
    if global_dir is not None:
        _add("global", Path(global_dir))
    elif ghome:
        _add("global", Path(ghome).expanduser() / "config" / "plugins" / "dynamic-fractal-planner")
    elif not is_tmp:
        _add("global", Path.home() / ".gemini" / "config" / "plugins" / "dynamic-fractal-planner")

    fed_env = os.environ.get("CAVERN_FEDERATION_ROOT", "").strip()
    if sibling_dirs is not None:
        for idx, sd in enumerate(sibling_dirs):
            _add(f"sibling:{sd.name or idx}", Path(sd))
    elif fed_env or not is_tmp:
        fed_file = repo / ".agents" / "federation.json"
        fed_root = Path(fed_env).expanduser() if fed_env else Path("/tmp/cavern-federation")
        if fed_file.is_file():
            try:
                fed = json.loads(fed_file.read_text(encoding="utf-8", errors="ignore"))
                if not fed_env and fed.get("mountRoot"):
                    fed_root = Path(str(fed["mountRoot"])).expanduser()
                for alias, spec in (fed.get("repos") or {}).items():
                    _add(f"fed:{alias}", fed_root / alias)
                    if not is_tmp and isinstance(spec, dict) and spec.get("termuxPath"):
                        _add(f"local:{alias}", Path(str(spec["termuxPath"])).expanduser())
            except Exception:
                pass
        elif fed_root.is_dir():
            try:
                for child in sorted(fed_root.iterdir()):
                    if child.is_dir() and not child.name.startswith("."):
                        _add(f"fed:{child.name}", child)
            except OSError:
                pass

    return peers


def check_plugin_sync_parity(
    repo: Path,
    files: list[str],
    warns: list[str],
    *,
    scan_all: bool = False,
    global_dir: Path | None = None,
    sibling_dirs: list[Path] | None = None,
) -> None:
    """Warn if plugin code changed without ledger updates OR `(semver, tree_sha256)` parity drifted vs mounted peers."""
    plug_prefix = ".agents/plugins/dynamic-fractal-planner/"
    is_standalone = (repo / "plugin.json").is_file()
    if is_standalone:
        emb_touched = [f for f in files if f.endswith((".py", "plugin.json", "hooks.json"))]
    else:
        emb_touched = [
            f for f in files
            if f.startswith(plug_prefix)
            and (f.endswith(".py") or f.endswith("plugin.json") or f.endswith("hooks.json"))
        ]
    if not emb_touched and not scan_all:
        return
    gitless_or_all = scan_all or not (repo / ".git").exists()
    cur_ver = ""
    if gitless_or_all and not is_standalone:
        manifest_p = repo / f"{plug_prefix}plugin.json"
        if manifest_p.is_file():
            try:
                cur_ver = str(json.loads(manifest_p.read_text(encoding="utf-8", errors="ignore")).get("version") or "").strip()
            except Exception:
                cur_ver = ""

    if emb_touched and not is_standalone:
        for doc_rel in (
            f"{plug_prefix}.agents/docs/EXTERNAL_AI_SYNC.md",
            f"{plug_prefix}.agents/docs/PROJECT_HISTORY.md",
        ):
            doc_full = repo / doc_rel
            if not doc_full.is_file():
                continue
            if doc_rel in files:
                continue
            if gitless_or_all and cur_ver and f"v{cur_ver}" in doc_full.read_text(encoding="utf-8", errors="ignore"):
                continue
            warns.append(f"Plugin code edited ({len(emb_touched)} file(s)) without updating `{doc_rel}`.")

    local_plugin_dir = repo if is_standalone else (repo / plug_prefix.rstrip("/"))
    if (local_plugin_dir / "plugin.json").is_file():
        peers = _collect_mounted_plugin_peers(repo, local_plugin_dir, global_dir=global_dir, sibling_dirs=sibling_dirs)
        if peers:
            local_ver, local_sha = _read_plugin_ver_sha(local_plugin_dir)
            for label, p_dir in peers:
                peer_ver, peer_sha = _read_plugin_ver_sha(p_dir)
                if local_ver != peer_ver or (local_sha and peer_sha and local_sha != peer_sha):
                    warns.append(
                        f"Plugin sync parity mismatch vs `{label}` ({p_dir}): "
                        f"local=(v{local_ver or '?'}, {local_sha[:12]}) != peer=(v{peer_ver or '?'}, {peer_sha[:12]})."
                    )


def check_file_budget(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
) -> None:
    """Guard total tracked/workspace file count against platform import limits (e.g. AI Studio 999-file cap)."""
    fb = profile.get("file_budget_limits")
    if not isinstance(fb, dict) or not fb.get("enabled", True):
        return
    warn_files = int(fb.get("warn_files") or 850)
    fail_files = int(fb.get("fail_files") or 950)
    cap_label = int(fb.get("hard_platform_cap") or 999)
    extra_ignore = {str(d).strip() for d in (fb.get("ignore_dirs") or []) if str(d).strip()}
    skip_dirs = set(SKIP_DISCOVERY_DIRS) | {".playwright-cli", ".aistudio"} | extra_ignore

    total = 0
    dir_counts: dict[str, int] = {}
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel_root = Path(root).relative_to(repo).as_posix()
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            rel = fn if rel_root == "." else f"{rel_root}/{fn}"
            parts = rel.split("/")
            bucket = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(root)")
            dir_counts[bucket] = dir_counts.get(bucket, 0) + 1
            total += 1

    if total <= warn_files and total <= fail_files:
        return

    top_dirs = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    top_str = ", ".join(f"{k}={v}" for k, v in top_dirs)
    msg = (
        f"Workspace file budget: {total}/{cap_label} files "
        f"(warn={warn_files}, fail={fail_files}; heaviest: {top_str}) — "
        f"pack cold/legacy dirs into archive/*.tar.gz or split sub-repos."
    )
    if fail_files > 0 and total > fail_files:
        fails.append(msg)
    elif warn_files > 0 and total > warn_files:
        warns.append(msg)


def check_host_matrix_coverage(repo: Path, fails: list[str], warns: list[str]) -> None:
    """Validate `.agents/host_matrix.json` and flag unclassified or stale cross-host tools."""
    import host_context  # noqa: E402

    res = host_context.audit_host_matrix(repo)
    if not res.get("present"):
        return
    for err in res.get("schema_errors") or []:
        fails.append(f"host_matrix.json: {err}")
    strict = bool(res.get("strict"))
    sink = fails if strict else warns
    stale = res.get("stale") or []
    if stale:
        sink.append(f"host_matrix.json: stale path(s) missing on disk -> {', '.join(stale[:5])}")
    for item in res.get("unclassified") or []:
        path_s = item.get("path", "")
        cat = item.get("suggested_category", "universal")
        sink.append(
            f"host_matrix.json: unclassified tool `{path_s}` (suggest `{cat}`) -> "
            f"run `plan_tool.py host-classify {path_s} --apply`"
        )


def check_blueprint_quality(
    repo: Path,
    files: list[str],
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
) -> None:
    """Lint changed SOTA blueprint files (`BP_*.md` / `blueprints/*.md`) & Plans Hub hygiene via `blueprint_engine`."""
    try:
        import blueprint_engine  # noqa: E402
    except Exception:
        return
    blueprint_engine.check_blueprint_files(repo, files, profile, fails, warns)
    blueprint_engine.check_plans_hub_hygiene(repo, profile, fails, warns)


def check_adversarial_qa_contracts(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
) -> None:
    """Validate & execute offline frozen 1-bug=1-test regression contracts via `qa_gate`."""
    try:
        import qa_gate  # noqa: E402
    except Exception:
        return
    qa_gate.check_adversarial_qa_contracts(repo, profile, fails, warns)


def check_coherence_local(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
) -> None:
    """Run read-only local ecosystem coherence checks (SSOT domains, AUTO doc blocks, tombstones)."""
    try:
        import coherence_engine  # noqa: E402
    except Exception:
        return
    res = coherence_engine.run_coherence_check(repo, local_only=True, profile=profile)
    fails.extend(res.get("fails") or [])
    warns.extend(res.get("warns") or [])


def check_meta_gov_t0_anchors(
    repo: Path,
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
) -> None:
    """Run read-only T0 synthetic mutation-canary (FAIL on <100% kill) & Living Blueprint CODE_ANCHORS check."""
    try:
        import meta_governance  # noqa: E402
    except Exception:
        return
    canary = meta_governance.run_t0_mutation_canary(repo)
    if not canary.get("ok"):
        fails.append(f"META_GOV|T0 mutation-canary failed ({canary.get('caught')}/{canary.get('total')}).")
    anc = meta_governance.verify_living_anchors(repo, profile=profile)
    for dr in anc.get("drifted") or []:
        warns.append(f"LIVING_ANCHOR_DRIFT|{dr['id']}|missing={','.join(dr['missing'][:4])}")


def check_token_overhead_budget(
    repo: Path,
    files: list[str],
    profile: dict[str, Any],
    fails: list[str],
    warns: list[str],
    scan_all: bool = False,
) -> None:
    """Enforce zero-accumulation token/char ratchet on always-loaded & boot governance docs."""
    try:
        import token_governor  # noqa: E402
    except Exception:
        return
    token_governor.check_token_overhead_budget(
        repo,
        profile,
        fails,
        warns,
        files=files,
        scan_all=scan_all,
        measure_hooks=False,
    )


VERIFY_CHECKS: list[dict[str, Any]] = []


def verify_check(
    check_id: str,
    *,
    scope: str = "static",
    cost_ms: int = 10,
    hosts: tuple[str, ...] = ("all",),
):
    """Declarative decorator registering a static check in `VERIFY_CHECKS`."""

    def _decorator(fn):
        VERIFY_CHECKS.append(
            {
                "id": check_id,
                "scope": scope,
                "cost_ms": cost_ms,
                "hosts": hosts,
                "fn": fn,
            }
        )
        return fn

    return _decorator


@verify_check("universal_syntax", scope="static", cost_ms=25)
def _vc_universal_syntax(ctx: dict[str, Any]) -> None:
    check_universal_syntax(ctx["repo"], ctx["files"], ctx["fails"])


@verify_check("csharp_syntax_deep", scope="static", cost_ms=30)
def _vc_csharp_syntax_deep(ctx: dict[str, Any]) -> None:
    check_csharp_syntax_deep(ctx["repo"], ctx["files"], ctx["fails"], ctx["warns"])


@verify_check("file_guards_state", scope="static", cost_ms=5)
def _vc_file_guards_state(ctx: dict[str, Any]) -> None:
    check_file_guards_state(ctx["repo"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("version_sync", scope="static", cost_ms=5)
def _vc_version_sync(ctx: dict[str, Any]) -> None:
    check_version_sync(ctx["repo"], ctx["profile"], ctx["warns"])


@verify_check("diff_rules", scope="static", cost_ms=15)
def _vc_diff_rules(ctx: dict[str, Any]) -> None:
    check_diff_rules(ctx["repo"], ctx["files"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("key_parity_rules", scope="static", cost_ms=15)
def _vc_key_parity_rules(ctx: dict[str, Any]) -> None:
    check_key_parity_rules(ctx["repo"], ctx["files"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("balanced_brackets", scope="static", cost_ms=20)
def _vc_balanced_brackets(ctx: dict[str, Any]) -> None:
    check_balanced_brackets(ctx["repo"], ctx["files"], ctx["fails"], ctx["warns"])


@verify_check("secrets_and_conflicts", scope="static", cost_ms=15)
def _vc_secrets_and_conflicts(ctx: dict[str, Any]) -> None:
    check_secrets_and_conflicts_files(ctx["repo"], ctx["files"], ctx["profile"], ctx["fails"])


@verify_check("orphan_calls", scope="static", cost_ms=15)
def _vc_orphan_calls(ctx: dict[str, Any]) -> None:
    check_orphan_calls(ctx["repo"], ctx["files"], ctx["warns"])


@verify_check("forward_wiring", scope="static", cost_ms=20)
def _vc_forward_wiring(ctx: dict[str, Any]) -> None:
    check_forward_wiring(ctx["repo"], ctx["files"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("memory_weight", scope="static", cost_ms=5)
def _vc_memory_weight(ctx: dict[str, Any]) -> None:
    check_memory_weight(ctx["repo"], ctx["profile"], ctx["warns"])


@verify_check("code_weight", scope="static", cost_ms=15)
def _vc_code_weight(ctx: dict[str, Any]) -> None:
    check_code_weight(
        ctx["repo"],
        ctx["files"],
        ctx["profile"],
        ctx["warns"],
        fails=ctx["fails"],
        scan_all=ctx["scan_all"],
    )


@verify_check("fitness_rules", scope="static", cost_ms=20)
def _vc_fitness_rules(ctx: dict[str, Any]) -> None:
    check_fitness_rules(
        ctx["repo"],
        ctx["files"],
        ctx["profile"],
        ctx["fails"],
        ctx["warns"],
        scan_all=ctx["scan_all"],
    )


@verify_check("file_budget", scope="static", cost_ms=20)
def _vc_file_budget(ctx: dict[str, Any]) -> None:
    check_file_budget(ctx["repo"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("host_matrix_coverage", scope="static", cost_ms=15)
def _vc_host_matrix_coverage(ctx: dict[str, Any]) -> None:
    check_host_matrix_coverage(ctx["repo"], ctx["fails"], ctx["warns"])


@verify_check("build_lessons_timelessness", scope="static", cost_ms=10)
def _vc_build_lessons_timelessness(ctx: dict[str, Any]) -> None:
    check_build_lessons_timelessness(ctx["repo"], ctx["files"], ctx["fails"])


@verify_check("blueprint_quality", scope="static", cost_ms=25)
def _vc_blueprint_quality(ctx: dict[str, Any]) -> None:
    check_blueprint_quality(ctx["repo"], ctx["files"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("adversarial_qa_contracts", scope="static", cost_ms=20)
def _vc_adversarial_qa_contracts(ctx: dict[str, Any]) -> None:
    check_adversarial_qa_contracts(ctx["repo"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("plugin_sync_parity", scope="static", cost_ms=25)
def _vc_plugin_sync_parity(ctx: dict[str, Any]) -> None:
    check_plugin_sync_parity(ctx["repo"], ctx["files"], ctx["warns"], scan_all=ctx["scan_all"])


@verify_check("coherence_local", scope="static", cost_ms=20)
def _vc_coherence_local(ctx: dict[str, Any]) -> None:
    check_coherence_local(ctx["repo"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("meta_gov_t0_anchors", scope="static", cost_ms=20)
def _vc_meta_gov_t0_anchors(ctx: dict[str, Any]) -> None:
    check_meta_gov_t0_anchors(ctx["repo"], ctx["profile"], ctx["fails"], ctx["warns"])


@verify_check("token_overhead_budget", scope="static", cost_ms=25)
def _vc_token_overhead_budget(ctx: dict[str, Any]) -> None:
    check_token_overhead_budget(ctx["repo"], ctx["files"], ctx["profile"], ctx["fails"], ctx["warns"], scan_all=ctx["scan_all"])


def run_static_checks(
    repo: Path,
    profile: dict[str, Any],
    changed: list[str] | None = None,
    scan_all: bool = False,
    host_id: str = "all",
) -> tuple[list[str], list[str], list[str]]:
    """Run all registered `@verify_check` static guardrails (<0.5s; safe for Stop hook auto-verify)."""
    files = list(changed) if changed is not None else get_changed_files(repo, profile, scan_all=scan_all)
    fails: list[str] = []
    warns: list[str] = []
    ctx: dict[str, Any] = {
        "repo": repo,
        "profile": profile,
        "files": files,
        "fails": fails,
        "warns": warns,
        "scan_all": scan_all,
    }
    for entry in VERIFY_CHECKS:
        if entry["scope"] != "static":
            continue
        allowed_hosts = entry.get("hosts") or ("all",)
        if "all" not in allowed_hosts and host_id not in allowed_hosts:
            continue
        entry["fn"](ctx)

    extra_rel, pending_extra = get_pending_extra_requests(repo, profile)
    if pending_extra:
        warns.append(f"`{extra_rel}` has {len(pending_extra)} pending item(s) — resolve before committing/shipping.")

    return files, fails, warns


def should_run_plugin_tests(
    repo: Path,
    plugin_root: Path,
    changed: list[str],
    cli_flag: bool,
) -> bool:
    if cli_flag:
        return True
    if os.environ.get("PLANNER_RUNNING_PLUGIN_TESTS") == "1":
        return False
    art_dir = (os.environ.get("PLANNER_ARTIFACT_DIR") or os.environ.get("CAVERN_ARTIFACT_DIR") or "").strip()
    if art_dir:
        hook_file = Path(art_dir) / "scratch" / ".hook_state.json"
        if hook_file.is_file():
            try:
                data = json.loads(hook_file.read_text(encoding="utf-8"))
                last_pt = float(data.get("last_plugin_test_ts") or 0.0)
                last_pe = float(data.get("last_plugin_edit_ts") or 0.0)
                if data.get("needs_plugin_tests") is False and last_pt > 0.0 and last_pt >= last_pe:
                    return False
                if data.get("needs_plugin_tests"):
                    return True
            except Exception:
                pass
    try:
        if repo.resolve() == plugin_root.resolve() and changed:
            return True
    except Exception:
        pass
    for f in changed:
        cand = str(repo / f) if not Path(f).is_absolute() else f
        if is_plugin_path(cand)[1]:
            return True
    return False


def run_plugin_tests(plugin_root: Path, fails: list[str], fast: bool = True) -> bool:
    test_script = plugin_root / "tests" / "run_plugin_tests.py"
    if not test_script.is_file():
        fails.append(f"Plugin test runner missing at `{test_script}`.")
        return False
    cmd = [sys.executable, str(test_script)]
    if os.environ.get("PLANNER_PLUGIN_TEST_FULL") != "1" and (
        fast or os.environ.get("PLANNER_PLUGIN_TEST_DEPTH") == "1" or os.environ.get("PLANNER_PLUGIN_TEST_FAST") == "1"
    ):
        cmd.append("--fast")
    print(f"Running plugin regression suite: `{test_script}`")
    res = subprocess.run(
        cmd,
        cwd=str(plugin_root),
        capture_output=True,
        text=True,
        timeout=480,
        check=False,
    )
    if res.returncode == 0:
        combined = [ln.strip() for ln in (res.stderr + "\n" + res.stdout).splitlines() if ln.strip()]
        summary = [ln for ln in combined if ln.startswith("Ran ") or ln == "OK" or ln.startswith("OK ")]
        for ln in (summary or combined[-2:]):
            print(ln)
        return True
    if res.stdout.strip():
        print(res.stdout.rstrip())
    if res.stderr.strip():
        print(res.stderr.rstrip(), file=sys.stderr)
    fails.append(f"Plugin regression tests failed (exit {res.returncode}).")
    return False


def update_hook_verify_stamp(built: bool, plugin_tests_passed: bool = False) -> None:
    art_dir = (os.environ.get("PLANNER_ARTIFACT_DIR") or os.environ.get("CAVERN_ARTIFACT_DIR") or "").strip()
    if not art_dir:
        return
    hook_file = Path(art_dir) / "scratch" / ".hook_state.json"
    try:
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = json.loads(hook_file.read_text(encoding="utf-8")) if hook_file.exists() else {}
        now = time.time()
        data["last_verify_ts"] = now
        data["needs_verify"] = False
        data["doc_verify_only"] = False
        if built:
            data["last_build_ts"] = now
            data["needs_build"] = False
            data["last_build_failed"] = False
        if plugin_tests_passed:
            last_pe = float(data.get("last_plugin_edit_ts") or 0.0)
            data["last_plugin_test_ts"] = max(now, last_pe)
            data["needs_plugin_tests"] = False
        hook_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal static & declarative guardrail verifier.")
    parser.add_argument("--build", action="store_true", help="Run matching project build_rules or verify_command.")
    parser.add_argument("--force-build", action="store_true", help="Force build_rules even without matching file diffs.")
    parser.add_argument("--plugin-tests", action="store_true", help="Run plugin self-test suite (`tests/run_plugin_tests.py`).")
    parser.add_argument("--fast", action="store_true", help="Use fast in-memory mode for plugin tests (default).")
    parser.add_argument("--full", action="store_true", help="Run full subprocess plugin tests instead of --fast.")
    parser.add_argument("--git-stat", action="store_true", help="On pass, append compact git status -s and git diff --stat (1-shot verify).")
    parser.add_argument("--all", action="store_true", help="Audit all discovered workspace files (also used automatically in gitless containers).")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--files", nargs="*", default=None)
    args = parser.parse_args()

    plugin_root = resolve_plugin_root()
    repo = resolve_repo_root(args.workspace)
    profile, profile_warnings = load_project_profile_with_warnings(repo)
    changed = [f.replace("\\", "/") for f in args.files] if args.files is not None else get_changed_files(repo, profile, scan_all=bool(args.all))

    changed, fails, warns = run_static_checks(repo, profile, changed=changed, scan_all=bool(args.all))
    warns = list(profile_warnings) + warns

    built = False
    plugin_tests_ran = False
    if not fails:
        build_req = bool(args.build or args.force_build)
        built = run_build_rules(repo, changed, profile, args.force_build, fails, build_requested=build_req)
        if not fails and run_legacy_verify_command(repo, profile, build_req, fails):
            built = True
        if not fails and should_run_plugin_tests(repo, plugin_root, changed, args.plugin_tests):
            plugin_tests_ran = run_plugin_tests(plugin_root, fails, fast=(not args.full))

    print(f"Audited {len(changed)} changed file(s).")
    if warns:
        print("WARNINGS:")
        for w in warns:
            print(f"  [WARN] {w}")
    if fails:
        print("FAILURES:")
        for f in fails:
            print(f"  [FAIL] {f}")
        raise SystemExit(1)

    update_hook_verify_stamp(built, plugin_tests_passed=plugin_tests_ran)
    extra_note = []
    if built:
        extra_note.append("build passed")
    if plugin_tests_ran:
        extra_note.append("plugin tests passed")
    suffix = f" ({', '.join(extra_note)})" if extra_note else ""
    print("UNIVERSAL VERIFY PASS: All guardrails satisfied." + suffix)

    if args.git_stat and (repo / ".git").exists():
        for gargs in (["status", "-s"], ["diff", "--stat"]):
            try:
                gres = subprocess.run(
                    ["git", "-C", str(repo), *gargs],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if gres.returncode == 0 and gres.stdout.strip():
                    print(gres.stdout.rstrip())
            except Exception:
                pass


if __name__ == "__main__":
    main()
