# Codex Session Viewer Plan

## Findings

- Codex stores user-level state under `CODEX_HOME`, which defaults to `~/.codex`.
- The useful history index is `state_5.sqlite`, especially the `threads` table.
- The `threads.rollout_path` column points to the full transcript JSONL file.
- Transcript files live under `sessions/YYYY/MM/DD/` for active threads and `archived_sessions/` for archived threads.
- `session_index.jsonl` and `history.jsonl` are useful secondary files, but they do not contain enough metadata or transcript detail for the viewer.
- Additional SQLite files exist for adjacent state:
  - `goals_1.sqlite`: thread goal state.
  - `memories_1.sqlite`: memory extraction jobs and outputs.
  - `logs_2.sqlite`: local log records.
- The app must translate rollout paths when running in Docker because SQLite stores host paths like `/home/ahmadnurfais/.codex/...`, while the container sees the same files at `/codex/...`.

## First Version Scope

- FastAPI backend plus static vanilla JS UI, matching the operational style of `../claude-session-viewer`.
- Browsing of Codex state plus explicit confirmed deletion of selected sessions.
- Project grouping by `threads.cwd`.
- Thread list driven by `state_5.sqlite`.
- Transcript rendering driven by rollout JSONL files.
- Search across thread id, title, preview, first user message, and cwd.
- Active/archived/all filter.
- Internal-message toggle for developer/system/session records.
- Raw JSONL link for exact transcript inspection.
- Copyable `codex resume <thread-id>` command.
- Live session rename with a built-in modal, backed by `codex-session-viewer-overrides.json` because Codex can regenerate `threads.title`.
- Session viewer controls for manual refresh, jump to top, and jump to bottom.
- Backup browsing by date, then project, then session.
- WebSocket refresh when Codex state or session files change.
- Startup, scheduled, and manual backups of:
  - `state_5.sqlite`
  - `goals_1.sqlite`
  - `memories_1.sqlite`
  - `logs_2.sqlite`
  - `session_index.jsonl`
  - `history.jsonl`
