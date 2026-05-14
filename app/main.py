import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(title="Codex Session Viewer")


def _default_codex_home() -> Path:
    env_home = os.getenv("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    if Path("/codex").exists():
        return Path("/codex")
    return Path.home() / ".codex"


CODEX_HOME = _default_codex_home()
HOST_CODEX_HOME = Path(os.getenv("HOST_CODEX_HOME", str(CODEX_HOME))).expanduser()
SQLITE_HOME = Path(os.getenv("CODEX_SQLITE_HOME", str(CODEX_HOME))).expanduser()
STATE_DB = Path(os.getenv("CODEX_STATE_DB", str(SQLITE_HOME / "state_5.sqlite"))).expanduser()
BACKUPS_BASE = Path(
    os.getenv(
        "BACKUP_DIR",
        "/backups" if Path("/backups").exists() else str(Path.cwd() / "backups"),
    )
).expanduser()
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))

STATIC_DIR = Path(__file__).parent / "static"
SESSION_DIR = CODEX_HOME / "sessions"
ARCHIVED_SESSION_DIR = CODEX_HOME / "archived_sessions"
SQLITE_DB_NAMES = ("state_5.sqlite", "goals_1.sqlite", "memories_1.sqlite", "logs_2.sqlite")
TITLE_OVERRIDES_NAME = "codex-session-viewer-overrides.json"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
INTERNAL_USER_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<apps_instructions>",
    "<skills_instructions>",
    "<plugins_instructions>",
)

THREAD_COLUMNS = """
    id,
    rollout_path,
    created_at,
    updated_at,
    source,
    model_provider,
    cwd,
    title,
    sandbox_policy,
    approval_mode,
    tokens_used,
    has_user_event,
    archived,
    archived_at,
    git_sha,
    git_branch,
    git_origin_url,
    cli_version,
    first_user_message,
    agent_nickname,
    agent_role,
    memory_mode,
    model,
    reasoning_effort,
    agent_path,
    created_at_ms,
    updated_at_ms,
    thread_source,
    preview,
    recency_at,
    recency_at_ms
"""


_connections: List[WebSocket] = []
_loop: Optional[asyncio.AbstractEventLoop] = None
_pending_broadcast: Optional[asyncio.TimerHandle] = None
_backup_running = False
_observers: List[Observer] = []


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _sec_to_iso(seconds: Optional[int]) -> Optional[str]:
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _sqlite_uri(path: Path, mode: str = "ro") -> str:
    return f"file:{path.resolve().as_posix()}?mode={mode}"


def _connect_db(path: Path, read_only: bool = True) -> sqlite3.Connection:
    if not path.exists():
        raise HTTPException(500, f"Codex state DB not found: {path}")
    try:
        if read_only:
            conn = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5)
        else:
            conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        mode = "read-only" if read_only else "read-write"
        raise HTTPException(500, f"Unable to open Codex state DB {mode}: {exc}") from exc


def _connect_state() -> sqlite3.Connection:
    return _connect_db(STATE_DB, read_only=True)


def _map_codex_path(path_text: str) -> Path:
    """Map host CODEX_HOME paths stored in SQLite to this process' CODEX_HOME."""
    path = Path(path_text).expanduser()
    if path.exists():
        return path
    if path.is_absolute():
        try:
            return CODEX_HOME / path.relative_to(HOST_CODEX_HOME)
        except ValueError:
            return path
    return CODEX_HOME / path


def _map_path_under_base(path_text: str, base: Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        for home in (HOST_CODEX_HOME, CODEX_HOME):
            try:
                return base / path.relative_to(home)
            except ValueError:
                pass
        return path
    return base / path


def _ensure_allowed_path(path: Path, bases: List[Path]) -> Path:
    resolved = path.resolve()
    for base in bases:
        try:
            resolved.relative_to(base.resolve())
            return resolved
        except ValueError:
            pass
    raise HTTPException(403, "Access denied")


def _load_title_overrides(base: Path) -> Dict[str, str]:
    path = base / TITLE_OVERRIDES_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    titles = data.get("titles", {}) if isinstance(data, dict) else {}
    overrides: Dict[str, str] = {}
    if not isinstance(titles, dict):
        return overrides

    for thread_id, value in titles.items():
        title = value.get("title") if isinstance(value, dict) else value
        if isinstance(thread_id, str) and isinstance(title, str) and title.strip():
            overrides[thread_id] = title.strip()
    return overrides


def _write_title_override(base: Path, thread_id: str, title: str) -> None:
    base = _ensure_allowed_path(base, [CODEX_HOME, BACKUPS_BASE])
    path = base / TITLE_OVERRIDES_NAME
    data: Dict[str, Any] = {"titles": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {"titles": {}}

    titles = data.setdefault("titles", {})
    if not isinstance(titles, dict):
        titles = {}
        data["titles"] = titles
    titles[thread_id] = {
        "title": title,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _delete_title_override(base: Path, thread_id: str) -> bool:
    path = base / TITLE_OVERRIDES_NAME
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    titles = data.get("titles", {}) if isinstance(data, dict) else {}
    if not isinstance(titles, dict) or thread_id not in titles:
        return False
    titles.pop(thread_id, None)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def _row_to_thread(
    row: sqlite3.Row,
    base: Optional[Path] = None,
    title_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    created_ms = row["created_at_ms"] or (row["created_at"] * 1000 if row["created_at"] else None)
    updated_ms = row["updated_at_ms"] or (row["updated_at"] * 1000 if row["updated_at"] else None)
    recency_ms = row["recency_at_ms"] or (row["recency_at"] * 1000 if row["recency_at"] else updated_ms)
    local_rollout_path = (
        _map_path_under_base(row["rollout_path"], base)
        if base is not None
        else _map_codex_path(row["rollout_path"])
    )
    file_exists = local_rollout_path.exists()
    if title_overrides is None:
        title_overrides = _load_title_overrides(base if base is not None else CODEX_HOME)
    title_override = title_overrides.get(row["id"])
    generated_title = row["title"] or row["preview"] or row["first_user_message"] or row["id"]

    size_bytes = None
    line_count = None
    if file_exists:
        try:
            size_bytes = local_rollout_path.stat().st_size
            with local_rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
                line_count = sum(1 for line in handle if line.strip())
        except OSError:
            pass

    return {
        "id": row["id"],
        "title": title_override or generated_title,
        "custom_title": bool(title_override),
        "generated_title": generated_title,
        "preview": row["preview"],
        "first_user_message": row["first_user_message"],
        "cwd": row["cwd"],
        "source": row["source"],
        "thread_source": row["thread_source"],
        "model_provider": row["model_provider"],
        "model": row["model"],
        "reasoning_effort": row["reasoning_effort"],
        "sandbox_policy": row["sandbox_policy"],
        "approval_mode": row["approval_mode"],
        "memory_mode": row["memory_mode"],
        "tokens_used": row["tokens_used"],
        "has_user_event": bool(row["has_user_event"]),
        "archived": bool(row["archived"]),
        "archived_at": _sec_to_iso(row["archived_at"]),
        "created_at": _ms_to_iso(created_ms),
        "updated_at": _ms_to_iso(updated_ms),
        "recency_at": _ms_to_iso(recency_ms),
        "created_at_ms": created_ms,
        "updated_at_ms": updated_ms,
        "recency_at_ms": recency_ms,
        "git_sha": row["git_sha"],
        "git_branch": row["git_branch"],
        "git_origin_url": row["git_origin_url"],
        "cli_version": row["cli_version"],
        "agent_nickname": row["agent_nickname"],
        "agent_role": row["agent_role"],
        "agent_path": row["agent_path"],
        "rollout_path": row["rollout_path"],
        "local_rollout_path": str(local_rollout_path),
        "rollout_exists": file_exists,
        "size_bytes": size_bytes,
        "line_count": line_count,
        "resume_command": f"codex resume {row['id']}",
        "codex_uri": f"codex://threads/{row['id']}",
        "backup": base is not None,
    }


def _get_thread_row(thread_id: str) -> sqlite3.Row:
    with _connect_state() as conn:
        row = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Thread not found")
    return row


def _get_thread_row_from_db(db_path: Path, thread_id: str) -> sqlite3.Row:
    with _connect_db(db_path, read_only=True) as conn:
        row = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Thread not found")
    return row


def _valid_date_dir(date: str) -> Path:
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, "Invalid backup date") from exc
    path = BACKUPS_BASE / date
    resolved = _ensure_allowed_path(path, [BACKUPS_BASE])
    if resolved.parent != BACKUPS_BASE.resolve():
        raise HTTPException(403, "Access denied")
    return resolved


def _backup_state_db(date: str) -> Path:
    backup_dir = _valid_date_dir(date)
    db_path = backup_dir / "state_5.sqlite"
    if not db_path.exists():
        raise HTTPException(404, "Backup state DB not found")
    return db_path


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, indent=2, ensure_ascii=False)

    parts = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        block_type = block.get("type", "")
        text = block.get("text")
        if text is None:
            text = block.get("content")
        if isinstance(text, str):
            parts.append(text)
        elif block_type:
            parts.append(f"[{block_type}]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _blocks_from_content(content: Any) -> List[Dict[str, str]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": json.dumps(content, indent=2, ensure_ascii=False)}]

    blocks: List[Dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append({"type": "text", "text": str(block)})
            continue

        block_type = block.get("type", "")
        if block_type in {"input_text", "output_text", "text"}:
            text = block.get("text") or block.get("content") or ""
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            continue

        if block_type == "input_image":
            image_url = block.get("image_url")
            if isinstance(image_url, str) and image_url:
                blocks.append({"type": "image", "src": image_url})
            continue

        text = block.get("text") or block.get("content")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        elif block_type:
            blocks.append({"type": "text", "text": f"[{block_type}]"})
        else:
            blocks.append({"type": "text", "text": json.dumps(block, ensure_ascii=False)})

    return blocks


def _format_arguments(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def _normalize_response_item(
    record: Dict[str, Any],
    line_no: int,
    include_internal: bool,
) -> Optional[Dict[str, Any]]:
    payload = record.get("payload", {})
    item_type = payload.get("type")
    timestamp = record.get("timestamp")

    if item_type == "message":
        role = payload.get("role") or "message"
        if not include_internal and role in {"developer", "system"}:
            return None
        content = payload.get("content")
        text = _text_from_content(content)
        blocks = _blocks_from_content(content)
        if not text and not blocks:
            return None
        if not include_internal and role == "user":
            stripped = text.lstrip()
            if any(stripped.startswith(prefix) for prefix in INTERNAL_USER_PREFIXES):
                return None
        return {
            "id": f"line-{line_no}",
            "line": line_no,
            "timestamp": timestamp,
            "kind": "message",
            "role": role,
            "phase": payload.get("phase"),
            "text": text,
            "blocks": blocks,
        }

    if item_type == "reasoning":
        text = _text_from_content(payload.get("summary")) or _text_from_content(payload.get("content"))
        if not text and not include_internal:
            return None
        return {
            "id": f"line-{line_no}",
            "line": line_no,
            "timestamp": timestamp,
            "kind": "reasoning",
            "role": "reasoning",
            "text": text or "(reasoning item)",
        }

    if item_type in {"function_call", "custom_tool_call"}:
        return {
            "id": f"line-{line_no}",
            "line": line_no,
            "timestamp": timestamp,
            "kind": "tool_call",
            "role": "tool",
            "name": payload.get("name") or item_type,
            "call_id": payload.get("call_id"),
            "text": _format_arguments(payload.get("arguments") or payload.get("input")),
        }

    if item_type in {"function_call_output", "custom_tool_call_output"}:
        output = payload.get("output")
        if not isinstance(output, str):
            output = _format_arguments(output)
        return {
            "id": f"line-{line_no}",
            "line": line_no,
            "timestamp": timestamp,
            "kind": "tool_output",
            "role": "tool_output",
            "call_id": payload.get("call_id"),
            "text": output or "",
        }

    if include_internal:
        return {
            "id": f"line-{line_no}",
            "line": line_no,
            "timestamp": timestamp,
            "kind": "raw_item",
            "role": item_type or "response_item",
            "text": json.dumps(payload, indent=2, ensure_ascii=False),
        }
    return None


def _normalize_event_msg(
    record: Dict[str, Any],
    line_no: int,
    include_internal: bool,
) -> Optional[Dict[str, Any]]:
    payload = record.get("payload", {})
    event_type = payload.get("type")
    keep = {
        "task_started",
        "task_complete",
        "task_failed",
        "turn_started",
        "turn_completed",
        "turn_failed",
        "error",
    }
    if event_type not in keep and not include_internal:
        return None

    text = payload.get("message") or payload.get("msg") or payload.get("error")
    if not text:
        compact = {
            key: value
            for key, value in payload.items()
            if key not in {"type"} and value not in (None, "", [], {})
        }
        text = json.dumps(compact, indent=2, ensure_ascii=False) if compact else event_type

    return {
        "id": f"line-{line_no}",
        "line": line_no,
        "timestamp": record.get("timestamp"),
        "kind": "event",
        "role": "event",
        "name": event_type,
        "text": text,
    }


def _parse_rollout(path: Path, include_internal: bool = False) -> Dict[str, Any]:
    allowed = [CODEX_HOME, BACKUPS_BASE]
    resolved = _ensure_allowed_path(path, allowed)
    if not resolved.exists():
        raise HTTPException(404, f"Rollout file not found: {path}")

    events: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    warnings: List[str] = []
    counts = {
        "raw": 0,
        "messages": 0,
        "user": 0,
        "assistant": 0,
        "tool_calls": 0,
        "tool_outputs": 0,
        "reasoning": 0,
        "events": 0,
    }

    with resolved.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            counts["raw"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"Line {line_no}: invalid JSON ({exc})")
                continue

            record_type = record.get("type")
            normalized = None
            if record_type == "session_meta":
                meta.update(record.get("payload") or {})
                if include_internal:
                    normalized = {
                        "id": f"line-{line_no}",
                        "line": line_no,
                        "timestamp": record.get("timestamp"),
                        "kind": "event",
                        "role": "event",
                        "name": "session_meta",
                        "text": json.dumps(record.get("payload") or {}, indent=2, ensure_ascii=False),
                    }
            elif record_type == "turn_context":
                if include_internal:
                    normalized = {
                        "id": f"line-{line_no}",
                        "line": line_no,
                        "timestamp": record.get("timestamp"),
                        "kind": "event",
                        "role": "event",
                        "name": "turn_context",
                        "text": json.dumps(record.get("payload") or {}, indent=2, ensure_ascii=False),
                    }
            elif record_type == "response_item":
                normalized = _normalize_response_item(record, line_no, include_internal)
            elif record_type == "event_msg":
                normalized = _normalize_event_msg(record, line_no, include_internal)
            elif include_internal:
                normalized = {
                    "id": f"line-{line_no}",
                    "line": line_no,
                    "timestamp": record.get("timestamp"),
                    "kind": "raw_item",
                    "role": record_type or "record",
                    "text": json.dumps(record, indent=2, ensure_ascii=False),
                }

            if not normalized:
                continue

            events.append(normalized)
            kind = normalized.get("kind")
            role = normalized.get("role")
            if kind == "message":
                counts["messages"] += 1
                if role == "user":
                    counts["user"] += 1
                if role == "assistant":
                    counts["assistant"] += 1
            elif kind == "tool_call":
                counts["tool_calls"] += 1
            elif kind == "tool_output":
                counts["tool_outputs"] += 1
            elif kind == "reasoning":
                counts["reasoning"] += 1
            elif kind == "event":
                counts["events"] += 1

    return {"events": events, "meta": meta, "counts": counts, "warnings": warnings}


def _query_threads(
    db_path: Path,
    cwd: str,
    archived: str,
    source: str,
    q: str,
    limit: int,
    offset: int,
    base: Optional[Path] = None,
) -> Dict[str, Any]:
    where = []
    params: List[Any] = []
    title_base = base if base is not None else CODEX_HOME
    title_overrides = _load_title_overrides(title_base)

    if cwd:
        where.append("cwd = ?")
        params.append(cwd)
    if archived == "active":
        where.append("archived = 0")
    elif archived == "archived":
        where.append("archived = 1")
    if source:
        where.append("source = ?")
        params.append(source)
    if q and not title_overrides:
        like = f"%{q.lower()}%"
        where.append(
            """
            (
                lower(id) LIKE ?
                OR lower(title) LIKE ?
                OR lower(preview) LIKE ?
                OR lower(first_user_message) LIKE ?
                OR lower(cwd) LIKE ?
            )
            """
        )
        params.extend([like, like, like, like, like])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with _connect_db(db_path, read_only=True) as conn:
        if q and title_overrides:
            rows = conn.execute(
                f"""
                SELECT {THREAD_COLUMNS}
                FROM threads
                {where_sql}
                ORDER BY recency_at_ms DESC, updated_at_ms DESC, id DESC
                """,
                params,
            ).fetchall()
        else:
            params_for_count = list(params)
            paged_params = [*params, limit, offset]
            total = conn.execute(f"SELECT COUNT(*) FROM threads {where_sql}", params_for_count).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT {THREAD_COLUMNS}
                FROM threads
                {where_sql}
                ORDER BY recency_at_ms DESC, updated_at_ms DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                paged_params,
            ).fetchall()

    if q and title_overrides:
        needle = q.lower()
        filtered = [
            row
            for row in rows
            if any(
                needle in str(value or "").lower()
                for value in (
                    row["id"],
                    title_overrides.get(row["id"]),
                    row["title"],
                    row["preview"],
                    row["first_user_message"],
                    row["cwd"],
                )
            )
        ]
        total = len(filtered)
        rows = filtered[offset : offset + limit]

    return {
        "threads": [_row_to_thread(row, base=base, title_overrides=title_overrides) for row in rows],
        "total": total,
    }


def _rewrite_jsonl_excluding(path: Path, thread_id: str) -> int:
    if not path.exists():
        return 0

    kept: List[str] = []
    removed = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            linked_id = obj.get("id") or obj.get("session_id") or obj.get("thread_id")
            if linked_id == thread_id:
                removed += 1
            else:
                kept.append(line)

    path.write_text("".join(kept), encoding="utf-8")
    return removed


def _rewrite_session_index_title(path: Path, thread_id: str, title: str) -> bool:
    if not path.exists():
        return False

    changed = False
    lines: List[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                lines.append(line)
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            if obj.get("id") == thread_id:
                obj["thread_name"] = title
                line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
                changed = True
            lines.append(line)

    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def _rename_thread(thread_id: str, title: str) -> Dict[str, Any]:
    if not UUID_RE.match(thread_id):
        raise HTTPException(400, "Thread id must be a UUID")

    clean_title = " ".join(title.strip().split())
    if not clean_title:
        raise HTTPException(400, "Title cannot be empty")
    if len(clean_title) > 240:
        raise HTTPException(400, "Title must be 240 characters or less")

    db_path = _ensure_allowed_path(STATE_DB, [CODEX_HOME])
    with _connect_db(db_path, read_only=False) as conn:
        row = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Thread not found")
        conn.execute("UPDATE threads SET title = ? WHERE id = ?", (clean_title, thread_id))
        conn.commit()
        updated = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()

    _write_title_override(CODEX_HOME, thread_id, clean_title)
    index_updated = _rewrite_session_index_title(CODEX_HOME / "session_index.jsonl", thread_id, clean_title)
    return {
        "thread": _row_to_thread(updated, title_overrides={thread_id: clean_title}),
        "session_index_updated": index_updated,
        "title_override_updated": True,
    }


def _delete_shell_snapshots(base: Path, thread_id: str) -> int:
    shell_dir = base / "shell_snapshots"
    if not shell_dir.exists():
        return 0
    deleted = 0
    for item in shell_dir.glob(f"{thread_id}.*"):
        if item.is_file():
            item.unlink()
            deleted += 1
    return deleted


def _delete_thread_from_store(db_path: Path, base: Path, thread_id: str) -> Dict[str, Any]:
    if not UUID_RE.match(thread_id):
        raise HTTPException(400, "Thread id must be a UUID")

    base = _ensure_allowed_path(base, [CODEX_HOME, BACKUPS_BASE])
    db_path = _ensure_allowed_path(db_path, [CODEX_HOME, BACKUPS_BASE])

    with _connect_db(db_path, read_only=False) as conn:
        row = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Thread not found")

        rollout_path = _map_path_under_base(row["rollout_path"], base)
        rollout_path = _ensure_allowed_path(rollout_path, [base])

        conn.execute(
            "DELETE FROM thread_spawn_edges WHERE parent_thread_id = ? OR child_thread_id = ?",
            (thread_id, thread_id),
        )
        conn.execute(
            "UPDATE agent_job_items SET assigned_thread_id = NULL WHERE assigned_thread_id = ?",
            (thread_id,),
        )
        conn.execute("DELETE FROM thread_dynamic_tools WHERE thread_id = ?", (thread_id,))
        deleted_rows = conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,)).rowcount
        conn.commit()

    transcript_deleted = False
    if rollout_path.exists():
        rollout_path.unlink()
        transcript_deleted = True

    index_removed = _rewrite_jsonl_excluding(base / "session_index.jsonl", thread_id)
    history_removed = _rewrite_jsonl_excluding(base / "history.jsonl", thread_id)
    shell_snapshots_deleted = _delete_shell_snapshots(base, thread_id)
    title_override_removed = _delete_title_override(base, thread_id)

    return {
        "deleted": thread_id,
        "db_rows": deleted_rows,
        "transcript_deleted": transcript_deleted,
        "index_rows_removed": index_removed,
        "history_rows_removed": history_removed,
        "shell_snapshots_deleted": shell_snapshots_deleted,
        "title_override_removed": title_override_removed,
    }


async def _broadcast(data: Dict[str, Any]) -> None:
    dead: List[WebSocket] = []
    for ws in _connections:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _connections:
            _connections.remove(ws)


async def _debounced_broadcast() -> None:
    global _pending_broadcast
    _pending_broadcast = None
    await _broadcast({"type": "refresh"})


class _FSHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        global _pending_broadcast
        if not _loop or _loop.is_closed():
            return
        if _pending_broadcast is not None:
            _pending_broadcast.cancel()
        _pending_broadcast = _loop.call_later(
            1.0,
            lambda: asyncio.run_coroutine_threadsafe(_debounced_broadcast(), _loop),
        )


def _schedule_watch(path: Path, recursive: bool) -> None:
    if not path.exists():
        logging.info("Watch skipped, path not found: %s", path)
        return
    observer = Observer()
    observer.schedule(_FSHandler(), str(path), recursive=recursive)
    observer.start()
    _observers.append(observer)
    logging.info("Watching %s (recursive=%s)", path, recursive)


@app.on_event("startup")
async def _startup() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    BACKUPS_BASE.mkdir(parents=True, exist_ok=True)

    _schedule_watch(SESSION_DIR, recursive=True)
    _schedule_watch(ARCHIVED_SESSION_DIR, recursive=True)
    _schedule_watch(STATE_DB.parent, recursive=False)

    today_dir = BACKUPS_BASE / datetime.now().strftime("%Y-%m-%d")
    if not today_dir.exists():
        asyncio.create_task(_run_backup("startup"))
    asyncio.create_task(_midnight_scheduler())


@app.on_event("shutdown")
async def _shutdown() -> None:
    for observer in _observers:
        observer.stop()
    for observer in _observers:
        observer.join(timeout=2)


@app.websocket("/ws")
async def _ws(ws: WebSocket) -> None:
    await ws.accept()
    _connections.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in _connections:
            _connections.remove(ws)


async def _midnight_scheduler() -> None:
    while True:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())
        await _run_backup("scheduled")


def _backup_sqlite(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        source = sqlite3.connect(_sqlite_uri(src), uri=True, timeout=5)
        target = sqlite3.connect(dest)
        with target:
            source.backup(target)
        source.close()
        target.close()
        return True
    except sqlite3.Error as exc:
        logging.warning("SQLite online backup failed for %s: %s; falling back to copy2", src, exc)
        shutil.copy2(src, dest)
        return True


def _copy_file_if_exists(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _copy_dir_if_exists(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return True


def _perform_backup(trigger: str) -> Dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    backup_dir = BACKUPS_BASE / today
    backup_dir.mkdir(parents=True, exist_ok=True)

    copied_sqlite = []
    for db_name in SQLITE_DB_NAMES:
        src = SQLITE_HOME / db_name
        if _backup_sqlite(src, backup_dir / db_name):
            copied_sqlite.append(db_name)

    copied_files = []
    for name in ("session_index.jsonl", "history.jsonl", "version.json", TITLE_OVERRIDES_NAME):
        if _copy_file_if_exists(CODEX_HOME / name, backup_dir / name):
            copied_files.append(name)

    copied_dirs = []
    for name in ("sessions", "archived_sessions"):
        if _copy_dir_if_exists(CODEX_HOME / name, backup_dir / name):
            copied_dirs.append(name)

    info = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "trigger": trigger,
        "codex_home": str(CODEX_HOME),
        "host_codex_home": str(HOST_CODEX_HOME),
        "domains": {
            "sqlite": {"files": copied_sqlite, "count": len(copied_sqlite), "status": "success"},
            "jsonl": {"files": copied_files, "count": len(copied_files), "status": "success"},
            "sessions": {"dirs": copied_dirs, "count": len(copied_dirs), "status": "success"},
        },
    }
    (backup_dir / "backup-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    _prune_backups()
    return info


async def _run_backup(trigger: str) -> Optional[Dict[str, Any]]:
    global _backup_running
    if _backup_running:
        return None
    _backup_running = True
    try:
        info = await asyncio.to_thread(_perform_backup, trigger)
        await _broadcast({"type": "backup_done", "date": datetime.now().strftime("%Y-%m-%d")})
        return info
    finally:
        _backup_running = False


def _prune_backups() -> None:
    if BACKUP_RETENTION_DAYS <= 0 or not BACKUPS_BASE.exists():
        return
    cutoff = datetime.now().timestamp() - BACKUP_RETENTION_DAYS * 86400
    for item in BACKUPS_BASE.iterdir():
        if not item.is_dir():
            continue
        try:
            dt = datetime.strptime(item.name, "%Y-%m-%d")
        except ValueError:
            continue
        if dt.timestamp() < cutoff:
            shutil.rmtree(item)


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    with _connect_state() as conn:
        total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM threads WHERE archived = 1").fetchone()[0]
        projects = conn.execute("SELECT COUNT(DISTINCT cwd) FROM threads").fetchone()[0]
        latest = conn.execute("SELECT MAX(recency_at_ms) FROM threads").fetchone()[0]

    backups = _list_backups()
    return {
        "codex_home": str(CODEX_HOME),
        "host_codex_home": str(HOST_CODEX_HOME),
        "state_db": str(STATE_DB),
        "backup_dir": str(BACKUPS_BASE),
        "backup_running": _backup_running,
        "threads": total,
        "archived_threads": archived,
        "active_threads": total - archived,
        "projects": projects,
        "latest_activity": _ms_to_iso(latest),
        "latest_backup": backups[0] if backups else None,
    }


@app.get("/api/projects")
def api_projects() -> Dict[str, Any]:
    with _connect_state() as conn:
        rows = conn.execute(
            """
            SELECT
                cwd,
                COUNT(*) AS total,
                SUM(CASE WHEN archived = 0 THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END) AS archived,
                MAX(recency_at_ms) AS latest_ms
            FROM threads
            GROUP BY cwd
            ORDER BY latest_ms DESC, cwd ASC
            """
        ).fetchall()

    projects = [
        {
            "cwd": row["cwd"],
            "name": Path(row["cwd"]).name or row["cwd"],
            "total": row["total"],
            "active": row["active"] or 0,
            "archived": row["archived"] or 0,
            "latest_at": _ms_to_iso(row["latest_ms"]),
            "latest_at_ms": row["latest_ms"],
        }
        for row in rows
    ]
    return {"projects": projects}


@app.get("/api/threads")
def api_threads(
    cwd: str = "",
    archived: str = Query("active", pattern="^(active|archived|all)$"),
    source: str = "",
    q: str = "",
    limit: int = Query(300, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    return _query_threads(STATE_DB, cwd, archived, source, q, limit, offset)


@app.get("/api/threads/{thread_id}")
def api_thread(
    thread_id: str,
    include_internal: bool = False,
) -> Dict[str, Any]:
    row = _get_thread_row(thread_id)
    thread = _row_to_thread(row)
    parsed = _parse_rollout(Path(thread["local_rollout_path"]), include_internal=include_internal)
    return {"thread": thread, **parsed}


@app.get("/api/threads/{thread_id}/raw", response_class=PlainTextResponse)
def api_thread_raw(thread_id: str) -> PlainTextResponse:
    row = _get_thread_row(thread_id)
    thread = _row_to_thread(row)
    path = _ensure_allowed_path(Path(thread["local_rollout_path"]), [CODEX_HOME, BACKUPS_BASE])
    if not path.exists():
        raise HTTPException(404, "Rollout file not found")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@app.patch("/api/threads/{thread_id}")
async def api_rename_thread(thread_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = _rename_thread(thread_id, str(payload.get("title") or ""))
    await _broadcast({"type": "refresh"})
    return result


@app.delete("/api/threads/{thread_id}")
async def api_delete_thread(thread_id: str) -> Dict[str, Any]:
    result = _delete_thread_from_store(STATE_DB, CODEX_HOME, thread_id)
    await _broadcast({"type": "refresh"})
    return result


def _list_backups() -> List[Dict[str, Any]]:
    if not BACKUPS_BASE.exists():
        return []
    backups = []
    for item in BACKUPS_BASE.iterdir():
        if not item.is_dir():
            continue
        try:
            datetime.strptime(item.name, "%Y-%m-%d")
        except ValueError:
            continue
        info_path = item / "backup-info.json"
        info: Dict[str, Any] = {}
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                info = {}
        thread_count = 0
        project_count = 0
        db_path = item / "state_5.sqlite"
        if db_path.exists():
            try:
                with _connect_db(db_path, read_only=True) as conn:
                    thread_count = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                    project_count = conn.execute("SELECT COUNT(DISTINCT cwd) FROM threads").fetchone()[0]
            except HTTPException:
                pass
        backups.append(
            {
                "date": item.name,
                "path": str(item),
                "timestamp": info.get("timestamp"),
                "trigger": info.get("trigger"),
                "domains": info.get("domains", {}),
                "thread_count": thread_count,
                "project_count": project_count,
            }
        )
    backups.sort(key=lambda entry: entry["date"], reverse=True)
    return backups


@app.get("/api/backups")
def api_backups() -> Dict[str, Any]:
    return {"backups": _list_backups(), "running": _backup_running}


@app.get("/api/backups/{date}/projects")
def api_backup_projects(date: str) -> Dict[str, Any]:
    db_path = _backup_state_db(date)
    with _connect_db(db_path, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT
                cwd,
                COUNT(*) AS total,
                SUM(CASE WHEN archived = 0 THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END) AS archived,
                MAX(recency_at_ms) AS latest_ms
            FROM threads
            GROUP BY cwd
            ORDER BY latest_ms DESC, cwd ASC
            """
        ).fetchall()

    projects = [
        {
            "cwd": row["cwd"],
            "name": Path(row["cwd"]).name or row["cwd"],
            "total": row["total"],
            "active": row["active"] or 0,
            "archived": row["archived"] or 0,
            "latest_at": _ms_to_iso(row["latest_ms"]),
            "latest_at_ms": row["latest_ms"],
        }
        for row in rows
    ]
    return {"projects": projects}


@app.get("/api/backups/{date}/threads")
def api_backup_threads(
    date: str,
    cwd: str = "",
    archived: str = Query("all", pattern="^(active|archived|all)$"),
    source: str = "",
    q: str = "",
    limit: int = Query(300, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    backup_dir = _valid_date_dir(date)
    db_path = _backup_state_db(date)
    return _query_threads(db_path, cwd, archived, source, q, limit, offset, base=backup_dir)


@app.get("/api/backups/{date}/threads/{thread_id}")
def api_backup_thread(
    date: str,
    thread_id: str,
    include_internal: bool = False,
) -> Dict[str, Any]:
    backup_dir = _valid_date_dir(date)
    row = _get_thread_row_from_db(_backup_state_db(date), thread_id)
    thread = _row_to_thread(row, base=backup_dir)
    parsed = _parse_rollout(Path(thread["local_rollout_path"]), include_internal=include_internal)
    return {"thread": thread, **parsed}


@app.get("/api/backups/{date}/threads/{thread_id}/raw", response_class=PlainTextResponse)
def api_backup_thread_raw(date: str, thread_id: str) -> PlainTextResponse:
    backup_dir = _valid_date_dir(date)
    row = _get_thread_row_from_db(_backup_state_db(date), thread_id)
    thread = _row_to_thread(row, base=backup_dir)
    path = _ensure_allowed_path(Path(thread["local_rollout_path"]), [backup_dir])
    if not path.exists():
        raise HTTPException(404, "Rollout file not found")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@app.delete("/api/backups/{date}/threads/{thread_id}")
async def api_delete_backup_thread(date: str, thread_id: str) -> Dict[str, Any]:
    backup_dir = _valid_date_dir(date)
    result = _delete_thread_from_store(_backup_state_db(date), backup_dir, thread_id)
    await _broadcast({"type": "refresh"})
    return result


@app.post("/api/backups/run")
async def api_run_backup() -> Dict[str, Any]:
    if _backup_running:
        return {"running": True, "started": False}
    info = await _run_backup("manual")
    return {"running": False, "started": True, "backup": info}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
