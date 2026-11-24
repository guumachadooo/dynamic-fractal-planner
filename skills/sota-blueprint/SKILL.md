---
name: sota-blueprint
description: >-
  Universal 6-stage SOTA Blueprint Generator for ANY domain: sparse domain classification,
  STORM + Wardley live research, blind anti-anchoring Zwicky morphogenesis + TRIZ,
  cold-context 1-round Red-Team Pre-Mortem, VoI Wave-0 spikes, Rolling-Wave DSM specs,
  and deterministic 0-100 Popperian rubric verification (>=85/100).
---

# Universal SOTA Blueprint Playbook (`sota-blueprint`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`).
> CLI|`python3 -B ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py blueprint <cmd>`

## Recursive Google-Only Subagent Rules
- Keep main chat as **Thin Coordinator** (`<=20L` symbolic EN replies from subagents; `!fork`, `!cmd-echo`).
- Enforce Google-only complexity routing: `L0:flash_lite` (grep/lookup) | `L1-2:flash` (STORM scouts, OSS curation, file discovery) | `L3:pro` (`[L3]`, blind morphogenesis solvers & cold-ctx Red-Team).
- Leaf scouts MAY spawn bounded nested `flash_lite`/`flash` leaf lookups (`max depth=2`), returning distilled findings + `url`/`path:line` only.

## 6-Stage SOTA Blueprint Pipeline

### Stage 0 — Classify & Scaffold
1. Run `plan_tool.py blueprint classify --prompt "<goal>" --json` -> inspect `domain_vector`, `active_lenses` (`w>=0.3`), `door_type` (`1-way|2-way`), `risk_tier`, & `morph_axes`.
2. Scaffold target file: `plan_tool.py blueprint init --title "<Title>" --slug <SLUG> --prompt "<goal>"` -> `.agents/docs/blueprints/BP_<SLUG>.md`.

### Stage 1 — Live SOTA & Strategic Positioning (Parallel `flash` Scouts)
- **STORM Gap-Search:** Spawn parallel `flash` scouts using `search_web` + `Context7`/`Google Developer Knowledge` MCP + codebase grep to falsify naive/stale defaults and map existing repo modules (`AUDIT_EXISTING`).
- **Wardley & Value Check:** Record `VALUE|` (measurable outcome), `WARDLEY|` (build custom differentiator vs reuse battle-tested OSS), and `>=2 NON_GOAL|` exclusions.

### Stage 2 — Blind Anti-Anchoring Morphogenesis (`>=2x pro [L3]`, Parallel & Isolated)
- **Zero-Anchoring Rule:** Spawn `>=2` isolated `pro [L3]` solver subagents assigned to **inverted Zwicky poles** (e.g., *Analytical Physics/DSP vs Invariant Neural*; *Stateless Edge vs Centralized State*). Solvers **MUST NOT** see each other's drafts (`!see_peer`).
- **TRIZ & Synthesis:** Extract core architectural contradictions (`TRIZ_CONTRADICTION|`), merge strengths into `WINNER|`, and log explicit `REJECTED_BECAUSE|<A|B>|<quantified flaw>` for each discarded pole (`ORI >= 2`).

### Stage 3 — Cold-Context Red-Team & Kill Criteria (`1x pro [L3]`, 1-Round Cap)
- **Cold Context (Anti-Groupthink):** Invoke `1x pro [L3]` reviewer with **0 drafting history** and **masked candidate origins** (never >1 MAD debate round).
- Reviewer executes BOTH:
  1. `PREMORTEM|T+6mo|<causal trace of P0 catastrophic failure>`
  2. `THREAT|<STRIDE / physics loop-gain / OOD / cost-asymmetry vector + defense>`
- Lock `>=2 KILL|id|risk|numeric_kill_expr|obs_window|rollback_or_pivot` circuits pre-code.

### Stage 4 — Epistemic Wave-0 Spikes & Rolling-Wave DSM Graph
- Rank `1-way` unknowns by `VoI = P(fail) * BlastRadius`; record `SPIKE|S1|...` verification gates BEFORE freezing `H1`.
- Define `BUDGET|` (latency/WCET, mem/VRAM, cost, degraded floor), `PLANES|` (offline | RT 0-alloc | async control), & `QAS|` 6-tuples (`QAS|id|stim|env|resp|numeric_bound+unit|executable_oracle_cmd`).
- **Anti-Waterfall DSM Split:** Detail `### H1 Atomic Slices` (`SLICE|`); keep `### H2+ Outcome Stubs` as **1-line outcome+gate stubs ONLY** (`<=25%` of lines, **ZERO** deep filenames/class signatures).

### Stage 5 — Deterministic Popperian Rubric Gate (`score >= 85/100`)
- Run `plan_tool.py blueprint lint .agents/docs/blueprints/BP_<SLUG>.md --strict`.
- Must achieve `>=85/100` with `0` `<FILL:...>` placeholders, `0` unqualified weasel words (`fast/robust/seamless/etc.` banned on normative lines unless paired with numeric bound+unit), `>=2 REJECTED_BECAUSE|`, `>=2 KILL|`, valid `QAS|`, and clean `H2+` stub ratio.

### Stage 6 — Execution Handoff & Living Evolution
- Hand off to execution: `plan_tool.py blueprint handoff .agents/docs/blueprints/BP_<SLUG>.md` -> feeds `plan_tool.py init`.
- Bind `FITNESS|` probes to CI/tests. On `kill_trigger` or probe fail: mark `status=SUPERSEDED` and delta-reenter Stages 2–3 (`!full rewrite`).

## Banned Anti-Patterns
1. Multi-round homogeneous LLM debate (>1 round amplifies shared bias).
2. Same-context self-review (token-path anchoring & sycophancy).
3. Premature Waterfall (`H2+` deep implementation prose/APIs before Wave-0 spikes).
4. Global multi-domain checklist bloat (inject active lenses `w>=0.3` ONLY).
5. Unfalsifiable adjectives w/o numeric bounds & runnable oracle commands.
