# Project Guidelines — Dynamic Fractal Planner (`guumachadooo/dynamic-fractal-planner`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim paths/symbols). Reply to USER in user's chat language.

## 0. Auto-Loaded AI Studio Boot Context & 3-Repo Federation
- **Universal Plugin Rules:** Follow `rules/AGENTS.md` (canonical universal plugin constitution). Real stack is **pure Python 3 stdlib** (`scripts/`, `hooks/`, `skills/`, `tests/`). Root `package.json` + `.agents/preview/server.mjs` is strictly a **zero-dep AI Studio preview shim** (`0.0.0.0:3000`) bridging Secrets to `/tmp/.cavern_runtime_env.json` & warming sibling repos at boot (0 LLM quota; excluded from `Cavern/.agents/plugins/dynamic-fractal-planner/` mirror). NEVER add pip/npm deps or host-specific hardcoding inside `scripts/`/`hooks/`.
- **Instant 3-Repo Access (`/tmp/cavern-federation/`):** At boot, `server.mjs` automatically hydrates `/tmp/cavern-federation/omniagent` (`guumachadooo/OmniAgent@main`) & `/tmp/cavern-federation/cavern` (`guumachadooo/Cavern@master`) and symlinks `/tmp/cavern-federation/fractal -> /app/applet`.
- **Mandatory Boot & Multi-Repo Commands (via hydrated OmniAgent CLI):**
  - Status & capabilities: `python3 -B /tmp/cavern-federation/omniagent/agent/omni_agent_client.py capabilities && python3 -B /tmp/cavern-federation/omniagent/agent/repo_federation.py --workspace . status`
  - Sync plugin changes -> Cavern embedded copy: `python3 -B /tmp/cavern-federation/omniagent/agent/repo_federation.py --workspace . sync-mirrors --from fractal --apply`
  - Push one or all modified repos (direct `GITHUB_TOKEN` or 1-tap Smart Knock Termux fallback via `.agents/bridge_profile.json`): `python3 -B /tmp/cavern-federation/omniagent/agent/repo_federation.py --workspace . push <fractal|cavern|omniagent|all> --commit "..."`
  - Verify plugin tests before push: `python3 -B tests/run_plugin_tests.py --fast` (and `--full` pre-release).
