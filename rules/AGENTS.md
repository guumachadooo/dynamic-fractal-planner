# Universal Dynamic Fractal Planning, Memory & Verification Rules

Apply adaptive planning and persistent anti-rot memory across every workspace:

1. **Phase 0 Before Heavy Exploration:** On multi-item (`>= 3` demands) or high-complexity tasks (`Tier 2` / `Tier 3`), lock every user requirement (`REQ-01..REQ-N`) in the session `TASK_PLAN.md` via `~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py init` **before** reading large source files.
2. **Dynamic Scaling & Pruning:**
   - Keep `Tier 0` (questions, tiny 1-line fixes) friction-free.
   - Decompose even a **single complex task** into specialized modules (`MOD-DISCOVERY -> MOD-CONTRACT -> MOD-IMPL -> MOD-HARDEN -> MOD-AUDIT`).
   - Expand steps dynamically mid-flight (`expand-step` / `add-module`) whenever scope grows.
3. **Lean Discovery:** Locate targets with `grep -rn` / `git grep -n` and bounded `read_file` slices (`<= 150` lines), or delegate broad multi-subsystem reconnaissance to a `research` subagent.
4. **Just-in-Time Checklists & Verification:** Run `plan_tool.py start` before coding each module so its domain guardrails are active; before finishing or committing, run `verify_universal.py` (with `--build` when compiled/tested code changed) and verify every `REQ-XX` with observed evidence via `plan_tool.py audit`.
5. **Project Memory & Parallel `/btw` Queue:** Never write static `"Current State"` headers (rely on `SessionStart`). Update `.agents/docs/PROJECT_HISTORY.md` (newest first) for non-obvious fixes/milestones, archive deferred ideas in `SAVED_IDEAS.md`, and always resolve pending items in `.agents/docs/EXTRA_REQUESTS.md` before committing or finishing (see `project-memory` skill / `plan_tool.py bootstrap`).
6. **Plugin Self-Update & Backward-Compatibility Gate:** Whenever editing files inside `~/.gemini/config/plugins/dynamic-fractal-planner/`, never break an older `schema_version` without adding a deterministic chained migrator in `scripts/schema_migrations.py`, keep frozen fixtures in `tests/fixtures/` intact, and verify all regression checks pass via `python3 ~/.gemini/config/plugins/dynamic-fractal-planner/tests/run_plugin_tests.py` (or `verify_universal.py --plugin-tests`) before stopping.
