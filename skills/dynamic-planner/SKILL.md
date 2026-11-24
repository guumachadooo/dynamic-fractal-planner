---
name: dynamic-planner
description: >-
  Universal v3 adaptive planning protocol (`lean` default | `strict` via profile/PLANNER_MODE):
  pure-Python auto-REQ triage, JIT domain checklists, token-budgeted discovery, fractal scaling,
  turn snapshots (`undo-turn`), and 1-turn audit sign-off (`finish`).
---

# Dynamic Fractal Planner (`dynamic-planner` v3 / Plugin v2.16.0)

Scripts (`~/.gemini/config/plugins/dynamic-fractal-planner/scripts/`): `plan_tool.py` | `blueprint_engine.py` | `verify_universal.py` | `log_digest.py` | `repo_map.py`

## Modes
| Mode | Activation | Flow |
| :--- | :--- | :--- |
| `auto` | `planner_mode:"auto"` \|\| `PLANNER_MODE=auto` | Exact cache/seed hit (`<1ms`, `0 net`); unseen -> safe `lean` + 1-time doc ctx -> `model-mode set`; `enter/exit_plan_mode` -> auto-pin `lean`; precedence `PLANNER_MODE` > profile > `auto_mode.overrides` |
| `lean` (default) | `schema_version=5` default | `PreInvocation` auto-REQs + JIT + `DELEGATE|` + T2/T3 `DISC` (`AUDIT_EXISTING`/`CURATE_OSS`) + T3 `SOTA_PREFLIGHT` + `domains.*.lens` -> direct edits -> Blast Radius warn |
| `strict` | `planner_mode:"strict"` \|\| `PLANNER_MODE=strict` | `plan_tool.py init` pre-edit -> `start`/`expand-step`/`add-module` -> `plan_tool.py finish --evidence "..."` |
| `recon` | `plan_tool.py recon start [--scope "..."]` | Read-only forensic sweep (`planner_guard_files` denies code edits; allows docs/plans); log `[BUG] \| [UNWIRED] \| [STRUCTURAL] \| [UPGRADE] @ path:line`; `recon stop` resumes |

## Tiers & Commands
- `Tier 0 (DIRECT)` -> trivial Q&A / 1-line fix / `[PLUGIN_SYNC_FASTPATH]` (concise plugin sync -> 0-thinking 1-cmd `sync-plugin` or `evolve sync-all --apply --push`, `<=3L` reply) -> direct exec.
- `Tier 1 (STANDARD)` -> 1-2 focused edits -> `Blast Radius` guards >=4th file in `lean`.
- `Tier 2 (META_MODULES)` -> `>=3` REQs \|\| `>=2` domains -> auto-REQs + domain JITs + `AUDIT_EXISTING` (`MOD-*`).
- `Tier 3 (FRACTAL_DEEP)` -> high coupling/arch -> `AUDIT_EXISTING` + `CURATE_OSS` + `SOTA_PREFLIGHT` (`MOD-SOTA-PREFLIGHT`: falsify assumptions vs SOTA, math/physics vs AI tournament, execution-plane & hardware budget split, offline golden-bench proof gate) + `MOD-DISCOVERY -> MOD-SOTA-PREFLIGHT -> MOD-CONTRACT -> MOD-IMPL -> MOD-HARDEN -> MOD-AUDIT`.
- Token Diet: `repo_map.py --workspace .` (`/tmp` cache) | `read_file` `<=250L` slice | archive docs `>150L` -> explicit `StartLine&EndLine <=120L` | `log_digest.py <log> [--errors-only]`.
- Doc/Plan Style: ALL `plan_tool.py` args (`--goal`, `--req`, `--evidence`, `--summary`, `--next`) && agent memory docs -> lossless symbolic telegraphic EN (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:sym`, invariants).
- External API Docs (MCP): Unknown external APIs/SDKs/flags -> consult `Context7` (`resolve-library-id`->`query-docs`) \|\| `Google Developer Knowledge` (`search_documents`/`answer_query`) BEFORE guessing.
- Key CLI ops: `triage --prompt "..."` | `bootstrap [--embed-plugin]` | `evolve {classify|sync|sync-plugin|sync-all --apply --push}` | `blueprint {classify|init|lint|score|status|list|handoff}` (playbook: `skills/sota-blueprint/SKILL.md`) | `init` | `start` | `expand-step` | `add-module` | `done` | `req-verify` | `finish --evidence "..."` | `recon {start|stop|status}` | `model-mode {set|get|list|clear}` | `docs-migrated` | `diag [--clear]` | `undo-turn [--dry-run]` | `handoff [--roadmap --wave --archive] --summary "..." --next "..."` | `stats`.
- Verify pre-stop: `verify_universal.py [--build] [--full] --git-stat` (static `wiring_gate` + `key_parity_rules.ref_regex` + `diff_rules.path_prefixes` + multi-layer `build_rules` shadow cascade w/ global diag dedup; `Stop` runs silent static checks on `needs_verify && !needs_build`).

## Thin-Coordinator Delegation & Model Routing (v5)
- **Adaptive Cubicle Pace (`pace=turbo|eco|normal`):** Default `turbo` fans out parallel isolated subagent cubicles across independent slices; `eco` still uses parallel cubicles whenever net-token-saving (isolated leaf ctx < parent replay bloat); `normal` uses standard adaptive routing.
- **Zero-Quota Bidirectional Sync & Auto-History:** `SessionStart` runs zero-quota bidirectional `global<->embedded` higher-version plugin auto-sync (never downgrades); host `PROJECT_HISTORY.md` updates/syncs automatically alongside version/handoff ledgers.
- **Thin Coordinator (T2/T3 & `/goal`):** Main chat orchestrates ONLY — keep context small; delegate bulk reads, multi-module edits, log digests, & sequential PRoot builds/tests to max safe parallel `invoke_subagent` / `run_workflow` / `swarm` (`<=20L` symbolic EN replies).
- **Subagent Context & Output Diet:** Subagents start fresh (0 parent ctx; `!swarm fork`). Prefer fresh 1-shot subagents per slice (`!reuse` after heavy logs or unrelated tasks; `send_message` ONLY for tiny stateful follow-ups `<=2` turns) && `!cmd-echo` (pass concise symbolic goal + target path/CLI verb ONLY — never paste long bash pipelines for subagent to echo back).
- **Nested Sub-Subagents (`delegation.sub_subagent`):** When `allow_nesting:true` (default), subagents may spawn nested `invoke_subagent flash_lite`/`flash` || `swarm` `gemini-*-medium` (`gemini-4-hydrogen-medium` w/ `allow_medium_tier:true`) / `gemini-*-low` on non-complex parallel/bulk slices whenever isolating work saves net tokens (`default_model:"flash_lite"`, `swarm_medium_model:"gemini-4-hydrogen-medium"`).
- **Complexity-Tiered Google-Only Models (`!Claude/!OpenAI/!ChatGPT/!o1-o4`, guarded by `hooks/planner_guard_delegation.py`):**
  - `L0 (trivial)` -> `flash_lite` / `gemini-*-low` (1-shot grep, file lists, trivial lookups, nested sub-subagents)
  - `L1-L2 (routine)` -> `flash` / `gemini-*-medium` (`gemini-4-hydrogen-medium` on `swarm` non-complex slices w/ `allow_medium_tier:true`; audits, multi-file impl, unit tests, static verify)
  - `L3 (complex)` -> `pro` (arch, DSP/physics, concurrency/races, SOTA preflight; tag `[L3]` on `research`)
  - Exempt: `image-generator`; explicit `Model` required (`!omit`, `!unannotated inherit`).
