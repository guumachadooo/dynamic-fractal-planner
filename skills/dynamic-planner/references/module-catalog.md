# Universal Module Catalog & Project Profile Guide (`module-catalog.md`)

---

## 1. Universal Composable Modules

Select **only** the modules needed for the task (prune unused modules on small tasks; expand recursively when complexity rises):

| Module ID | Domain Key | Activate When... | Exit Gate |
| :--- | :--- | :--- | :--- |
| `MOD-DISCOVERY` | `discovery` | Tier 2/3 or unknown symbols | Exact `file:line` map + project invariants noted |
| `MOD-CONTRACT` | `contract` | Single complex task or multi-layer coupling | Interfaces, state ownership, and error handling written before editing |
| `MOD-UI-FRONTEND` | `ui-frontend` | UI, styling, layout, animations, i18n | Responsive/anti-clipping, accessibility, locale parity |
| `MOD-BACKEND` | `api-backend` | APIs, auth, DB/migrations, services | Input validation, auth boundaries, backward-compatible contracts |
| `MOD-CORE-LOGIC` | `core-logic` | Algorithms, parsers, codecs, state machines | Boundary/null/EOF guards + deterministic invariants |
| `MOD-RUNTIME` | `runtime-infra` | Concurrency, processes/IPC, FS/permissions | Race/disposal safety, exit codes, diagnostics logging |
| `MOD-TESTS` | `tests-qa` | Unit/integration/e2e test creation or updates | Real test execution observed after final edit |
| `MOD-AGENT-INFRA` | `agent-infra` | Rules, skills, hooks, planner scripts | Syntax checks + simulated hook/tool verification |
| `MOD-HARDEN` | `harden` | Tier 3 or high-risk failure paths | Failure modes + zero regression of existing capabilities |
| `MOD-AUDIT` | `audit` | Always on Tier >= 1 | `verify_universal.py` passes + `plan_tool.py audit` = 0 |
| `MOD-BUILD-CI` | `build-ci` | User asked to build, package, commit, or release | Clean build/CI artifact + release/changelog synced |

---

## 2. Optional Per-Project Profile (`.agents/planner_profile.json`)

Any repository can customize triage, paths, Just-in-Time guardrails, and verifiers without touching the global plugin by creating `.agents/planner_profile.json`:

```json
{
  "project_name": "MyProject",
  "code_path_prefixes": ["src/", "app/"],
  "code_extensions": [".ts", ".tsx", ".py"],
  "extra_requests_file": ".agents/docs/EXTRA_REQUESTS.md",
  "verify_command": "npm test && npm run typecheck",
  "verify_non_code_command": "python3 .agents/scripts/verify_changes.py",
  "domains": {
    "payments": {
      "module_id": "MOD-PAYMENTS",
      "module_title": "Billing, Webhooks & Idempotency",
      "keywords": ["stripe*", "webhook*", "billing", "fatura*"],
      "checklist": [
        "Verify webhook signature and idempotency keys before updating ledger."
      ],
      "is_ship": false
    }
  }
}
```
