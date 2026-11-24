# Project History & Pitfalls (`PROJECT_HISTORY.md`)

> RULE|chronological archive ONLY (`SessionStart` injects live git/versions/queue) -> `!static("Current State"|"Current Version")`.
> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:line`, `symbol()`, invariants).

---

## 1. Env & Stack
- Python 3 stdlib only (zero pip deps); tests: `python3 -B tests/run_plugin_tests.py --fast` (`--full` pre-release); verify: `python3 -B scripts/verify_universal.py --git-stat`.

## 2. Pitfalls & Invariants
- PITFALL|`planner_state.py:is_real_project_root` -> embedded `.agents/plugins/*` outside `~/.gemini` scanned as project -> always exclude `resolve_plugin_root().resolve()`.
- PITFALL|third-party crypto/HTTP imports -> break Termux/CI/sandbox hosts -> keep 100% stdlib (`hashlib`/`hmac`/`urllib`).

## 3. Version History (Newest First)
<!-- RULE: PREPEND 1-3L entries directly below (newest first). !static("Current State"). -->
<!-- FORMAT: - YYYY-MM-DD|<tag>|<scope/file:sym> <change> -> <effect> | verified:<cmd> -->
- 2026-11-23|v2.11.0|`scripts/{host_context,verify_universal,plan_tool,self_evolve}.py` + `hooks/planner_session_start.py` + `rules/AGENTS.md` + `tests/run_plugin_tests.py` -> added universal multi-host adaptive integration & zero-talk baton (`detect_host`, `audit_host_matrix`, `register_tool_entry`, `detect_baton_state`, `run_host_boot`, `<1800`-char `SessionStart` budget trim) | verified:`tests/run_plugin_tests.py`
- 2026-11-23|v2.10.0|`scripts/verify_universal.py:check_file_budget` + `README.md` + `tests/run_plugin_tests.py` + `plugin.json` -> added universal `file_budget_limits` workspace file-count guard (warn/fail thresholds + top-5 heaviest dirs) & refreshed `README.md` for standalone GitHub release | verified:`tests/run_plugin_tests.py --fast`
- 2026-11-23|v2.9.0|`scripts/{schema_migrations,self_evolve,plan_tool,verify_universal}.py` + `hooks/{planner_state,planner_session_start}.py` + `install.sh` + `tests/{run_plugin_tests.py,fixtures/profile_v3_cavern.json}` -> added retroactive `v3->v4` workspace auto-upgrade + `plugin_version` tracking (downgrade-safe), bidirectional higher-version plugin sync (`plan_tool.py evolve sync-plugin`), universal `code_weight_limits` anti-monolith guard, cross-file `key_parity_rules.target_path`, & zero-FP gitless `--all` plugin-sync check | verified:`tests/run_plugin_tests.py --fast`
- 2026-11-23|v2.8.0|`scripts/{complexity_triage,verify_universal}.py` + `hooks/planner_pre_invocation.py` + `rules/AGENTS.md` + `skills/dynamic-planner/SKILL.md` + `tests/run_plugin_tests.py` -> added universal Tier 3 `MOD-SOTA-PREFLIGHT` (`SOTA_PREFLIGHT|` hook), raised `_compact_domain_lenses` cap `220 -> 420` chars (eliminating silent lens truncation), && added byte-bloat warnings (`PROJECT_HISTORY.md > 28 KB`, `EXTERNAL_AI_SYNC.md > 48 KB`) in `check_memory_weight()` | verified:`tests/run_plugin_tests.py --fast`
- 2026-11-22|v2.7.0-r1|`scripts/live_bridge_client.py` -> added `EXIT_SESSION_EXPIRED=4` (HTTP 404 distinction vs network timeout) + `on_session_acquired` pre-cmd callback so host wrappers persist approved biometric wake tokens immediately | verified:`tests/run_plugin_tests.py --fast`
- 2026-11-22|v2.7.0|`scripts/{verify_universal,live_bridge_client}.py` + `tests/run_plugin_tests.py` + `plugin.json` -> added gitless auto-discovery (`--all`), `check_csharp_syntax_deep` (catches `CS1002`/`CS1513` trailing-comma statement assignments), & Live Bridge acoustic/UI-perf command help | verified:`tests/run_plugin_tests.py --fast`
- 2026-11-22|v2.6.0-r2|`scripts/{live_diagnostics,live_bridge_client}.py` -> added Cloudflare Unified Telemetry Hub v3 fetcher (`--cloudflare`/`--history`/`derive_telemetry_headers`) + Live Bridge v3 `--pipeline` & Tier 2 dynamic execution CLI | verified:`tests/run_plugin_tests.py --fast`
- 2026-11-22|v2.6.0|`scripts/{error_learner,live_diagnostics,live_bridge_client,self_evolve}.py` + `plan_tool.py` + `verify_universal.py` + `planner_session_start.py` + `.agents/docs/{EXTERNAL_AI_SYNC,HANDOFF,BUILD_LESSONS}.md` -> universalized timeless error learning, live GitHub diagnostics, biometric wake bridge client v2, autonomous dual-scope self-evolution & 1:1 standalone plugin sync ledgers | verified:`tests/run_plugin_tests.py --fast`
