---
name: project-memory
description: >-
  Universal project memory, anti-rot documentation, shelved ideas archive, and parallel /btw extra
  request queue protocol. Use when initializing agent structure in any repository, recording bugs or
  milestones in PROJECT_HISTORY.md, archiving deferred prototypes in SAVED_IDEAS.md, or resolving
  queued requests in EXTRA_REQUESTS.md.
---

# Universal Project Memory & Anti-Rot Protocol (`project-memory`)

Use this system across any project so knowledge compounds cleanly across conversations without stale or contradictory docs.

## 1. Bootstrapping Any New or Existing Repository
Run once inside any project root to scaffold `.agents/docs/` and an auto-detected `.agents/planner_profile.json`:
```bash
python3 ~/.gemini/config/plugins/dynamic-fractal-planner/scripts/plan_tool.py bootstrap
```
*(Never overwrites existing files unless `--force` is passed.)*

## 2. Anti-Rot Memory Rules
- **No Static "Current State" Headers:** Live branch, dirty files, versions, and watched artifacts are injected dynamically by `SessionStart` (`planner_session_start.py`). Never hardcode `"Current State"` or `"Current Version"` inside Markdown docs.
- **Reverse-Chronological History (`.agents/docs/PROJECT_HISTORY.md`):**
  - Prepend a concise 1–4 line summary at the **top** of `## 3. Version History (Newest First)` after major milestones, architecture changes, or non-obvious bug fixes.
  - Record reusable engineering pitfalls under `## 2. Technical Lessons & Known Pitfalls`.
- **Archived Ideas (`.agents/docs/SAVED_IDEAS.md`):** Whenever a promising idea, shader, algorithm, or prototype is deferred/replaced, save the concept and reference snippet there so it is never lost.
- **Parallel `/btw` Queue (`.agents/docs/EXTRA_REQUESTS.md`):**
  - Automatically surfaced at `SessionStart` and enforced before `git commit`, `git push`, and `Stop`.
  - When items exist under `## Pending Extra Requests`, stop before committing/shipping, plan and implement them, move them to `## Completed Requests`, restore `(No pending requests.)`, and then proceed.
- **Automatic Memory Feedback Reminder:** `planner_guard_command.py` gently reminds once per session before committing `>= 2` modified code files if none of the project's memory docs were updated recently.
