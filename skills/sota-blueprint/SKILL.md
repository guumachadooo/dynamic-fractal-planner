---
name: sota-blueprint
description: >-
  Universal 6-stage SOTA Blueprint Generator for ANY domain: sparse domain classification,
  STORM + Wardley live research, blind anti-anchoring Zwicky morphogenesis + TRIZ,
  cold-context 1-round Red-Team Pre-Mortem, VoI Wave-0 spikes, Rolling-Wave DSM specs,
  and deterministic 0-100 Popperian rubric verification (>=85/100).
---

# Universal SOTA Blueprint Playbook (`sota-blueprint` / Plugin v2.22.0)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`).
> CLI|`python3 -B ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py blueprint <cmd>`

<!--AUTO:START:sota-blueprint-domains:sha256=3a1a18c1d468-->
AUTO_SSOT|plugin=v2.22.5|blueprint_domains=ai-ml,biz-org,data-bi,dsp-hw,infra-ops,security,software,ux-product|formats=mini,compact,full|ssot=scripts/domains.json
<!--AUTO:END:sota-blueprint-domains-->

## Tiered Blueprint Formats (Right-Sized Ceremony & Token-Diet)
- **`mini` (`T1/2-way`, `<=35L`, `<=4 KB`, `0` subagents):** 4 sections (`Meta`, `Value & Non-Goals`, `Decision & Kill`, `Invariants & H1 Slices`), `>=1 REJECTED_BECAUSE|`, `>=1 KILL|`, `>=1 QAS|`. Zero subagent overhead.
- **`compact` (`T2`, `<=65L`, `<=10 KB`, `<=1 flash` scout + `1 flash` cold check):** 5 sections (adds condensed `Design & Trade-offs` + `Red-Team`), `>=2 REJECTED_BECAUSE|`, `>=1 KILL|`, `>=2 QAS|`.
- **`full` (`T3` or `1-way` door, `<=350L`, `<=24 KB`, legacy default when `format=` omitted):** full 6-stage pipeline w/ `>=2 pro [L3]` blind solvers + `1 pro [L3]` cold Red-Team + Wave-0 VoI spikes + Rolling-Wave DSM (`H2+ <=25%`).
- **T0 Tiny-Fix Exemption:** 1-line typos/renames/UI copy/label/color tweaks (`intent=tiny_fix`) skip blueprints automatically (`0` tokens wasted).

## Recursive Google-Only Subagent Rules
- **Adaptive Cubicle Pace (`pace=turbo|eco|normal`):** Default `turbo` runs parallel isolated subagent cubicles (strictly right-sized by blueprint format — `0` subagents for `mini`); `eco` uses parallel cubicles only when net-token-saving; `normal` uses standard adaptive routing. `SessionStart` performs zero-quota bidirectional `global<->embedded` auto-sync.
- Keep main chat as **Thin Coordinator** (`<=20L` symbolic EN replies from subagents; `!fork`, `!cmd-echo`).
- Enforce Google-only complexity routing: `L0:flash_lite` (grep/lookup) | `L1-2:flash` (STORM scouts, OSS curation, compact red-team) | `L3:pro` (`[L3]`, `full`/`1-way` blind morphogenesis solvers & cold-ctx Red-Team).
- Leaf scouts MAY spawn bounded nested `flash_lite`/`flash` leaf lookups (`max depth=2`), returning distilled findings + `url`/`path:line` only.

## 6-Stage SOTA Blueprint Pipeline

### Stage 0 — Reuse-Before-Create, Classify & Scaffold
1. **Reuse Check:** Run `plan_tool.py blueprint match --prompt "<goal>"` FIRST — if an `ACTIVE`/`PARTIAL` blueprint overlaps (`AMEND`), update it instead of spawning duplicate `BP_*.md` files.
2. **Classify:** Run `plan_tool.py blueprint classify --prompt "<goal>" --json` -> inspect `recommended_format` (`mini|compact|full`), `domain_vector`, `active_lenses` (`w>=0.3`), `door_type` (`1-way|2-way`), `risk_tier`, & `subagent_topology`.
3. **Scaffold:** `plan_tool.py blueprint init --title "<Title>" --slug <SLUG> --format <mini|compact|full> --prompt "<goal>"` -> `.agents/docs/blueprints/BP_<SLUG>.md` (zero repeated boilerplate).

### Stage 1 — Live SOTA & Strategic Positioning (`full`: parallel `flash` Scouts; `compact`: `<=1 flash`; `mini`: inline)
- **STORM Gap-Search:** Use `search_web` + `Context7`/`Google Developer Knowledge` MCP + codebase grep to falsify naive/stale defaults and map existing repo modules (`AUDIT_EXISTING`).
- **Wardley & Value Check:** Record `VALUE|` (measurable outcome), `WARDLEY|` (on `full`), and `NON_GOAL|` exclusions (`>=1` `mini`/`compact`, `>=2` `full`).

### Stage 2 — Blind Anti-Anchoring Morphogenesis (`full`/`1-way`: `>=2x pro [L3]`; `mini`/`compact`: inline)
- **Zero-Anchoring Rule (`full`):** Spawn `>=2` isolated `pro [L3]` solver subagents assigned to **inverted Zwicky poles** (`!see_peer`).
- **TRIZ & Synthesis:** Extract core architectural contradictions (`TRIZ_CONTRADICTION|`), merge strengths into `WINNER|`, and log substantive `REJECTED_BECAUSE|<A|B>|<quantified flaw >=12c & >=3 words>` for each discarded pole.

### Stage 3 — Cold-Context Red-Team & Kill Criteria (`full`: `1x pro [L3]`; `compact`: `1x flash`; `mini`: inline)
- **Cold Context (Anti-Groupthink):** Invoke reviewer with **0 drafting history** and **masked candidate origins** (never >1 MAD debate round).
- Record `PREMORTEM|T+6mo|<causal trace>` + `THREAT|` (`compact`/`full`) and lock `KILL|id|risk|numeric_kill_expr|obs_window|rollback_or_pivot` rules (`numeric_kill_expr` MUST contain a real comparator/numeric threshold).

### Stage 4 — Epistemic Wave-0 Spikes & Rolling-Wave DSM Graph
- On `1-way`/`full`, rank unknowns by `VoI = P(fail) * BlastRadius` (`SPIKE|S1|...`).
- Define `BUDGET|` (latency/mem/cost/degraded floor), `PLANES|` (`full`), & `QAS|` 6-tuples (`QAS|id|stim|env|resp|numeric_bound+unit|executable_oracle_cmd` — dummy oracles like `echo ok`/`pass`/`manual` are rejected).
- **Anti-Waterfall DSM Split & Dual-Mode Wave DAG:** Detail `### H1 Atomic Slices` (`SLICE|`) plus companion `SLICE_DAG|<id>|deps=<ids|none>|writes=<globs|RO>|reads=<globs|none>|locks=<lks|none>|tier=<L0..L3>` + optional `DAG_POLICY|default_mode=seq|max_width_termux=2|max_width_local=4|barrier=epoch` (NEVER bloat `SLICE|` lines; keep `### H2+ Outcome Stubs` as 1L outcome+gate stubs ONLY `<=25%`).
- **Dual-Mode Conflict-Free Execution (`blueprint schedule`):** Run `plan_tool.py blueprint schedule <bp> --mode <seq|parallel> [--emit-workflow <artifact>/scratch/wf_<bp>.py|--emit-prompts]` (`seq` = 1 wave/epoch; `parallel` = Coffman-Graham min-epoch packing respecting authoring locks vs barrier-deferred `proot_build`/`ledgers`, Termux `max_width<=2`, `[LEASE:...]` D1/D2 auto-bind, dirty-safe Epoch Barrier rollback, & `O(1)` `<=12L`/epoch root ctx).

### Stage 5 — Deterministic Anti-Gaming Popperian Rubric Gate (`score >= 85/100`)
- Run `plan_tool.py blueprint lint .agents/docs/blueprints/BP_<SLUG>.md --strict`.
- Enforces: `>=85/100`, `0` `<FILL:...>` placeholders, code-aware pipe parsing (honoring `||` & backticks), clause-local `±90c` weasel-word ban (ignoring `--flags` & backticked symbols), substantive `REJECTED_BECAUSE`, numeric/comparator `KILL` triggers, non-dummy `QAS` oracles, `>600c` line-density warning, and format-specific line/KB caps.

### Stage 6 — Lifecycle, Lossless Compaction, Plan Hygiene & Central Hub Sync
- **Handoff & Transition:** `plan_tool.py blueprint handoff <bp>` -> `plan_tool.py init`; update lifecycle atomically via `plan_tool.py blueprint transition <bp> --status <ACTIVE|PARTIAL|DONE|DEFERRED|SUPERSEDED> [--note "..."] [--superseded-by BP_ID]` (auto-syncs `PLANS_HUB.md`).
- **O(1) File-Budget Archival (`blueprint compact`):** Compact completed `DONE` blueprints losslessly via `plan_tool.py blueprint compact --apply [--keep BP_ID]` -> writes & SHA-256-verifies `.agents/docs/archive/blueprints_done.tar.gz` BEFORE unlinking `.md` files, maintains `.agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md`, and syncs `PLANS_HUB.md`.
- **Pre-Resumption Plan Hygiene Gate (`[PLAN_HYGIENE_GATE]`):** Before resuming an older plan/roadmap after intervening work, run `plan_tool.py blueprint hygiene --strict` (single-pass `1x` lint/file), inspect `.agents/docs/PLANS_HUB.md`, resolve every `CONFLICTS_WITH|` into `CONFLICT_RESOLVED|`, and run `plan_tool.py blueprint hub --sync`.
- **Blueprint-First Default Policy (`[BLUEPRINT_FIRST|SOTA]`):** When `blueprint.require_by_default:true`, run `blueprint match` (amend active) or author a right-sized `mini|compact|full` blueprint (`>=85/100 --strict`) BEFORE edits on `tier >= default_min_tier`, skipping ONLY on explicit user opt-out (`"sem blueprint"`, `"skip blueprint"`, `"economizar token"`), `ECO_MODE`, or `T0` tiny-fix.

## Banned Anti-Patterns
1. Multi-round homogeneous LLM debate (>1 round amplifies shared bias).
2. Same-context self-review (token-path anchoring & sycophancy).
3. Premature Waterfall (`H2+` deep implementation prose/APIs before Wave-0 spikes).
4. Over-ceremony on `T1/2-way` work (use `format=mini` w/ `0` subagents, never `full` w/ `4` subagents).
5. Unfalsifiable adjectives, 1-word rejections, non-numeric kill switches, dummy `echo ok` oracles, or unbounded `DONE` file sprawl.
