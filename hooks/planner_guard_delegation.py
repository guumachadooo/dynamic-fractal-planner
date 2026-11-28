#!/usr/bin/env python3
"""Universal PreToolUse hook (`invoke_subagent|swarm|run_workflow`): enforces thin-coordinator
complexity-tiered model routing and Google/Gemini-only quota protection.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = HOOKS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

COMPLEX_MARKERS = (
    "[l3]",
    "architect",
    "dsp",
    "physics",
    "math",
    "security",
    "concurrency",
    "race",
    "deadlock",
    "sota",
    "root cause",
    "forensic",
    "deep audit",
)
_POSITIVE_COMPLEX_MARKERS = tuple(m for m in COMPLEX_MARKERS if m != "escalat")
_NEG_MARKER_ALT = "(?:" + "|".join(
    re.escape(m) if m.startswith("[") else (re.escape(m) + r"[a-z0-9_-]*")
    for m in _POSITIVE_COMPLEX_MARKERS
) + ")"
_FORBIDDEN_PAREN_RE = re.compile(r"(?i)\(\s*forbidden\s*:[^)\n]*\)")
_FORBIDDEN_CLAUSE_RE = re.compile(
    r"(?im)\bforbidden\s*:(?:[^\n.]|\.(?!\s|$)|(?<=e\.g)\.|(?<=i\.e)\.)*"
)
_NEGATED_MARKER_RE = re.compile(
    r"\b(?:no|not|non|without|never|avoid|skip)"
    r"(?:[-\s:]+(?:[a-z0-9_'-]+\s+){0,3}|[-\s:]+)"
    + _NEG_MARKER_ALT
    + r"(?:(?:\s*(?:/|\bor\b|\bnor\b|\band\b)\s*|\s+)" + _NEG_MARKER_ALT + r")*",
    re.IGNORECASE,
)
INHERIT_MARKERS = ("[l3]", "[inherit]")
NESTING_MARKERS = ("sub-subagent", "sub_subagent", "nested subagent", "nested cubicle")
WORKFLOW_MODEL_RE = re.compile(r"""model\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _has_positive_complex_marker(text: str) -> bool:
    """Return True if text has a non-negated, non-FORBIDDEN complex domain marker."""
    s = (text or "").lower()
    if not s.strip():
        return False
    cleaned = _FORBIDDEN_PAREN_RE.sub(" ", s)
    cleaned = _FORBIDDEN_CLAUSE_RE.sub(" ", cleaned)
    cleaned = _NEGATED_MARKER_RE.sub(" ", cleaned)
    return any(m in cleaned for m in _POSITIVE_COMPLEX_MARKERS)


def _is_flat_cubicle(payload: dict[str, Any] | None, repo: Path | None = None) -> bool:
    """Lazily check whether caller conversationId belongs to a flat horizontal lease cubicle."""
    if not isinstance(payload, dict):
        return False
    conv_id = str(payload.get("conversationId") or payload.get("conversation_id") or "").strip()
    if not conv_id:
        return False
    try:
        _ensure_paths()
        if repo is None:
            from planner_state import resolve_repo_root  # noqa: E402

            repo = resolve_repo_root(payload)
        from wave_scheduler import is_flat_lease_worker  # noqa: E402

        sig = inspect.signature(is_flat_lease_worker)
        params = list(sig.parameters.values())
        pos_params = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if len(pos_params) == 1 and not has_varargs:
            return bool(is_flat_lease_worker(conv_id))
        if pos_params and pos_params[0].name.lower().startswith("conv"):
            return bool(is_flat_lease_worker(conv_id, repo))
        return bool(is_flat_lease_worker(repo, conv_id))
    except Exception:
        return False


def _is_termux_constrained(repo: Path | None) -> bool:
    """Lazily check Termux PPK concurrency constraint with graceful fallback."""
    try:
        _ensure_paths()
        from wave_scheduler import is_termux_constrained  # noqa: E402

        sig = inspect.signature(is_termux_constrained)
        params = list(sig.parameters.values())
        pos_params = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if not pos_params and not has_varargs:
            return bool(is_termux_constrained())
        return bool(is_termux_constrained(repo))
    except Exception:
        return False


def _ensure_paths() -> None:
    for p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _allow() -> None:
    print('{"decision":"allow"}')


def _deny(
    reason: str,
    payload: dict[str, Any] | None = None,
    repo: Path | None = None,
    rule_sig: str = "",
) -> None:
    print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))
    if payload is not None:
        try:
            _ensure_paths()
            from planner_state import get_paths, load_json, resolve_repo_root  # noqa: E402
            from plugin_diagnostics import record_guard_deny_if_burst  # noqa: E402

            _, _, _, hook_file = get_paths(payload)
            hook_state = load_json(hook_file)
            sig = rule_sig or reason.split("|", 1)[0].strip()[:40]
            r_path = repo if repo is not None else resolve_repo_root(payload)
            record_guard_deny_if_burst("planner_guard_delegation.py", sig, reason, hook_file, hook_state, repo=r_path)
        except Exception:
            pass


def _find_blocked_vendor(model: str, blocked_patterns: list[str]) -> str:
    ml = (model or "").strip().lower()
    if not ml:
        return ""
    if ml in ("o1", "o3", "o4"):
        return ml
    for pat in blocked_patterns:
        p = str(pat).strip().lower()
        if p and p in ml:
            return p
    return ""


def _is_google_medium_model(model: str, sub_cfg: dict[str, Any]) -> bool:
    ml = (model or "").strip().lower()
    if not ml:
        return False
    swarm_med = str(sub_cfg.get("swarm_medium_model") or "gemini-4-hydrogen-medium").strip().lower()
    return ml == swarm_med or (ml.startswith("gemini-") and ml.endswith("-medium"))


def _check_invoke_subagent(
    args: dict[str, Any],
    cfg: dict[str, Any],
    payload: dict[str, Any] | None = None,
    repo: Path | None = None,
) -> tuple[str, str] | None:
    if _is_flat_cubicle(payload, repo):
        return (
            "DENY|flat_cubicle_no_nest: flat horizontal cubicle cannot spawn subagents or swarm workers.",
            "DENY:flat_cubicle_no_nest",
        )

    type_name = str(args.get("TypeName") or args.get("typeName") or "").strip()
    exempt = {str(x).strip().lower() for x in (cfg.get("exempt_subagent_types") or []) if str(x).strip()}
    if type_name.lower() in exempt:
        return None

    model = str(args.get("Model") or args.get("model") or "").strip()
    blocked_patterns = list(cfg.get("blocked_model_patterns") or [])
    if cfg.get("enforce_google_only", True) and model:
        hit = _find_blocked_vendor(model, blocked_patterns)
        if hit:
            return (
                f"DENY|google_only: blocked non-Google model `{model}` (matched `{hit}`). Use Google/Gemini (`flash_lite`|`flash`|`pro`).",
                f"DENY:google_only:{hit}",
            )

    if cfg.get("require_explicit_model", True) and not model:
        return (
            "DENY|explicit_model: omit Model -> defaults to costly `inherit`. Pass explicit `flash_lite` (L0), `flash` (L1-2), or `pro` (L3).",
            "DENY:explicit_model",
        )

    ml = model.lower()
    prompt_role = f"{args.get('Role') or args.get('role') or ''} {args.get('Prompt') or args.get('prompt') or ''}".lower()
    sub_cfg = cfg.get("sub_subagent") if isinstance(cfg.get("sub_subagent"), dict) else {}
    allow_nesting = bool(sub_cfg.get("allow_nesting", True))
    allow_medium_tier = bool(sub_cfg.get("allow_medium_tier", True))

    if not allow_nesting and any(m in prompt_role for m in NESTING_MARKERS):
        return (
            "DENY|sub_subagent: nested sub-subagent disabled (`delegation.sub_subagent.allow_nesting=false`).",
            "DENY:sub_subagent_disabled",
        )

    if not allow_medium_tier and cfg.get("prefer_cheap_models", True) and _is_google_medium_model(model, sub_cfg):
        if not _has_positive_complex_marker(prompt_role):
            return (
                f"DENY|sub_subagent: medium tier model `{model}` disabled (`allow_medium_tier=false`) on non-complex slice -> use `flash_lite`|`flash`|`gemini-*-low`.",
                "DENY:sub_medium_disabled",
            )

    if cfg.get("prefer_cheap_models", True) and ml == "inherit":
        if not any(m in prompt_role for m in INHERIT_MARKERS):
            return (
                "DENY|prefer_cheap_models: `inherit` unannotated -> pick explicit `flash_lite` (L0) / `flash` (L1-2) / `pro` (L3), or tag `[L3]`/`[inherit]`.",
                "DENY:inherit_model",
            )

    if cfg.get("prefer_cheap_models", True) and ml == "pro" and type_name.lower() == "research":
        if not _has_positive_complex_marker(prompt_role):
            return (
                "DENY|prefer_cheap_models: trivial `research` on `pro` -> use `flash_lite`/`flash` or tag `[L3]` / complex domain.",
                "DENY:research_pro",
            )

    return None


def _check_swarm(
    args: dict[str, Any],
    cfg: dict[str, Any],
    payload: dict[str, Any] | None = None,
    repo: Path | None = None,
) -> tuple[str, str] | None:
    action = str(args.get("action") or args.get("Action") or "").strip().lower()
    if action in ("add", "") and _is_flat_cubicle(payload, repo):
        return (
            "DENY|flat_cubicle_no_nest: flat horizontal cubicle cannot spawn subagents or swarm workers.",
            "DENY:flat_cubicle_no_nest",
        )
    if action not in ("add", "model"):
        return None

    model = str(args.get("model") or args.get("Model") or "").strip()
    blocked_patterns = list(cfg.get("blocked_model_patterns") or [])
    if cfg.get("enforce_google_only", True) and model:
        hit = _find_blocked_vendor(model, blocked_patterns)
        if hit:
            return (
                f"DENY|google_only: blocked non-Google swarm model `{model}` (matched `{hit}`). Use Google/Gemini (`flash_lite`|`flash`|`pro`|`gemini-*`).",
                f"DENY:swarm_google_only:{hit}",
            )

    if action == "add" and cfg.get("require_explicit_model", True) and not model:
        return (
            "DENY|explicit_model: `swarm add` missing `model`. Pass explicit cheap Google/Gemini model (`flash_lite`, `flash`, `gemini-4-hydrogen-medium`, or `gemini-*-low`).",
            "DENY:swarm_explicit_model",
        )

    sub_cfg = cfg.get("sub_subagent") if isinstance(cfg.get("sub_subagent"), dict) else {}
    allow_nesting = bool(sub_cfg.get("allow_nesting", True))
    allow_medium_tier = bool(sub_cfg.get("allow_medium_tier", True))
    brief_txt = f"{args.get('name') or args.get('Name') or ''} {args.get('brief') or args.get('Brief') or ''}".lower()

    if not allow_nesting and any(m in brief_txt for m in NESTING_MARKERS):
        return (
            "DENY|sub_subagent: nested swarm worker disabled (`delegation.sub_subagent.allow_nesting=false`).",
            "DENY:swarm_sub_disabled",
        )

    if not allow_medium_tier and cfg.get("prefer_cheap_models", True) and _is_google_medium_model(model, sub_cfg):
        if not _has_positive_complex_marker(brief_txt):
            return (
                f"DENY|sub_subagent: swarm medium tier `{model}` disabled (`allow_medium_tier=false`) on non-complex slice -> use `flash_lite`|`flash`|`gemini-*-low`.",
                "DENY:swarm_medium_disabled",
            )

    return None


def _check_define_subagent(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str] | None:
    sub_cfg = cfg.get("sub_subagent") if isinstance(cfg.get("sub_subagent"), dict) else {}
    allow_nesting = bool(sub_cfg.get("allow_nesting", True))
    enable_sub = args.get("EnableSubagentTools")
    if enable_sub is None:
        enable_sub = args.get("enableSubagentTools")
    if bool(enable_sub) and not allow_nesting:
        return (
            "DENY|sub_subagent: `define_subagent(EnableSubagentTools=true)` disabled (`allow_nesting=false`).",
            "DENY:define_sub_disabled",
        )
    blocked_patterns = list(cfg.get("blocked_model_patterns") or [])
    if cfg.get("enforce_google_only", True):
        model = str(args.get("Model") or args.get("model") or "").strip()
        if model and (hit := _find_blocked_vendor(model, blocked_patterns)):
            return (
                f"DENY|google_only: blocked non-Google model `{model}` (matched `{hit}`).",
                f"DENY:define_google_only:{hit}",
            )
        sys_txt = str(args.get("SystemPrompt") or args.get("systemPrompt") or "")
        for m in WORKFLOW_MODEL_RE.findall(sys_txt):
            if (hit := _find_blocked_vendor(m, blocked_patterns)):
                return (
                    f"DENY|google_only: `define_subagent` prompt uses blocked model `{m}` (matched `{hit}`).",
                    f"DENY:define_prompt_google_only:{hit}",
                )
    return None


_WORKFLOW_HEAVY_RE = re.compile(
    r"(?:(?:/root/\.dotnet/)?dotnet\s+(?:build|test|publish|pack|run)\b|"
    r"(?:^|[\s\"'`;|&])(?:python[0-9.]*\s+(?:-[A-Za-z0-9]+\s+)*|bash\s+|sh\s+|\./|\brun\s+|\bexec\s+)"
    r"[^\n\"']*\b(?:run_plugin_tests|verify_universal|run_w46_unit_tests|run_audio_ground_truth|check_android_cs)\.py\b|"
    r"(?:^|[\s\"'`;|&])(?:python[0-9.]*\s+(?:-[A-Za-z0-9]+\s+)*-m\s+)?\bpytest\b|"
    r"omni_agent\.py\s+(?:[^\n\"']*\s+)?ship\b|"
    r"git\s+(?:-C\s+\S+\s+)?(?:push|commit)\b)",
    re.IGNORECASE,
)
_ALLOWED_GOOGLE_MODELS = frozenset({"flash_lite", "flash", "pro", "inherit"})
_ALLOWED_SUBPROCESS_SCRIPTS = frozenset({"plan_tool.py", "repo_map.py", "file_budget_guard.py"})
_ALLOWED_GIT_SUBCMDS = frozenset({"status", "diff", "log", "rev-parse"})
_DANGEROUS_BUILTINS = frozenset({"eval", "exec", "__import__"})
_SAFE_PYTHON_FLAGS = frozenset({"-B", "-u", "-E", "-s", "-O", "-OO"})
_SAFE_GIT_FLAGS = frozenset({"--no-pager", "--literal-pathspecs", "--no-optional-locks"})
_SYS_EXEC_SENTINEL = "__SYS_EXECUTABLE__"
_STARRED_SENTINEL = "__STARRED__"
_UNRESOLVED = object()


class _BoundedInt:
    """Represents an upper-bounded integer value such as `min(2, dynamic_expr)`."""

    def __init__(self, upper: int) -> None:
        self.upper = upper


class _L3GuardedModel:
    """Marks a model value whose `'pro'` branch is guarded by `tier == 'L3'`."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _WorkflowAstAuditor(ast.NodeVisitor):
    """Stdlib AST auditor enforcing workflow model routing, subprocess allowlist, heavy-op ban, and Termux width."""

    def __init__(self, script_src: str, cfg: dict[str, Any], repo: Path | None = None) -> None:
        self.script_src = script_src
        self.cfg = cfg
        self.repo = repo
        self.parents: dict[ast.AST, ast.AST] = {}
        self.aliases: dict[str, str] = {}
        self.func_defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.module_env: dict[str, list[ast.AST]] = {}
        self.func_envs: dict[str, dict[str, list[ast.AST]]] = {}
        self.call_sites: dict[str, list[tuple[ast.Call, str | None]]] = {}
        self.all_calls: list[tuple[ast.Call, str | None]] = []
        self.parallel_seeds: list[tuple[ast.AST, str | None]] = []
        self.sem_guarded_fns: set[str] = set()
        self.schedule_entries: list[dict[str, Any]] = []
        self._fn_stack: list[str] = []

    def _index_tree(self, tree: ast.Module) -> None:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.visit(tree)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            as_name = alias.asname or alias.name.split(".")[0]
            self.aliases[as_name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            as_name = alias.asname or alias.name
            self.aliases[as_name] = f"{mod}.{alias.name}" if mod else alias.name
        self.generic_visit(node)

    def _record_assign(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            env = self.func_envs.setdefault(self._fn_stack[-1], {}) if self._fn_stack else self.module_env
            env.setdefault(target.id, []).append(value)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            for sub_t, sub_v in zip(target.elts, value.elts):
                self._record_assign(sub_t, sub_v)

    def visit_Assign(self, node: ast.Assign) -> None:
        for tgt in node.targets:
            self._record_assign(tgt, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record_assign(node.target, node.value)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.func_defs[node.name] = node
        self.func_envs.setdefault(node.name, {})
        self._fn_stack.append(node.name)
        self.generic_visit(node)
        self._fn_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.func_defs[node.name] = node
        self.func_envs.setdefault(node.name, {})
        self._fn_stack.append(node.name)
        self.generic_visit(node)
        self._fn_stack.pop()

    def _callee_name(self, func: ast.AST, fn_name: str | None = None, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(func, ast.Name):
            raw = func.id
            if raw in self.aliases:
                return self.aliases[raw]
            if fn_name and raw in self.func_envs.get(fn_name, {}):
                vals = self.func_envs[fn_name][raw]
                if len(vals) == 1 and isinstance(vals[0], (ast.Name, ast.Attribute)):
                    resolved = self._callee_name(vals[0], fn_name, depth + 1)
                    if resolved:
                        return resolved
            if raw in self.module_env:
                vals = self.module_env[raw]
                if len(vals) == 1 and isinstance(vals[0], (ast.Name, ast.Attribute)):
                    resolved = self._callee_name(vals[0], None, depth + 1)
                    if resolved:
                        return resolved
            return raw
        if isinstance(func, ast.Attribute):
            base = self._callee_name(func.value, fn_name, depth + 1)
            return f"{base}.{func.attr}" if base else func.attr
        return ""

    def visit_Call(self, node: ast.Call) -> None:
        fn_name = self._fn_stack[-1] if self._fn_stack else None
        self.all_calls.append((node, fn_name))
        cname = self._callee_name(node.func, fn_name)
        if cname:
            short = cname.split(".")[-1]
            self.call_sites.setdefault(cname, []).append((node, fn_name))
            if short != cname:
                self.call_sites.setdefault(short, []).append((node, fn_name))
            if cname in ("parallel", "asyncio.gather", "gather") or short in ("ThreadPoolExecutor", "TaskGroup"):
                self.parallel_seeds.append((node, fn_name))
            elif cname.endswith((".submit", ".map", ".create_task")) and self._in_executor_or_tg(node, fn_name):
                self.parallel_seeds.append((node, fn_name))
        self.generic_visit(node)

    def _in_executor_or_tg(self, node: ast.AST, fn_name: str | None) -> bool:
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.With, ast.AsyncWith)):
                for item in cur.items:
                    if isinstance(item.context_expr, ast.Call):
                        cname = self._callee_name(item.context_expr.func, fn_name)
                        if cname.split(".")[-1] in ("ThreadPoolExecutor", "TaskGroup"):
                            return True
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            cur = self.parents.get(cur)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            var = node.func.value.id
            rhs_list = (
                (self.func_envs.get(fn_name, {}).get(var, []) if fn_name else [])
                + self.module_env.get(var, [])
            )
            for rhs in rhs_list:
                if isinstance(rhs, ast.Call):
                    cname = self._callee_name(rhs.func, fn_name)
                    if cname.split(".")[-1] in ("ThreadPoolExecutor", "TaskGroup"):
                        return True
        return False

    def visit_With(self, node: ast.With) -> None:
        self._check_with_node(node)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._check_with_node(node)
        self.generic_visit(node)

    def _check_with_node(self, node: ast.With | ast.AsyncWith) -> None:
        fn_name = self._fn_stack[-1] if self._fn_stack else None
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                cname = self._callee_name(item.context_expr.func, fn_name)
                if cname.split(".")[-1] in ("ThreadPoolExecutor", "TaskGroup"):
                    self.parallel_seeds.append((node, fn_name))
            if fn_name and self._is_bounded_semaphore_expr(item.context_expr, fn_name):
                self.sem_guarded_fns.add(fn_name)

    def _collect_binding_envs(
        self,
        node: ast.AST,
        fn_name: str | None,
        fn_depth: int = 0,
    ) -> list[dict[str, Any]]:
        if fn_depth > 4:
            return [{}]
        base_envs: list[dict[str, Any]] = [{}]
        if fn_name and fn_name in self.func_defs:
            fdef = self.func_defs[fn_name]
            sites = self.call_sites.get(fn_name, [])
            if sites:
                base_envs = []
                for c_node, caller_fn in sites[:16]:
                    caller_envs = self._collect_binding_envs(c_node, caller_fn, fn_depth + 1)
                    for c_env in caller_envs:
                        mapped = self._bind_call_params(fdef, c_node, caller_fn, c_env)
                        base_envs.append(mapped)
                        if len(base_envs) >= 32:
                            break
                    if len(base_envs) >= 32:
                        break
            else:
                defaults_env = self._bind_param_defaults(fdef, fn_name)
                base_envs = [defaults_env]

        # Collect enclosing loop/comprehension generators from outermost to innermost
        gens: list[tuple[ast.AST, ast.AST]] = []
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)) and cur is not node:
                break
            if isinstance(cur, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                for g in reversed(cur.generators):
                    gens.append((g.target, g.iter))
            elif isinstance(cur, (ast.For, ast.AsyncFor)):
                gens.append((cur.target, cur.iter))
            cur = self.parents.get(cur)
        gens.reverse()

        envs = base_envs
        for tgt, iter_expr in gens:
            next_envs: list[dict[str, Any]] = []
            for env in envs:
                items = self._eval_iter_items(iter_expr, fn_name, env)
                if items is None:
                    next_envs.append(env)
                elif not items:
                    continue
                else:
                    for item in items[:32]:
                        nb = dict(env)
                        self._bind_target(tgt, item, nb)
                        next_envs.append(nb)
                        if len(next_envs) >= 64:
                            break
                if len(next_envs) >= 64:
                    break
            envs = next_envs or base_envs
        return envs[:64]

    def _bind_param_defaults(self, fdef: ast.FunctionDef | ast.AsyncFunctionDef, fn_name: str) -> dict[str, Any]:
        env: dict[str, Any] = {}
        pos_args = list(fdef.args.posonlyargs) + list(fdef.args.args)
        defs = list(fdef.args.defaults)
        offset = len(pos_args) - len(defs)
        for i, d_node in enumerate(defs):
            p_name = pos_args[offset + i].arg
            vals = self._eval_variants(d_node, fn_name, {})
            if vals and vals[0] is not _UNRESOLVED:
                env[p_name] = vals[0]
        for kw_arg, kw_def in zip(fdef.args.kwonlyargs, fdef.args.kw_defaults):
            if kw_def is not None:
                vals = self._eval_variants(kw_def, fn_name, {})
                if vals and vals[0] is not _UNRESOLVED:
                    env[kw_arg.arg] = vals[0]
        return env

    def _bind_call_params(
        self,
        fdef: ast.FunctionDef | ast.AsyncFunctionDef,
        c_node: ast.Call,
        caller_fn: str | None,
        caller_env: dict[str, Any],
    ) -> dict[str, Any]:
        env = self._bind_param_defaults(fdef, fdef.name)
        pos_args = list(fdef.args.posonlyargs) + list(fdef.args.args)
        eval_pos: list[Any] = []
        for a in c_node.args:
            if isinstance(a, ast.Starred):
                sv = self._eval_variants(a.value, caller_fn, caller_env)
                if sv and isinstance(sv[0], (list, tuple)):
                    eval_pos.extend(sv[0])
                else:
                    eval_pos.append(_STARRED_SENTINEL)
            else:
                av = self._eval_variants(a, caller_fn, caller_env)
                eval_pos.append(av[0] if av else _UNRESOLVED)
        for p, val in zip(pos_args, eval_pos):
            env[p.arg] = val
        if fdef.args.vararg is not None:
            rem = eval_pos[len(pos_args):]
            env[fdef.args.vararg.arg] = tuple(rem)
        for kw in c_node.keywords:
            if kw.arg is not None:
                kv = self._eval_variants(kw.value, caller_fn, caller_env)
                env[kw.arg] = kv[0] if kv else _UNRESOLVED
        return env

    def _bind_target(self, tgt: ast.AST, val: Any, env: dict[str, Any]) -> None:
        if isinstance(tgt, ast.Name):
            env[tgt.id] = val
        elif isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(val, (tuple, list)):
            for sub_t, sub_v in zip(tgt.elts, val):
                self._bind_target(sub_t, sub_v, env)

    def _eval_iter_items(self, iter_expr: ast.AST, fn_name: str | None, env: dict[str, Any]) -> list[Any] | None:
        if isinstance(iter_expr, ast.Call):
            if isinstance(iter_expr.func, ast.Attribute) and iter_expr.func.attr in ("items", "values", "keys"):
                base = self._eval_variants(iter_expr.func.value, fn_name, env)
                if base and isinstance(base[0], dict):
                    if iter_expr.func.attr == "items":
                        return list(base[0].items())
                    if iter_expr.func.attr == "values":
                        return list(base[0].values())
                    return list(base[0].keys())
            if isinstance(iter_expr.func, ast.Name) and iter_expr.func.id == "enumerate" and iter_expr.args:
                seq = self._eval_iter_items(iter_expr.args[0], fn_name, env)
                if seq is not None:
                    return list(enumerate(seq))
        vals = self._eval_variants(iter_expr, fn_name, env)
        if not vals or vals[0] is _UNRESOLVED:
            return None
        v = vals[0]
        if isinstance(v, (list, tuple)):
            return list(v)
        if isinstance(v, dict):
            return list(v.keys())
        return None

    def _unwrap(self, v: Any) -> Any:
        return v.value if isinstance(v, _L3GuardedModel) else v

    def _eval_variants(
        self,
        expr: ast.AST,
        fn_name: str | None = None,
        bindings: dict[str, Any] | None = None,
        depth: int = 0,
    ) -> list[Any]:
        if depth > 12:
            return [_UNRESOLVED]
        b = bindings or {}
        if isinstance(expr, ast.Constant):
            return [expr.value]
        if isinstance(expr, ast.Name):
            if expr.id in b:
                return [b[expr.id]]
            if fn_name and expr.id in self.func_envs.get(fn_name, {}):
                out: list[Any] = []
                for rhs in self.func_envs[fn_name][expr.id]:
                    out.extend(self._eval_variants(rhs, fn_name, b, depth + 1))
                return out or [_UNRESOLVED]
            if expr.id in self.module_env:
                out = []
                for rhs in self.module_env[expr.id]:
                    out.extend(self._eval_variants(rhs, None, b, depth + 1))
                return out or [_UNRESOLVED]
            return [_UNRESOLVED]
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name):
                mod = self.aliases.get(expr.value.id, expr.value.id)
                if mod == "sys" and expr.attr == "executable":
                    return [_SYS_EXEC_SENTINEL]
            return [_UNRESOLVED]
        if isinstance(expr, (ast.List, ast.Tuple)):
            res: list[Any] = []
            for elt in expr.elts:
                if isinstance(elt, ast.Starred):
                    sv = self._eval_variants(elt.value, fn_name, b, depth + 1)
                    if sv and isinstance(sv[0], (list, tuple)):
                        res.extend(sv[0])
                    else:
                        res.append(_STARRED_SENTINEL)
                else:
                    ev = self._eval_variants(elt, fn_name, b, depth + 1)
                    res.append(ev[0] if ev else _UNRESOLVED)
            return [tuple(res) if isinstance(expr, ast.Tuple) else res]
        if isinstance(expr, ast.Dict):
            d: dict[Any, Any] = {}
            for k_node, v_node in zip(expr.keys, expr.values):
                if k_node is None:
                    return [_UNRESOLVED]
                kv = self._eval_variants(k_node, fn_name, b, depth + 1)
                vv = self._eval_variants(v_node, fn_name, b, depth + 1)
                if kv and kv[0] is not _UNRESOLVED:
                    try:
                        hash(kv[0])
                        d[kv[0]] = vv[0] if vv else _UNRESOLVED
                    except TypeError:
                        return [_UNRESOLVED]
            return [d]
        if isinstance(expr, ast.JoinedStr):
            parts: list[str] = []
            for val in expr.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    parts.append(val.value)
                elif isinstance(val, ast.FormattedValue):
                    fv = self._eval_variants(val.value, fn_name, b, depth + 1)
                    v0 = self._unwrap(fv[0]) if fv else _UNRESOLVED
                    parts.append(str(v0) if isinstance(v0, (str, int, float)) and v0 != _SYS_EXEC_SENTINEL else "{*}")
                else:
                    parts.append("{*}")
            return ["".join(parts)]
        if isinstance(expr, ast.BinOp):
            lv = self._eval_variants(expr.left, fn_name, b, depth + 1)
            rv = self._eval_variants(expr.right, fn_name, b, depth + 1)
            l0 = self._unwrap(lv[0]) if lv else _UNRESOLVED
            r0 = self._unwrap(rv[0]) if rv else _UNRESOLVED
            if isinstance(expr.op, ast.Add):
                if isinstance(l0, str) and isinstance(r0, str):
                    return [l0 + r0]
                if isinstance(l0, list) and isinstance(r0, list):
                    return [l0 + r0]
                if isinstance(l0, tuple) and isinstance(r0, tuple):
                    return [l0 + r0]
                if isinstance(l0, int) and not isinstance(l0, bool) and isinstance(r0, int) and not isinstance(r0, bool):
                    return [l0 + r0]
            elif isinstance(expr.op, ast.Sub):
                if isinstance(l0, int) and not isinstance(l0, bool) and isinstance(r0, int) and not isinstance(r0, bool):
                    return [l0 - r0]
            elif isinstance(expr.op, ast.Div) and isinstance(r0, str):
                prefix = l0.rstrip("/") if isinstance(l0, str) else "{*}"
                return [f"{prefix}/{r0.lstrip('/')}"]
            return [_UNRESOLVED]
        if isinstance(expr, ast.Subscript):
            vv = self._eval_variants(expr.value, fn_name, b, depth + 1)
            v0 = vv[0] if vv else _UNRESOLVED
            if isinstance(expr.slice, ast.Slice):
                if isinstance(v0, (list, tuple)):
                    lo = self._eval_variants(expr.slice.lower, fn_name, b, depth + 1)[0] if expr.slice.lower else None
                    hi = self._eval_variants(expr.slice.upper, fn_name, b, depth + 1)[0] if expr.slice.upper else None
                    st = self._eval_variants(expr.slice.step, fn_name, b, depth + 1)[0] if expr.slice.step else None
                    if all(x is None or (isinstance(x, int) and not isinstance(x, bool)) for x in (lo, hi, st)):
                        return [v0[lo:hi:st]]
                return [_UNRESOLVED]
            kv = self._eval_variants(expr.slice, fn_name, b, depth + 1)
            k0 = kv[0] if kv else _UNRESOLVED
            if isinstance(v0, dict) and k0 is not _UNRESOLVED:
                try:
                    return [v0.get(k0, _UNRESOLVED)]
                except TypeError:
                    return [_UNRESOLVED]
            if isinstance(v0, (list, tuple)) and isinstance(k0, int) and not isinstance(k0, bool):
                if -len(v0) <= k0 < len(v0):
                    return [v0[k0]]
            return [_UNRESOLVED]
        if isinstance(expr, ast.BoolOp):
            if isinstance(expr.op, ast.Or):
                for val_node in expr.values:
                    vv = self._eval_variants(val_node, fn_name, b, depth + 1)
                    if vv and vv[0] is not _UNRESOLVED and vv[0]:
                        return [vv[0]]
                return [_UNRESOLVED]
            if isinstance(expr.op, ast.And):
                last: Any = _UNRESOLVED
                for val_node in expr.values:
                    vv = self._eval_variants(val_node, fn_name, b, depth + 1)
                    if not vv or vv[0] is _UNRESOLVED:
                        return [_UNRESOLVED]
                    last = vv[0]
                    if not last:
                        return [last]
                return [last]
        if isinstance(expr, ast.IfExp):
            cond = self._eval_cond(expr.test, fn_name, b, depth + 1)
            if cond is True:
                return self._eval_variants(expr.body, fn_name, b, depth + 1)
            if cond is False:
                return self._eval_variants(expr.orelse, fn_name, b, depth + 1)
            body_vals = self._eval_variants(expr.body, fn_name, b, depth + 1)
            if self._is_l3_tier_test(expr.test):
                body_vals = [
                    _L3GuardedModel(v) if isinstance(v, str) and v.strip().lower() == "pro" else v
                    for v in body_vals
                ]
            else_vals = self._eval_variants(expr.orelse, fn_name, b, depth + 1)
            return body_vals + else_vals
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Attribute) and expr.func.attr == "get" and expr.args:
                ov = self._eval_variants(expr.func.value, fn_name, b, depth + 1)
                kv = self._eval_variants(expr.args[0], fn_name, b, depth + 1)
                dv = self._eval_variants(expr.args[1], fn_name, b, depth + 1) if len(expr.args) > 1 else [None]
                if ov and isinstance(ov[0], dict) and kv and kv[0] is not _UNRESOLVED:
                    try:
                        return [ov[0].get(kv[0], dv[0] if dv else None)]
                    except TypeError:
                        return [_UNRESOLVED]
            cname = self._callee_name(expr.func, fn_name)
            short = cname.split(".")[-1]
            if short in ("str", "Path") and expr.args:
                av = self._eval_variants(expr.args[0], fn_name, b, depth + 1)
                a0 = self._unwrap(av[0]) if av else _UNRESOLVED
                if isinstance(a0, (str, int, float)) and a0 != _SYS_EXEC_SENTINEL:
                    return [str(a0)]
                return ["{*}"]
            if short == "int" and expr.args:
                av = self._eval_variants(expr.args[0], fn_name, b, depth + 1)
                a0 = self._unwrap(av[0]) if av else _UNRESOLVED
                if isinstance(a0, _BoundedInt):
                    return [a0]
                if isinstance(a0, (int, float)) and not isinstance(a0, bool):
                    return [int(a0)]
                if isinstance(a0, str):
                    try:
                        return [int(a0.strip())]
                    except ValueError:
                        return [_UNRESOLVED]
                return [_UNRESOLVED]
            if short == "min" and expr.args:
                ints: list[int] = []
                all_exact = True
                for a in expr.args:
                    av = self._eval_variants(a, fn_name, b, depth + 1)
                    v0 = av[0] if av else _UNRESOLVED
                    if isinstance(v0, int) and not isinstance(v0, bool):
                        ints.append(v0)
                    elif isinstance(v0, _BoundedInt):
                        ints.append(v0.upper)
                        all_exact = False
                    else:
                        all_exact = False
                if ints:
                    return [min(ints) if all_exact else _BoundedInt(min(ints))]
            if short == "max" and expr.args:
                bounds: list[int] = []
                exact_ints: list[int] = []
                all_exact = True
                for a in expr.args:
                    av = self._eval_variants(a, fn_name, b, depth + 1)
                    v0 = av[0] if av else _UNRESOLVED
                    if isinstance(v0, int) and not isinstance(v0, bool):
                        exact_ints.append(v0)
                        bounds.append(v0)
                    elif isinstance(v0, _BoundedInt):
                        bounds.append(v0.upper)
                        all_exact = False
                    else:
                        return [_UNRESOLVED]
                if all_exact and exact_ints:
                    return [max(exact_ints)]
                if bounds:
                    return [_BoundedInt(max(bounds))]
            if short == "range" and 1 <= len(expr.args) <= 3:
                rargs: list[int] = []
                for a in expr.args:
                    av = self._eval_variants(a, fn_name, b, depth + 1)
                    if av and isinstance(av[0], int) and not isinstance(av[0], bool):
                        rargs.append(av[0])
                    else:
                        return [_UNRESOLVED]
                seq = range(*rargs)
                if len(seq) <= 64:
                    return [list(seq)]
                return [list(seq[:64])]
        return [_UNRESOLVED]

    def _eval_cond(self, expr: ast.AST, fn_name: str | None, b: dict[str, Any], depth: int) -> bool | None:
        if isinstance(expr, ast.Compare) and len(expr.ops) == 1 and len(expr.comparators) == 1:
            lv = self._eval_variants(expr.left, fn_name, b, depth + 1)
            rv = self._eval_variants(expr.comparators[0], fn_name, b, depth + 1)
            l0 = self._unwrap(lv[0]) if lv else _UNRESOLVED
            r0 = self._unwrap(rv[0]) if rv else _UNRESOLVED
            if l0 is _UNRESOLVED or r0 is _UNRESOLVED:
                return None
            op = expr.ops[0]
            if isinstance(op, ast.Eq):
                return l0 == r0
            if isinstance(op, ast.NotEq):
                return l0 != r0
            if isinstance(op, ast.In):
                try:
                    return l0 in r0
                except TypeError:
                    return None
        return None

    def _is_l3_tier_test(self, expr: ast.AST) -> bool:
        if isinstance(expr, ast.Compare):
            src = ast.unparse(expr).lower()
            return ("tier" in src or "l3" in src) and ("'l3'" in src or '"l3"' in src or "[l3]" in src)
        return False

    def _validate_model_value(
        self,
        raw_model: Any,
        ctx_text: str,
        tier_val: str,
    ) -> tuple[str, str] | None:
        l3_guarded = isinstance(raw_model, _L3GuardedModel)
        val = self._unwrap(raw_model)
        if val is _UNRESOLVED or not isinstance(val, str) or not val.strip():
            return (
                "DENY|workflow_explicit_model: `agent(...)` requires a statically resolvable Google model string.",
                "DENY:workflow_explicit_model",
            )
        model = val.strip()
        ml = model.lower()
        blocked_patterns = list(self.cfg.get("blocked_model_patterns") or [])
        if self.cfg.get("enforce_google_only", True):
            hit = _find_blocked_vendor(model, blocked_patterns)
            if hit:
                return (
                    f"DENY|google_only: workflow script uses non-Google model `{model}` (matched `{hit}`). Use Google/Gemini models only.",
                    f"DENY:workflow_google_only:{hit}",
                )
            if ml not in _ALLOWED_GOOGLE_MODELS and not ml.startswith("gemini-"):
                return (
                    f"DENY|google_only: workflow script uses non-allowlisted model `{model}`. Use `flash_lite`|`flash`|`pro`|`gemini-*`.",
                    "DENY:workflow_google_only:non_google",
                )
        if self.cfg.get("prefer_cheap_models", True) and ml == "inherit":
            if not any(m in ctx_text.lower() for m in INHERIT_MARKERS):
                return (
                    "DENY|prefer_cheap_models: workflow `inherit` unannotated -> pick explicit `flash_lite`/`flash`/`pro`, or tag `[L3]`/`[inherit]`.",
                    "DENY:inherit_model",
                )
        if self.cfg.get("prefer_cheap_models", True) and ml == "pro":
            tier_u = (tier_val or "").strip().upper()
            has_l3_tier = tier_u in ("L3", "[L3]", "COMPLEX")
            if not (l3_guarded or has_l3_tier or _has_positive_complex_marker(ctx_text)):
                return (
                    "DENY|prefer_cheap_models: workflow `pro` on trivial slice -> use `flash_lite`/`flash`, tag `[L3]`/complex marker, or set `tier='L3'`.",
                    "DENY:workflow_pro_trivial",
                )
        return None

    def _collect_dict_entries(self, obj: Any) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if isinstance(obj, dict):
            if "model" in obj:
                entries.append(obj)
            for v in obj.values():
                if isinstance(v, (dict, list, tuple)):
                    entries.extend(self._collect_dict_entries(v))
        elif isinstance(obj, (list, tuple)):
            for x in obj:
                if isinstance(x, (dict, list, tuple)):
                    entries.extend(self._collect_dict_entries(x))
        return entries

    def _entry_context(self, entry: dict[str, Any]) -> tuple[str, str]:
        parts = [
            str(entry.get(k))
            for k in ("role", "Role", "prompt", "Prompt", "brief", "Brief", "title", "label", "name")
            if isinstance(entry.get(k), str)
        ]
        tier = str(entry.get("tier") or entry.get("Tier") or "")
        return " ".join(parts), tier

    def _audit_schedule_literals(self) -> tuple[str, str] | None:
        self.schedule_entries = []
        for key in ("SCHEDULE", "CUBICLES"):
            for rhs in self.module_env.get(key, []):
                for val in self._eval_variants(rhs):
                    for entry in self._collect_dict_entries(val):
                        ctx, tier = self._entry_context(entry)
                        if (hit := self._validate_model_value(entry.get("model"), ctx, tier)):
                            return hit
                        self.schedule_entries.append(entry)
        return None

    def _extract_entry_from_expr(self, expr: ast.AST, fn_name: str | None, b: dict[str, Any]) -> dict[str, Any] | None:
        target_node: ast.AST | None = None
        if isinstance(expr, ast.Subscript):
            target_node = expr.value
        elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "get":
            target_node = expr.func.value
        if target_node is not None:
            vals = self._eval_variants(target_node, fn_name, b)
            if vals and isinstance(vals[0], dict):
                return vals[0]
        return None

    def _audit_calls(self) -> tuple[str, str] | None:
        for call, fn_name in self.all_calls:
            cname = self._callee_name(call.func, fn_name)
            short = cname.split(".")[-1]
            # 1. Dangerous dynamic execution builtins
            if cname in _DANGEROUS_BUILTINS or cname in ("builtins.eval", "builtins.exec", "builtins.__import__"):
                return (
                    f"DENY|workflow_explicit_model: forbidden dynamic execution builtin `{short}(...)` in workflow.",
                    "DENY:workflow_explicit_model",
                )
            # 2. agent(...) enforcement
            if cname == "agent" or (isinstance(call.func, ast.Attribute) and call.func.attr == "agent"):
                if (hit := self._audit_agent_call(call, fn_name)):
                    return hit
            # 3. Strict subprocess / os / asyncio process allowlist
            if (hit := self._audit_subprocess_call(call, cname, fn_name)):
                return hit
        return None

    def _audit_agent_call(self, call: ast.Call, fn_name: str | None) -> tuple[str, str] | None:
        if any(kw.arg is None for kw in call.keywords):
            return (
                "DENY|workflow_explicit_model: `agent(...)` forbids `**kwargs`.",
                "DENY:workflow_explicit_model",
            )
        kw_map = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
        model_expr = kw_map.get("model")
        if model_expr is None:
            return (
                "DENY|workflow_explicit_model: `agent(...)` call missing explicit `model=` keyword.",
                "DENY:workflow_explicit_model",
            )
        for b in self._collect_binding_envs(call, fn_name):
            m_vals = self._eval_variants(model_expr, fn_name, b)
            ctx_parts: list[str] = []
            for a in call.args:
                if not isinstance(a, ast.Starred):
                    for av in self._eval_variants(a, fn_name, b):
                        u = self._unwrap(av)
                        if isinstance(u, str):
                            ctx_parts.append(u)
            for k in ("role", "Role", "prompt", "Prompt", "brief", "Brief"):
                if k in kw_map:
                    for kv in self._eval_variants(kw_map[k], fn_name, b):
                        u = self._unwrap(kv)
                        if isinstance(u, str):
                            ctx_parts.append(u)
            tier_val = ""
            if "tier" in kw_map:
                tv = self._eval_variants(kw_map["tier"], fn_name, b)
                if tv and isinstance(tv[0], str):
                    tier_val = tv[0]
            elif "tier" in b and isinstance(b["tier"], str):
                tier_val = b["tier"]
            entry = self._extract_entry_from_expr(model_expr, fn_name, b)
            if entry is not None:
                e_ctx, e_tier = self._entry_context(entry)
                if e_ctx:
                    ctx_parts.append(e_ctx)
                if not tier_val and e_tier:
                    tier_val = e_tier
            ctx_text = " ".join(ctx_parts)
            is_model_lookup = (
                (isinstance(model_expr, ast.Call) and isinstance(model_expr.func, ast.Attribute) and model_expr.func.attr == "get"
                 and model_expr.args and self._eval_variants(model_expr.args[0], fn_name, b) == ["model"])
                or (isinstance(model_expr, ast.Subscript) and self._eval_variants(model_expr.slice, fn_name, b) == ["model"])
            )
            for mv in m_vals:
                if mv is _UNRESOLVED and is_model_lookup and self.schedule_entries:
                    if isinstance(model_expr, ast.Call) and len(model_expr.args) >= 2:
                        for dv in self._eval_variants(model_expr.args[1], fn_name, b):
                            if (dhit := self._validate_model_value(dv, ctx_text, tier_val)):
                                return dhit
                    continue
                if (hit := self._validate_model_value(mv, ctx_text, tier_val)):
                    return hit
        return None

    def _is_allowlisted_argv(self, argv: Any) -> bool:
        if not isinstance(argv, (list, tuple)) or not argv:
            return False
        head = argv[0]
        if not isinstance(head, str) or not head or head == _STARRED_SENTINEL:
            return False
        head_name = Path(head).name if head != _SYS_EXEC_SENTINEL else _SYS_EXEC_SENTINEL
        if head == _SYS_EXEC_SENTINEL or head_name.startswith("python"):
            idx = 1
            while idx < len(argv):
                tok = argv[idx]
                if not isinstance(tok, str) or tok == _STARRED_SENTINEL:
                    return False
                if tok in _SAFE_PYTHON_FLAGS:
                    idx += 1
                    continue
                if tok.startswith("-"):
                    return False
                return Path(tok).name in _ALLOWED_SUBPROCESS_SCRIPTS
            return False
        if head_name in _ALLOWED_SUBPROCESS_SCRIPTS:
            return True
        if head_name == "git":
            idx = 1
            while idx < len(argv):
                tok = argv[idx]
                if not isinstance(tok, str) or tok == _STARRED_SENTINEL:
                    return False
                if tok == "-C":
                    if idx + 1 >= len(argv) or argv[idx + 1] == _STARRED_SENTINEL:
                        return False
                    nxt = argv[idx + 1]
                    if isinstance(nxt, str) and nxt.startswith("-"):
                        return False
                    idx += 2
                    continue
                if tok in _SAFE_GIT_FLAGS:
                    idx += 1
                    continue
                if tok.startswith("-"):
                    return False
                return tok in _ALLOWED_GIT_SUBCMDS
            return False
        return False

    def _audit_subprocess_call(self, call: ast.Call, cname: str, fn_name: str | None) -> tuple[str, str] | None:
        banned_direct = {
            "os.system",
            "os.popen",
            "asyncio.create_subprocess_shell",
            "asyncio.create_subprocess_exec",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.getoutput",
            "subprocess.getstatusoutput",
        }
        if cname in banned_direct:
            return (
                f"DENY|workflow_direct_heavy_subprocess: forbidden process call `{cname}(...)` in workflow.",
                "DENY:workflow_direct_heavy_subprocess",
            )
        if cname not in ("subprocess.run", "subprocess.check_output"):
            return None
        for kw in call.keywords:
            if kw.arg is None:
                return (
                    "DENY|workflow_direct_heavy_subprocess: `subprocess` call forbids `**kwargs`.",
                    "DENY:workflow_direct_heavy_subprocess",
                )
            if kw.arg == "shell":
                for sv in self._eval_variants(kw.value, fn_name):
                    if sv is not False:
                        return (
                            "DENY|workflow_direct_heavy_subprocess: `shell=True`/non-False `shell=` forbidden in workflow.",
                            "DENY:workflow_direct_heavy_subprocess",
                        )
        kw_map = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
        argv_expr = call.args[0] if call.args else kw_map.get("args")
        if argv_expr is None:
            return (
                "DENY|workflow_direct_heavy_subprocess: `subprocess` call missing list-argv.",
                "DENY:workflow_direct_heavy_subprocess",
            )
        envs = self._collect_binding_envs(call, fn_name)
        for b in envs:
            argvs = self._eval_variants(argv_expr, fn_name, b)
            for argv in argvs:
                if argv is _UNRESOLVED or not self._is_allowlisted_argv(argv):
                    return (
                        "DENY|workflow_direct_heavy_subprocess: subprocess allowed ONLY w/ `shell=False` list-argv for `plan_tool.py`|`repo_map.py`|`file_budget_guard.py`|read-only `git`.",
                        "DENY:workflow_direct_heavy_subprocess",
                    )
        return None

    def _is_prompt_context_str(self, node: ast.AST) -> bool:
        cur: ast.AST | None = node
        for _ in range(4):
            par = self.parents.get(cur) if cur is not None else None
            if par is None:
                break
            if isinstance(par, ast.keyword) and par.arg in ("prompt", "Prompt", "role", "Role", "brief", "Brief"):
                return True
            if isinstance(par, ast.Dict):
                for k_n, v_n in zip(par.keys, par.values):
                    if v_n is cur and isinstance(k_n, ast.Constant) and k_n.value in ("prompt", "Prompt", "role", "Role", "brief", "Brief"):
                        return True
            cur = par
        return False

    def _audit_parallel_heavy_ops(self) -> tuple[str, str] | None:
        if not self.parallel_seeds:
            return None
        work: list[tuple[ast.AST, str | None]] = list(self.parallel_seeds)
        seen_nodes: set[int] = set()
        seen_fns: set[str] = set()
        while work:
            node, fn_ctx = work.pop()
            nid = id(node)
            if nid in seen_nodes:
                continue
            seen_nodes.add(nid)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    txt = sub.value
                    if self._is_prompt_context_str(sub):
                        txt = _FORBIDDEN_CLAUSE_RE.sub(" ", _FORBIDDEN_PAREN_RE.sub(" ", txt))
                    if (hm := _WORKFLOW_HEAVY_RE.search(txt)):
                        return (
                            f"DENY|parallel_heavy_op: `run_workflow` runs `{hm.group(0)}` inside parallel execution -> move builds/tests/push to sequential Epoch Barrier.",
                            "DENY:workflow_parallel_heavy",
                        )
                elif isinstance(sub, ast.JoinedStr):
                    joined = "".join(
                        p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else " "
                        for p in sub.values
                    )
                    if self._is_prompt_context_str(sub):
                        joined = _FORBIDDEN_CLAUSE_RE.sub(" ", _FORBIDDEN_PAREN_RE.sub(" ", joined))
                    if (hm := _WORKFLOW_HEAVY_RE.search(joined)):
                        return (
                            f"DENY|parallel_heavy_op: `run_workflow` runs `{hm.group(0)}` inside parallel execution -> move builds/tests/push to sequential Epoch Barrier.",
                            "DENY:workflow_parallel_heavy",
                        )
                elif isinstance(sub, (ast.List, ast.Tuple)):
                    strs = [e.value for e in sub.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    if strs:
                        if (hm := _WORKFLOW_HEAVY_RE.search(" ".join(strs))):
                            return (
                                f"DENY|parallel_heavy_op: `run_workflow` runs `{hm.group(0)}` inside parallel execution -> move builds/tests/push to sequential Epoch Barrier.",
                                "DENY:workflow_parallel_heavy",
                            )
                elif isinstance(sub, ast.Name):
                    # Follow function references and variable bindings passed into parallel helpers
                    target_fn = sub.id
                    if target_fn in self.func_defs and target_fn not in seen_fns:
                        seen_fns.add(target_fn)
                        work.append((self.func_defs[target_fn], target_fn))
                    elif fn_ctx and target_fn in self.func_envs.get(fn_ctx, {}):
                        for rhs in self.func_envs[fn_ctx][target_fn]:
                            work.append((rhs, fn_ctx))
                    elif target_fn in self.module_env:
                        for rhs in self.module_env[target_fn]:
                            work.append((rhs, None))
                    if fn_ctx and fn_ctx in self.func_defs:
                        # If target_fn is a parameter of fn_ctx, follow argument expressions at call sites
                        fdef = self.func_defs[fn_ctx]
                        pnames = [p.arg for p in (list(fdef.args.posonlyargs) + list(fdef.args.args))]
                        if target_fn in pnames:
                            pidx = pnames.index(target_fn)
                            for c_node, c_fn in self.call_sites.get(fn_ctx, []):
                                if pidx < len(c_node.args):
                                    work.append((c_node.args[pidx], c_fn))
                                for kw in c_node.keywords:
                                    if kw.arg == target_fn:
                                        work.append((kw.value, c_fn))
                elif isinstance(sub, ast.Call):
                    cname = self._callee_name(sub.func, fn_ctx).split(".")[-1]
                    if cname in self.func_defs and cname not in seen_fns:
                        seen_fns.add(cname)
                        work.append((self.func_defs[cname], cname))
        return None

    def _is_bounded_width(self, expr: ast.AST, fn_name: str | None) -> bool:
        envs = self._collect_binding_envs(expr, fn_name)
        for b in envs:
            vals = self._eval_variants(expr, fn_name, b)
            if not vals:
                return False
            for v in vals:
                if isinstance(v, _BoundedInt):
                    if not (1 <= v.upper <= 2):
                        return False
                elif isinstance(v, int) and not isinstance(v, bool):
                    if not (1 <= v <= 2):
                        return False
                else:
                    return False
        return True

    def _is_bounded_semaphore_expr(self, expr: ast.AST, fn_name: str | None) -> bool:
        if isinstance(expr, ast.Call):
            cname = self._callee_name(expr.func, fn_name)
            if cname.split(".")[-1] == "Semaphore":
                cap_expr = expr.args[0] if expr.args else next((kw.value for kw in expr.keywords if kw.arg == "value"), None)
                return cap_expr is not None and self._is_bounded_width(cap_expr, fn_name)
        if isinstance(expr, ast.Name):
            sources: list[tuple[ast.AST, str | None]] = []
            if fn_name and expr.id in self.func_envs.get(fn_name, {}):
                sources.extend((r, fn_name) for r in self.func_envs[fn_name][expr.id])
            if expr.id in self.module_env:
                sources.extend((r, None) for r in self.module_env[expr.id])
            if sources and all(self._is_bounded_semaphore_expr(r, r_fn) for r, r_fn in sources):
                return True
        return False

    def _static_seq_len(self, expr: ast.AST, fn_name: str | None) -> int | None:
        if isinstance(expr, (ast.List, ast.Tuple)):
            if any(isinstance(e, ast.Starred) for e in expr.elts):
                return None
            return len(expr.elts)
        if isinstance(expr, (ast.ListComp, ast.GeneratorExp)) and len(expr.generators) == 1:
            g = expr.generators[0]
            if not g.ifs:
                return self._static_seq_len(g.iter, fn_name)
        if isinstance(expr, ast.Call) and self._callee_name(expr.func, fn_name).split(".")[-1] == "range":
            vals = self._eval_variants(expr, fn_name)
            if vals and isinstance(vals[0], list):
                return len(vals[0])
        if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Slice):
            if expr.slice.lower is None and expr.slice.upper is not None and expr.slice.step is None:
                uv = self._eval_variants(expr.slice.upper, fn_name)
                if uv and isinstance(uv[0], int) and not isinstance(uv[0], bool) and uv[0] >= 0:
                    return uv[0]
        if isinstance(expr, ast.Name):
            vals = self._eval_variants(expr, fn_name)
            if len(vals) == 1 and isinstance(vals[0], (list, tuple)):
                return len(vals[0])
        return None

    def _is_batch_var_bounded(self, var_name: str, fn_name: str | None) -> bool:
        rhs_list = (
            (self.func_envs.get(fn_name, {}).get(var_name, []) if fn_name else [])
            + self.module_env.get(var_name, [])
        )
        for rhs in rhs_list:
            if isinstance(rhs, ast.ListComp) and len(rhs.generators) == 1:
                it = rhs.generators[0].iter
                if isinstance(it, ast.Call) and self._callee_name(it.func, fn_name).split(".")[-1] == "range" and len(it.args) == 3:
                    if self._is_bounded_width(it.args[2], fn_name):
                        return True
            if isinstance(rhs, ast.Call):
                cname = self._callee_name(rhs.func, fn_name).split(".")[-1]
                if cname in ("batched", "chunked", "chunks") and len(rhs.args) >= 2:
                    if self._is_bounded_width(rhs.args[1], fn_name):
                        return True
        return False

    def _has_bounded_chunking_context(self, p_call: ast.Call, fn_name: str | None) -> bool:
        cur: ast.AST | None = p_call
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            if isinstance(cur, (ast.For, ast.AsyncFor)):
                it = cur.iter
                if isinstance(it, ast.Call):
                    cname = self._callee_name(it.func, fn_name).split(".")[-1]
                    if cname == "range" and len(it.args) == 3 and self._is_bounded_width(it.args[2], fn_name):
                        return True
                    if cname in ("batched", "chunked", "chunks"):
                        w_expr = it.args[1] if len(it.args) >= 2 else next((kw.value for kw in it.keywords if kw.arg in ("n", "size", "width")), None)
                        if w_expr is not None and self._is_bounded_width(w_expr, fn_name):
                            return True
                if isinstance(it, ast.Name) and self._is_batch_var_bounded(it.id, fn_name):
                    return True
            elif isinstance(cur, (ast.With, ast.AsyncWith)):
                if any(self._is_bounded_semaphore_expr(item.context_expr, fn_name) for item in cur.items):
                    return True
            cur = self.parents.get(cur)
        # Also allow when every task in parallel(...) calls a Semaphore(<=2)-guarded function
        task_calls: list[ast.Call] = []
        for a in p_call.args:
            node = a.value if isinstance(a, ast.Starred) else a
            if isinstance(node, ast.Call):
                task_calls.append(node)
            elif isinstance(node, (ast.ListComp, ast.GeneratorExp)) and isinstance(node.elt, ast.Call):
                task_calls.append(node.elt)
            else:
                return False
        return bool(task_calls) and all(
            self._callee_name(tc.func, fn_name).split(".")[-1] in self.sem_guarded_fns
            for tc in task_calls
        )

    def _audit_termux_parallel_width(self) -> tuple[str, str] | None:
        if not _is_termux_constrained(self.repo):
            return None
        for call, fn_name in self.all_calls:
            if self._callee_name(call.func, fn_name) != "parallel":
                continue
            if call.keywords:
                return (
                    "DENY|workflow_unbounded_parallel: Termux PPK forbids keyword args on `parallel(...)`.",
                    "DENY:workflow_unbounded_parallel",
                )
            if not call.args:
                continue
            explicit_over = False
            literal_ok = False
            if len(call.args) > 2:
                explicit_over = True
            elif len(call.args) == 2:
                if any(isinstance(a, ast.Starred) for a in call.args):
                    explicit_over = True
                elif self._static_seq_len(call.args[1], fn_name) is None:
                    slen = self._static_seq_len(call.args[0], fn_name)
                    if slen is not None:
                        if slen <= 2:
                            literal_ok = True
                        else:
                            explicit_over = True
                else:
                    literal_ok = True
            else:  # len(call.args) == 1
                a0 = call.args[0]
                target = a0.value if isinstance(a0, ast.Starred) else a0
                slen = self._static_seq_len(target, fn_name)
                if slen is not None:
                    if slen <= 2:
                        literal_ok = True
                    else:
                        explicit_over = True
                elif not isinstance(a0, ast.Starred) and isinstance(a0, (ast.Call, ast.Lambda)):
                    if not isinstance(a0, ast.Call) or self._callee_name(a0.func, fn_name).split(".")[-1] not in ("list", "tuple"):
                        literal_ok = True
            if explicit_over or (not literal_ok and not self._has_bounded_chunking_context(call, fn_name)):
                return (
                    "DENY|workflow_unbounded_parallel: Termux PPK requires `parallel(...)` literal len <= 2 or bounded chunking (`max_width <= 2` / `Semaphore(<=2)`).",
                    "DENY:workflow_unbounded_parallel",
                )
        return None

    def audit(self) -> tuple[str, str] | None:
        try:
            tree = ast.parse(self.script_src)
        except SyntaxError as exc:
            return (
                f"DENY|workflow_explicit_model: unparseable workflow script ({exc.msg}).",
                "DENY:workflow_explicit_model",
            )
        self._index_tree(tree)
        for step in (
            self._audit_schedule_literals,
            self._audit_calls,
            self._audit_parallel_heavy_ops,
            self._audit_termux_parallel_width,
        ):
            if (hit := step()):
                return hit
        return None


def _check_run_workflow(args: dict[str, Any], cfg: dict[str, Any], repo: Path | None = None) -> tuple[str, str] | None:
    script_src = str(args.get("Script") or args.get("script") or "")
    if not script_src:
        sp = str(args.get("ScriptPath") or args.get("scriptPath") or "").strip()
        if sp:
            p = Path(sp).expanduser()
            if not p.is_absolute() and repo is not None:
                p = (repo / p).resolve()
            if p.is_file():
                try:
                    script_src = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    script_src = ""
    if not script_src:
        return None
    return _WorkflowAstAuditor(script_src, cfg=cfg, repo=repo).audit()


def main() -> None:
    if (PLUGIN_ROOT / ".bypass_gates").exists():
        _allow()
        return

    t0 = time.monotonic()
    repo: Path | None = None
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        tool = payload.get("toolCall") or {}
        name = str(tool.get("name") or payload.get("toolName") or "").strip()
        args = tool.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in ("invoke_subagent", "define_subagent", "swarm", "run_workflow"):
            _allow()
            return

        _ensure_paths()
        from planner_state import load_project_profile, resolve_repo_root  # noqa: E402
        from schema_migrations import resolve_delegation_config  # noqa: E402

        repo = resolve_repo_root(payload)
        profile = load_project_profile(repo)
        cfg = resolve_delegation_config(profile)
        if not cfg.get("enabled", True):
            _allow()
            return

        hit: tuple[str, str] | None = None
        if name == "invoke_subagent":
            hit = _check_invoke_subagent(args, cfg, payload=payload, repo=repo)
        elif name == "define_subagent":
            hit = _check_define_subagent(args, cfg)
        elif name == "swarm":
            hit = _check_swarm(args, cfg, payload=payload, repo=repo)
        elif name == "run_workflow":
            hit = _check_run_workflow(args, cfg, repo=repo)

        if hit:
            _deny(hit[0], payload=payload, repo=repo, rule_sig=hit[1])
            return

        _allow()
    finally:
        if (time.monotonic() - t0) * 1000.0 >= float(os.environ.get("PLANNER_SLOW_HOOK_MS") or 1500.0):
            _ensure_paths()
            from plugin_diagnostics import check_slow_hook  # noqa: E402

            check_slow_hook("planner_guard_delegation.py", t0, repo=repo)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _ensure_paths()
            from plugin_diagnostics import record_hook_exception  # noqa: E402

            record_hook_exception("planner_guard_delegation.py", exc)
        except Exception:
            pass
        _allow()
