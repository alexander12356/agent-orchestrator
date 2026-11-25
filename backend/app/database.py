import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import settings


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    avatar_url TEXT NOT NULL DEFAULT '🤖',
    role_prompt TEXT NOT NULL DEFAULT '',
    concurrency_limit INTEGER NOT NULL DEFAULT 1 CHECK(concurrency_limit > 0),
    primary_config TEXT NOT NULL,
    fallback_configs TEXT NOT NULL DEFAULT '[]',
    fallback_triggers TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    external_issue_id TEXT,
    external_issue_key TEXT,
    external_issue_url TEXT,
    milestone TEXT,
    sprint TEXT,
    current_stage INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    branch_name TEXT NOT NULL,
    pr_url TEXT,
    merged_to_main INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    stage_agents TEXT NOT NULL DEFAULT '{}',
    worktree_path TEXT,
    repository_url TEXT,
    auto_retries INTEGER NOT NULL DEFAULT 0,
    doc_returns INTEGER NOT NULL DEFAULT 0,
    code_returns INTEGER NOT NULL DEFAULT 0,
    hold_reason TEXT,
    token_budget INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY(task_id, depends_on_task_id),
    CHECK(task_id != depends_on_task_id)
);
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    agent_id INTEGER REFERENCES agents(id),
    cli_used TEXT,
    model_used TEXT,
    is_fallback INTEGER NOT NULL DEFAULT 0,
    artifact_path TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_tokens INTEGER NOT NULL DEFAULT 0,
    tokens_estimated INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    UNIQUE(task_id, stage, attempt)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_task ON stage_runs(task_id, stage);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


BRANCH_PREFIX = "Agent-Orchestrator"
ISSUE_KEY_RE = re.compile(r"[A-Za-z0-9]+-[0-9]+")


def branch_name(task_id: int, issue_key: str | None = None) -> str:
    key = (issue_key or "").strip()
    if not ISSUE_KEY_RE.fullmatch(key):
        key = f"task-{task_id}"
    return f"{BRANCH_PREFIX}/{key}"


def init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column in ("external_issue_key", "external_issue_url", "milestone", "sprint", "repository_url"):
            if column not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
        for column, definition in (
            ("auto_retries", "INTEGER NOT NULL DEFAULT 0"),
            ("doc_returns", "INTEGER NOT NULL DEFAULT 0"),
            ("code_returns", "INTEGER NOT NULL DEFAULT 0"),
            ("hold_reason", "TEXT"),
            ("token_budget", "INTEGER"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        # A task that already reached the worker may have its old branch pushed to origin.
        legacy = conn.execute(
            "SELECT id, external_issue_key FROM tasks WHERE branch_name='feat/task-' || id AND NOT EXISTS (SELECT 1 FROM stage_runs WHERE task_id=tasks.id AND stage=3)"
        ).fetchall()
        conn.executemany("UPDATE tasks SET branch_name=? WHERE id=?", [(branch_name(row["id"], row["external_issue_key"]), row["id"]) for row in legacy])


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def decode_agent(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("primary_config", "fallback_configs", "fallback_triggers"):
        result[key] = json.loads(result[key])
    return result


def decode_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["stage_agents"] = {int(k): int(v) for k, v in json.loads(result["stage_agents"]).items()}
    result["merged_to_main"] = bool(result["merged_to_main"])
    result["repository_url"] = result.get("repository_url") or ""
    for key in ("auto_retries", "doc_returns", "code_returns"):
        result[key] = int(result.get(key) or 0)
    return result


def spent_tokens(conn: sqlite3.Connection, task_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(input_tokens+output_tokens+cache_tokens), 0) AS total FROM stage_runs WHERE task_id=?",
        (task_id,),
    ).fetchone()["total"]
