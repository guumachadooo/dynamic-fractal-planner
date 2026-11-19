---
name: dynamic-planner
description: >-
  Universal v3 adaptive planning protocol (`lean` default | `strict` via profile/PLANNER_MODE):
  pure-Python auto-REQ triage, JIT domain checklists, token-budgeted discovery, fractal scaling,
  turn snapshots (`undo-turn`), and 1-turn audit sign-off (`finish`).
---

# Dynamic Fractal Planner (`dynamic-planner` v3)

Scripts (`~/.gemini/config/plugins/dynamic-fractal-planner/scripts/`): `plan_tool.py` | `verify_universal.py` | `log_digest.py` | `repo_map.py`

## Modes
| Mode | Activation | Flow |
| :--- | :--- | :--- |
| `lean` (default) | `schema_version=3` default | `PreInvocation` injects auto-REQs + JIT checklists -> edit directly -> Blast Radius `force_ask` on 4th code file in `Tier<=1` |
| `strict` | `planner_mode:"strict"` \|\| `PLANNER_MODE=strict` | `plan_tool.py init` before edits -> `start`/`expand-step`/`add-module` -> `plan_tool.py finish --evidence "..."` |

## Tiers & Commands
- `Tier 0 (DIRECT)` -> trivial Q&A / 1-line fix -> execute directly.
- `Tier 1 (STANDARD)` -> 1-2 focused edits -> `Blast Radius` guards >=4th file in `lean`.
- `Tier 2 (META_MODULES)` -> `>=3` REQs \|\| `>=2` domains -> auto-REQs + domain JITs (`MOD-*`).
- `Tier 3 (FRACTAL_DEEP)` -> high coupling/architecture -> `MOD-DISCOVERY -> MOD-CONTRACT -> MOD-IMPL -> MOD-HARDEN -> MOD-AUDIT`.
- Token Diet: `repo_map.py --workspace .` (cached in `/tmp`), `read_file` slices `<=250L`, `log_digest.py <log>`.
- Key CLI ops: `triage --prompt "..."` | `init` | `start` | `expand-step` | `add-module` | `done` | `req-verify` | `finish --evidence "..."` | `undo-turn [--dry-run]` | `handoff --summary "..." --next "..."` | `stats`.
- Verify before finish: `verify_universal.py [--build]` (`Stop` auto-runs static checks silently when `needs_verify && !needs_build`).
