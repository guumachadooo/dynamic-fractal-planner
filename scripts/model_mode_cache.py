#!/usr/bin/env python3
"""Zero-regex, event-driven model mode cache & official docs context gatherer for `auto` mode."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1
DEFAULT_DOCS_MD_URL = "https://antigravity.google/docs/models.md"
DEFAULT_DOCS_HTML_URL = "https://antigravity.google/docs/models"
DEFAULT_FETCH_TIMEOUT = 2.0
DEFAULT_OFFLINE_COOLDOWN_SECS = 1800
DEFAULT_PROMPTED_COOLDOWN_SECS = 900

EFFORT_SUFFIXES = (
    "-extra-high",
    "-xhigh",
    "-extra",
    "-high",
    "-medium",
    "-med",
    "-low",
    "-max",
    "-thinking",
)

SEEDED_MODEL_MODES: dict[str, dict[str, str]] = {
    "gemini-4-neon": {"mode": "lean", "why": "Gemini 4 Neon: frontier default reasoning/planner"},
    "gemini-4-carbon": {"mode": "lean", "why": "Gemini 4 Carbon: Ultra frontier reasoning/planner"},
    "gemini-4-argon": {"mode": "lean", "why": "Gemini 4 Argon: frontier reasoning/planner"},
    "orchestrator-neon": {"mode": "lean", "why": "Orchestrator (Neon): native plan->exec orchestrator"},
    "gemini-pro-orchestrator-neon": {"mode": "lean", "why": "Orchestrator (Neon): native plan->exec orchestrator"},
    "orchestrator-carbon": {"mode": "lean", "why": "Orchestrator (Carbon): native plan->exec orchestrator"},
    "gemini-pro-orchestrator-carbon": {"mode": "lean", "why": "Orchestrator (Carbon): native plan->exec orchestrator"},
    "orchestrator-argon": {"mode": "lean", "why": "Orchestrator (Argon): native plan->exec orchestrator"},
    "gemini-pro-orchestrator-argon": {"mode": "lean", "why": "Orchestrator (Argon): native plan->exec orchestrator"},
    "claude-opus-5-5": {"mode": "lean", "why": "Claude Opus 5.5: flagship frontier reasoning"},
    "claude-opus-4-6": {"mode": "lean", "why": "Claude Opus 4.6: flagship frontier reasoning"},
    "gemini-4-hydrogen": {"mode": "strict", "why": "Gemini 4 Hydrogen: fast lightweight everyday executor"},
    "gemini-3-8-flash": {"mode": "strict", "why": "Gemini 3.8 Flash: fast everyday/free executor"},
    "gemini-3-7-flash": {"mode": "strict", "why": "Gemini 3.7 Flash: fast everyday Flash executor"},
    "gemini-3-6-flash": {"mode": "strict", "why": "Gemini 3.6 Flash: fast everyday Flash executor"},
    "gemini-3-1-pro": {"mode": "strict", "why": "Gemini 3.1 Pro: legacy Enterprise non-orchestrator"},
    "claude-sonnet-5-5": {"mode": "strict", "why": "Claude Sonnet 5.5: mid-tier workhorse executor"},
    "claude-sonnet-4-6": {"mode": "strict", "why": "Claude Sonnet 4.6: mid-tier workhorse executor"},
    "gpt-oss-120b": {"mode": "strict", "why": "GPT-OSS-120B: open-weight medium executor"},
}

_GENERIC_TOKENS = frozenset({"gemini", "claude", "gpt", "oss", "model", "antigravity", "pro", "v1", "v2"})
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_model_key(raw_model: str) -> str:
    """Syntactically normalize a raw model identifier by stripping effort/thinking suffixes."""
    s = (raw_model or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[\s_()\[\]/.]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        return ""
    changed = True
    while changed:
        changed = False
        for suf in EFFORT_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)].rstrip("-")
                changed = True
                break
    return s


def resolve_cache_path(cache_path: Path | None = None) -> Path:
    if cache_path is not None:
        return Path(cache_path).expanduser()
    env_p = os.environ.get("PLANNER_MODEL_CACHE_FILE", "").strip()
    if env_p:
        return Path(env_p).expanduser()
    return Path(__file__).resolve().parents[1] / ".model_modes.json"


def _empty_cache() -> dict[str, Any]:
    return {"schema_version": CACHE_SCHEMA_VERSION, "models": {}, "pending": {}}


def load_cache(cache_path: Path | None = None) -> dict[str, Any]:
    """Load local model mode cache (0 disk reads if absent)."""
    target = resolve_cache_path(cache_path)
    if not target.is_file():
        return _empty_cache()
    try:
        raw = json.loads(target.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return _empty_cache()
    if not isinstance(raw, dict):
        return _empty_cache()
    models = raw.get("models")
    pending = raw.get("pending")
    return {
        "schema_version": int(raw.get("schema_version") or CACHE_SCHEMA_VERSION),
        "models": dict(models) if isinstance(models, dict) else {},
        "pending": dict(pending) if isinstance(pending, dict) else {},
    }


def save_cache(data: dict[str, Any], cache_path: Path | None = None) -> None:
    """Atomically persist local model mode cache without raising."""
    target = resolve_cache_path(cache_path)
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "models": data.get("models") if isinstance(data.get("models"), dict) else {},
        "pending": data.get("pending") if isinstance(data.get("pending"), dict) else {},
    }
    tmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _profile_override_mode(raw_low: str, key: str, profile: dict[str, Any] | None) -> str | None:
    auto_cfg = (profile or {}).get("auto_mode")
    if not isinstance(auto_cfg, dict):
        return None
    for field in ("overrides", "models"):
        mapping = auto_cfg.get(field)
        if not isinstance(mapping, dict):
            continue
        norm_map: dict[str, str] = {}
        for k, v in mapping.items():
            val = v.get("mode") if isinstance(v, dict) else v
            mode_val = str(val or "").strip().lower()
            if mode_val not in ("lean", "strict"):
                continue
            k_low = str(k or "").strip().lower()
            if k_low:
                norm_map[k_low] = mode_val
                k_norm = normalize_model_key(k_low)
                if k_norm and k_norm not in norm_map:
                    norm_map[k_norm] = mode_val
        for cand in (raw_low, key):
            if cand and cand in norm_map:
                return norm_map[cand]
    return None


def lookup_model_mode(
    raw_model: str,
    profile: dict[str, Any] | None = None,
    cache_path: Path | None = None,
) -> tuple[str | None, str, str]:
    """Exact-key fast lookup (`<1ms`, zero regex, zero network).

    Returns `(mode_or_none, source, canonical_key)`.
    """
    raw_low = (raw_model or "").strip().lower()
    key = normalize_model_key(raw_model)
    if not key or raw_low == "auto":
        return "lean", "auto:no_model", ""

    prof_mode = _profile_override_mode(raw_low, key, profile)
    if prof_mode in ("lean", "strict"):
        return prof_mode, "auto:profile_override", key

    cache = load_cache(cache_path)
    models = cache.get("models") or {}
    for cand in (raw_low, key):
        entry = models.get(cand)
        if isinstance(entry, dict):
            m_val = str(entry.get("mode") or "").strip().lower()
            if m_val in ("lean", "strict"):
                src = str(entry.get("source") or "ai").strip() or "ai"
                return m_val, f"auto:cache:{src}", key

    for cand in (raw_low, key):
        seed_entry = SEEDED_MODEL_MODES.get(cand)
        if seed_entry:
            return seed_entry["mode"], "auto:seed", key

    return None, "auto:unseen", key


def set_model_mode(
    raw_model: str,
    mode: str,
    why: str = "",
    source: str = "ai",
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Persist mode verdict (`lean`|`strict`) for `raw_model` in the local cache."""
    m_val = (mode or "").strip().lower()
    if m_val not in ("lean", "strict"):
        raise ValueError(f"Invalid mode {mode!r}; expected 'lean' or 'strict'.")
    key = normalize_model_key(raw_model)
    if not key:
        raise ValueError(f"Invalid empty model key from {raw_model!r}.")

    cache = load_cache(cache_path)
    entry = {
        "mode": m_val,
        "source": (source or "ai").strip() or "ai",
        "why": re.sub(r"\s+", " ", (why or "").strip())[:160],
        "raw_model": (raw_model or key).strip(),
        "updated_ts": int(time.time()),
    }
    cache["models"][key] = entry
    cache["pending"].pop(key, None)
    save_cache(cache, cache_path)
    return {"key": key, **entry}


def pin_native_plan_model(raw_model: str, tool_name: str, cache_path: Path | None = None) -> bool:
    """Auto-pin any model observed calling native plan tools (`enter/exit_plan_mode`) to `lean`."""
    raw_s = (raw_model or "").strip()
    key = normalize_model_key(raw_s)
    if not key or raw_s.lower() == "auto":
        return False
    cache = load_cache(cache_path)
    existing = cache["models"].get(key)
    if (
        isinstance(existing, dict)
        and existing.get("mode") == "lean"
        and existing.get("source") == "native_plan_tool"
    ):
        return False
    set_model_mode(
        raw_s,
        "lean",
        why=f"Observed native {tool_name}",
        source="native_plan_tool",
        cache_path=cache_path,
    )
    return True


def clear_model_mode(raw_model: str = "", clear_all: bool = False, cache_path: Path | None = None) -> int:
    """Remove cached/pending entries for one model or all dynamic entries."""
    cache = load_cache(cache_path)
    removed = 0
    if clear_all:
        removed = len(cache["models"]) + len(cache["pending"])
        cache["models"] = {}
        cache["pending"] = {}
    else:
        raw_low = (raw_model or "").strip().lower()
        key = normalize_model_key(raw_model)
        for cand in {c for c in (raw_low, key) if c}:
            if cand in cache["models"]:
                cache["models"].pop(cand, None)
                removed += 1
            if cand in cache["pending"]:
                cache["pending"].pop(cand, None)
                removed += 1
    if removed:
        save_cache(cache, cache_path)
    return removed


def list_model_modes(cache_path: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return `(merged_sorted_entries, pending_dict)` merging seeds with cached overrides."""
    cache = load_cache(cache_path)
    merged: dict[str, dict[str, Any]] = {}
    for k, v in SEEDED_MODEL_MODES.items():
        merged[k] = {"key": k, "mode": v["mode"], "source": "seed", "why": v["why"]}
    for k, v in (cache.get("models") or {}).items():
        if isinstance(v, dict):
            merged[k] = {
                "key": k,
                "mode": str(v.get("mode") or ""),
                "source": str(v.get("source") or "ai"),
                "why": str(v.get("why") or ""),
            }
    rows = [merged[k] for k in sorted(merged.keys())]
    return rows, dict(cache.get("pending") or {})


def _clean_doc_text(text: str, is_html: bool = False) -> str:
    txt = text or ""
    if is_html or ("<html" in txt[:500].lower() or "<main" in txt[:2000].lower()):
        txt = _SCRIPT_STYLE_RE.sub(" ", txt)
        txt = re.sub(r"<(?:h[1-6]|p|li|tr|div|br)[^>]*>", "\n", txt, flags=re.IGNORECASE)
        txt = _HTML_TAG_RE.sub(" ", txt)
    txt = _MD_LINK_RE.sub(r"\1", txt)
    return txt


def extract_compact_doc_context(text: str, raw_model: str, key: str, max_chars: int = 550) -> str:
    """Extract ultra-compact lines matching the target model and/or capability tables."""
    cleaned = _clean_doc_text(text)
    raw_tokens = [t for t in re.split(r"[^a-z0-9]+", key.lower()) if len(t) >= 3 and t not in _GENERIC_TOKENS]
    raw_low = (raw_model or "").strip().lower()

    matched: list[str] = []
    tables: list[str] = []
    seen: set[str] = set()
    in_table_sec = False

    for raw_ln in cleaned.splitlines():
        ln = re.sub(r"[ \t]+", " ", raw_ln).strip()
        if not ln or ln.startswith("---") or set(ln) <= {"|", "-", ":", " "}:
            continue
        low = ln.lower()
        if low.startswith(("#", "##", "###")):
            in_table_sec = any(h in low for h in ("reasoning model", "orchestrator", "when to use", "model"))
        is_hit = (raw_low and len(raw_low) >= 4 and raw_low in low) or any(tok in low for tok in raw_tokens)
        compact = ln.replace("**", "").strip("| ").strip()
        if compact in seen:
            continue
        if is_hit:
            seen.add(compact)
            matched.append(compact[:140])
        elif in_table_sec and ("|" in ln or low.startswith("* ")) and len(tables) < 6:
            seen.add(compact)
            tables.append(compact[:110])

    ordered = matched[:6] + tables[:4]
    out = " ; ".join(ordered)
    if len(out) > max_chars:
        out = out[: max_chars - 3].rstrip() + "..."
    return out


def gather_model_context(raw_model: str, key: str) -> tuple[bool, str]:
    """Gather official model docs (`antigravity.google/docs/models.md` + optional `agy models`)."""
    if os.environ.get("PLANNER_MODEL_OFFLINE") == "1":
        return False, ""

    online_ok = False
    parts: list[str] = []

    if os.environ.get("PLANNER_MODEL_DISABLE_AGY_CLI") != "1" and shutil.which("agy"):
        try:
            res = subprocess.run(
                ["agy", "models"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                online_ok = True
                excerpt = extract_compact_doc_context(res.stdout, raw_model, key, max_chars=220)
                parts.append("agy:" + (excerpt or re.sub(r"\s+", " ", res.stdout.strip())[:180]))
        except Exception:
            pass

    url = os.environ.get("PLANNER_MODEL_DOCS_URL", "").strip() or DEFAULT_DOCS_MD_URL
    try:
        timeout = float(os.environ.get("PLANNER_MODEL_FETCH_TIMEOUT") or DEFAULT_FETCH_TIMEOUT)
    except Exception:
        timeout = DEFAULT_FETCH_TIMEOUT

    body = ""
    is_html = False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dynamic-fractal-planner/2.2"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) < 400:
                body = resp.read(250000).decode("utf-8", errors="ignore")
                online_ok = True
                is_html = "html" in str(resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and url == DEFAULT_DOCS_MD_URL:
            try:
                req2 = urllib.request.Request(
                    DEFAULT_DOCS_HTML_URL,
                    headers={"User-Agent": "dynamic-fractal-planner/2.2"},
                )
                with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                    if getattr(resp2, "status", 200) < 400:
                        body = resp2.read(250000).decode("utf-8", errors="ignore")
                        online_ok = True
                        is_html = True
            except Exception:
                pass
    except Exception:
        pass

    if body:
        ctx = extract_compact_doc_context(_clean_doc_text(body, is_html=is_html), raw_model, key, max_chars=480)
        if ctx:
            parts.append("docs:" + ctx)
        else:
            parts.append("docs:fetched(model unlisted)")

    return online_ok, " | ".join(p for p in parts if p)[:620]


def prepare_unseen_classification(
    raw_model: str,
    plan_cli: Path | str,
    profile: dict[str, Any] | None = None,
    cache_path: Path | None = None,
    now: float | None = None,
) -> str:
    """Gather context once for an unseen model & emit compact telegraphic classification prompt."""
    ts = time.time() if now is None else float(now)
    key = normalize_model_key(raw_model)
    if not key or (raw_model or "").strip().lower() == "auto":
        return ""

    cache = load_cache(cache_path)
    if key in (cache.get("models") or {}):
        return ""

    pend = (cache.get("pending") or {}).get(key) or {}
    if isinstance(pend, dict) and ts < float(pend.get("next_retry_ts") or 0.0):
        return ""

    env_cd = os.environ.get("PLANNER_MODEL_COOLDOWN_SECS", "").strip()
    auto_cfg = (profile or {}).get("auto_mode")
    cfg = auto_cfg if isinstance(auto_cfg, dict) else {}
    offline_cd = (
        int(env_cd)
        if env_cd.isdigit()
        else int(cfg.get("offline_cooldown_secs") or cfg.get("retry_cooldown_secs") or DEFAULT_OFFLINE_COOLDOWN_SECS)
    )
    prompted_cd = (
        int(env_cd)
        if env_cd.isdigit()
        else int(cfg.get("prompted_cooldown_secs") or DEFAULT_PROMPTED_COOLDOWN_SECS)
    )

    online_ok, ctx = gather_model_context(raw_model, key)
    attempts = int(pend.get("attempts") or 0) + 1 if isinstance(pend, dict) else 1

    if not online_ok:
        cache["pending"][key] = {
            "status": "offline",
            "last_attempt_ts": int(ts),
            "next_retry_ts": int(ts + offline_cd),
            "attempts": attempts,
        }
        save_cache(cache, cache_path)
        return ""

    cache["pending"][key] = {
        "status": "prompted",
        "last_attempt_ts": int(ts),
        "next_retry_ts": int(ts + prompted_cd),
        "attempts": attempts,
    }
    save_cache(cache, cache_path)

    ctx_txt = ctx if ctx else "docs fetched; model unlisted -> reason from capabilities/tier"
    return "\n".join([
        f"[MODEL_MODE_CLASSIFY|model={raw_model}|key={key}|tmp=lean]",
        "CRITERIA|frontier/flagship/native-orchestrator -> lean | lightweight/budget/everyday-executor -> strict",
        f"CTX|{ctx_txt}",
        f'ACT|reason(CRITERIA && CTX) -> persist once: `python3 {plan_cli} model-mode set "{key}" <lean|strict> --why "<=12w rationale>"`',
    ])
