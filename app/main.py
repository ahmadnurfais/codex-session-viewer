import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Codex Session Viewer")

CODEX_HOME = Path(
    os.getenv("CODEX_HOME", "/codex" if Path("/codex").exists() else str(Path.home() / ".codex"))
).expanduser()
HOST_CODEX_HOME = Path(os.getenv("HOST_CODEX_HOME", str(CODEX_HOME))).expanduser()
STATE_DB = Path(os.getenv("CODEX_STATE_DB", str(CODEX_HOME / "state_5.sqlite"))).expanduser()
STATIC_DIR = Path(__file__).parent / "static"

THREAD_COLUMNS = """
    id,
    rollout_path,
    created_at,
    updated_at,
    source,
    model_provider,
    cwd,
    title,
    archived,
    archived_at,
    first_user_message,
    preview,
    recency_at,
    recency_at_ms,
    updated_at_ms,
    model
"""


def _ms_to_iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _connect_state():
    if not STATE_DB.exists():
        raise HTTPException(500, f"Codex state DB not found: {STATE_DB}")
    conn = sqlite3.connect(f"file:{STATE_DB.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _map_codex_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.exists():
        return path
    try:
        return CODEX_HOME / path.relative_to(HOST_CODEX_HOME)
    except ValueError:
        return path


def _row_to_thread(row):
    title = row["title"] or row["preview"] or row["first_user_message"] or row["id"]
    rollout = _map_codex_path(row["rollout_path"])
    return {
        "id": row["id"],
        "title": title,
        "cwd": row["cwd"],
        "source": row["source"],
        "model": row["model"],
        "archived": bool(row["archived"]),
        "recency_at": _ms_to_iso(row["recency_at_ms"] or row["updated_at_ms"]),
        "rollout_path": row["rollout_path"],
        "local_rollout_path": str(rollout),
        "rollout_exists": rollout.exists(),
        "resume_command": f"codex resume {row['id']}",
    }


def _text_from_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, indent=2, ensure_ascii=False)
    parts = []
    for block in content:
        if isinstance(block, dict):
            parts.append(block.get("text") or block.get("content") or block.get("type") or "")
        else:
            parts.append(str(block))
    return "\n".join(part for part in parts if part)


def _parse_rollout(path: Path, include_internal: bool = False):
    if not path.exists():
        raise HTTPException(404, "Rollout file not found")
    events = []
    counts = {"raw": 0, "messages": 0, "tool_calls": 0, "tool_outputs": 0, "events": 0}
    warnings = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            counts["raw"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.append(f"Line {line_no}: invalid JSON ({exc})")
                continue
            payload = record.get("payload", {})
            record_type = record.get("type")
            item_type = payload.get("type")
            if record_type == "response_item" and item_type == "message":
                role = payload.get("role") or "message"
                text = _text_from_content(payload.get("content"))
                if text or include_internal:
                    events.append(
                        {
                            "id": f"line-{line_no}",
                            "line": line_no,
                            "timestamp": record.get("timestamp"),
                            "kind": "message",
                            "role": role,
                            "text": text,
                            "blocks": [{"type": "text", "text": text}],
                        }
                    )
                    counts["messages"] += 1
            elif record_type == "response_item" and item_type in {"function_call", "custom_tool_call"}:
                events.append(
                    {
                        "id": f"line-{line_no}",
                        "line": line_no,
                        "timestamp": record.get("timestamp"),
                        "kind": "tool_call",
                        "role": "tool",
                        "name": payload.get("name"),
                        "call_id": payload.get("call_id"),
                        "text": json.dumps(
                            payload.get("arguments") or payload.get("input") or {},
                            indent=2,
                            ensure_ascii=False,
                        ),
                    }
                )
                counts["tool_calls"] += 1
            elif record_type == "response_item" and item_type in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                events.append(
                    {
                        "id": f"line-{line_no}",
                        "line": line_no,
                        "timestamp": record.get("timestamp"),
                        "kind": "tool_output",
                        "role": "tool_output",
                        "call_id": payload.get("call_id"),
                        "text": str(payload.get("output") or ""),
                    }
                )
                counts["tool_outputs"] += 1
            elif include_internal:
                events.append(
                    {
                        "id": f"line-{line_no}",
                        "line": line_no,
                        "timestamp": record.get("timestamp"),
                        "kind": "event",
                        "role": "event",
                        "text": json.dumps(record, indent=2, ensure_ascii=False),
                    }
                )
                counts["events"] += 1
    return {"events": events, "counts": counts, "warnings": warnings, "meta": {}}


@app.get("/api/status")
def api_status():
    with _connect_state() as conn:
        total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM threads WHERE archived = 1").fetchone()[0]
        projects = conn.execute("SELECT COUNT(DISTINCT cwd) FROM threads").fetchone()[0]
    return {"threads": total, "active_threads": total - archived, "archived_threads": archived, "projects": projects}


@app.get("/api/projects")
def api_projects():
    with _connect_state() as conn:
        rows = conn.execute(
            "SELECT cwd, COUNT(*) AS total, MAX(recency_at_ms) AS latest_ms FROM threads GROUP BY cwd ORDER BY latest_ms DESC"
        ).fetchall()
    return {
        "projects": [
            {
                "cwd": row["cwd"],
                "total": row["total"],
                "active": row["total"],
                "archived": 0,
                "latest_at": _ms_to_iso(row["latest_ms"]),
            }
            for row in rows
        ]
    }


@app.get("/api/threads")
def api_threads(
    cwd: str = "",
    archived: str = Query("active", pattern="^(active|archived|all)$"),
    q: str = "",
):
    where = []
    params = []
    if cwd:
        where.append("cwd = ?")
        params.append(cwd)
    if archived == "active":
        where.append("archived = 0")
    elif archived == "archived":
        where.append("archived = 1")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect_state() as conn:
        rows = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads {where_sql} ORDER BY recency_at_ms DESC LIMIT 300",
            params,
        ).fetchall()
    threads = [_row_to_thread(row) for row in rows]
    if q:
        needle = q.lower()
        threads = [thread for thread in threads if needle in str(thread).lower()]
    return {"threads": threads, "total": len(threads)}


@app.get("/api/threads/{thread_id}")
def api_thread(thread_id: str, include_internal: bool = False):
    with _connect_state() as conn:
        row = conn.execute(f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Thread not found")
    thread = _row_to_thread(row)
    return {"thread": thread, **_parse_rollout(Path(thread["local_rollout_path"]), include_internal)}


@app.get("/api/threads/{thread_id}/raw", response_class=PlainTextResponse)
def api_thread_raw(thread_id: str):
    with _connect_state() as conn:
        row = conn.execute(f"SELECT {THREAD_COLUMNS} FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Thread not found")
    path = Path(_row_to_thread(row)["local_rollout_path"])
    if not path.exists():
        raise HTTPException(404, "Rollout file not found")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
