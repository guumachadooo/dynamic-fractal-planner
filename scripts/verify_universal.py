#!/usr/bin/env python3
"""Universal static verifier, declarative guardrail engine, and build dispatcher."""

from __future__ import annotations

import argparse
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


def get_changed_files(repo: Path) -> list[str]:
    if not (repo / ".git").exists():
        return []
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


def check_diff_rules(repo: Path, files: list[str], profile: dict[str, Any], fails: list[str], warns: list[str]) -> None:
    rules = profile.get("diff_rules") or []
    if not rules:
        return

    for rel in files:
        matching_rules = []
        for r in rules:
            pref = (r.get("path_prefix") or "").replace("\\", "/")
            preg = r.get("path_regex") or ""
            if pref and not rel.startswith(pref):
                continue
            if preg and not re.search(preg, rel):
                continue
            matching_rules.append(r)
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
        rel = (r.get("path") or "").replace("\\", "/")
        if not rel or rel not in files:
            continue
        full = repo / rel
        if not full.is_file():
            continue
        text = full.read_text(encoding="utf-8", errors="ignore")
        sec_pat = r.get("target_section_regex") or ""
        m_sec = re.search(sec_pat, text, flags=re.MULTILINE | re.DOTALL)
        if not m_sec:
            fails.append(f"{rel}: Could not locate target section for key parity rule.")
            continue
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

        cmd = (r.get("command") or "").strip()
        if not cmd or cmd in seen_cmds:
            continue
        seen_cmds.add(cmd)
        ran_any = True

        env = os.environ.copy()
        for k, v in (r.get("env") or {}).items():
            env[str(k)] = str(v)

        label = r.get("label") or cmd
        print(f"Building `{label}`...")
        res = subprocess.run(cmd, shell=True, cwd=str(repo), env=env, capture_output=True, text=True, timeout=240, check=False)
        if res.returncode != 0:
            tail = "\n".join((res.stdout + "\n" + res.stderr).splitlines()[-25:])
            fails.append(f"Build `{label}` failed (exit {res.returncode}):\n{tail}")
        else:
            print(f"  [OK] {label}")
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


def check_memory_weight(repo: Path, profile: dict[str, Any], warns: list[str]) -> None:
    hist = repo / ".agents" / "docs" / "PROJECT_HISTORY.md"
    if hist.is_file():
        n_lines = len(hist.read_text(encoding="utf-8", errors="ignore").splitlines())
        if n_lines > 150:
            warns.append(f"`.agents/docs/PROJECT_HISTORY.md` has {n_lines}L (>150L); archive older milestones to `.agents/docs/archive/`.")

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


def run_static_checks(
    repo: Path,
    profile: dict[str, Any],
    changed: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Run all fast static guardrails (<0.5s; safe for Stop hook auto-verify)."""
    files = list(changed) if changed is not None else get_changed_files(repo)
    fails: list[str] = []
    warns: list[str] = []

    check_universal_syntax(repo, files, fails)
    check_file_guards_state(repo, profile, fails, warns)
    check_version_sync(repo, profile, warns)
    check_diff_rules(repo, files, profile, fails, warns)
    check_key_parity_rules(repo, files, profile, fails, warns)
    check_balanced_brackets(repo, files, fails, warns)
    check_secrets_and_conflicts_files(repo, files, profile, fails)
    check_orphan_calls(repo, files, warns)
    check_memory_weight(repo, profile, warns)

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
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--files", nargs="*", default=None)
    args = parser.parse_args()

    plugin_root = resolve_plugin_root()
    repo = resolve_repo_root(args.workspace)
    profile, profile_warnings = load_project_profile_with_warnings(repo)
    changed = [f.replace("\\", "/") for f in args.files] if args.files is not None else get_changed_files(repo)

    changed, fails, warns = run_static_checks(repo, profile, changed=changed)
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
