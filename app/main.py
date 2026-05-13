import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/api/status")
def api_status():
    with _connect_state() as conn:
        total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM threads WHERE archived = 1").fetchone()[0]
        projects = conn.execute("SELECT COUNT(DISTINCT cwd) FROM threads").fetchone()[0]
    return {
        "threads": total,
        "active_threads": total - archived,
        "archived_threads": archived,
        "projects": projects,
    }


@app.get("/api/projects")
def api_projects():
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
    return {
        "projects": [
            {
                "cwd": row["cwd"],
                "total": row["total"],
                "active": row["active"] or 0,
                "archived": row["archived"] or 0,
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
    if q:
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
        params.extend([f"%{q.lower()}%"] * 5)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect_state() as conn:
        rows = conn.execute(
            f"SELECT {THREAD_COLUMNS} FROM threads {where_sql} ORDER BY recency_at_ms DESC LIMIT 300",
            params,
        ).fetchall()
    return {"threads": [_row_to_thread(row) for row in rows], "total": len(rows)}


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
