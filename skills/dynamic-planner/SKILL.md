---
name: dynamic-planner
description: >-
  Universal adaptive fractal planning protocol that scales planning depth dynamically by task
  breadth and technical complexity across any project. Locks requirements before heavy exploration,
  composes domain planning modules with just-in-time guardrails (supporting project-specific
  .agents/planner_profile.json extensions), prevents context drift, and enforces item-by-item
  verification before finishing. Activate for any non-trivial, multi-item, or high-complexity task.
---

# Universal Dynamic Fractal Planner (`dynamic-planner`)

Use this protocol in **any workspace** so **no requirement is ever lost before or during execution**, even on massive multi-item requests or single highly complex tasks.

> **Tool Paths (Global Plugin):**
> - Planner CLI: `~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py`
> - Verifier CLI: `~/.gemini/config/plugins/dynamic-fractal-planner/scripts/verify_universal.py`
> *(If a repo defines `.agents/planner_profile.json`, both tools automatically load project-specific domains, guardrails, and verifiers.)*

---

## Phase 0 — Lock Requirements & Triage BEFORE Heavy Exploration

**Golden Rule:** Do **not** read large source files before locking what the user asked for. Context bloat during discovery is the #1 cause of forgotten requirements.

1. **Run Triage** (or use the `PreInvocation` hook triage summary):
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py triage --prompt "<user request>"
   ```
2. **Choose Adaptive Planning Tier:**
   - **Tier 0 (`DIRECT`):** Trivial Q&A or single obvious 1-line fix -> Execute & verify directly.
   - **Tier 1 (`STANDARD`):** 1–2 focused edits in 1–2 files -> Run `plan_tool.py init --tier 1` and work step-by-step.
   - **Tier 2 (`META_MODULES` — Plan-of-Plans):** `>= 3` user requests OR `>= 2` subsystems -> Build a **Meta-Plan** locking every `REQ-01..REQ-N` first, then compose one **Planning Module** per domain/workstream.
   - **Tier 3 (`FRACTAL_DEEP` — Deep Recursive Sub-Planning):** **Even for a single task** if complexity/coupling is high (architecture, concurrency/streaming, parsers/codecs, multi-layer refactors, UI physics, infra) -> Compose specialized modules (`MOD-DISCOVERY -> MOD-CONTRACT -> MOD-IMPL-* -> MOD-HARDEN -> MOD-AUDIT`).

3. **Initialize the Plan Ledger Immediately:**
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py init \
     --tier 2 \
     --goal "<short goal>" \
     --req "REQ-01: <exact item 1 + acceptance>" \
     --req "REQ-02: <exact item 2 + acceptance>"
   ```
   *(For multi-module custom plans, pipe JSON via `--json -`; see [module-catalog.md](./references/module-catalog.md).)*

---

## Phase 1 — Context-Budgeted Discovery (Stay Lean!)

- **Never dump entire giant files just to plan.**
- **For `<= 2` subsystems:** Locate exact symbols with `grep -rn` / `git grep -n` first, then `read_file` in bounded `<= 150`-line slices.
- **For `>= 3` subsystems or unfamiliar repos:** Delegate discovery to a `research` subagent (returning ONLY file paths, line ranges, symbol signatures, and invariants) so your main planning window stays clean.
- Check local project instructions (`AGENTS.md`, `GEMINI.md`, README, `.agents/docs/`) for project invariants.

---

## Phase 2 — Dynamic Execution & Fractal Scaling

Work **one module at a time** in strict checkpoint loops:

1. **Activate module (prints Just-in-Time Guardrails):**
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py start MOD-CORE-LOGIC 1
   ```
2. **Edit small verifiable batches** (`<= 3` files per step).
3. **Fractal Escalation (Mid-Flight Sub-Planning):**
   - If a step turns out harder than expected (`>= 3` files, tricky edge case, or hidden coupling), split it dynamically:
     ```bash
     python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py expand-step \
       --module MOD-CORE-LOGIC --step 1 --substep "Isolate edge case" --substep "Update callers safely"
     ```
   - If a new subsystem emerges, attach a new module dynamically:
     ```bash
     python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py add-module \
       --id MOD-TESTS --title "Regression Tests" --domain tests-qa --req REQ-01
     ```
4. **Checkpoint immediately after verifying the slice:**
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py done MOD-CORE-LOGIC 1 --evidence "src/engine.py:L40-L85"
   ```
5. **Re-anchor on long tasks:** Run `plan_tool.py status` whenever unsure what remains.

*(If blocked or awaiting user input, run `plan_tool.py pause --state awaiting_user --reason "..."` so the `Stop` hook lets you pause cleanly.)*

---

## Phase 3 — Mandatory Verification & Item-by-Item Sign-Off

Before claiming completion or committing:

1. **Run the Verifier:**
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/verify_universal.py --build
   ```
   *(Omit `--build` if zero compiled/project build files changed.)*
2. **Verify EVERY `REQ-01..REQ-N` with observed evidence:**
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py req-verify REQ-01 --evidence "file:L10-L40" --check "tests/verifier passed"
   ```
3. **Final Completeness Audit:**
   ```bash
   python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py audit
   ```
   All modules must be `[x] done` and all requirements `[x] verified`.
