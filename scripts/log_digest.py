#!/usr/bin/env python3
"""Compact log digestor: latest run block -> error/crash lines + context, consecutive repeats collapsed."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RUN_START_RE = re.compile(
    r"^(?:={3,}|-{3,}\s*(?:Session|Run|Build|Start)|\[\d{4}-\d{2}-\d{2}[^\]]*\]\s*(?:===|---|\bApp started|\bSession start|\bStarting)|\b(?:App started|Session started|Build started)\b)",
    re.IGNORECASE,
)
ERROR_LINE_RE = re.compile(
    r"\b(?:Error|Exception|FATAL|FAIL(?:ED)?|SIGSEGV|SIGABRT|SIGSYS|Traceback)\b|\bexit(?:\s+code)?[\s:=]+[1-9]\d*\b|\b(?:CS|MSB)\d{4}\b",
    re.IGNORECASE,
)
DIAG_ERROR_RE = re.compile(
    r"(?::\s*error\b|\berror\s+[A-Z]{2,}\d+:|\bfatal error:|\berror TS\d+:|^error(?:\[E\d+\])?:|\.go:\d+:\d+:|\b(?:SyntaxError|IndentationError|ImportError|NameError|TypeError|AssertionError)\b|\bFAILED\s+\S+|^e:\s+|\berror:\s+)",
    re.IGNORECASE,
)
DIAG_PROJ_SUFFIX_RE = re.compile(r"\s*\[[^\]]+\.(?:csproj|vbproj|fsproj|vcxproj|targets)\]\s*$", re.IGNORECASE)
LEADING_TS_RE = re.compile(r"^(?:\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\]|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?|\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*")


def normalize_diag_key(line: str) -> str:
    """Normalize compiler/test error line for cross-layer global deduplication."""
    cleaned = LEADING_TS_RE.sub("", line or "").strip()
    cleaned = DIAG_PROJ_SUFFIX_RE.sub("", cleaned)
    cleaned = cleaned.replace("\\", "/")
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_unique_diagnostics(
    text: str,
    global_seen: set[str] | None = None,
) -> tuple[list[str], int]:
    """Extract deduplicated diagnostic error lines, updating `global_seen` across layers."""
    seen = global_seen if global_seen is not None else set()
    unique: list[str] = []
    dups = 0
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or not DIAG_ERROR_RE.search(s):
            continue
        key = normalize_diag_key(s)
        if not key:
            continue
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        unique.append(s)
    return unique, dups


def _extract_latest_block(lines: list[str]) -> list[str]:
    if not lines:
        return []
    start_idx = 0
    for idx in range(len(lines) - 1, -1, -1):
        if RUN_START_RE.search(lines[idx]):
            start_idx = idx
            break
    if start_idx == 0 and len(lines) > 2000:
        start_idx = len(lines) - 2000
    return lines[start_idx:]


def _collapse_repeats(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    prev_norm: str | None = None
    prev_idx = -1
    count = 0

    def _flush() -> None:
        nonlocal count
        if count > 1 and out:
            ln_no, txt = out[-1]
            out[-1] = (ln_no, f"{txt} [repeated {count}x]")
        count = 0

    for ln_no, raw in entries:
        norm = LEADING_TS_RE.sub("", raw).strip()
        if norm and norm == prev_norm:
            count += 1
        else:
            _flush()
            out.append((ln_no, raw))
            prev_norm = norm
            prev_idx = ln_no
            count = 1
    _flush()
    return out


def digest_log_text(text: str, name: str = "log", context: int = 5, max_lines: int = 120) -> str:
    all_lines = (text or "").splitlines()
    total = len(all_lines)
    if total == 0:
        return f"[LOG_DIGEST|{name}|total=0|block=0|matches=0]"

    block = _extract_latest_block(all_lines)
    block_start_no = total - len(block) + 1
    hit_indices = [i for i, ln in enumerate(block) if ERROR_LINE_RE.search(ln)]

    if not hit_indices:
        tail_entries = [(block_start_no + i, block[i]) for i in range(max(0, len(block) - 40), len(block))]
        collapsed = _collapse_repeats(tail_entries)[-max_lines:]
        body = [f"L{ln}|{txt}" for ln, txt in collapsed]
        return f"[LOG_DIGEST|{name}|total={total}|block={len(block)}|matches=0]\n" + "\n".join(body)

    keep_mask = [False] * len(block)
    for hi in hit_indices:
        lo = max(0, hi - context)
        up = min(len(block) - 1, hi + context)
        for j in range(lo, up + 1):
            keep_mask[j] = True

    selected: list[tuple[int, str]] = []
    prev_kept = -2
    for idx, keep in enumerate(keep_mask):
        if not keep:
            continue
        if prev_kept >= 0 and idx > prev_kept + 1:
            selected.append((-1, "..."))
        selected.append((block_start_no + idx, block[idx]))
        prev_kept = idx

    collapsed = _collapse_repeats(selected)
    if len(collapsed) > max_lines:
        collapsed = collapsed[-max_lines:]

    body = [txt if ln == -1 else f"L{ln}|{txt}" for ln, txt in collapsed]
    return f"[LOG_DIGEST|{name}|total={total}|block={len(block)}|matches={len(hit_indices)}]\n" + "\n".join(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract latest run block + error context from large logs.")
    parser.add_argument("path", help="Path to log file.")
    parser.add_argument("--context", type=int, default=5, help="Context lines before/after each match.")
    parser.add_argument("--max-lines", type=int, default=120, help="Max output lines.")
    parser.add_argument("--errors-only", action="store_true", help="Output only globally deduplicated diagnostic errors.")
    args = parser.parse_args()

    p = Path(args.path).expanduser()
    if not p.is_file():
        print(f"[LOG_DIGEST|missing|{p}]", file=sys.stderr)
        raise SystemExit(1)

    text = p.read_text(encoding="utf-8", errors="ignore")
    if args.errors_only:
        uniq, dups = extract_unique_diagnostics(text)
        print(f"[LOG_DIGEST_ERRORS|{p.name}|unique={len(uniq)}|deduped={dups}]")
        for ln in uniq[: max(10, args.max_lines)]:
            print(ln)
        return
    print(digest_log_text(text, name=p.name, context=max(0, args.context), max_lines=max(10, args.max_lines)))


if __name__ == "__main__":
    main()
