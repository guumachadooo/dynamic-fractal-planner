#!/usr/bin/env python3
"""On-demand compact codebase symbol index cached in `/tmp` (zero repo pollution)."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = SCRIPTS_DIR.parent / "hooks"
for p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from planner_state import DEFAULT_IGNORED_PREFIXES, extract_symbol_map  # noqa: E402

MAP_EXTS = {
    ".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".swift", ".dart", ".sh",
}


def _git_out(repo: Path, args: list[str]) -> str:
    if not (repo / ".git").exists():
        return ""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return res.stdout if res.returncode == 0 else ""
    except Exception:
        return ""


def _cache_signature(repo: Path) -> str:
    head = _git_out(repo, ["rev-parse", "HEAD"]).strip()
    status = _git_out(repo, ["status", "--porcelain"])
    if head or status:
        return hashlib.sha256(f"{head}\n{status}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    # Non-git fallback signature using root mtime
    try:
        return f"nogit-{int(repo.stat().st_mtime)}"
    except Exception:
        return "nogit"


def _list_candidate_files(repo: Path) -> list[str]:
    if (repo / ".git").exists():
        tracked = _git_out(repo, ["ls-files", "-z"])
        if tracked:
            files = [
                f.replace("\\", "/")
                for f in tracked.split("\0")
                if f and Path(f).suffix.lower() in MAP_EXTS
            ]
            return sorted(files)

    out: list[str] = []
    skip_dirs = {d.rstrip("/") for d in DEFAULT_IGNORED_PREFIXES}
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in skip_dirs)
        for name in sorted(names):
            if Path(name).suffix.lower() in MAP_EXTS:
                rel = (Path(root) / name).relative_to(repo).as_posix()
                out.append(rel)
                if len(out) >= 400:
                    return out
    return out


def build_repo_map(repo: Path, max_lines: int = 55, use_cache: bool = True) -> str:
    repo = repo.expanduser().resolve()
    repo_hash = hashlib.sha256(str(repo).encode("utf-8", errors="ignore")).hexdigest()[:12]
    cache_file = Path(f"/tmp/.agy_repomap_{repo_hash}.txt")
    sig = _cache_signature(repo)

    if use_cache and cache_file.is_file():
        try:
            cached = cache_file.read_text(encoding="utf-8", errors="ignore")
            first_nl = cached.find("\n")
            header = cached[:first_nl] if first_nl != -1 else cached
            if f"|sig={sig}|max={max_lines}]" in header:
                return cached
        except Exception:
            pass

    files = _list_candidate_files(repo)
    rows: list[str] = []
    for rel in files:
        full = repo / rel
        if not full.is_file():
            continue
        try:
            lines = full.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        syms = extract_symbol_map(lines, full.suffix, max_entries=10)
        if not syms:
            continue
        names = []
        for entry in syms:
            sym = entry.split("|", 1)[1] if "|" in entry else entry
            if sym.startswith("..."):
                continue
            names.append(sym)
            if len(names) >= 6:
                break
        if names:
            rows.append(f"{rel}|{','.join(names)}")
        if len(rows) >= max(5, max_lines - 1):
            break

    rendered = f"[REPO_MAP|{repo.name}|files={len(files)}|shown={len(rows)}|sig={sig}|max={max_lines}]"
    if rows:
        rendered += "\n" + "\n".join(rows)

    if use_cache:
        try:
            cache_file.write_text(rendered + "\n", encoding="utf-8")
        except Exception:
            pass
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(description="Build compact cached repo symbol map in /tmp.")
    parser.add_argument("--workspace", default=".", help="Repository root.")
    parser.add_argument("--max-lines", type=int, default=55, help="Max output lines (30-60 typical).")
    parser.add_argument("--no-cache", action="store_true", help="Ignore /tmp cache and rebuild.")
    args = parser.parse_args()

    print(build_repo_map(Path(args.workspace), max_lines=max(10, args.max_lines), use_cache=not args.no_cache))


if __name__ == "__main__":
    main()
