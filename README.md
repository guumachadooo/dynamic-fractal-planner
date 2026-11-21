# Dynamic Fractal Planner v3 (`plugin 2.5.1`)

Quota-efficient adaptive planning, 4-layer context-aware triage, token-diet read/terminal guards, shift-left shields, forward wiring-gate, read-only recon sweep, multi-layer build cascade/dedup & anti-rot memory. Python stdlib only.

## Capabilities (`v3`)
| Area | Mechanism | Behavior |
| :--- | :--- | :--- |
| Triage | `complexity_triage.py`, `planner_state.py`, `planner_pre_invocation.py`, `planner_post_tool.py` | 4-layer adaptive triage + multi-turn `task_context` (30m TTL) + `exit_plan_mode(plan=...)` escalation + T2/T3 JIT `DISC\|AUDIT_EXISTING`/`CURATE_OSS` + per-domain `"lens"` (`LENS\|...`) + `/btw` auto-queue (`[BTW_QUEUE]`, `<=2L` reply) |
| Schema | `scripts/schema_migrations.py` | Chained `v1->v2->v3` in-memory migration (`CURRENT_SCHEMA_VERSION=3`); optional `v2.5` blocks (`wiring_gate`, `archive_docs`, `diff_rules.path_prefixes`, `domains.*.lens`) backward-compatible |
| Modes | `auto` \| `lean` \| `strict` \| `recon` | `auto`/`lean`/`strict` planning modes + `plan_tool.py recon {start\|stop\|status}` (`MOD-RECON-SWEEP`): read-only forensic audit blocking code edits while allowing docs/plans & standardizing `[BUG]\|[UNWIRED]\|[STRUCTURAL]\|[UPGRADE] @ path:line` |
| Token Diet | `planner_guard_read.py`, `log_digest.py`, `repo_map.py`, `planner_guard_command.py` | Deny `transcript*.jsonl`; block `>300L` reads w/ symbol map; archive doc guard (`.agents/docs/archive/`, `archive_docs`, `HANDOFF` `ARCHIVE\|`) requiring bounded `StartLine&EndLine <=120L`; `log_digest.py` (`--errors-only` global dedup); `repo_map.py` |
| Shields | `planner_guard_files.py`, `planner_guard_command.py` | Shift-left `fail` `diff_rules` (`path_prefix` + `path_prefixes` list); secret/PEM/conflict shield; `recon` code-edit block; destructive `write_file` shield; `undo-turn`; pre-push flight check |
| Wiring & Build | `verify_universal.py`, `log_digest.py` | `MOD-WIRING-GATE` (`wiring_gate`): orphan new public APIs (`library_prefixes`), identical `if/else` stubs (`.py` AST + brace scanner), unwired `settings_files` keys, `key_parity_rules` `ref_regex` UI coverage; multi-layer `build_rules` (`continue_on_error`, `on_upstream_fail_command` shadow fallback, global diag dedup + `BUILD_SUMMARY\|...`) |
| Reliability | `planner_post_tool.py`, `planner_stop.py`, `plugin_diagnostics.py` | Distinct-invocation Circuit Breaker; silent `<0.5s` Stop static verify (`syntax`, `brackets`, `orphan_calls`, `wiring_gate`, `diff_rules`, `memory_weight`); 1-shot `verify_universal.py --git-stat`; zero-token `.diagnostics/anomalies.jsonl` (`plan_tool.py diag`) |
| Continuity | `plan_tool.py`, `planner_session_start.py`, `install.sh` | `handoff [--roadmap --wave --archive]` (`<=20L` `HANDOFF.md` + compact `WAVE_BRIDGE\|...` on `SessionStart`), `stats`, `bootstrap`, `docs-migrated`, `.bypass_gates`, `install.sh --rollback` |
| Doc Grounding | `planner_state.py`, `complexity_triage.py`, `planner_session_start.py`, `planner_pre_invocation.py` | Auto-detects `google-developer-knowledge` & `context7` MCP plugins; injects compact `DOC_MCP`/`DOC_LOOKUP` routing |
