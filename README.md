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
