# Codex Session Viewer

Local viewer for Codex session history stored under `~/.codex`.

## Runtime

Use Docker Compose. This project is packaged like `claude-session-viewer` and does not require a local Python, venv, or conda environment for normal use.

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8890/
```

## What It Reads

- `/home/ahmadnurfais/.codex` is mounted read-write as `/codex` so confirmed deletes can update Codex state.
- `state_5.sqlite` drives the thread/project index.
- Rollout JSONL files under `sessions/` and `archived_sessions/` drive transcript display.

## Delete Behavior

The viewer can delete live Codex sessions after a confirmation prompt. Deleting a live session removes its thread row from `state_5.sqlite`, removes the rollout JSONL transcript, removes matching `session_index.jsonl` and `history.jsonl` rows, and removes shell snapshots for that thread id.

Backup sessions can also be deleted from the selected backup copy. This does not affect live Codex state.

## Rename Behavior

Live Codex sessions can be renamed from the session header using the built-in rename modal. Codex can regenerate `threads.title` when a session continues, so the viewer also stores a durable display-title override in `codex-session-viewer-overrides.json`. Rename still updates `threads.title` in `state_5.sqlite` and the matching `thread_name` entry in `session_index.jsonl`, but the viewer-owned override is what keeps the title stable in this app. It does not rewrite the transcript JSONL or change session recency ordering.

Backup sessions keep the title captured at backup time.

## Backups

Backups are written to:

```text
/mnt/linux_data/backup/codex-backups
```

The container runs with `TZ=Asia/Jakarta`, so backup date folders follow local time.

In the Backups tab, choose a backup date first, then choose a project from that backup to see its sessions.
