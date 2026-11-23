# Plugin Build & Verification Lessons (`BUILD_LESSONS.md`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:sym`).
> INVARIANT|Timeless, version-agnostic principles ONLY (`!hardcode ephemeral OS/SDK/Action/pkg versions or dates`). Auto-audited by `scripts/error_learner.py --audit-lessons` && `verify_universal.py`.

---

## Core Plugin Lessons

### `BL-001` | Embedded Plugin Root Guard (`is_real_project_root`)
- `Category`: `plugin-hooks`
- `Codes`: `ROOT_GUARD`
- `Keywords`: `is_real_project_root, plugin_root, embedded`
- `RootCause`: Excluding only `~/.gemini` fails when plugin lives embedded inside a host repo (`.agents/plugins/dynamic-fractal-planner`).
- `TimelessRule`: Always exclude `resolve_plugin_root().resolve()` (and its children) inside `planner_state.is_real_project_root()`.
- `DynamicLookup`: Run `tests/run_plugin_tests.py --fast` to validate root-guard behavior across embedded and global installs.
- `AutoGuard`: `tests/run_plugin_tests.py:test_is_real_project_root_guards`

### `BL-002` | Stdlib-Only Portable Crypto & HTTP
- `Category`: `plugin-scripts`
- `Codes`: `IMPORT_ERR`
- `Keywords`: `stdlib, cryptography, requests, dependency`
- `RootCause`: Third-party Python packages break zero-setup execution across Termux PRoot, CI runners, and sandboxed AI hosts.
- `TimelessRule`: Keep all plugin scripts 100% Python stdlib (`urllib.request`, `hashlib`, `hmac`, `secrets`, `struct`, `json`); use Encrypt-then-MAC (`SHA256-CTR + HMAC-SHA256`) when authenticated encryption is needed without external C-extension libraries.
- `DynamicLookup`: Audit imports in `scripts/*.py` && verify via `python3 -B tests/run_plugin_tests.py --fast`.
- `AutoGuard`: `tests/run_plugin_tests.py`

### `BL-003` | Dual-Ledger Parity & Autonomous Self-Evolution Scope
- `Category`: `plugin-memory`
- `Codes`: `SYNC_PARITY`
- `Keywords`: `external_ai_sync, self_evolve, dual_scope`
- `RootCause`: Universal workflow innovations trapped inside a single host project duplicate effort and drift across repos.
- `TimelessRule`: Classify every workflow/tooling/diagnostic improvement via `self_evolve.py classify`; implement reusable cores in plugin w/ zero host hardcoding && update BOTH host and plugin `EXTERNAL_AI_SYNC.md`.
- `DynamicLookup`: Run `verify_universal.py --git-stat` to check plugin sync parity && regression suite.
- `AutoGuard`: `verify_universal.py:check_plugin_sync_parity`

---

## Auto-Learned Lessons

<!-- AUTO-LEARNED-LESSONS:START -->

(No new uncatalogued structural lessons pending.)

<!-- AUTO-LEARNED-LESSONS:END -->
