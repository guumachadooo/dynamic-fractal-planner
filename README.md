# Dynamic Fractal Planner v3 (`plugin 2.2.0`)

Quota-efficient adaptive planning, token-diet read/terminal guards, shift-left shields, anti-loop circuit breaker, silent static auto-verify & anti-rot memory. Python stdlib only.

## Capabilities (`v3`)
| Area | Mechanism | Behavior |
| :--- | :--- | :--- |
| Schema | `scripts/schema_migrations.py` | Chained `v1->v2->v3` in-memory migration (`CURRENT_SCHEMA_VERSION=3`); zero legacy repo mutation |
| Modes | `auto` \| `lean` \| `strict` | `auto`: `<1ms` exact seed/cache hit (`model_mode_cache.py`) \| unseen -> safe `lean` + 1-time doc ctx -> AI `model-mode set` \| `enter/exit_plan_mode` -> auto-pin `lean`; `lean`: auto-REQs + JIT + non-blocking Blast Radius; `strict`: `PLAN GATE` + `finish` |
| Token Diet | `planner_guard_read.py`, `log_digest.py`, `repo_map.py`, `planner_guard_command.py` | Deny `transcript*.jsonl`; block `>300L` full reads w/ free symbol map; `log_digest.py` error+5L context + `[repeated Nx]`; cached `/tmp` `repo_map.py`; `dotnet build` auto `--nologo -v:minimal` |
| Shields | `planner_guard_files.py`, `planner_guard_command.py` | Shift-left `fail` diff_rules; secret/PEM/conflict shield; destructive `write_file` shield (`<70%` `force_ask`, `>=70% >150L` `deny`); `git stash create` + `undo-turn`; pre-push flight check (`untracked` \| `secrets` \| `unbuilt` \| `2s DNS` \| `[skip ci]`) |
| Reliability | `planner_post_tool.py`, `planner_stop.py`, `verify_universal.py` | Neon handoff turn preservation; Circuit Breaker (`same_cmd_fails>=2` \|\| `edits>=4`); silent `<0.5s` Stop static verify (`syntax`, `brackets`, `orphan_calls`, `diff_rules`, `memory_weight`); universal build/test tracker |
| Continuity | `plan_tool.py`, `install.sh` | `handoff` (`<=20L` `HANDOFF.md`), `stats` (`.metrics.jsonl`), `bootstrap` (TS/Next/Vite, .NET, Gradle/Kotlin, Flutter/Dart), `.bypass_gates`, `install.sh --rollback` |
| Doc Grounding | `planner_state.py`, `complexity_triage.py`, `planner_session_start.py`, `planner_pre_invocation.py` | Auto-detects installed `google-developer-knowledge` & `context7` MCP plugins; injects compact `DOC_MCP`/`DOC_LOOKUP` routing on triage, Tier 0/1 API queries & Circuit Breaker (configurable via `doc_lookup` in profile) |
