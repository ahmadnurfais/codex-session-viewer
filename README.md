# Codex Session Viewer

Codex Session Viewer is a local, Docker-run web app for reviewing, managing, and backing up Codex session history stored under `~/.codex`.

## Why This Exists

Codex keeps useful local history, but the data is not easy to review directly. The session index lives in SQLite, transcripts live in JSONL rollout files, and related data such as snapshots, tool calls, outputs, project paths, archive state, and backup-worthy metadata are spread across the Codex home directory. You can inspect that through the CLI or by opening `~/.codex` manually, but it quickly becomes awkward because you have to connect those pieces yourself.

This project was built to make that history easier to use:

- Browse Codex sessions by project instead of raw files.
- Read transcripts with user, assistant, tool call, tool output, reasoning, and event records rendered together.
- Open raw JSONL when exact inspection is needed.
- Rename or delete sessions through a UI with explicit confirmation.
- Browse backup copies without touching live Codex state.
- Run scheduled, startup, and manual backups so losing or deleting `~/.codex` is not the end of the story.

The main goal is preservation. Codex sessions are a useful personal log of what was asked, what was delegated to AI, what tools ran, and what decisions were made. This viewer exists so that history can be reviewed and backed up instead of being treated as disposable local state.

## Runtime

Use Docker Compose. The app runs inside a container and does not require a local Python, venv, or conda environment for normal use.

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8890/
```

## Platform Notes

This project was built and used on Linux. The default `docker-compose.yaml` assumes a Linux host and Linux-style bind mounts.

Windows and macOS can still run it through Docker, but you may need to adjust the mounted paths, file-sharing settings, timezone, and backup directory before starting the service.

## What It Reads

- `~/.codex` is mounted read-write as `/codex` so confirmed deletes can update Codex state.
- `state_5.sqlite` drives the thread/project index.
- Rollout JSONL files under `sessions/` and `archived_sessions/` drive transcript display.

All important paths are configurable through Docker Compose environment variables and volume mounts:

- `CODEX_HOME` controls where the container reads Codex files.
- `HOST_CODEX_HOME` maps host paths stored inside Codex SQLite rows back to the container mount.
- `CODEX_STATE_DB` points to the Codex state database.
- `BACKUP_DIR` controls where backups are written inside the container.
- `BACKUP_RETENTION_DAYS` controls automatic backup pruning.

## Delete Behavior

The viewer can delete live Codex sessions after a confirmation prompt. Deleting a live session removes its thread row from `state_5.sqlite`, removes the rollout JSONL transcript, removes matching `session_index.jsonl` and `history.jsonl` rows, and removes shell snapshots for that thread id.

Backup sessions can also be deleted from the selected backup copy. This does not affect live Codex state.

## Rename Behavior

Live Codex sessions can be renamed from the session header using the built-in rename modal. Codex can regenerate `threads.title` when a session continues, so the viewer also stores a durable display-title override in `codex-session-viewer-overrides.json`. Rename still updates `threads.title` in `state_5.sqlite` and the matching `thread_name` entry in `session_index.jsonl`, but the viewer-owned override is what keeps the title stable in this app. It does not rewrite the transcript JSONL or change session recency ordering.

Backup sessions keep the title captured at backup time.

## Backups

Backups are written to the configured backup directory. In the included Linux-oriented Compose file, the host backup directory is mounted into the container as:

```text
/backups
```

The container runs with `TZ=Asia/Jakarta`, so backup date folders follow local time.

In the Backups tab, choose a backup date first, then choose a project from that backup to see its sessions.
