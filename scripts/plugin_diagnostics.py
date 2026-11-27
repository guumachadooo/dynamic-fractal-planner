#!/usr/bin/env python3
"""Zero-token self-diagnostic flight recorder for `dynamic-fractal-planner`.

Silently captures hook crashes, latency spikes, suspect guard triggers, Stop-gate loops,
guard deny bursts, injection bloat, profile warnings, and triage mismatches into
`<PLUGIN_ROOT>/.diagnostics/anomalies.jsonl` (gitignored, auto-redacted, ring-buffered).
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MAX_ANOMALY_LINES = 200
TRIM_THRESHOLD_BYTES = 48_000
DEFAULT_SLOW_HOOK_MS = 1500.0

_SECRET_REDACT_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[^\s,'\";|]{6,}"),
)


def resolve_diagnostics_file() -> Path | None:
    """Return path to `anomalies.jsonl`, or `None` when disabled/in isolated test runs."""
    override = os.environ.get("PLANNER_DIAGNOSTICS_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "anomalies.jsonl"
    if os.environ.get("PLANNER_DISABLE_TELEMETRY") == "1" or os.environ.get("PLANNER_RUNNING_PLUGIN_TESTS") == "1":
        return None
    return PLUGIN_ROOT / ".diagnostics" / "anomalies.jsonl"


def redact_detail(text: str, max_chars: int = 200) -> str:
    """Collapse whitespace, redact secret tokens, and bound string length."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    for pat in _SECRET_REDACT_RES:
        if pat.groups:
            clean = pat.sub(r"\1=<REDACTED>", clean)
        else:
            clean = pat.sub("<REDACTED>", clean)
    return clean[:max_chars]


def _trim_ring_buffer(target: Path) -> None:
    if target.stat().st_size <= TRIM_THRESHOLD_BYTES:
        return
    raw_lines = [ln for ln in target.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    if len(raw_lines) > MAX_ANOMALY_LINES:
        target.write_text("\n".join(raw_lines[-MAX_ANOMALY_LINES:]) + "\n", encoding="utf-8")


def record_anomaly(
    kind: str,
    hook: str,
    sig: str,
    detail: str,
    repo: Path | str | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Append 1 compact redacted anomaly record to `anomalies.jsonl` without ever raising."""
    try:
        target = resolve_diagnostics_file()
        if target is None:
            return False
        repo_name = ""
        if repo:
            try:
                repo_name = Path(repo).name
            except Exception:
                repo_name = str(repo)
        entry: dict[str, Any] = {
            "ts": int(time.time()),
            "kind": redact_detail(kind, 32),
            "hook": redact_detail(hook, 40),
            "sig": redact_detail(sig, 80),
            "repo": redact_detail(repo_name, 40),
            "detail": redact_detail(detail, 200),
        }
        if meta:
            for k, v in list(meta.items())[:4]:
                if k not in entry:
                    entry[str(k)[:24]] = redact_detail(str(v), 80) if isinstance(v, str) else v
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        _trim_ring_buffer(target)
        return True
    except Exception:
        return False


def record_hook_exception(hook: str, exc: BaseException, repo: Path | str | None = None) -> bool:
    """Record uncaught hook exception with compact file:line traceback frames."""
    try:
        tb = traceback.extract_tb(exc.__traceback__)[-2:] if exc.__traceback__ else []
        frames = [f"{Path(fr.filename).name}:{fr.lineno} {fr.name}()" for fr in tb]
        last_frame = frames[-1] if frames else "unknown"
        frame_txt = " -> ".join(frames) if frames else "no_tb"
        return record_anomaly(
            "hook_exception",
            hook,
            f"{type(exc).__name__}:{last_frame}",
            f"{type(exc).__name__}({exc}) @ {frame_txt}",
            repo=repo,
        )
    except Exception:
        return False


def check_slow_hook(hook: str, start_monotonic: float, repo: Path | str | None = None) -> None:
    """Record `hook_slow` if elapsed wall-clock time meets/exceeds configured threshold."""
    try:
        ms = (time.monotonic() - start_monotonic) * 1000.0
        raw_thr = os.environ.get("PLANNER_SLOW_HOOK_MS", "").strip()
        threshold = float(raw_thr) if raw_thr else DEFAULT_SLOW_HOOK_MS
        if ms >= threshold:
            record_anomaly(
                "hook_slow",
                hook,
                f"{hook}:slow",
                f"elapsed={int(ms)}ms (>={int(threshold)}ms)",
                repo=repo,
            )
    except Exception:
        pass


def record_guard_deny_if_burst(
    hook: str,
    rule_sig: str,
    reason: str,
    hook_file: Path | None,
    hook_state: dict[str, Any],
    repo: Path | str | None = None,
) -> None:
    """Track per-turn guard denies/asks and emit `guard_deny_burst` on >=2 repeats."""
    try:
        key = f"{hook}:{rule_sig}"
        denies = dict(hook_state.get("guard_denies") or {})
        cnt = int(denies.get(key) or 0) + 1
        denies[key] = cnt
        hook_state["guard_denies"] = denies
        if hook_file is not None:
            hook_file.parent.mkdir(parents=True, exist_ok=True)
            hook_file.write_text(json.dumps(hook_state, ensure_ascii=False, indent=2), encoding="utf-8")
        if cnt >= 2:
            record_anomaly("guard_deny_burst", hook, key, f"{cnt}x {reason}", repo=repo)
    except Exception:
        pass


def load_anomalies(diag_file: Path | None = None) -> list[dict[str, Any]]:
    target = diag_file if diag_file is not None else resolve_diagnostics_file()
    if target is None or not target.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for raw in target.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                entries.append(obj)
        except Exception:
            continue
    return entries


def summarize_anomalies(diag_file: Path | None = None) -> tuple[int, int]:
    entries = load_anomalies(diag_file)
    if not entries:
        return 0, 0
    sigs = {(str(e.get("kind") or ""), str(e.get("sig") or "")) for e in entries}
    return len(entries), len(sigs)


def format_diagnostics_report(
    kind_filter: str = "",
    limit: int = 25,
    raw: bool = False,
    diag_file: Path | None = None,
) -> str:
    entries = load_anomalies(diag_file)
    if kind_filter:
        kf = kind_filter.strip().lower()
        entries = [e for e in entries if str(e.get("kind") or "").lower() == kf]
    if not entries:
        return "DIAGNOSTICS|total=0|clean"

    if raw:
        sliced = entries[-max(1, limit):]
        lines = [f"DIAGNOSTICS|raw={len(sliced)}/{len(entries)}"]
        for e in sliced:
            lines.append(
                f"{e.get('ts', 0)}|{e.get('kind', '?')}|{e.get('hook', '?')}|"
                f"{e.get('sig', '?')}|repo={e.get('repo', '-')}|{e.get('detail', '')}"
            )
        return "\n".join(lines)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for e in entries:
        gk = (str(e.get("kind") or "?"), str(e.get("sig") or "?"))
        cur = grouped.get(gk)
        ts = int(e.get("ts") or 0)
        if cur is None:
            grouped[gk] = {
                "count": 1,
                "last_ts": ts,
                "hook": str(e.get("hook") or "?"),
                "repo": str(e.get("repo") or "-"),
                "detail": str(e.get("detail") or ""),
            }
        else:
            cur["count"] += 1
            if ts >= cur["last_ts"]:
                cur["last_ts"] = ts
                cur["repo"] = str(e.get("repo") or cur["repo"])
                cur["detail"] = str(e.get("detail") or cur["detail"])

    rows = sorted(grouped.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["last_ts"]))[:max(1, limit)]
    lines = [f"DIAGNOSTICS|total={len(entries)}|unique={len(grouped)}|shown={len(rows)}"]
    for (kind, sig), info in rows:
        lines.append(
            f"{info['count']}x|{kind}|{info['hook']}|{sig}|repo={info['repo'] or '-'}|{info['detail']}"
        )
    return "\n".join(lines)


def clear_anomalies(kind_filter: str = "", diag_file: Path | None = None) -> tuple[int, Path | None]:
    target = diag_file if diag_file is not None else resolve_diagnostics_file()
    if target is None or not target.is_file():
        return 0, target
    entries = load_anomalies(target)
    if not kind_filter:
        removed = len(entries)
        target.unlink(missing_ok=True)
        return removed, target
    kf = kind_filter.strip().lower()
    kept = [e for e in entries if str(e.get("kind") or "").lower() != kf]
    removed = len(entries) - len(kept)
    if not kept:
        target.unlink(missing_ok=True)
    else:
        target.write_text(
            "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in kept),
            encoding="utf-8",
        )
    return removed, target
