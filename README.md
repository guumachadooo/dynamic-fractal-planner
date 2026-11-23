# Dynamic Fractal Planner

[![Version](https://img.shields.io/github/v/release/guumachadooo/dynamic-fractal-planner?label=Release&color=0A84FF)](https://github.com/guumachadooo/dynamic-fractal-planner/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.10%2B%20Stdlib%20Only-3776AB?logo=python&logoColor=white)](#requirements)
[![Schema](https://img.shields.io/badge/Profile%20Schema-v4%20Auto--Migrating-34C759)](#schema--retroactive-upgrades)
[![Zero Dependencies](https://img.shields.io/badge/External%20Deps-0-111827)](#requirements)

A **zero-dependency, token-efficient AI engineering planner and verification framework** designed for multi-agent workflows across **Antigravity**, **Google AI Studio**, **Claude Code**, and terminal environments. It combines 4-layer adaptive complexity triage, first-principles SOTA pre-flight checks, forward wiring gates, static guardrails, anti-bloat file/code/memory budgets, timeless error learning, and autonomous dual-scope self-evolution.

---

## Core Capabilities

| Layer | Modules | What It Does |
| :--- | :--- | :--- |
| **4-Layer Adaptive Triage** | `scripts/complexity_triage.py`, `hooks/planner_pre_invocation.py`, `hooks/planner_state.py` | Automatically classifies prompts into Tiers 0–3 with multi-turn `task_context` (30m TTL), live `exit_plan_mode` escalation, domain expert lenses (`LENS\|`), and `/btw` side-request queueing. |
| **SOTA Pre-Flight (`MOD-SOTA-PREFLIGHT`)** | `scripts/complexity_triage.py`, `hooks/planner_pre_invocation.py` | Injects a structured 4-step architectural challenge on complex Tier 3 tasks (falsify assumptions vs SOTA, math/physics vs AI tournament, execution-plane budgets, offline golden-bench gate), auto-suppressed in `ECO_MODE`. |
| **Token Diet & Read Guards** | `hooks/planner_guard_read.py`, `scripts/log_digest.py`, `scripts/repo_map.py` | Blocks blind reads of `>300L` files (returning symbol maps instead), enforces `<=120L` bounded slices on archive docs, denies raw transcript dumps, and deduplicates compiler logs (`--errors-only`). |
| **Universal Static & Wiring Verifier** | `scripts/verify_universal.py` | `<0.5s` multi-language verifier covering syntax (`.py`/`.json`/`.sh`/deep C#), balanced brackets, secret/conflict shields, `MOD-WIRING-GATE` (orphan public APIs, identical `if/else` stubs, unwired settings), cross-file localization parity (`target_path`), and gitless `--all` container scans. |
| **Code, Memory & File-Budget Guards** | `scripts/verify_universal.py`, `scripts/schema_migrations.py` | Enforces anti-monolith line limits (`code_weight_limits`), compact handoff/history byte ceilings (`memory_weight_limits`), and total workspace file-count caps (`file_budget_limits`, preventing AI Studio 999-file import lockouts). |
| **Timeless Error Learning & Live Diagnostics** | `scripts/error_learner.py`, `scripts/live_diagnostics.py`, `scripts/live_bridge_client.py` | Extracts version-agnostic build lessons from CI logs (Roslyn, TypeScript, Rust, Clang, GitHub Actions), enforces anti-obsolescence audits, fetches uncached CI diagnostics, and drives E2E-encrypted device bridges. |
| **Autonomous Self-Evolution (`MOD-SELF-EVOLVE`)** | `scripts/self_evolve.py`, `scripts/plan_tool.py`, `install.sh` | Classifies reusable improvements (`project-only`, `plugin-universal`, `dual-project+plugin`), keeps embedded and global plugin copies synced at the highest semver (`sync-plugin`), and auto-upgrades `v1..v3 -> v4` profiles losslessly. |

---

## Quick Start

### Install / Upgrade Globally
```bash
git clone https://github.com/guumachadooo/dynamic-fractal-planner.git
cd dynamic-fractal-planner
bash install.sh
```
> Downgrade protection is built in — `install.sh` never overwrites a newer installed version unless `--force` is explicitly passed, and always keeps a `.last_good_backup` rollback copy (`bash install.sh --rollback`).

### Bootstrap a Repository
```bash
python3 -B scripts/plan_tool.py bootstrap
```

### Run Full Verification & Self-Tests
```bash
# Run all static guardrails, wiring gates, code/memory/file budgets
python3 -B scripts/verify_universal.py --all

# Run the full 54-test plugin regression suite
python3 -B tests/run_plugin_tests.py --fast
```

---

## Key CLI Commands (`scripts/plan_tool.py`)

| Command | Purpose |
| :--- | :--- |
| `plan_tool.py bootstrap` | Initialize `.agents/planner_profile.json` (`schema_version: 4`) in a project. |
| `plan_tool.py verify --all` | Execute full universal static checks and optional multi-layer build rules. |
| `plan_tool.py recon start \| stop \| status` | Toggle read-only forensic audit mode (`MOD-RECON-SWEEP`). |
| `plan_tool.py handoff --roadmap <path> --wave <id>` | Generate compact `<=20L` `HANDOFF.md` with cross-session `WAVE_BRIDGE\|`. |
| `plan_tool.py evolve classify --summary "..."` | Classify an improvement into `project-only`, `plugin-universal`, or `dual-project+plugin`. |
| `plan_tool.py evolve sync-plugin --apply` | Propagate the highest semver bidirectionally between embedded & global installations. |
| `plan_tool.py learn --audit-lessons` | Audit `BUILD_LESSONS.md` for timeless, version-agnostic rules. |
| `plan_tool.py diag` | Inspect zero-token anomaly diagnostics (`.diagnostics/anomalies.jsonl`). |

---

## Schema & Retroactive Upgrades

Workspace profiles (`.agents/planner_profile.json`) auto-migrate in memory and on disk from `schema_version: 1..3` to `4` without breaking existing custom rules. Optional `v2.10` guard block:

```json
"file_budget_limits": {
  "enabled": true,
  "warn_files": 850,
  "fail_files": 920,
  "hard_platform_cap": 999
}
```

---

## Requirements

- **Python 3.10+** (standard library only — zero `pip` packages, safe on Linux, macOS, Termux ARM64, CI runners, and sandboxed containers).

---

## Ecosystem

- [**Cavern / Cavernize Android**](https://github.com/guumachadooo/Cavern) — Spatial audio player, Dolby Atmos/IAMF/HOA3 engine, and neural stem studio developed with Dynamic Fractal Planner.
- [**OmniAgent**](https://github.com/guumachadooo/OmniAgent) — Zero-Trust E2E Android + Termux + Cloudflare Durable Objects remote development bridge.

## License

MIT License — see repository metadata for details.
