# Ecosystem Coherence Hub (`COHERENCE_HUB.md`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`). Auto-synced by `plan_tool.py coherence sync`.

## 1. SSOT_MAP (Authoritative Source -> Consumers)
| Domain | Authoritative SSOT | Consumers |
| :--- | :--- | :--- |
| Domains & Checklists | `.agents/plugins/dynamic-fractal-planner/scripts/domains.json` | `complexity_triage.py`, `plan_tool.py`, `blueprint_engine.py`, `planner_pre_invocation.py` |
| Verify Registry | `verify_universal.py:@verify_check` (`VERIFY_CHECKS`) | `verify_universal.py:run_static_checks`, `planner_stop.py`, CI |
| Multi-Host Matrix | `.agents/host_matrix.json` (`strict_coverage=true`) | `host_context.py`, `verify_universal.py`, `plan_tool.py host-*` |
| Triad Federation | `.agents/federation.json` | `repo_federation.py`, `self_evolve.py`, `verify_universal.py`, `coherence_engine.py` |
| Profile Schema | `scripts/schema_migrations.py:migrate_profile` | `planner_state.py`, `blueprint_hub.py`, `complexity_triage.py` |
| Living Docs Parity | `<!--AUTO:START:<id>:sha256=...-->` | `skills/*/SKILL.md`, `.agents/docs/EXTERNAL_AI_ONBOARDING.md` |

## 2. DEPRECATED_REGISTRY (`WARN -> GRACE -> TOMBSTONE`)
<!-- Format: DEP|<id>|<stage>|<path_or_surface>|<replacement>|owner=<owner>|expires_ver=<semver> -->
DEP|none|TOMBSTONE|cli:none|none|owner=dfp|expires_ver=0.0.0

## 3. COVERAGE_MATRIX
COV|plugin=v2.23.0|triage_domains=8|bp_domains=8|verify_checks=21|hooks=9(unwired=0)
COV|host_matrix_strict=true|host_entries=6|auto_blocks=2(drifted=0)|deprecated=1

## 4. CROSS_REPO_PARITY
PARITY|SKIP|local_only

## 5. CONSTITUTION_AND_META_GOV
CONSTITUTION|T0=immutable_canary|T1=ssot_parity|T2=observe_telemetry|anchors=0(drifted=0)|plane2_cap=4/wk
