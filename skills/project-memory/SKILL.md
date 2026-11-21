---
name: project-memory
description: >-
  Universal project memory, anti-rot docs, HANDOFF.md, archive weight monitoring, and /btw EXTRA_REQUESTS queue.
---

# Project Memory & Anti-Rot (`project-memory` v3)

- **Style (mandatory):** ALL entries in `PROJECT_HISTORY.md`, `SAVED_IDEAS.md`, `EXTRA_REQUESTS.md`, `HANDOFF.md` -> lossless symbolic telegraphic EN (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`, verbatim `path:line`, `symbol()`, invariants) regardless of chat language.
- **Bootstrap:** `plan_tool.py bootstrap` creates missing `PROJECT_HISTORY.md`, `SAVED_IDEAS.md`, `EXTRA_REQUESTS.md`, `planner_profile.json` (`schema_version:3`, auto-detects TS/Next/Vite, .NET, Gradle/Kotlin, Flutter/Dart, Python, Rust, Go); `!overwrite` existing.
- **Auto-Upgrade:** `SessionStart`/`PreInvocation` auto-migrates legacy `planner_profile.json` (`v1/v2->v3`) && untouched legacy templates (0 tokens); populated legacy docs (incl. root `AGENTS.md`/`GEMINI.md` by default; `docs_migration.include_root_agent_docs:false` to opt out) trigger `[DOCS_AUTO_MIGRATE]` -> losslessly convert to symbolic telegraphic EN -> `plan_tool.py docs-migrated` stamps `docs_format_version:1`.
- **`!static("Current State")`:** `SessionStart` injects live git/versions/dirty/`HANDOFF.md`.
- **`PROJECT_HISTORY.md`:** Sec 2 = `PITFALL|<path:sym> -> <constraint/cause> -> <safe fix>`; Sec 3 = prepend 1-3L newest-first (`YYYY-MM-DD|<tag>|<change> -> <effect> | verified:<cmd>`); `>150L` -> archive older entries to `.agents/docs/archive/`.
- **`SAVED_IDEAS.md`:** deferred visuals/shaders/algos + snippets (`IDEA|<name> -> <why deferred> | <ref/snippet>`).
- **`EXTRA_REQUESTS.md`:** checked at `SessionStart`, `git commit`/`push`, `Stop`; `/btw` or main-agent queue intent ("manda ele...", "quando acabar...", "tell the main agent...") -> auto-append `- REQ|<scope> -> <goal>` (w/o asking) && reply `<=2L`; resolve -> move to `## Completed Requests` -> restore `(No pending requests.)`; completed `>15` -> archive to `.agents/docs/archive/`.
- **`HANDOFF.md`:** `plan_tool.py handoff [--roadmap <path>] [--wave <id>] [--archive <path>] --summary "..." --next "..."` -> `<=20L` bridge for fresh low-quota chats (`--clear` removes); `SessionStart` injects compact `WAVE_BRIDGE|` && `planner_guard_read.py` restricts archive docs (`.agents/docs/archive/`, `archive_docs`, `ARCHIVE|`) >150L to explicit `<=120L` slices; pass args in symbolic telegraphic EN.
