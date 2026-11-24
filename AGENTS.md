# Project Guidelines — Dynamic Fractal Planner (`guumachadooo/dynamic-fractal-planner`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim paths/symbols). Reply to USER in user's chat language.

## 0. Auto-Loaded Host-Aware Boot Context (`antigravity-termux` <-> `ai-studio-cloud`)
- **Universal Plugin Rules & Stack:** Follow `rules/AGENTS.md` (canonical universal plugin constitution). Real stack is **pure Python 3 stdlib** (`scripts/`, `hooks/`, `rules/`, `skills/`, `tests/`, `install.sh`). Root `package.json` + `.agents/preview/server.mjs` is strictly a **zero-dep AI Studio preview shim** (`0.0.0.0:3000`, excluded from `Cavern/.agents/plugins/dynamic-fractal-planner/` mirror). NEVER add pip/npm deps or host-specific hardcoding inside `scripts/`/`hooks/`/`rules/`/`skills/`.
- **Unified Host-Aware Boot & Baton (`.agents/host_matrix.json`):**
  1. Run `python3 -B scripts/plan_tool.py host-boot` (OR inspect `SessionStart` `HOST|`/`BATON_SWITCH|`). Auto-detects host (`antigravity-termux` vs `ai-studio-cloud` vs `ci-runner` vs `local-ide`):
     - **On `antigravity-termux`:** skip AI Studio `preview/` shim & `missingExpectedVars` alerts (preserve `.agents/{preview,env_manifest,bridge_profile}` intact for AI Studio!); use native `git`, `gh`, filesystem, & local `/root/.gemini/antigravity/scratch/{dynamic-fractal-planner,Cavern,OmniAgent}` directly.
     - **On `ai-studio-cloud`:** `server.mjs` bridges Secrets (`/tmp/.cavern_runtime_env.json`) & warm-hydrates `/tmp/cavern-federation/{cavern,omniagent}` + symlinks `fractal -> /app/applet`; use `/tmp/cavern-federation/omniagent/agent/{omni_agent_client,repo_federation}.py` for `capabilities`, `status`, & Smart Knock Termux fallback.
  2. End work on ANY host with `python3 -B scripts/plan_tool.py handoff --summary "..." --next "..."` (stamps `host=<host_id>`). Classify any new script/config under `.agents/` or repo root via `python3 -B scripts/plan_tool.py host-classify <path> --apply` (audited by `host-audit` & `verify_universal.py`).
- **Triad Higher-Version Sync & Verification Gates:**
  - Sync canonical <-> global (`~/.gemini/config/plugins/dynamic-fractal-planner`) <-> embedded (`Cavern`, `OmniAgent`) + auto-push in 1 cmd: `python3 -B scripts/plan_tool.py evolve sync-all --apply --push` (or local `sync-plugin --apply`; higher semver ALWAYS wins, never downgrades; AI Studio fallback: `repo_federation.py --workspace . sync-mirrors --from fractal --apply`).
  - Dual-ledger parity: every plugin code edit MUST prepend an entry in `.agents/docs/EXTERNAL_AI_SYNC.md` + `CHANGELOG.md` (and host `EXTERNAL_AI_SYNC.md` when synced).
  - Verify before push: `python3 -B scripts/plan_tool.py host-audit && python3 -B scripts/verify_universal.py --git-stat` and `python3 -B tests/run_plugin_tests.py --fast` (`--full` pre-release).
