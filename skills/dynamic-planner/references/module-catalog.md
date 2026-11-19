# Module Catalog & `.agents/planner_profile.json` v3

## 1. Universal Modules
| Module | Domain | Trigger | Gate |
| :--- | :--- | :--- | :--- |
| `MOD-DISCOVERY` | `discovery` | Tier >= 2 \|\| unknown symbols | `file:line` map + invariants + GDK/Context7 API check |
| `MOD-CONTRACT` | `contract` | Complex / multi-layer | Types, state, failure semantics |
| `MOD-UI-FRONTEND` | `ui-frontend` | UI/CSS/layout/i18n | Anti-clipping, responsive, locale parity |
| `MOD-BACKEND` | `api-backend` | API/auth/DB/migrations | Validation, auth, schema compat |
| `MOD-CORE-LOGIC` | `core-logic` | Parsers/codecs/DSP/state | Null/EOF/bounds invariants |
| `MOD-RUNTIME` | `runtime-infra` | Threads/IPC/FS/permissions | Race/disposal/logs |
| `MOD-TESTS` | `tests-qa` | Automated tests | Real run after final edit |
| `MOD-AGENT-INFRA` | `agent-infra` | Rules/hooks/skills/scripts | Syntax + simulated hook tests |
| `MOD-HARDEN` | `harden` | Tier 3 / error paths | Zero regression + cleanup |
| `MOD-AUDIT` | `audit` | Tier >= 1 | `verify_universal.py` + `audit` == 0 |
| `MOD-BUILD-CI` | `build-ci` | Build/package/commit/push | Clean build + `EXTRA_REQUESTS.md` empty |

## 2. Profile v3 Schema (`.agents/planner_profile.json`)
```json
{
  "schema_version": 3,
  "project_name": "MyApp",
  "planner_mode": "auto",
  "auto_mode": {"overrides": {}},
  "read_budget": {"max_full_read_lines": 300, "max_slice_lines": 250},
  "secret_guard": {"enabled": true},
  "blast_radius": {"simple_tier_max_files": 3},
  "build_rules": [{"label": "test", "command": "npm test", "trigger_extensions": [".ts"], "run_on_force": true}],
  "domains": {}
}
```
Legacy `v1`/`v2` profiles migrate losslessly in memory via `scripts/schema_migrations.py` (`plan_tool.py migrate` writes v3 to disk).
