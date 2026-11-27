#!/usr/bin/env python3
"""Forward-wiring, orphan-call, AST/brace branch, and setting-reader checks for `verify_universal.py`."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = SCRIPTS_DIR.parent / "hooks"
for _p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from planner_state import extract_symbol_map  # noqa: E402

BRACKET_EXTS = (".cs", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java")
ORPHAN_CHECK_EXTS = (".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".kt", ".kts", ".java", ".go", ".rs")
GENERIC_ORPHAN_NAMES = frozenset({
    "main", "update", "run", "init", "dispose", "tostring", "build", "load", "save", "get", "set", "__init__",
})

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
