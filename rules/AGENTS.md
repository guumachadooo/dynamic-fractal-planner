# Universal Dynamic Fractal Planner & Token-Diet Rules (v3)

CLI: `~/.gemini/config/plugins/dynamic-fractal-planner/scripts/` (`plan_tool.py`, `verify_universal.py`, `log_digest.py`, `repo_map.py`, `model_mode_cache.py`).

## Core Rules
- **Reply:** user's language | ~8-12L routine (expand ONLY when requested) | report verified output ONLY.
- **Batch & Subagents:** independent reads/greps in 1 turn && subagent replies <=20L.
- **Termux ARM64 PRoot:** `parallel(heavy_tests || dotnet_build || git_push) -> FORBIDDEN` (run sequentially).
- **Mode (`auto` | `lean` | `strict`):** precedence `PLANNER_MODE` > `profile.planner_mode` > `auto_mode.overrides`.
  - `auto` -> known cache/seed hit (`<1ms`, `0 net`) | unseen `modelName` -> safe `lean` + 1-time doc ctx -> AI runs `plan_tool.py model-mode set <model> <lean|strict> --why "..."` | offline -> safe `lean` w/ cooldown | `enter_plan_mode`/`exit_plan_mode` -> auto-pin `lean`.
  - `lean` -> `PreInvocation` auto-REQs + JIT (0-turn overhead); `Tier<=1` >=4th file -> Blast Radius warn (`action:"force_ask"` opt-in once/turn).
  - `strict` -> Phase 0 `plan_tool.py init` BEFORE edits (`PLAN GATE`) && `plan_tool.py finish --evidence "..."`.
- **Token Diet:** `!read(transcript*.jsonl)` | files `>300L` -> `<=250L` slice via symbol map/`repo_map.py` | logs `>300L` -> `log_digest.py` | `PROJECT_HISTORY.md` `L1-60` | `ECO_MODE` drops secondary hints.
- **External API Docs (MCP):** Unknown external APIs/SDKs/flags -> consult `Context7` (3rd-party libs: `resolve-library-id`->`query-docs`) || `Google Developer Knowledge` (Android/Google/Cloud/Firebase/Flutter/Chrome/Go: `search_documents`/`answer_query`) BEFORE guessing.
- **Shift-Left & Anti-Loop:** secrets/conflicts/fail-`diff_rules`/destructive `write_file` block pre-disk | `same_cmd_fails>=2 || file_edits>=4` -> Circuit Breaker stop | `undo-turn` reverts active turn.
- **Memory & Verify:** `!static("Current State")` | prepend `PROJECT_HISTORY.md` | defer -> `SAVED_IDEAS.md` | clear `EXTRA_REQUESTS.md` pre-commit/push | Stop runs fast static checks (`--build` on compiled/test edits).
- **Plugin Compat Gate:** preserve `profile_v[12]_*.json` && add chained migrator in `schema_migrations.py` && pass `tests/run_plugin_tests.py` pre-stop.
