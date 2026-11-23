#!/usr/bin/env python3
"""Universal Live Diagnostics Fetcher (Dynamic Fractal Planner v2.6.1).

Supports TWO clean, professional live backends with zero CDN cache lag:
  1. GitHub Contents REST API (`diagnostics` branch): CI build diagnostics & learned lessons
     (plus optional legacy device logs fallback).
  2. Cloudflare Unified Telemetry Hub v3 (`--cloudflare` / `--relay-url` + `--pair-secret`,
     or env `LIVE_BRIDGE_URL` + `LIVE_BRIDGE_PAIR_SECRET` / `CAVERN_RELAY_URL` +
     `CAVERN_BRIDGE_PAIR_SECRET`): instant global edge retrieval of runtime device logs,
     crash reports, session summaries, ultra-deep self-checkup reports, and rolling history
     without git commit bloat or GitHub PATs.

Zero third-party dependencies (Python stdlib only). Never writes secrets to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BRANCH = "diagnostics"
USER_AGENT = "DynamicFractalPlanner-LiveDiagnostics/2.6"

DEFAULT_PATHS = {
    "ci_md": "ci/latest-build-diagnostics.md",
    "lessons_md": "ci/latest-learned-lessons.md",
    "selfcheck_md": "device-logs/latest_selfcheck.md",
    "summary_json": "device-logs/latest_summary.json",
    "log_txt": "device-logs/app_log.txt",
    "fallback_log_branch": "master",
    "fallback_log_path": "",
}


def find_repo_root(start_dir: Path | None = None) -> Path:
    base = (start_dir or Path.cwd()).expanduser().resolve()
    try:
        res = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except Exception:
        pass
    return base


def load_diagnostics_config(repo_root: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "repo": "",
        "branch": DEFAULT_BRANCH,
        **DEFAULT_PATHS,
    }
    prof_file = repo_root / ".agents" / "planner_profile.json"
    if prof_file.is_file():
        try:
            prof = json.loads(prof_file.read_text(encoding="utf-8", errors="ignore"))
            diag = prof.get("diagnostics")
            if isinstance(diag, dict):
                for k, v in diag.items():
                    if isinstance(v, str) and v.strip():
                        cfg[k] = v.strip()
        except Exception:
            pass
    if not cfg["repo"]:
        try:
            res = subprocess.run(
                ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if res.returncode == 0:
                m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", res.stdout.strip())
                if m:
                    cfg["repo"] = m.group(1)
        except Exception:
            pass
    return cfg


def normalize_pair_secret_hex(raw_secret: str) -> str:
    s = (raw_secret or "").strip()
    s = re.sub(r"^(?:CAVPAIR|PAIR)[-_:]?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s\-_]", "", s).lower()
    if len(s) != 64 or not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ValueError("Pairing secret must be 64 hexadecimal characters (256-bit key).")
    return s


def derive_telemetry_headers(pair_secret_hex: str) -> dict[str, str]:
    norm = normalize_pair_secret_hex(pair_secret_hex)
    dev_hash = hashlib.sha256(f"wake-channel-v2:{norm}".encode("utf-8")).hexdigest()
    tel_key = hashlib.sha256(f"telemetry-access-v3:{norm}".encode("utf-8")).hexdigest()
    return {
        "X-Device-Hash": dev_hash,
        "X-Telemetry-Key": tel_key,
    }


def _http_get(
    url: str,
    accept: str = "application/vnd.github+json",
    extra_headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> tuple[int, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if "api.github.com" in url:
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return exc.code, body
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def fetch_cloudflare_telemetry(relay_url: str, pair_secret: str, kind: str = "all") -> tuple[int, dict[str, Any]]:
    clean_url = (relay_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(clean_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return 0, {"ok": False, "error": "Cloudflare relay URL must be a valid https:// URL."}
    headers = derive_telemetry_headers(pair_secret)
    endpoint = f"{clean_url}/v1/telemetry/history" if kind == "history" else f"{clean_url}/v1/telemetry/latest?kind={urllib.parse.quote(kind)}"
    code, body = _http_get(endpoint, accept="application/json", extra_headers=headers)
    try:
        return code, json.loads(body) if body else {}
    except Exception:
        return code, {"ok": False, "error": body}


def fetch_raw_file(repo: str, branch: str, path: str) -> tuple[int, str]:
    url = f"https://api.github.com/repos/{repo}/contents/{path.lstrip('/')}?ref={branch}"
    return _http_get(url, accept="application/vnd.github.raw")


def fetch_branch_head(repo: str, branch: str) -> dict[str, Any] | None:
    code, body = _http_get(f"https://api.github.com/repos/{repo}/commits/{branch}")
    if code != 200:
        return None
    try:
        data = json.loads(body)
        commit = data.get("commit", {})
        return {
            "sha": (data.get("sha") or "")[:8],
            "date": (commit.get("committer") or {}).get("date", ""),
            "message": (commit.get("message") or "").splitlines()[0] if commit.get("message") else "",
        }
    except Exception:
        return None


def fetch_latest_workflow_run(repo: str) -> dict[str, Any] | None:
    code, body = _http_get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=3")
    if code != 200:
        return None
    try:
        runs = json.loads(body).get("workflow_runs", [])
        if not runs:
            return None
        r = runs[0]
        return {
            "name": r.get("name", ""),
            "status": r.get("status", ""),
            "conclusion": r.get("conclusion", ""),
            "created_at": r.get("created_at", ""),
            "sha": (r.get("head_sha") or "")[:8],
            "url": r.get("html_url", ""),
        }
    except Exception:
        return None


def print_section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def _filter_log_lines(body: str, tail: int, errors_only: bool) -> str:
    lines = body.splitlines()
    if errors_only:
        lines = [
            ln for ln in lines
            if any(tok in ln for tok in ("[WARN]", "[ERROR]", "[FATAL]", "=== Session started", "Exception", "FAIL"))
        ]
    return "\n".join(lines[-max(1, tail):])


def main(
    argv: list[str] | None = None,
    *,
    default_repo: str = "",
    default_branch: str = "",
    overrides: dict[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Fetch live uncached CI & Cloudflare/GitHub runtime diagnostics.")
    parser.add_argument("--workspace", default="", help="Workspace root override")
    parser.add_argument("--repo", default="", help="GitHub owner/repo (auto-detected from profile or git remote)")
    parser.add_argument("--branch", default="", help="Diagnostics branch (default: profile or 'diagnostics')")
    parser.add_argument("--cloudflare", action="store_true", help="Prefer Cloudflare Unified Telemetry Hub v3 for device logs/selfcheck")
    parser.add_argument("--relay-url", default="", help="Cloudflare Worker HTTPS URL (or env LIVE_BRIDGE_URL / CAVERN_RELAY_URL)")
    parser.add_argument("--pair-secret", default="", help="64-hex Pair Secret (or env LIVE_BRIDGE_PAIR_SECRET / CAVERN_BRIDGE_PAIR_SECRET)")
    parser.add_argument("--history", action="store_true", help="Show rolling Cloudflare telemetry upload history")
    parser.add_argument("--all", action="store_true", help="Fetch CI diagnostics, learned lessons, self-check, summary, and log tail")
    parser.add_argument("--ci", action="store_true", help="Fetch CI build diagnostics report")
    parser.add_argument("--lessons", action="store_true", help="Fetch CI dynamic error-learner candidates")
    parser.add_argument("--selfcheck", action="store_true", help="Fetch device/runtime self-check report")
    parser.add_argument("--summary", action="store_true", help="Fetch device/runtime diagnostic JSON summary")
    parser.add_argument("--log", action="store_true", help="Fetch runtime/device log tail")
    parser.add_argument("--file", default="", help="Fetch any arbitrary file path from the GitHub diagnostics branch")
    parser.add_argument("--tail", type=int, default=120, help="Lines to display from log (default: 120)")
    parser.add_argument("--errors-only", action="store_true", help="Filter log lines to WARN/ERROR/FATAL/Exception")
    args = parser.parse_args(argv)

    repo_root = find_repo_root(Path(args.workspace) if args.workspace else None)
    cfg = load_diagnostics_config(repo_root)
    if overrides:
        for k, v in overrides.items():
            if v:
                cfg[k] = v

    relay_url = args.relay_url or os.environ.get("LIVE_BRIDGE_URL", "").strip() or os.environ.get("CAVERN_RELAY_URL", "").strip()
    pair_secret = args.pair_secret or os.environ.get("LIVE_BRIDGE_PAIR_SECRET", "").strip() or os.environ.get("CAVERN_BRIDGE_PAIR_SECRET", "").strip()
    use_cf = bool(relay_url and pair_secret)

    if args.cloudflare and not use_cf:
        print("ERROR: --cloudflare requires --relay-url and --pair-secret (or LIVE_BRIDGE_URL + LIVE_BRIDGE_PAIR_SECRET).")
        return 2

    repo = args.repo or default_repo or cfg["repo"]
    branch = args.branch or default_branch or cfg["branch"] or DEFAULT_BRANCH

    if args.file:
        if not repo:
            print("ERROR: Could not detect GitHub owner/repo.")
            return 2
        print_section(f"FILE ({repo} @ {branch} : {args.file})")
        code, body = fetch_raw_file(repo, branch, args.file)
        if code == 200:
            print(body.rstrip())
            return 0
        print(f"[HTTP {code}] {args.file} unavailable.")
        return 1

    if not (args.all or args.ci or args.lessons or args.selfcheck or args.summary or args.log or args.history):
        args.all = True

    if repo and (args.all or args.ci or args.lessons or (not use_cf and (args.selfcheck or args.summary or args.log))):
        head = fetch_branch_head(repo, branch)
        run = fetch_latest_workflow_run(repo)
        print_section(f"LIVE GITHUB SNAPSHOT ({repo} @ {branch})")
        if head:
            print(f"Branch '{branch}' HEAD: {head['sha']} ({head['date']}) - {head['message']}")
        else:
            print(f"Branch '{branch}': not created yet.")
        if run:
            print(
                f"Latest Actions run: {run['name']} | status={run['status']} "
                f"conclusion={run['conclusion']} | sha={run['sha']} | {run['created_at']}"
            )

    if repo and (args.all or args.ci):
        ci_p = cfg["ci_md"]
        print_section(f"CI BUILD DIAGNOSTICS ({ci_p})")
        code, body = fetch_raw_file(repo, branch, ci_p)
        if code == 200:
            lines = body.splitlines()
            print("\n".join(lines[:160]))
            if len(lines) > 160:
                print(f"... (+{len(lines) - 160} more lines)")
        else:
            print(f"[HTTP {code}] {ci_p} unavailable yet.")

    if repo and (args.all or args.lessons):
        les_p = cfg["lessons_md"]
        print_section(f"DYNAMIC BUILD LESSONS CANDIDATES ({les_p})")
        code, body = fetch_raw_file(repo, branch, les_p)
        if code == 200:
            print(body.strip())
        else:
            print(f"[HTTP {code}] {les_p} unavailable yet.")

    # Device runtime telemetry: prefer Cloudflare Unified Hub v3 when configured!
    cf_bundle: dict[str, Any] | None = None
    if use_cf and (args.all or args.selfcheck or args.summary or args.log):
        c_code, c_data = fetch_cloudflare_telemetry(relay_url, pair_secret, "all")
        if c_code == 200 and c_data.get("ok"):
            cf_bundle = c_data
            meta = c_data.get("meta") or {}
            print_section(f"CLOUDFLARE UNIFIED TELEMETRY HUB v3 ({urllib.parse.urlparse(relay_url).netloc})")
            if meta:
                print(
                    f"Last upload: {meta.get('uploadedAtUtc', '?')} | trigger={meta.get('trigger', '?')} | "
                    f"app={meta.get('appVersion', '?')} | verdict={meta.get('verdict', '-')}"
                )
        else:
            print(f"[Cloudflare Hub HTTP {c_code}] {c_data.get('error', 'unavailable')}; falling back to GitHub if available.")

    if use_cf and args.history:
        h_code, h_data = fetch_cloudflare_telemetry(relay_url, pair_secret, "history")
        print_section("CLOUDFLARE TELEMETRY HISTORY")
        if h_code == 200 and h_data.get("ok"):
            print(json.dumps(h_data.get("history", []), indent=2, ensure_ascii=False))
        else:
            print(f"[HTTP {h_code}] {h_data.get('error', 'History unavailable')}")

    if args.all or args.selfcheck:
        cf_sc = (cf_bundle or {}).get("artifacts", {}).get("selfcheck_md") if cf_bundle else None
        if cf_sc:
            print_section("SELF-CHECK REPORT (Cloudflare Hub v3)")
            print(cf_sc.strip())
        elif repo:
            sc_p = cfg["selfcheck_md"]
            print_section(f"SELF-CHECK REPORT ({sc_p})")
            code, body = fetch_raw_file(repo, branch, sc_p)
            print(body.strip() if code == 200 else f"[HTTP {code}] {sc_p} unavailable yet.")

    if args.all or args.summary:
        cf_sum = (cf_bundle or {}).get("artifacts", {}).get("summary") if cf_bundle else None
        if cf_sum:
            print_section("SESSION SUMMARY (Cloudflare Hub v3)")
            print(cf_sum.strip())
        elif repo:
            sum_p = cfg["summary_json"]
            print_section(f"SESSION SUMMARY ({sum_p})")
            code, body = fetch_raw_file(repo, branch, sum_p)
            print(body.strip() if code == 200 else f"[HTTP {code}] {sum_p} unavailable yet.")

    if args.all or args.log:
        cf_log = (cf_bundle or {}).get("artifacts", {}).get("log") if cf_bundle else None
        if cf_log:
            print_section(f"LOG TAIL (Cloudflare Hub v3, last {args.tail} lines)")
            print(_filter_log_lines(cf_log, args.tail, args.errors_only))
        elif repo:
            log_p = cfg["log_txt"]
            print_section(f"LOG TAIL ({log_p}, last {args.tail} lines)")
            code, body = fetch_raw_file(repo, branch, log_p)
            if code != 200 and cfg.get("fallback_log_path"):
                fb_branch = cfg.get("fallback_log_branch") or "master"
                fb_path = cfg["fallback_log_path"]
                code, body = fetch_raw_file(repo, fb_branch, fb_path)
                if code == 200:
                    print(f"(Fallback from {fb_branch}/{fb_path})")
            if code == 200:
                print(_filter_log_lines(body, args.tail, args.errors_only))
            else:
                print(f"[HTTP {code}] Log unavailable yet.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
