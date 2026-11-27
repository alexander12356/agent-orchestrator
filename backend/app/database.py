import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .role_prompts import update_role_prompts
from .usage import FIELDS, objects, normalize, kimi_usage, zcode_usage


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
    project TEXT NOT NULL DEFAULT '',
    milestone TEXT,
    sprint TEXT,
    current_stage INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    single_stage INTEGER NOT NULL DEFAULT 0,
    branch_name TEXT NOT NULL,
    pr_url TEXT,
    merged_to_main INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    stage_agents TEXT NOT NULL DEFAULT '{}',
    worktree_path TEXT,
    cleanup_pending INTEGER NOT NULL DEFAULT 0,
    cleanup_head TEXT,
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
    sent_prompt TEXT,
    is_fallback INTEGER NOT NULL DEFAULT 0,
    artifact_path TEXT,
    reviewed_document TEXT,
    reviewed_commit TEXT,
    cli_session TEXT,
    yougile_question_message_id INTEGER,
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
CREATE TABLE IF NOT EXISTS documentation_launches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES stage_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    cli TEXT NOT NULL,
    model TEXT,
    is_fallback INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    usage_partial INTEGER NOT NULL DEFAULT 1,
    model_mixed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE INDEX IF NOT EXISTS idx_documentation_launches_run ON documentation_launches(run_id);
CREATE TABLE IF NOT EXISTS model_prices (
    model TEXT PRIMARY KEY,
    input_rate REAL NOT NULL,
    cache_rate REAL,
    cache_write_rate REAL,
    output_rate REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS yougile_moves (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    issue_id TEXT NOT NULL,
    column_title TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 1,
    retry_after INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_activity INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_task ON stage_runs(task_id, stage);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""

# Persist column changes in the same transaction as the task transition.
# Retain the last target so moving between stages 1–4 does not repeat the API call.
YOUGILE_MOVE_TRIGGERS = "\n".join(
    f"""
    CREATE TRIGGER IF NOT EXISTS {name} AFTER {event} ON tasks
    WHEN NULLIF(TRIM(NEW.external_issue_id), '') IS NOT NULL
        AND NEW.status NOT IN ('open', 'blocked_main')
        AND (NEW.merged_to_main = 1 OR NEW.current_stage BETWEEN 1 AND 5)
    BEGIN
        INSERT INTO yougile_moves(task_id, issue_id, column_title)
        VALUES (
            NEW.id, TRIM(NEW.external_issue_id),
            CASE
                WHEN NEW.merged_to_main = 1 THEN 'Complete'
                WHEN NEW.status = 'waiting_answer' THEN 'Blocked'
                WHEN NEW.current_stage = 5 THEN 'Feedback'
                ELSE 'In progress'
            END
        )
        ON CONFLICT(task_id) DO UPDATE SET
            issue_id=excluded.issue_id, column_title=excluded.column_title, pending=1, retry_after=0
        WHERE yougile_moves.issue_id != excluded.issue_id OR yougile_moves.column_title != excluded.column_title;
    END;
    """
    for name, event in (
        ("yougile_move_on_insert", "INSERT"),
        ("yougile_move_on_update", "UPDATE OF current_stage, status, merged_to_main, external_issue_id"),
    )
)


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
        conn.executescript(SCHEMA + YOUGILE_MOVE_TRIGGERS)
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column in ("external_issue_key", "external_issue_url", "milestone", "sprint", "repository_url"):
            if column not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
        for column, definition in (
            ("project", "TEXT NOT NULL DEFAULT ''"),
            ("single_stage", "INTEGER NOT NULL DEFAULT 0"),
            ("cleanup_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("cleanup_head", "TEXT"),
            ("auto_retries", "INTEGER NOT NULL DEFAULT 0"),
            ("doc_returns", "INTEGER NOT NULL DEFAULT 0"),
            ("code_returns", "INTEGER NOT NULL DEFAULT 0"),
            ("hold_reason", "TEXT"),
            ("token_budget", "INTEGER"),
            ("accepted_document", "TEXT"),
            ("document_accepted_at", "TEXT"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stage_runs)")}
        if "yougile_question_message_id" not in run_columns:
            conn.execute("ALTER TABLE stage_runs ADD COLUMN yougile_question_message_id INTEGER")
        if "reviewed_document" not in run_columns:
            conn.execute("ALTER TABLE stage_runs ADD COLUMN reviewed_document TEXT")
        if "reviewed_commit" not in run_columns:
            conn.execute("ALTER TABLE stage_runs ADD COLUMN reviewed_commit TEXT")
        if "cli_session" not in run_columns:
            conn.execute("ALTER TABLE stage_runs ADD COLUMN cli_session TEXT")
        if "sent_prompt" not in run_columns:
            conn.execute("ALTER TABLE stage_runs ADD COLUMN sent_prompt TEXT")
        if "usage_partial" not in run_columns:
            conn.execute("ALTER TABLE stage_runs ADD COLUMN usage_partial INTEGER NOT NULL DEFAULT 0")
            restore_usage(conn)
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "is_activity" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN is_activity INTEGER NOT NULL DEFAULT 0")
        # A task that already reached the worker may have its old branch pushed to origin.
        legacy = conn.execute(
            "SELECT id, external_issue_key FROM tasks WHERE branch_name='feat/task-' || id AND NOT EXISTS (SELECT 1 FROM stage_runs WHERE task_id=tasks.id AND stage=3)"
        ).fetchall()
        conn.executemany("UPDATE tasks SET branch_name=? WHERE id=?", [(branch_name(row["id"], row["external_issue_key"]), row["id"]) for row in legacy])
        update_role_prompts(conn)


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


def restore_usage(conn):
    from .runner import KIMI_HOME

    legacy = "NOT EXISTS (SELECT 1 FROM documentation_launches WHERE run_id=stage_runs.id)"
    conn.execute(f"UPDATE stage_runs SET usage_partial=1 WHERE ({legacy}) AND (tokens_estimated OR is_fallback OR status!='completed')")
    conn.execute(
        "UPDATE documentation_launches SET status='interrupted', usage_partial=1 WHERE status='running'"
    )
    kimi_sessions = {}
    for base in {KIMI_HOME, Path.home() / ".kimi-code"}:
        for state in (base / "sessions").glob("*/session_*/state.json"):
            try:
                meta = json.loads(state.read_text(encoding="utf-8"))
                created = datetime.fromtimestamp(meta["createdAt"] / 1000, UTC)
                kimi_sessions.setdefault(meta["cwd"], []).append((created, state.parent))
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                continue
    for run in conn.execute(
        "SELECT r.*, t.worktree_path FROM stage_runs r JOIN tasks t ON t.id=r.task_id "
        "WHERE (tokens_estimated OR cli_used IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM documentation_launches WHERE run_id=r.id)"
    ):
        if not run["finished_at"]:
            continue
        cwd = settings.storage_dir / "tasks" / str(run["task_id"])
        log = cwd / "logs" / f"stage_{run['stage']}_attempt_{run['attempt']}.log"
        try:
            transcript = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            transcript = ""
        usage = {}
        if run["stage"] == 3 and run["worktree_path"]:
            cwd = Path(run["worktree_path"])
        start, end = (datetime.fromisoformat(run[k]) for k in ("started_at", "finished_at"))
        sessions = [p for created, p in kimi_sessions.get(str(cwd), []) if start <= created <= end]
        if len(sessions) == 1 and (run["cli_used"] == "kimi" or "Fallback: kimi" in transcript):
            usage = kimi_usage(sessions[0], cwd)
        if not usage and transcript:
            ids = re.findall(r"(?im)^session id: ([a-f0-9-]{36})\s*$", transcript)
            if len(ids) == 1:
                for file in (Path.home() / ".codex/sessions").glob(f"*/*/*/*-{ids[0]}.jsonl"):
                    try:
                        with file.open(encoding="utf-8", errors="replace") as stream:
                            matched = False
                            for event in objects(stream):
                                payload = event.get("payload")
                                if not isinstance(payload, dict):
                                    continue
                                if event.get("type") == "session_meta":
                                    matched = payload.get("cwd") == str(cwd) and payload.get("id") == ids[0]
                                info = payload.get("info")
                                if matched and payload.get("type") == "token_count" and isinstance(info, dict):
                                    usage = normalize(info.get("total_token_usage")) or usage
                    except OSError:
                        continue
        zcode = zcode_usage(cwd, start, end) if run["cli_used"] == "zcode" or "Fallback: zcode" in transcript else {}
        if zcode:
            usage = {key: usage.get(key, 0) + zcode[key] for key in FIELDS}
        if usage:
            partial = bool(run["is_fallback"] or run["status"] != "completed")
            conn.execute("UPDATE stage_runs SET input_tokens=?, cache_tokens=?, output_tokens=?, tokens_estimated=0, usage_partial=? WHERE id=?",
                         (*(usage[k] for k in FIELDS), partial, run["id"]))


def token_summary(conn, task_id):
    sums = ", ".join(f"COALESCE(SUM(CASE WHEN tokens_estimated=0 THEN {k} END),0) AS {k}" for k in FIELDS)
    return dict(conn.execute(f"SELECT {sums}, COALESCE(MAX(usage_partial OR tokens_estimated),0) AS usage_partial FROM stage_runs WHERE task_id=?", (task_id,)).fetchone())


def spent_tokens(conn: sqlite3.Connection, task_id: int) -> int:
    values = token_summary(conn, task_id)
    return sum(values[k] for k in FIELDS)
