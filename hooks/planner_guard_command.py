#!/usr/bin/env python3
"""Universal PreToolUse hook for run_command: terminal diet, EXTRA_REQUESTS gate,
active plan check on commit, memory feedback, declarative command_guards,
pre-push flight check, and tool-path/artifact routing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def _ensure_paths() -> None:
    for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def allow(overwrite_cmd: str | None = None) -> None:
    if overwrite_cmd is not None:
        print(json.dumps({"decision": "allow", "overwrite": {"CommandLine": overwrite_cmd}}, ensure_ascii=False))
    else:
        print(json.dumps({"decision": "allow"}))


def reply(
    decision: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    repo: Path | None = None,
) -> None:
    print(json.dumps({"decision": decision, "reason": reason}, ensure_ascii=False))
    if decision in ("deny", "force_ask") and payload is not None:
        try:
            _ensure_paths()
            from planner_state import get_paths, load_json  # noqa: E402
            from plugin_diagnostics import record_guard_deny_if_burst  # noqa: E402

            _, _, _, hook_file = get_paths(payload)
            hook_state = load_json(hook_file)
            head = reason.split("|", 1)[0].split("(", 1)[0].strip()[:40]
            if head == "DENY" and "|" in reason:
                sub = reason.split("|", 1)[1].strip().split()[0][:28]
                rule_sig = f"DENY:{sub}"
            else:
                rule_sig = head
            record_guard_deny_if_burst("planner_guard_command.py", rule_sig, reason, hook_file, hook_state, repo=repo)
        except Exception:
            pass


def deny(reason: str, payload: dict[str, Any] | None = None, repo: Path | None = None) -> None:
    reply("deny", reason, payload=payload, repo=repo)


def _split_shell_parts_with_seps(cmd: str) -> list[tuple[str, str]]:
    """Split shell command on &&, ||, ;, |, newline outside quotes, preserving exact separators."""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    in_sq = False
    in_dq = False
    escape = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\" and not in_sq:
            buf.append(ch)
            escape = True
            i += 1
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            buf.append(ch)
            i += 1
            continue
        if not in_sq and not in_dq:
            if cmd[i : i + 2] in ("&&", "||"):
                parts.append(("".join(buf), cmd[i : i + 2]))
                buf.clear()
                i += 2
                continue
            if ch in ";\n|":
                parts.append(("".join(buf), ch))
                buf.clear()
                i += 1
                continue
        buf.append(ch)
        i += 1
    parts.append(("".join(buf), ""))
    return parts


def split_shell_segments(cmd: str) -> list[str]:
    return [seg.strip() for seg, _ in _split_shell_parts_with_seps(cmd) if seg.strip()]


def _split_pipelines(cmd: str) -> list[str]:
    """Split shell command on &&, ||, ;, newline ONLY (preserving `|` pipelines)."""
    parts: list[str] = []
    buf: list[str] = []
    in_sq = False
    in_dq = False
    escape = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\" and not in_sq:
            buf.append(ch)
            escape = True
            i += 1
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            buf.append(ch)
            i += 1
            continue
        if not in_sq and not in_dq:
            if ch in ";\n":
                p = "".join(buf).strip()
                if p:
                    parts.append(p)
                buf.clear()
                i += 1
                continue
            if cmd[i : i + 2] in ("&&", "||"):
                p = "".join(buf).strip()
                if p:
                    parts.append(p)
                buf.clear()
                i += 2
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def strip_env_prefix(segment: str) -> tuple[dict[str, str], str]:
    envs: dict[str, str] = {}
    rest = segment.lstrip()
    env_pat = re.compile(r"""^([A-Za-z_][A-Za-z0-9_]*)=('[^']*'|"[^"]*"|\S+)\s*""")
    while True:
        m = env_pat.match(rest)
        if not m:
            break
        envs[m.group(1)] = m.group(2).strip("'\"")
        rest = rest[m.end() :].lstrip()
    return envs, rest


_ENV_ASSIGN_TOK_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_REDIR_STAND_OPS = frozenset({">", ">>", "<", "<<", "<<<", "1>", "1>>", "2>", "2>>", "&>", "&>>"})
_REDIR_ATTACHED_RE = re.compile(r"^(?:\d*>&\d+|\d*>>?\S+|&>>?\S+|<\S+)$")
_TRAILING_REDIR_RE = re.compile(
    r"(?:\s+(?:\d*>&\d+|\d*>>?\s*(?:\"[^\"]*\"|'[^']*'|\S+)|&>>?\s*(?:\"[^\"]*\"|'[^']*'|\S+)|<\s*(?:\"[^\"]*\"|'[^']*'|\S+)))+\s*$"
)
_PYTHON_EXE_RE = re.compile(r"python(?:\d+(?:\.\d+)?)?", re.IGNORECASE)
_PYTHON_SHORT_BUNDLE_RE = re.compile(r"-[BEIOPSqsuvxbd]+")


def _shlex_strip_env_and_wrappers(seg: str) -> tuple[dict[str, str], list[str]] | None:
    """Parse segment with shlex and strip leading K=V assignments and `env` wrappers."""
    try:
        tokens = shlex.split(seg)
    except ValueError:
        return None
    envs: dict[str, str] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        m = _ENV_ASSIGN_TOK_RE.match(tok)
        if m:
            envs[m.group(1)] = m.group(2)
            i += 1
            continue
        if Path(tok).name == "env":
            i += 1
            while i < n:
                etok = tokens[i]
                em = _ENV_ASSIGN_TOK_RE.match(etok)
                if em:
                    envs[em.group(1)] = em.group(2)
                    i += 1
                elif etok in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                    i += 2
                elif etok.startswith(("--unset=", "--chdir=", "--split-string=")) or etok in ("-i", "-0", "--ignore-environment", "-v", "--debug"):
                    i += 1
                elif etok == "--":
                    i += 1
                    break
                else:
                    break
            continue
        break
    return envs, tokens[i:]


def _strip_redirect_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _REDIR_STAND_OPS:
            i += 2
            continue
        if _REDIR_ATTACHED_RE.match(tok):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _parse_python_script_invocation(tokens: list[str]) -> tuple[str, list[str]] | None:
    """Return `(script_path, script_args)` ONLY when tokens invoke python[3] executing a `.py` file."""
    if not tokens:
        return None
    exe = Path(tokens[0]).name.lower()
    if not _PYTHON_EXE_RE.fullmatch(exe):
        return None
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok in ("-c", "-m", "-h", "--help", "-V", "--version", "--help-env", "--help-xoptions", "--help-all"):
            return None
        if tok in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if tok.startswith(("--check-hash-based-pycs=", "-W", "-X")) and len(tok) > 2:
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            if not _PYTHON_SHORT_BUNDLE_RE.fullmatch(tok):
                return None
            i += 1
            continue
        break
    if i >= n:
        return None
    return tokens[i], tokens[i + 1 :]


_GIT_ARG_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"})
_SHIP_FLAG_WITH_VAL = frozenset({
    "-C", "--workspace", "--repo", "--dir", "--global-dir", "--plugin-rel",
    "--commit", "--next", "--owner-repo", "--branch", "--remote", "--via",
    "--url", "--pair-secret", "--admin-key", "--channel", "--timeout", "--mount-root",
})


def _classify_ship_segment(seg: str) -> dict[str, Any] | None:
    parsed = _shlex_strip_env_and_wrappers(seg)
    if parsed is None:
        return None
    _, raw_tokens = parsed
    clean = _strip_redirect_tokens(raw_tokens)
    if not clean:
        return None
    exe = Path(clean[0]).name.lower()

    if exe == "git":
        if "-h" in clean or "--help" in clean:
            return None
        git_dir = ""
        i = 1
        while i < len(clean):
            tok = clean[i]
            if tok == "-C" and i + 1 < len(clean):
                git_dir = clean[i + 1]
                i += 2
                continue
            if tok in _GIT_ARG_FLAGS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            if tok == "commit":
                return {
                    "kind": "git_commit",
                    "is_commit": True,
                    "is_push": False,
                    "is_ship": True,
                    "amend": "--amend" in clean,
                    "skip_history": "[skip-history]" in seg,
                    "auto_skip_ci": False,
                    "no_test": False,
                    "target_dir": git_dir,
                }
            if tok == "push":
                return {
                    "kind": "git_push",
                    "is_commit": False,
                    "is_push": True,
                    "is_ship": True,
                    "amend": False,
                    "skip_history": False,
                    "auto_skip_ci": False,
                    "no_test": False,
                    "target_dir": git_dir,
                }
            return None
        return None

    py_inv = _parse_python_script_invocation(clean)
    if py_inv is not None:
        script_tok, s_args = py_inv
    elif clean[0].lower().endswith(".py"):
        script_tok, s_args = clean[0], clean[1:]
    else:
        return None

    if "-h" in s_args or "--help" in s_args:
        return None

    target_dir = ""
    pos_args: list[str] = []
    i = 0
    while i < len(s_args):
        tok = s_args[i]
        if tok.startswith("--workspace="):
            target_dir = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.startswith("--repo=") and not target_dir:
            cand_r = tok.split("=", 1)[1]
            if "/" in cand_r or cand_r.startswith((".", "~")):
                target_dir = cand_r
            i += 1
            continue
        if tok in _SHIP_FLAG_WITH_VAL:
            if tok in ("--workspace", "--repo") and i + 1 < len(s_args):
                val = s_args[i + 1]
                if tok == "--workspace" or not target_dir:
                    target_dir = val
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        pos_args.append(tok)
        i += 1

    sname = Path(script_tok).name.lower()
    if sname in ("omni_agent.py", "omni_agent_client.py") and pos_args and pos_args[0] in ("ship", "push-workspace"):
        return {
            "kind": "omni_ship" if pos_args[0] == "ship" else "omni_push_workspace",
            "is_commit": True,
            "is_push": True,
            "is_ship": True,
            "amend": False,
            "skip_history": False,
            "auto_skip_ci": "--auto-skip-ci" in s_args,
            "no_test": False,
            "target_dir": target_dir,
        }
    if sname == "github_direct.py" and "push" in pos_args:
        return {
            "kind": "github_direct_push",
            "is_commit": True,
            "is_push": True,
            "is_ship": True,
            "amend": False,
            "skip_history": False,
            "auto_skip_ci": False,
            "no_test": False,
            "target_dir": target_dir,
        }
    if sname == "repo_federation.py" and pos_args and pos_args[0] == "push":
        return {
            "kind": "repo_federation_push",
            "is_commit": True,
            "is_push": True,
            "is_ship": True,
            "amend": False,
            "skip_history": False,
            "auto_skip_ci": False,
            "no_test": False,
            "target_dir": target_dir,
        }
    if sname == "plan_tool.py" and len(pos_args) >= 2 and pos_args[0] == "evolve" and pos_args[1] == "sync-all" and "--push" in s_args:
        return {
            "kind": "plan_evolve_sync_all_push",
            "is_commit": True,
            "is_push": True,
            "is_ship": True,
            "amend": False,
            "skip_history": False,
            "auto_skip_ci": False,
            "no_test": "--no-test" in s_args,
            "target_dir": target_dir,
        }
    if sname == "self_evolve.py" and pos_args and pos_args[0] == "sync-all" and "--push" in s_args:
        return {
            "kind": "self_evolve_sync_all_push",
            "is_commit": True,
            "is_push": True,
            "is_ship": True,
            "amend": False,
            "skip_history": False,
            "auto_skip_ci": False,
            "no_test": "--no-test" in s_args,
            "target_dir": target_dir,
        }
    return None


def _resolve_ship_target_repo(
    segments: list[str],
    default_repo: Path,
    *,
    want_push: bool = False,
    want_commit: bool = False,
) -> tuple[Path, dict[str, Any] | None]:
    cur_dir = default_repo
    for seg in segments:
        _, rest = strip_env_prefix(seg)
        m_cd = re.match(r"^cd\s+(.+)$", rest)
        if m_cd:
            raw_dir = m_cd.group(1).strip().strip("'\"")
            if raw_dir:
                cand = (cur_dir / raw_dir) if not raw_dir.startswith(("/", "~")) else Path(raw_dir).expanduser()
                if cand.is_dir():
                    cur_dir = cand.resolve()
            continue
        info = _classify_ship_segment(seg)
        if not info:
            continue
        if want_push and not info.get("is_push"):
            continue
        if want_commit and not info.get("is_commit"):
            continue
        tdir = str(info.get("target_dir") or "").strip()
        if tdir:
            cand = (cur_dir / tdir) if not tdir.startswith(("/", "~")) else Path(tdir).expanduser()
            if cand.is_dir():
                return cand.resolve(), info
        return cur_dir, info
    return default_repo, None


def _resolve_target_repo(segments: list[str], default_repo: Path, subcmd_re: str) -> Path:
    want_push = "push" in subcmd_re
    want_commit = "commit" in subcmd_re and not want_push
    repo_p, _ = _resolve_ship_target_repo(segments, default_repo, want_push=want_push, want_commit=want_commit)
    return repo_p


def check_raw_cat_log(cmd: str, repo: Path, profile: dict[str, Any]) -> str | None:
    max_full = int((profile.get("read_budget") or {}).get("max_full_read_lines") or 300)
    cur_dir = repo
    for pipe in _split_pipelines(cmd):
        _, rest = strip_env_prefix(pipe)
        m_cd = re.match(r"^cd\s+(.+)$", rest)
        if m_cd:
            raw_dir = m_cd.group(1).strip().strip("'\"")
            cand = (cur_dir / raw_dir) if not raw_dir.startswith(("/", "~")) else Path(raw_dir).expanduser()
            if cand.is_dir():
                cur_dir = cand.resolve()
            continue
        if "|" in rest:
            continue
        if not re.match(r"^cat\s+", rest):
            continue
        try:
            tokens = shlex.split(rest)
        except Exception:
            tokens = rest.split()
        file_args = [t for t in tokens[1:] if not t.startswith("-") and not t.startswith((">", "<", "2>"))]
        for fa in file_args:
            p = Path(fa).expanduser() if fa.startswith(("/", "~")) else (cur_dir / fa)
            if not p.is_file():
                continue
            if p.suffix.lower() == ".log" or p.name.lower().endswith("_log.txt"):
                n_lines = len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
                if n_lines > max_full:
                    from planner_state import record_telemetry  # noqa: E402

                    record_telemetry("cmd_blocked_raw_cat_log", {"file": p.name, "lines": n_lines})
                    digest_cli = PLUGIN_ROOT / "scripts" / "log_digest.py"
                    return (
                        f"TERMINAL DIET|Raw `cat` on `{p.name}` ({n_lines}L > {max_full}L) blocked -> "
                        f"run `python3 {digest_cli} {p}` or `tail -n 120 {p}`."
                    )
    return None


def check_extra_requests_before_ship(segments: list[str], repo: Path, profile: dict[str, Any]) -> str | None:
    from planner_state import get_pending_extra_requests, load_project_profile  # noqa: E402

    target_repo, ship_info = _resolve_ship_target_repo(segments, repo)
    if not ship_info:
        return None
    t_profile = profile if target_repo.resolve() == repo.resolve() else load_project_profile(target_repo)
    rel, pending = get_pending_extra_requests(target_repo, t_profile)
    if not pending:
        return None
    preview = "\n".join(f"  - {line[:120]}" for line in pending[:5])
    if len(pending) > 5:
        preview += f"\n  (+{len(pending) - 5} line(s))"
    return (
        f"BLOCKED BEFORE COMMIT/PUSH|Pending extra requests in `{rel}`:\n"
        f"{preview}\n"
        f"Read `{rel}`, implement & clear before commit/push."
    )


def check_plan_before_commit(segments: list[str], payload: dict[str, Any]) -> str | None:
    from planner_state import get_paths, has_active_turn_plan, load_json  # noqa: E402

    _, ship_info = _resolve_ship_target_repo(segments, Path("."), want_commit=True)
    if not ship_info or ship_info.get("amend"):
        return None

    _, _, plan_json, hook_file = get_paths(payload)
    hook_state = load_json(hook_file)
    plan_state = load_json(plan_json)
    if not plan_state or not has_active_turn_plan(payload, hook_state):
        return None
    if plan_state.get("lifecycle") in ("awaiting_user", "blocked"):
        return None

    mods = plan_state.get("modules") or []
    ship_only_reqs = set()
    non_ship_reqs = set()
    for m in mods:
        if m.get("domain") == "audit":
            continue
        is_ship_mod = bool(m.get("is_ship")) or m.get("domain") in ("build-ci", "build-deliver")
        for rid in (m.get("linked_reqs") or []):
            if is_ship_mod:
                ship_only_reqs.add(rid)
            else:
                non_ship_reqs.add(rid)
    ship_only_reqs -= non_ship_reqs

    open_mods = [
        m["id"]
        for m in mods
        if m.get("status") != "done" and not m.get("is_ship") and m.get("domain") not in ("build-ci", "build-deliver", "audit")
    ]
    unverified_reqs = [
        r["id"]
        for r in (plan_state.get("requirements") or [])
        if r.get("status") != "verified" and r["id"] not in ship_only_reqs
    ]
    if not open_mods and not unverified_reqs:
        return None

    return (
        "BLOCKED BEFORE COMMIT|Active plan incomplete:\n"
        + (f"- Open modules: {','.join(open_mods)}\n" if open_mods else "")
        + (f"- Unverified REQs: {','.join(unverified_reqs)}\n" if unverified_reqs else "")
        + "Run `plan_tool.py done/req-verify` or `plan_tool.py finish --evidence \"...\"` first."
    )


def check_commit_memory_feedback(
    segments: list[str],
    payload: dict[str, Any],
    repo: Path,
    profile: dict[str, Any],
    hook_state: dict[str, Any],
) -> str | None:
    if hook_state.get("eco_mode"):
        return None

    from planner_state import DEFAULT_CODE_EXTS, DEFAULT_MEMORY_DOCS  # noqa: E402

    commit_seg = None
    for seg in segments:
        _, rest = strip_env_prefix(seg)
        if re.match(r"^git(?:\s+-C\s+\S+)?\s+commit\b", rest):
            commit_seg = seg
            break
    if not commit_seg or "--amend" in commit_seg or "[skip-history]" in commit_seg:
        return None
    if not (repo / ".git").exists():
        return None

    mem_cfg = profile.get("memory_feedback") or {}
    if mem_cfg.get("enabled") is False:
        return None

    doc_rels = mem_cfg.get("docs") or list(DEFAULT_MEMORY_DOCS)
    existing_docs = [repo / r for r in doc_rels if (repo / r).is_file()]
    if not existing_docs:
        return None

    import subprocess  # noqa: E402

    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        changed = [l[3:].strip() for l in res.stdout.splitlines() if len(l) > 3]
    except Exception:
        return None

    exts = tuple(e.lower() for e in (profile.get("code_extensions") or DEFAULT_CODE_EXTS))
    changed_code = [f for f in changed if f.lower().endswith(exts)]
    min_files = int(mem_cfg.get("min_code_files") or 2)
    if len(changed_code) < min_files:
        return None

    now = time.time()
    for mf in existing_docs:
        if (now - mf.stat().st_mtime) < 3600:
            return None

    conv_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(payload.get("conversationId") or "default"))
    marker = Path(f"/tmp/.agy_mem_reminded_{conv_id}")
    if marker.exists() and (now - marker.stat().st_mtime) < 900:
        return None

    try:
        marker.touch()
    except Exception:
        pass

    custom_msg = mem_cfg.get("message")
    if custom_msg:
        return custom_msg
    target_doc = next((r for r in doc_rels if (repo / r).is_file()), doc_rels[0])
    return (
        f"MEMORY_FEEDBACK|{len(changed_code)} code files changed w/o recent `{target_doc}` update -> "
        f"prepend 1-3L milestone/pitfall in symbolic telegraphic EN (`!articles/filler`, ops `-> | &&`, verbatim `path:sym`) or retry `git commit` if N/A."
    )


def check_declarative_command_guards(
    segments: list[str],
    repo: Path,
    profile: dict[str, Any],
) -> str | None:
    guards = profile.get("command_guards") or []
    if not guards:
        return None

    from planner_state import eval_conditions  # noqa: E402

    session_envs: set[str] = set()
    for seg in segments:
        stripped = seg.strip()
        if re.match(r"^export\s+", stripped):
            for em in re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", stripped):
                session_envs.add(em.group(1))
        inline_envs, rest = strip_env_prefix(stripped)
        active_envs = session_envs | set(inline_envs.keys())

        for g in guards:
            if not eval_conditions(g.get("conditions"), repo):
                continue
            match_pat = g.get("match_regex") or ""
            if match_pat and not re.search(match_pat, rest):
                continue

            forbid_args = g.get("forbid_args_regex") or ""
            if forbid_args and re.search(forbid_args, rest):
                return g.get("reason") or "Command blocked by project command guard."

            req_envs = g.get("require_env_vars") or []
            if req_envs:
                missing = [v for v in req_envs if v not in active_envs]
                if missing:
                    tmpl = g.get("reason") or "Missing required environment variables: {missing}."
                    return tmpl.replace("{missing}", ", ".join(missing))
    return None


def _fast_dns_check(host: str = "github.com", port: int = 443, timeout_s: float = 2.0) -> bool:
    if os.environ.get("PLANNER_FORCE_OFFLINE") == "1":
        return False
    if os.environ.get("PLANNER_SKIP_DNS_CHECK") == "1":
        return True
    import socket  # noqa: E402
    import threading  # noqa: E402

    ok = False

    def _probe() -> None:
        nonlocal ok
        try:
            socket.getaddrinfo(host, port)
            ok = True
        except Exception:
            ok = False

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout_s)
    return ok


def check_pre_push_flight(
    segments: list[str],
    ws_repo: Path,
    profile: dict[str, Any],
    hook_state: dict[str, Any],
) -> tuple[str, str] | None:
    from planner_state import (  # noqa: E402
        is_app_code_path,
        load_project_profile,
        record_telemetry,
        scan_secrets_and_conflicts,
    )

    target_repo, ship_info = _resolve_ship_target_repo(segments, ws_repo, want_push=True)
    if not ship_info:
        return None

    t_profile = profile if target_repo.resolve() == ws_repo.resolve() else load_project_profile(target_repo)
    same_ws = target_repo.resolve() == ws_repo.resolve()
    has_git = (target_repo / ".git").exists()

    # 1. Turn build/verify state (applies BOTH with .git and in gitless AI Studio containers)
    if same_ws:
        if hook_state.get("code_files_edited") and (hook_state.get("last_build_failed") or hook_state.get("needs_build")):
            record_telemetry("guarded_push_blocked", {"why": "needs_build_or_failed"})
            return (
                "deny",
                "PRE-PUSH BLOCKED|Code edited in turn with `needs_build` or `last_build_failed` -> run build/verifier first.",
            )
        is_sync_self_test = ship_info["kind"] in ("plan_evolve_sync_all_push", "self_evolve_sync_all_push") and not ship_info.get("no_test")
        if not has_git:
            if hook_state.get("needs_verify"):
                record_telemetry("guarded_push_blocked", {"why": "gitless_needs_verify"})
                return (
                    "deny",
                    "PRE-PUSH BLOCKED|Gitless workspace has unverified changes (`needs_verify=True`) -> run `verify_universal.py` before ship/push.",
                )
            if hook_state.get("needs_plugin_tests") and not is_sync_self_test:
                record_telemetry("guarded_push_blocked", {"why": "gitless_needs_plugin_tests"})
                return (
                    "deny",
                    "PRE-PUSH BLOCKED|Plugin edited without passing tests (`needs_plugin_tests=True`) -> run `verify_universal.py --plugin-tests` before ship/push.",
                )
        else:
            if (hook_state.get("code_files_edited") or hook_state.get("doc_files_edited")) and hook_state.get("needs_verify"):
                record_telemetry("guarded_push_blocked", {"why": "needs_verify"})
                return (
                    "deny",
                    "PRE-PUSH BLOCKED|Edited files have unverified changes (`needs_verify=True`) -> run `verify_universal.py` before ship/push.",
                )
            if hook_state.get("needs_plugin_tests") and not is_sync_self_test:
                record_telemetry("guarded_push_blocked", {"why": "needs_plugin_tests"})
                return (
                    "deny",
                    "PRE-PUSH BLOCKED|Plugin edited without passing tests (`needs_plugin_tests=True`) -> run `verify_universal.py --plugin-tests` before ship/push.",
                )

    binary_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ttf", ".apk", ".dll", ".exe", ".zip")
    sg = t_profile.get("secret_guard") or {}

    if not has_git:
        # Gitless AI Studio fallback: scan hook_state edited files + DNS check without calling `git`
        if same_ws and sg.get("enabled", True):
            edited_rels: set[str] = set()
            for k in ("code_files_edited", "doc_files_edited"):
                for r in hook_state.get(k) or []:
                    if r:
                        c = str(r).replace("\\", "/")
                        while c.startswith("./"):
                            c = c[2:]
                        edited_rels.add(c.lstrip("/"))
            for r in hook_state.get("plugin_files_edited") or []:
                if r:
                    edited_rels.add(f".agents/plugins/dynamic-fractal-planner/{str(r).lstrip('/')}")
            for rel in sorted(edited_rels):
                if rel.lower().endswith(binary_exts):
                    continue
                fp = target_repo / rel
                if not fp.is_file():
                    continue
                txt = fp.read_text(encoding="utf-8", errors="ignore")
                hits = scan_secrets_and_conflicts(txt, rel_path=rel, check_filename=True)
                if hits:
                    record_telemetry("guarded_push_blocked", {"why": "secret_or_conflict_gitless", "file": rel})
                    return ("deny", f"PRE-PUSH BLOCKED|`{rel}`: {hits[0]}")
        if not _fast_dns_check("github.com", 443, 2.0):
            record_telemetry("guarded_push_offline")
            return (
                "deny",
                "PRE-PUSH BLOCKED|Offline/DNS resolution for `github.com` failed (<2s check). Retry once online.",
            )
        return None

    import subprocess  # noqa: E402

    def _git(gargs: list[str]) -> tuple[int, str]:
        try:
            r = subprocess.run(["git", "-C", str(target_repo), *gargs], capture_output=True, text=True, timeout=4, check=False)
            return r.returncode, r.stdout
        except Exception:
            return 1, ""

    # 2. Untracked code files that could break CI
    rc_st, st_out = _git(["status", "--porcelain", "-uall"])
    untracked_code: list[str] = []
    scan_candidates: set[str] = set()
    if rc_st == 0:
        for ln in st_out.splitlines():
            if len(ln) < 4:
                continue
            xy = ln[:2]
            path_part = ln[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1].strip()
            rel = path_part.strip('"').replace("\\", "/")
            if not rel:
                continue
            if xy == "??" and is_app_code_path(rel, target_repo, t_profile):
                untracked_code.append(rel)
            if "D" not in xy:
                scan_candidates.add(rel)

    if untracked_code:
        record_telemetry("guarded_push_blocked", {"why": "untracked_code", "count": len(untracked_code)})
        preview = ", ".join(untracked_code[:4])
        return (
            "deny",
            f"PRE-PUSH BLOCKED|Untracked code file(s) could break CI: {preview}. Stage/commit or remove before push.",
        )

    # 3. Secret & merge-conflict scan on staged/changed/unpushed files
    if sg.get("enabled", True):
        rc_up, up_files = _git(["diff", "--name-only", "@{u}..HEAD"])
        if rc_up == 0:
            for f in up_files.splitlines():
                if f.strip():
                    scan_candidates.add(f.strip())
        else:
            rc_hd, hd_files = _git(["show", "--name-only", "--pretty=", "HEAD"])
            if rc_hd == 0:
                for f in hd_files.splitlines():
                    if f.strip():
                        scan_candidates.add(f.strip())

        for rel in sorted(scan_candidates):
            if rel.lower().endswith(binary_exts):
                continue
            fp = target_repo / rel
            if not fp.is_file():
                continue
            txt = fp.read_text(encoding="utf-8", errors="ignore")
            hits = scan_secrets_and_conflicts(txt, rel_path=rel, check_filename=True)
            if hits:
                record_telemetry("guarded_push_blocked", {"why": "secret_or_conflict", "file": rel})
                return ("deny", f"PRE-PUSH BLOCKED|`{rel}`: {hits[0]}")

    # 4. Fast 2s DNS check (unless remote is local path)
    rc_rm, rm_out = _git(["remote", "get-url", "origin"])
    remote_url = rm_out.strip() if rc_rm == 0 else ""
    is_local_remote = remote_url.startswith(("/", "file://"))
    if not is_local_remote and ("github.com" in remote_url or not remote_url or os.environ.get("PLANNER_FORCE_OFFLINE") == "1"):
        if not _fast_dns_check("github.com", 443, 2.0):
            record_telemetry("guarded_push_offline")
            return (
                "deny",
                "PRE-PUSH BLOCKED|Offline/DNS resolution for `github.com` failed (<2s check). Retry once online.",
            )

    # 5. [skip ci] hint when .github/workflows exists & unpushed commit touches ONLY docs/.agents
    if ship_info["kind"] == "git_push" and (target_repo / ".github" / "workflows").is_dir():
        rc_rng, rng_out = _git(["diff", "--name-only", "@{u}..HEAD"])
        if rc_rng != 0:
            rc_rng, rng_out = _git(["show", "--name-only", "--pretty=", "HEAD"])
        touched = [f.strip().replace("\\", "/") for f in rng_out.splitlines() if f.strip()]
        rc_msg, msg_out = _git(["log", "-1", "--pretty=%B", "HEAD"])
        msg_low = msg_out.lower() if rc_msg == 0 else ""
        if (
            touched
            and all(re.match(r"^(?:.+/)?(?:\.agents/.*|AGENTS\.md|README\.md)$", f) for f in touched)
            and "[skip ci]" not in msg_low
            and "[ci skip]" not in msg_low
        ):
            record_telemetry("guarded_push_skip_ci_hint")
            return (
                "force_ask",
                "PRE-PUSH TIP|`.github/workflows/` present && commit touches ONLY `.agents/|AGENTS.md|README.md` without `[skip ci]`. Confirm push or amend with `[skip ci]`?",
            )

    return None


def _inject_dotnet_minimal(cmd: str) -> str:
    """Append `--nologo -v:minimal` to `dotnet build` segments lacking verbosity/clp flags."""
    def _rewrite_seg(match: re.Match[str]) -> str:
        seg = match.group(0)
        if re.search(r"(?<!\S)(?:-v\b|-v:|--verbosity\b|-clp\b|-clp:|--consoleloggerparameters\b|-consoleloggerparameters\b|--help\b|-h\b)", seg):
            return seg
        # Insert before trailing shell redirect like `2>&1` or `> out.log` if present
        m_redir = re.search(r"\s+(?:\d*>>?|\&>)\s*\S+(?:\s+2>&1)?\s*$", seg)
        flags = " --nologo -v:minimal" if "--nologo" not in seg else " -v:minimal"
        if m_redir:
            pos = m_redir.start()
            return seg[:pos] + flags + seg[pos:]
        return seg + flags

    return re.sub(
        r"(?:[^\n;|&]*?)(?<![A-Za-z0-9_])(?:/root/\.dotnet/)?dotnet\s+build\b[^\n;|&]*",
        _rewrite_seg,
        cmd,
    )


def _inject_pytest_quiet(cmd: str) -> str:
    """Append `-q --tb=short` to bare `pytest` segments lacking verbosity/tb flags."""
    def _rewrite_seg(match: re.Match[str]) -> str:
        seg = match.group(0)
        if re.search(r"(?<!\S)(?:-q\b|--quiet\b|-v\b|--verbose\b|--tb\b|--tb=|-h\b|--help\b)", seg):
            return seg
        m_redir = re.search(r"\s+(?:\d*>>?|\&>)\s*\S+(?:\s+2>&1)?\s*$", seg)
        flags = " -q --tb=short"
        if m_redir:
            pos = m_redir.start()
            return seg[:pos] + flags + seg[pos:]
        return seg + flags

    return re.sub(
        r"(?:[^\n;|&]*?)(?<![A-Za-z0-9_./])(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?pytest\b[^\n;|&]*",
        _rewrite_seg,
        cmd,
    )


def _inject_plugin_tests_fast(cmd: str) -> str:
    """Append `--fast` ONLY to actual python[3] invocations of `run_plugin_tests.py` lacking mode/help/Test filter."""
    parts = _split_shell_parts_with_seps(cmd)
    out: list[str] = []
    for seg_raw, sep in parts:
        parsed = _shlex_strip_env_and_wrappers(seg_raw)
        if parsed is not None:
            _, raw_tokens = parsed
            clean_tokens = _strip_redirect_tokens(raw_tokens)
            py_inv = _parse_python_script_invocation(clean_tokens)
            if py_inv is not None:
                script_tok, script_args = py_inv
                if Path(script_tok).name == "run_plugin_tests.py":
                    has_mode_or_filter = any(
                        arg in ("--fast", "--full", "-h", "--help")
                        or re.fullmatch(r"Test[A-Za-z0-9_.]*", arg) is not None
                        for arg in script_args
                    )
                    if not has_mode_or_filter:
                        m_redir = _TRAILING_REDIR_RE.search(seg_raw)
                        if m_redir:
                            pos = m_redir.start()
                            seg_raw = seg_raw[:pos] + " --fast" + seg_raw[pos:]
                        else:
                            stripped = seg_raw.rstrip()
                            trail = seg_raw[len(stripped) :]
                            seg_raw = stripped + " --fast" + trail
        out.append(seg_raw + sep)
    return "".join(out)


def check_polling_command(cmd: str, hook_file: Path, hook_state: dict[str, Any]) -> str | None:
    """Block shell gh-run sleep-poll loops and repeated `ps` process polling within 120s."""
    if (
        re.search(r"(?:^|[;\n&|]\s*)(?:while|for|until)\b", cmd)
        and re.search(r"(?:^|[;\n&|\s])sleep\s+\d+", cmd)
        and re.search(r"\bgh\s+run\s+(?:list|view)\b", cmd)
    ):
        return (
            "DENY|anti_poll: shell loop polling `gh run list|view` with `sleep` blocked "
            "-> use `gh run watch --exit-status --compact` (or reactive `NotificationTimeoutSeconds`)."
        )

    ps_pipe = re.search(r"\bps\b[^;\n]*\|\s*(?:e?grep|rg)\b([^;\n]*)", cmd, re.IGNORECASE)
    if ps_pipe:
        targets = re.findall(r"\b(dotnet|msbuild|python(?:\d+(?:\.\d+)?)?|pytest)\b", ps_pipe.group(1), re.IGNORECASE)
        if targets:
            from planner_state import save_json  # noqa: E402

            key = "|".join(sorted({t.lower() for t in targets}))
            now = time.time()
            raw_polls = hook_state.get("ps_polls")
            polls: dict[str, float] = (
                {
                    str(k): float(v)
                    for k, v in raw_polls.items()
                    if isinstance(v, (int, float)) and (now - float(v)) < 3600.0
                }
                if isinstance(raw_polls, dict)
                else {}
            )
            prev = polls.get(key)
            if prev is not None and (now - prev) < 120.0:
                elapsed = int(max(0.0, now - prev))
                return (
                    f"DENY|anti_poll: repeated `ps` poll (`{key}`) within 120s ({elapsed}s ago). "
                    "Wait for reactive command completion (`NotificationTimeoutSeconds`) instead of polling `ps`."
                )
            polls[key] = now
            hook_state["ps_polls"] = polls
            save_json(hook_file, hook_state)

    return None


_TOOL_INVOKE_NAMES = frozenset({"plan_tool.py", "verify_universal.py", "verify_changes.py", "run_plugin_tests.py"})


def _segment_invokes_planner_tool(seg: str) -> bool:
    parsed = _shlex_strip_env_and_wrappers(seg)
    if parsed is None:
        return False
    clean = _strip_redirect_tokens(parsed[1])
    if not clean:
        return False
    py_inv = _parse_python_script_invocation(clean)
    if py_inv is not None:
        return Path(py_inv[0]).name in _TOOL_INVOKE_NAMES
    return Path(clean[0]).name in _TOOL_INVOKE_NAMES


def maybe_rewrite_command(cmd: str, repo: Path, payload: dict[str, Any]) -> str | None:
    from planner_state import prompt_hash, resolve_artifact_dir  # noqa: E402

    updated = _inject_plugin_tests_fast(_inject_pytest_quiet(_inject_dotnet_minimal(cmd)))

    if ".agents/scripts/plan_tool.py" in updated and not (repo / ".agents/scripts/plan_tool.py").exists():
        updated = updated.replace(".agents/scripts/plan_tool.py", str(PLUGIN_ROOT / "scripts/plan_tool.py"))
    if ".agents/scripts/verify_universal.py" in updated and not (repo / ".agents/scripts/verify_universal.py").exists():
        updated = updated.replace(".agents/scripts/verify_universal.py", str(PLUGIN_ROOT / "scripts/verify_universal.py"))

    invokes_tool = any(_segment_invokes_planner_tool(seg) for seg in split_shell_segments(updated))
    if invokes_tool and "PLANNER_ARTIFACT_DIR=" not in updated:
        art_dir = str(resolve_artifact_dir(payload))
        conv_id = str(payload.get("conversationId") or "default")
        p_hash = prompt_hash(payload.get("lastUserInput") or "")
        prefix = (
            f"export PLANNER_ARTIFACT_DIR={shlex.quote(art_dir)} "
            f"CAVERN_ARTIFACT_DIR={shlex.quote(art_dir)} "
            f"PLANNER_CONV_ID={shlex.quote(conv_id)} "
            f"PLANNER_LAST_PROMPT_HASH={shlex.quote(p_hash)} "
            f"PLANNER_WORKSPACE_ROOT={shlex.quote(str(repo))}"
        )
        updated = f"{prefix}\n{updated}"

    return updated if updated != cmd else None


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        allow()
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        tool = payload.get("toolCall") or {}
        args = tool.get("args") or {}
        cmd = (args.get("CommandLine") or "").strip()
        if not cmd:
            allow()
            return

        _ensure_paths()
        from planner_state import get_paths, load_json, load_project_profile, resolve_repo_root  # noqa: E402

        repo = resolve_repo_root(payload)
        profile = load_project_profile(repo)
        segments = split_shell_segments(cmd)

        cat_msg = check_raw_cat_log(cmd, repo, profile)
        if cat_msg:
            deny(cat_msg, payload=payload, repo=repo)
            return

        extra_msg = check_extra_requests_before_ship(segments, repo, profile)
        if extra_msg:
            deny(extra_msg, payload=payload, repo=repo)
            return

        plan_msg = check_plan_before_commit(segments, payload)
        if plan_msg:
            deny(plan_msg, payload=payload, repo=repo)
            return

        _, _, _, hook_file = get_paths(payload)
        hook_state = load_json(hook_file)

        try:
            from wave_scheduler import is_parallel_wave_worker  # noqa: E402

            conv_id = str(payload.get("conversationId") or "").strip()
            if (w_info := is_parallel_wave_worker(repo, conv_id)):
                heavy_pat = re.compile(
                    r"(?:(?:/root/\.dotnet/)?dotnet\s+(?:build|test|publish|pack|run)\b|"
                    r"(?:run_plugin_tests|verify_universal|run_w46_unit_tests|run_audio_ground_truth)\.py\b|"
                    r"git\s+(?:-C\s+\S+\s+)?(?:push|commit)\b)",
                    re.I,
                )
                for seg in segments:
                    _, rest = strip_env_prefix(seg)
                    hm = heavy_pat.search(rest)
                    ship_hit = _classify_ship_segment(seg)
                    if hm or ship_hit:
                        what = hm.group(0) if hm else str(ship_hit["kind"])
                        deny(
                            f"LEASE CMD DENY|Parallel wave `{w_info[2]}` ({w_info[1]}) cannot run `{what}` directly; "
                            "Epoch Barrier runs verification & git sequentially ONCE.",
                            payload=payload,
                            repo=repo,
                        )
                        return
        except Exception:
            pass

        poll_msg = check_polling_command(cmd, hook_file, hook_state)
        if poll_msg:
            deny(poll_msg, payload=payload, repo=repo)
            return

        mem_msg = check_commit_memory_feedback(segments, payload, repo, profile, hook_state)
        if mem_msg:
            deny(mem_msg, payload=payload, repo=repo)
            return

        cmd_msg = check_declarative_command_guards(segments, repo, profile)
        if cmd_msg:
            deny(cmd_msg, payload=payload, repo=repo)
            return

        push_hit = check_pre_push_flight(segments, repo, profile, hook_state)
        if push_hit:
            reply(push_hit[0], push_hit[1], payload=payload, repo=repo)
            return

        allow(maybe_rewrite_command(cmd, repo, payload))
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            _ensure_paths()
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_guard_command.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _ensure_paths()
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_guard_command.py", exc)
        except Exception:
            pass
        print('{"decision": "allow"}')
