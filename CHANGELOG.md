# Plugin History (`CHANGELOG.md`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:sym`).

- 2026-11-21|v2.4.0|4-layer adaptive complexity triage: structural/semantic signals (`_extract_file_refs`, `_has_broad_scope`, verb-chained `_split_action_clauses`, `_strip_long_quotes`, read-only vs `plan_proposal` guard) + multi-turn `task_context` state machine (`build_carried_task_context`, `classify_followup_intent`, 1800s TTL, Q&A preservation) + live `exit_plan_mode(plan=...)` escalation (`analyze_plan_text`, `merge_plan_triage`) + deduped `unique_edited_files()` & `triage_mismatch` src/conf metadata | verified:`verify_universal.py --plugin-tests --full`
- 2026-11-19|v2.3.0|symbolic-EN micro-grammar + `docs_format_version=1` auto-upgrade (incl. root `AGENTS.md`/`GEMINI.md`) + 1-shot `verify_universal.py --git-stat` & test dedup + distinct-invocation Circuit Breaker & Blast Radius dedup + zero-token `.diagnostics/anomalies.jsonl` (`plan_tool.py diag`) + minimal-anchor edit diet + `.git` template cache (`--fast` <10s) + `git status -sb` + `install.sh` `.diagnostics` exclude | verified:`verify_universal.py --plugin-tests --full`
- 2026-11-18|v2.2.0|AI-reasoned cached `auto` model-mode (`model_mode_cache.py`), native `enter/exit_plan_mode` auto-pin, GDK/Context7 MCP routing & non-blocking Blast Radius (`d7ea967`)
- 2026-11-18|v2.0.0|v3 `lean`/`strict` modes, read/terminal token diet, shift-left shields, static Stop auto-verify, `handoff`/`undo-turn`/`stats` & `install.sh --rollback` (`9bf851c`)
- 2026-11-18|v1.0.0|universal plugin extraction, `v1->v2` schema migrations, auto-bootstrap & regression gate (`b06b052`)
