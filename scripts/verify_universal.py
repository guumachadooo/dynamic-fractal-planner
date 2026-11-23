#!/usr/bin/env python3
"""Universal static verifier, declarative guardrail engine, and build dispatcher."""

from __future__ import annotations

import argparse
import ast
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
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
    extract_symbol_map,
    get_pending_extra_requests,
    is_plugin_path,
    load_project_profile_with_warnings,
    resolve_plugin_root,
    scan_secrets_and_conflicts,
)

BRACKET_EXTS = (".cs", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java")
ORPHAN_CHECK_EXTS = (".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".kt", ".kts", ".java", ".go", ".rs")
BINARY_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ttf", ".otf", ".apk", ".dll", ".exe", ".so", ".zip", ".gz", ".tar", ".mp3", ".flac", ".mp4", ".mkv", ".wav")
GENERIC_ORPHAN_NAMES = frozenset({
    "main", "update", "run", "init", "dispose", "tostring", "build", "load", "save", "get", "set", "__init__",
})


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
            if not low.endswith(DISCOVERY_EXTS) and fn != "BUILD_LESSONS.md":
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
            local_el = repo / ".agents" / "scripts" / "error_learner.py"
            el_cmd = (
                "python3 -B .agents/scripts/error_learner.py --from-log - --apply"
                if local_el.is_file()
                else f"python3 -B {SCRIPTS_DIR / 'error_learner.py'} --workspace {repo} --from-log - --apply"
            )
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


def check_balanced_brackets(repo: Path, files: list[str], fails: list[str], warns: list[str]) -> None:
    """Fast lexer checking `{}`, `()`, `[]` balance ignoring strings/char literals/comments."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    closers = {"}": "{", ")": "(", "]": "["}

    for rel in files:
        if not rel.lower().endswith(BRACKET_EXTS):
            continue
        full = repo / rel
        if not full.is_file():
            continue
        txt = full.read_text(encoding="utf-8", errors="ignore")
        is_cs = rel.lower().endswith(".cs")
        is_js_ts = rel.lower().endswith((".ts", ".tsx", ".js", ".jsx"))
        has_cs_preproc_branch = is_cs and bool(re.search(r"^\s*#if\b", txt, re.M)) and bool(re.search(r"^\s*#(?:else|elif)\b", txt, re.M))

        stack: list[tuple[str, int]] = []
        mode: str | None = None  # None | 'line' | 'block' | 'dq' | 'sq' | 'vq' | 'tq' | 'tmpl'
        tmpl_brace_stack: list[int] = []
        line_no = 1
        i = 0
        n = len(txt)
        err: str | None = None

        while i < n:
            ch = txt[i]
            nxt = txt[i + 1] if i + 1 < n else ""
            if ch == "\n":
                line_no += 1

            if mode == "line":
                if ch == "\n":
                    mode = None
                i += 1
                continue
            if mode == "block":
                if ch == "*" and nxt == "/":
                    mode = None
                    i += 2
                    continue
                i += 1
                continue
            if mode == "dq":
                if ch == "\\":
                    if nxt == "\n":
                        line_no += 1
                    i += 2
                    continue
                if ch == '"' or ch == "\n":
                    mode = None
                i += 1
                continue
            if mode == "sq":
                if ch == "\\":
                    if nxt == "\n":
                        line_no += 1
                    i += 2
                    continue
                if ch == "'" or (not is_js_ts and ch == "\n"):
                    mode = None
                i += 1
                continue
            if mode == "vq":
                if ch == '"':
                    if nxt == '"':
                        i += 2
                        continue
                    mode = None
                i += 1
                continue
            if mode == "tq":
                if txt[i : i + 3] == '"""':
                    mode = None
                    i += 3
                    continue
                i += 1
                continue
            if mode == "tmpl":
                if ch == "\\":
                    if nxt == "\n":
                        line_no += 1
                    i += 2
                    continue
                if ch == "`":
                    mode = None
                    i += 1
                    continue
                if ch == "$" and nxt == "{":
                    tmpl_brace_stack.append(len(stack))
                    stack.append(("{", line_no))
                    mode = None
                    i += 2
                    continue
                i += 1
                continue

            # Normal code mode
            if ch == "/" and nxt == "/":
                mode = "line"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                mode = "block"
                i += 2
                continue
            if txt[i : i + 3] == '"""':
                mode = "tq"
                i += 3
                continue
            if is_cs and ch == "@" and nxt == '"':
                mode = "vq"
                i += 2
                continue
            if is_cs and ch == "$" and nxt == "@" and i + 2 < n and txt[i + 2] == '"':
                mode = "vq"
                i += 3
                continue
            if ch == '"':
                mode = "dq"
                i += 1
                continue
            if ch == "'":
                mode = "sq"
                i += 1
                continue
            if is_js_ts and ch == "`":
                mode = "tmpl"
                i += 1
                continue

            if ch in pairs:
                stack.append((ch, line_no))
            elif ch in closers:
                expected_open = closers[ch]
                if not stack:
                    err = f"{rel}:{line_no}: Unbalanced closing `{ch}` (no matching `{expected_open}`)."
                    break
                top_open, top_ln = stack.pop()
                if top_open != expected_open:
                    err = f"{rel}:{line_no}: Mismatched `{ch}` (expected `{pairs[top_open]}` for `{top_open}` opened at L{top_ln})."
                    break
                if tmpl_brace_stack and len(stack) == tmpl_brace_stack[-1]:
                    tmpl_brace_stack.pop()
                    mode = "tmpl"
            i += 1

        if err is None and stack:
            top_open, top_ln = stack[-1]
            err = f"{rel}:{top_ln}: Unclosed `{top_open}` (missing `{pairs[top_open]}`)."

        if err:
            if has_cs_preproc_branch:
                warns.append(f"{err} (#if/#else C# file -> verify manually)")
            else:
                fails.append(err)


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


def check_orphan_calls(repo: Path, files: list[str], warns: list[str]) -> None:
    if not (repo / ".git").exists():
        return
    removed_candidates: list[str] = []
    seen: set[str] = set()

    for rel in files:
        suf = Path(rel).suffix.lower()
        if suf not in ORPHAN_CHECK_EXTS:
            continue
        res = subprocess.run(
            ["git", "-C", str(repo), "diff", "-U0", "HEAD", "--", rel],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            continue
        rem_lines: list[str] = []
        add_lines: list[str] = []
        for ln in res.stdout.splitlines():
            if ln.startswith("-") and not ln.startswith("---"):
                rem_lines.append(ln[1:])
            elif ln.startswith("+") and not ln.startswith("+++"):
                add_lines.append(ln[1:])
        if not rem_lines:
            continue

        def _names(lines: list[str]) -> set[str]:
            out: set[str] = set()
            for entry in extract_symbol_map(lines, suf, max_entries=100):
                sym = entry.split("|", 1)[1] if "|" in entry else entry
                if sym.startswith("..."):
                    continue
                if sym.startswith(("class ", "ns ")):
                    sym = sym.split(" ", 1)[1]
                sym = sym.rstrip("()")
                if sym:
                    out.add(sym)
            return out

        deleted_syms = _names(rem_lines) - _names(add_lines)
        for sym in sorted(deleted_syms):
            if len(sym) < 4 or sym.lower() in GENERIC_ORPHAN_NAMES:
                continue
            if sym not in seen:
                seen.add(sym)
                removed_candidates.append(sym)
                if len(removed_candidates) >= 15:
                    break
        if len(removed_candidates) >= 15:
            break

    for sym in removed_candidates:
        try:
            g = subprocess.run(
                ["git", "-C", str(repo), "grep", "-n", "-w", "--", sym],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if g.returncode == 0 and g.stdout.strip():
                hits = [ln.strip() for ln in g.stdout.splitlines() if ln.strip()]
                if hits:
                    preview = "; ".join(hits[:2])
                    warns.append(f"Orphan-call check: deleted/renamed symbol `{sym}` still referenced ({preview[:140]}).")
        except Exception:
            continue


def _is_ident_ch(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _advance_literal_or_comment(
    txt: str,
    i: int,
    is_cs: bool,
    is_js_ts: bool,
) -> tuple[int, int, str]:
    """If `txt[i:]` starts a comment or string/char literal, return `(next_i, newlines, kind)`."""
    n = len(txt)
    ch = txt[i]
    nxt = txt[i + 1] if i + 1 < n else ""

    if ch == "/" and nxt == "/":
        j = txt.find("\n", i + 2)
        return (n if j == -1 else j), 0, "comment"
    if ch == "/" and nxt == "*":
        end = txt.find("*/", i + 2)
        j = n if end == -1 else end + 2
        return j, txt[i:j].count("\n"), "comment"
    if txt[i : i + 3] == '"""':
        end = txt.find('"""', i + 3)
        j = n if end == -1 else end + 3
        return j, txt[i:j].count("\n"), "string"
    if is_cs and ((ch == "@" and nxt == '"') or (ch == "$" and nxt == "@" and i + 2 < n and txt[i + 2] == '"')):
        j = i + (3 if ch == "$" else 2)
        nl = 0
        while j < n:
            if txt[j] == "\n":
                nl += 1
            elif txt[j] == '"':
                if j + 1 < n and txt[j + 1] == '"':
                    j += 2
                    continue
                j += 1
                break
            j += 1
        return j, nl, "string"
    if ch in ('"', "'"):
        j = i + 1
        nl = 0
        while j < n:
            c = txt[j]
            if c == "\\":
                if j + 1 < n and txt[j + 1] == "\n":
                    nl += 1
                j += 2
                continue
            if c == "\n":
                nl += 1
                if ch == '"' or not is_js_ts:
                    j += 1
                    break
            elif c == ch:
                j += 1
                break
            j += 1
        return j, nl, "string"
    if is_js_ts and ch == "`":
        j = i + 1
        nl = 0
        brace_depth = 0
        while j < n:
            c = txt[j]
            if c == "\\":
                if j + 1 < n and txt[j + 1] == "\n":
                    nl += 1
                j += 2
                continue
            if c == "\n":
                nl += 1
            elif brace_depth == 0 and c == "$" and j + 1 < n and txt[j + 1] == "{":
                brace_depth = 1
                j += 2
                continue
            elif brace_depth > 0:
                if c == "{":
                    brace_depth += 1
                elif c == "}":
                    brace_depth -= 1
            elif c == "`":
                j += 1
                break
            j += 1
        return j, nl, "string"
    return i, 0, ""


def _strip_comments_only(txt: str, is_cs: bool, is_js_ts: bool, is_py: bool = False) -> str:
    out: list[str] = []
    i, n = 0, len(txt)
    while i < n:
        if is_py and txt[i] == "#":
            j = txt.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
            continue
        nxt_i, _, kind = _advance_literal_or_comment(txt, i, is_cs, is_js_ts)
        if kind == "comment":
            out.append(" ")
            i = nxt_i
            continue
        if kind == "string":
            out.append(txt[i:nxt_i])
            i = nxt_i
            continue
        out.append(txt[i])
        i += 1
    return "".join(out)


def _skip_ws_comments(txt: str, i: int, is_cs: bool, is_js_ts: bool, line_no: int) -> tuple[int, int]:
    n = len(txt)
    while i < n:
        if txt[i].isspace():
            if txt[i] == "\n":
                line_no += 1
            i += 1
            continue
        if txt[i : i + 2] in ("//", "/*"):
            nxt_i, nl, _ = _advance_literal_or_comment(txt, i, is_cs, is_js_ts)
            i = nxt_i
            line_no += nl
            continue
        break
    return i, line_no


def _find_matching_delim(
    txt: str,
    open_idx: int,
    open_ch: str,
    close_ch: str,
    is_cs: bool,
    is_js_ts: bool,
) -> tuple[int, int] | None:
    depth = 0
    i, n = open_idx, len(txt)
    nl_total = 0
    while i < n:
        nxt_i, nl, kind = _advance_literal_or_comment(txt, i, is_cs, is_js_ts)
        if kind:
            nl_total += nl
            i = nxt_i
            continue
        c = txt[i]
        if c == "\n":
            nl_total += 1
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i, nl_total
        i += 1
    return None


def _scan_brace_if_else(txt: str, rel: str) -> list[tuple[int, int, str, str]]:
    """Find `(if_line, end_line, norm_a, norm_b)` for `if (...) { A } else { B }` in brace languages."""
    low = rel.lower()
    is_cs = low.endswith(".cs")
    is_js_ts = low.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
    no_paren_ok = low.endswith((".go", ".rs"))
    out: list[tuple[int, int, str, str]] = []
    i, n = 0, len(txt)
    line_no = 1

    while i < n:
        nxt_i, nl, kind = _advance_literal_or_comment(txt, i, is_cs, is_js_ts)
        if kind:
            line_no += nl
            i = nxt_i
            continue
        if txt[i] == "\n":
            line_no += 1
            i += 1
            continue
        if (
            txt[i : i + 2] == "if"
            and (i == 0 or not _is_ident_ch(txt[i - 1]))
            and (i + 2 == n or not _is_ident_ch(txt[i + 2]))
        ):
            if_line = line_no
            p, ln_p = _skip_ws_comments(txt, i + 2, is_cs, is_js_ts, line_no)
            if p < n and txt[p] == "(":
                cond_res = _find_matching_delim(txt, p, "(", ")", is_cs, is_js_ts)
                if not cond_res:
                    i += 2
                    continue
                p, ln_p = _skip_ws_comments(txt, cond_res[0] + 1, is_cs, is_js_ts, ln_p + cond_res[1])
            elif no_paren_ok:
                while p < n and txt[p] not in ("{", ";", "\n"):
                    ni, nln, k = _advance_literal_or_comment(txt, p, is_cs, is_js_ts)
                    if k:
                        ln_p += nln
                        p = ni
                    else:
                        p += 1
            else:
                i += 2
                continue

            if p >= n or txt[p] != "{":
                i += 2
                continue
            res_a = _find_matching_delim(txt, p, "{", "}", is_cs, is_js_ts)
            if not res_a:
                i += 2
                continue
            body_a_end, dl_a = res_a
            after_a, ln_after = _skip_ws_comments(txt, body_a_end + 1, is_cs, is_js_ts, ln_p + dl_a)
            if txt[after_a : after_a + 4] != "else" or (after_a + 4 < n and _is_ident_ch(txt[after_a + 4])):
                i += 2
                continue
            after_else, ln_else = _skip_ws_comments(txt, after_a + 4, is_cs, is_js_ts, ln_after)
            if (
                txt[after_else : after_else + 2] == "if"
                and (after_else + 2 == n or not _is_ident_ch(txt[after_else + 2]))
            ) or after_else >= n or txt[after_else] != "{":
                i += 2
                continue
            res_b = _find_matching_delim(txt, after_else, "{", "}", is_cs, is_js_ts)
            if not res_b:
                i += 2
                continue
            body_b_end, dl_b = res_b
            norm_a = re.sub(r"\s+", " ", _strip_comments_only(txt[p + 1 : body_a_end], is_cs, is_js_ts)).strip()
            norm_b = re.sub(r"\s+", " ", _strip_comments_only(txt[after_else + 1 : body_b_end], is_cs, is_js_ts)).strip()
            out.append((if_line, ln_else + dl_b, norm_a, norm_b))
            i += 2
            continue
        i += 1
    return out


_WIRING_CONTROL_WORDS = frozenset({
    "if", "for", "foreach", "while", "switch", "catch", "using", "lock",
    "return", "throw", "new", "sizeof", "typeof", "nameof", "await",
    "else", "when", "try", "finally", "do", "case", "yield", "super", "this",
})
_PUB_CS_JAVA_RE = re.compile(
    r"^\s*public\s+(?:(?:static|virtual|async|sealed|new|unsafe|readonly|extern|synchronized|final|default)\s+)*"
    r"(?!class\b|struct\b|interface\b|enum\b|record\b|delegate\b|event\b)"
    r"([\w.<>\[\]?,\s]+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]+>)?\s*\("
)
_PUB_TS_JS_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\*?|class|const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)
_PUB_PY_RE = re.compile(r"^\s{0,4}(?:async\s+)?def\s+([A-Za-z][A-Za-z0-9_]*)\s*\(")
_PUB_KT_RE = re.compile(
    r"^\s*(?:(?:public|open|suspend|inline|operator|tailrec)\s+)*fun\s+(?:<[^>]+>\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_PUB_RS_RE = re.compile(r"^\s*pub(?:\([^)]*\))?\s+(?:async\s+|unsafe\s+|const\s+)*fn\s+([A-Za-z_][A-Za-z0-9_]*)")
_PUB_GO_RE = re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Z][A-Za-z0-9_]*)\s*\(")

_SET_CS_KT_RE = re.compile(
    r"^\s*(?:public|internal)\s+(?:(?:static|const|readonly|override|val|var)\s+)*[\w.<>\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\{|=>|=|;)"
)
_SET_TS_JS_RE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var|readonly)?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::\s*[^=;]+)?\s*[=:]")
_SET_PY_RE = re.compile(r'^(?:([A-Za-z][A-Za-z0-9_]*)\s*(?::\s*[^=]+)?=|"([A-Za-z_][A-Za-z0-9_.-]*)"\s*:)')
_SET_JSON_RE = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_.-]*)"\s*:')
_SETTING_IGNORE_NAMES = frozenset({"true", "false", "null", "none", "default", "version", "name", "type", "enabled", "value", "data"})


def _get_diff_add_rem(repo: Path, rel: str) -> tuple[list[tuple[int, str]], list[str]]:
    if (repo / ".git").exists():
        for diff_args in (["diff", "-U0", "HEAD", "--", rel], ["diff", "--cached", "-U0", "--", rel]):
            res = subprocess.run(["git", "-C", str(repo), *diff_args], capture_output=True, text=True, timeout=5, check=False)
            if res.returncode == 0 and res.stdout.strip():
                added: list[tuple[int, str]] = []
                removed: list[str] = []
                cur_line = 0
                for ln in res.stdout.splitlines():
                    if ln.startswith("@@"):
                        m = re.search(r"\+(\d+)", ln)
                        cur_line = int(m.group(1)) if m else 0
                    elif ln.startswith("+") and not ln.startswith("+++"):
                        added.append((cur_line, ln[1:]))
                        cur_line += 1
                    elif ln.startswith("-") and not ln.startswith("---"):
                        removed.append(ln[1:])
                return added, removed
        st = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel], capture_output=True, text=True, timeout=3, check=False)
        if st.returncode == 0:
            return [], []
    full = repo / rel
    if full.is_file():
        return [(i, ln) for i, ln in enumerate(full.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)], []
    return [], []


def _extract_public_symbols(lines: list[tuple[int, str]], suf: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for ln_no, raw in lines:
        s = raw.strip()
        if not s or s.startswith(("//", "/*", "*", "#", "[", "@")):
            continue
        name = ""
        if suf in (".cs", ".java"):
            if re.search(r"\b(?:override|extern)\b", s) or "=" in s.split("(", 1)[0]:
                continue
            m = _PUB_CS_JAVA_RE.match(raw)
            if m:
                name = m.group(2)
        elif suf in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            m = _PUB_TS_JS_RE.match(raw)
            if m:
                name = m.group(1)
        elif suf == ".py":
            m = _PUB_PY_RE.match(raw)
            if m:
                name = m.group(1)
        elif suf in (".kt", ".kts"):
            if re.search(r"\b(?:private|protected|internal|override)\b", s):
                continue
            m = _PUB_KT_RE.match(raw)
            if m:
                name = m.group(1)
        elif suf == ".rs":
            m = _PUB_RS_RE.match(raw)
            if m:
                name = m.group(1)
        elif suf == ".go":
            m = _PUB_GO_RE.match(raw)
            if m:
                name = m.group(1)
        if name and name not in _WIRING_CONTROL_WORDS:
            out.append((ln_no, name))
    return out


def _extract_setting_keys(lines: list[tuple[int, str]], suf: str, custom_pat: str = "") -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    cre = re.compile(custom_pat) if custom_pat else None
    for ln_no, raw in lines:
        s = raw.strip()
        if not s or s.startswith(("//", "/*", "*", "#")):
            continue
        key = ""
        if cre:
            m = cre.search(raw)
            if m:
                key = m.group(1) if m.groups() else m.group(0)
        elif suf in (".cs", ".java", ".kt", ".kts"):
            m = _SET_CS_KT_RE.match(raw)
            if m and "(" not in raw.split("{", 1)[0].split("=", 1)[0]:
                key = m.group(1)
        elif suf in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            m = _SET_TS_JS_RE.match(raw)
            if m:
                key = m.group(1)
        elif suf == ".py":
            m = _SET_PY_RE.match(s)
            if m:
                key = m.group(1) or m.group(2) or ""
        elif suf == ".json":
            m = _SET_JSON_RE.match(raw)
            if m:
                key = m.group(1)
        if key and key.lower() not in _SETTING_IGNORE_NAMES and key not in _WIRING_CONTROL_WORDS:
            out.append((ln_no, key))
    return out


def _is_test_path(rel: str, wg: dict[str, Any]) -> bool:
    low = rel.lower().lstrip("./")
    test_prefs = tuple(str(p).lower().replace("\\", "/").lstrip("./") for p in (wg.get("ignore_caller_prefixes") or ("tests/", "test/", "__tests__/")))
    if test_prefs and low.startswith(test_prefs):
        return True
    fname = Path(low).name
    return fname.startswith("test_") or any(
        tok in fname for tok in (".test.", ".spec.", "_test.", "tests.cs", "test.cs", "test.kt", "test.java", "tests.py")
    )


def _has_external_code_ref(repo: Path, sym: str, defining_rel: str, wg: dict[str, Any]) -> bool:
    if not (repo / ".git").exists():
        return True
    match_flag = "-F" if any(c in sym for c in ".-:") else "-w"
    try:
        g = subprocess.run(
            ["git", "-C", str(repo), "grep", "-n", match_flag, "--untracked", "--", sym],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return True
    if g.returncode != 0 or not g.stdout.strip():
        return False

    allow_tests = bool(wg.get("allow_test_callers"))
    sym_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(sym)}(?![A-Za-z0-9_])")
    for raw_hit in g.stdout.splitlines():
        parts = raw_hit.split(":", 2)
        if len(parts) < 3:
            continue
        hit_rel = parts[0].replace("\\", "/").lstrip("./")
        content = parts[2]
        if hit_rel == defining_rel:
            continue
        low_h = hit_rel.lower()
        if low_h.startswith((".agents/", "docs/")) or low_h.endswith((".md", ".txt", ".rst", ".json", ".yml", ".yaml", ".log", ".lock")):
            continue
        if not allow_tests and _is_test_path(hit_rel, wg):
            continue
        sc = content.lstrip()
        if sc.startswith(("//", "#", "*", "/*", "<!--")):
            continue
        code_only = _strip_comments_only(content, low_h.endswith(".cs"), low_h.endswith((".ts", ".tsx", ".js", ".jsx")), is_py=low_h.endswith(".py"))
        if sym_re.search(code_only):
            return True
    return False


def check_forward_wiring(repo: Path, files: list[str], profile: dict[str, Any], fails: list[str], warns: list[str]) -> None:
    """MOD-WIRING-GATE: audit new public APIs, identical if/else stubs, and settings key readers."""
    wg = profile.get("wiring_gate") or {}
    if wg.get("enabled") is False:
        return

    sev = str(wg.get("severity") or "warn").lower()
    bucket = fails if sev == "fail" else warns
    max_syms = int(wg.get("max_symbols") or 20)
    ignore_pat = wg.get("ignore_symbol_regex") or r"^(?:test_|Test[A-Z]|On[A-Z]|on[A-Z]|Dispose|ToString|Equals|GetHashCode|CompareTo|Clone|Finalize|setUp|tearDown)"
    ignore_re = re.compile(ignore_pat)

    lib_prefs = tuple(str(p).replace("\\", "/").lstrip("./") for p in (wg.get("library_prefixes") or []) if str(p).strip())
    settings_files = [str(s).replace("\\", "/").lstrip("./") for s in (wg.get("settings_files") or []) if str(s).strip()]
    check_branches = wg.get("check_identical_branches", True) is not False
    has_git = (repo / ".git").exists()

    checked_syms = 0
    for rel in files:
        suf = Path(rel).suffix.lower()
        full = repo / rel
        if not full.is_file():
            continue

        added, removed = _get_diff_add_rem(repo, rel)
        added_line_nos = {ln for ln, _ in added}

        # (a) Zero Orphan New Public APIs
        if has_git and lib_prefs and suf in ORPHAN_CHECK_EXTS and rel.startswith(lib_prefs) and not _is_test_path(rel, wg) and added:
            rem_names = {s for _, s in _extract_public_symbols([(0, r) for r in removed], suf)}
            seen_local: set[str] = set()
            for ln_no, sym in _extract_public_symbols(added, suf):
                if checked_syms >= max_syms:
                    break
                if len(sym) < 4 or sym.lower() in GENERIC_ORPHAN_NAMES or sym in rem_names or sym in seen_local or ignore_re.search(sym):
                    continue
                seen_local.add(sym)
                checked_syms += 1
                if not _has_external_code_ref(repo, sym, rel, wg):
                    bucket.append(f"Wiring-gate (orphan API): {rel}:{ln_no}: new public symbol `{sym}` has 0 external callers.")

        # (b) Identical if/else No-Op Branch Detector
        if check_branches and suf in ORPHAN_CHECK_EXTS and added_line_nos:
            txt = full.read_text(encoding="utf-8", errors="ignore")
            if suf == ".py":
                try:
                    tree = ast.parse(txt)
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.If) or not node.orelse:
                            continue
                        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                            continue
                        end_ln = getattr(node.orelse[-1], "end_lineno", None) or node.orelse[-1].lineno
                        if not any(node.lineno <= al <= end_ln for al in added_line_nos):
                            continue
                        if [ast.dump(st, include_attributes=False) for st in node.body] == [
                            ast.dump(st, include_attributes=False) for st in node.orelse
                        ]:
                            bucket.append(f"Wiring-gate (identical if/else): {rel}:{node.lineno}: identical if/else branches (no-op stub).")
                except SyntaxError:
                    pass
            elif suf in (".cs", ".ts", ".tsx", ".js", ".jsx", ".kt", ".kts", ".java", ".go", ".rs"):
                for if_ln, end_ln, norm_a, norm_b in _scan_brace_if_else(txt, rel):
                    if not any(if_ln <= al <= end_ln for al in added_line_nos):
                        continue
                    if norm_a == norm_b and len(norm_a) >= 2:
                        bucket.append(f"Wiring-gate (identical if/else): {rel}:{if_ln}: identical if/else branches (no-op stub).")

        # (c) Settings / Config Key Wiring
        if has_git and settings_files and any(rel == sf or rel.endswith("/" + sf) or rel.endswith(sf) for sf in settings_files) and added:
            custom_pat = str(wg.get("settings_key_regex") or "")
            rem_keys = {k for _, k in _extract_setting_keys([(0, r) for r in removed], suf, custom_pat)}
            seen_keys: set[str] = set()
            for ln_no, key in _extract_setting_keys(added, suf, custom_pat):
                if len(key) < 3 or key in rem_keys or key in seen_keys or ignore_re.search(key):
                    continue
                seen_keys.add(key)
                if not _has_external_code_ref(repo, key, rel, wg):
                    bucket.append(f"Wiring-gate (unwired setting): {rel}:{ln_no}: setting/config key `{key}` has 0 readers outside `{rel}`.")


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
    scan_all: bool = False,
) -> None:
    """Warn on oversized source files breaching profile `code_weight_limits` (anti-monolith guard)."""
    cw = profile.get("code_weight_limits")
    if not isinstance(cw, dict) or not cw.get("enabled", True):
        return
    limit = int(cw.get("warn_lines_all") or 2000) if scan_all else int(cw.get("warn_lines") or 1500)
    if limit <= 0:
        return
    ignore_prefixes = tuple(
        str(p).replace("\\", "/").lstrip("./")
        for p in (cw.get("ignore_path_prefixes") or [".agents/", "Libraries/", "node_modules/"])
        if str(p).strip()
    )
    allowed_exts = {
        str(e).lower()
        for e in (cw.get("extensions") or (".cs", ".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".rs", ".go", ".c", ".cpp", ".h", ".swift"))
    }
    for rel in files:
        norm = rel.replace("\\", "/").lstrip("./")
        if any(norm.startswith(pref) for pref in ignore_prefixes):
            continue
        if Path(norm).suffix.lower() not in allowed_exts:
            continue
        full = repo / norm
        if not full.is_file():
            continue
        try:
            n_lines = len(full.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            continue
        if n_lines > limit:
            warns.append(f"{norm}: {n_lines}L (>{limit}L) — consider splitting into modular/partial files (`code_weight_limits`).")


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


def check_plugin_sync_parity(
    repo: Path,
    files: list[str],
    warns: list[str],
    *,
    scan_all: bool = False,
) -> None:
    """Warn if embedded plugin code changed without recording an entry in plugin EXTERNAL_AI_SYNC.md."""
    plug_prefix = ".agents/plugins/dynamic-fractal-planner/"
    code_touched = [
        f for f in files
        if f.startswith(plug_prefix)
        and (f.endswith(".py") or f.endswith("plugin.json") or f.endswith("hooks.json"))
    ]
    if not code_touched:
        return
    plug_sync = f"{plug_prefix}.agents/docs/EXTERNAL_AI_SYNC.md"
    sync_full = repo / plug_sync
    if not sync_full.is_file():
        return
    if plug_sync in files:
        return
    if scan_all or not (repo / ".git").exists():
        # In full-workspace / gitless scans, `files` includes all tracked plugin files regardless of edit state.
        # Only warn if the current plugin.json version is missing from plugin EXTERNAL_AI_SYNC.md.
        manifest_p = repo / f"{plug_prefix}plugin.json"
        if manifest_p.is_file():
            try:
                cur_ver = str(json.loads(manifest_p.read_text(encoding="utf-8", errors="ignore")).get("version") or "").strip()
            except Exception:
                cur_ver = ""
            if cur_ver and f"v{cur_ver}" in sync_full.read_text(encoding="utf-8", errors="ignore"):
                return
    warns.append(f"Plugin code edited ({len(code_touched)} file(s)) without updating `{plug_sync}`.")


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


def run_static_checks(
    repo: Path,
    profile: dict[str, Any],
    changed: list[str] | None = None,
    scan_all: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Run all fast static guardrails (<0.5s; safe for Stop hook auto-verify)."""
    files = list(changed) if changed is not None else get_changed_files(repo, profile, scan_all=scan_all)
    fails: list[str] = []
    warns: list[str] = []

    check_universal_syntax(repo, files, fails)
    check_csharp_syntax_deep(repo, files, fails, warns)
    check_file_guards_state(repo, profile, fails, warns)
    check_version_sync(repo, profile, warns)
    check_diff_rules(repo, files, profile, fails, warns)
    check_key_parity_rules(repo, files, profile, fails, warns)
    check_balanced_brackets(repo, files, fails, warns)
    check_secrets_and_conflicts_files(repo, files, profile, fails)
    check_orphan_calls(repo, files, warns)
    check_forward_wiring(repo, files, profile, fails, warns)
    check_memory_weight(repo, profile, warns)
    check_code_weight(repo, files, profile, warns, scan_all=scan_all)
    check_file_budget(repo, profile, fails, warns)
    check_build_lessons_timelessness(repo, files, fails)
    check_plugin_sync_parity(repo, files, warns, scan_all=scan_all)

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
        timeout=240,
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
