# Universal Dynamic Fractal Planner & Token-Diet Rules (v3)

CLI base: `~/.gemini/config/plugins/dynamic-fractal-planner/scripts/` (`plan_tool.py`, `verify_universal.py`, `log_digest.py`, `repo_map.py`).

## Core Rules
- **Reply language & brevity:** Reply in the user's language concisely (~8-12L routine; expand ONLY when requested).
- **Batching & subagents:** Batch independent reads/greps in 1 turn && keep subagent replies <=20L.
- **Termux ARM64 PRoot safety:** `parallel(heavy_tests || dotnet_build || git_push) -> FORBIDDEN` (run sequentially to avoid OOM/PRoot kill).
- **Mode (`lean` default | `strict` via profile/`PLANNER_MODE=strict`):**
  - `lean` -> `PreInvocation` injects auto-REQs + JIT checklists (0 plan-init overhead); `Tier <= 1` 4th file triggers Blast Radius `force_ask`.
  - `strict` -> Phase 0 `plan_tool.py init` before code edits (`PLAN GATE`); close cleanly via `plan_tool.py finish --evidence "..."`.
- **Token Diet:** Never read `transcript*.jsonl` (use `manage_subagents`) | files `>300L` -> read `<=250L` slices via symbol map or `repo_map.py` | logs `>300L` -> `log_digest.py` | `PROJECT_HISTORY.md` -> lines `1-60` | `ECO_MODE` prompts suppress secondary anchors/reminders.
- **Shift-Left & Anti-Loop:** Secrets/conflicts/fail-`diff_rules`/destructive `write_file` block pre-disk | `same_cmd_fails>=2 || file_edits>=4` -> Circuit Breaker stop & rethink | `undo-turn` restores active turn.
- **Memory & Verification:** No static `"Current State"` headers; prepend `PROJECT_HISTORY.md`, archive deferred ideas in `SAVED_IDEAS.md`, resolve `EXTRA_REQUESTS.md` before commit/push, use `handoff` for fresh chats. Stop auto-runs fast static checks; run `--build` when compiled/test files changed.
- **Plugin Compat Gate:** Edits inside plugin -> never break older `schema_version` (add chained migrator in `schema_migrations.py`), preserve `tests/fixtures/profile_v[12]_*.json`, run `tests/run_plugin_tests.py` before stopping.
