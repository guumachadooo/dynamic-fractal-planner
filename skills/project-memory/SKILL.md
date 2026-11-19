---
name: project-memory
description: >-
  Universal project memory, anti-rot docs, HANDOFF.md, archive weight monitoring, and /btw EXTRA_REQUESTS queue.
---

# Project Memory & Anti-Rot (`project-memory` v3)

- **Bootstrap:** `plan_tool.py bootstrap` creates `PROJECT_HISTORY.md`, `SAVED_IDEAS.md`, `EXTRA_REQUESTS.md`, and `planner_profile.json` (`schema_version: 3`, auto-detecting TS/Next/Vite, .NET, Gradle/Kotlin, Flutter/Dart, Python, Rust, Go) without overwriting existing files.
- **No static `"Current State"` headers:** `SessionStart` injects live git/versions/dirty/`HANDOFF.md`.
- **`PROJECT_HISTORY.md`:** Prepend 1-4L milestones at top of Section 3; keep Section 2 pitfalls updated. If `>150L`, archive older entries to `.agents/docs/archive/`.
- **`SAVED_IDEAS.md`:** Archive deferred visuals/shaders/algorithms + snippets.
- **`EXTRA_REQUESTS.md`:** Checked at `SessionStart`, `git commit`/`push`, and `Stop`. Implement pending items -> move to `## Completed Requests` -> restore `(No pending requests.)`. If completed >15, archive to `.agents/docs/archive/`.
- **`HANDOFF.md`:** `plan_tool.py handoff --summary "..." --next "..."` writes `<=20L` context bridge for fresh low-quota chats (`--clear` removes).
