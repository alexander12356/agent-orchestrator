"""Measured CLI usage. Cached reads are excluded from ordinary input."""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path, PureWindowsPath

FIELDS = ("input_tokens", "cache_tokens", "output_tokens")


def objects(lines):
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def normalize(usage):
    if not isinstance(usage, dict):
        return {}
    data = {}
    for key in ("input_tokens", "inputTokens", "prompt_tokens", "inputOther",
                "output_tokens", "outputTokens", "completion_tokens", "output",
                "cached_input_tokens", "cached_tokens", "cache_read_input_tokens",
                "cacheReadTokens", "inputCacheRead", "cache_creation_input_tokens",
                "cacheWriteTokens", "inputCacheCreation"):
        if key in usage:
            value = usage[key]
            if type(value) is not int or value < 0:
                return {}
            data[key] = value

    def first(*names):
        return next((data[k] for k in names if k in data), None)

    incoming = first("input_tokens", "inputTokens", "prompt_tokens", "inputOther")
    outgoing = first("output_tokens", "outputTokens", "completion_tokens", "output")
    if incoming is None or outgoing is None:
        return {}
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    included = first("cached_input_tokens", "cached_tokens")
    if isinstance(details, dict) and "cached_tokens" in details:
        included = details["cached_tokens"]
    if included is not None:
        if type(included) is not int or not 0 <= included <= incoming:
            return {}
        cache = included
        incoming -= cache
    else:
        cache = first("cache_read_input_tokens", "cacheReadTokens", "inputCacheRead") or 0
        incoming += first("cache_creation_input_tokens", "cacheWriteTokens", "inputCacheCreation") or 0
    return dict(input_tokens=incoming, cache_tokens=cache, output_tokens=outgoing, estimated=False)


def kimi_session_state(session, cwd):
    try:
        state = json.loads((session / "state.json").read_text(encoding="utf-8"))
        recorded = state["cwd"]
        if isinstance(cwd, PureWindowsPath):
            return state if PureWindowsPath(recorded) == cwd else {}
        return state if Path(recorded).resolve() == cwd.resolve() else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def kimi_usage(session, cwd, since=None):
    if not kimi_session_state(session, cwd):
        return {}
    try:
        totals = dict.fromkeys(FIELDS, 0)
        found = False
        for wire in session.glob("agents/*/wire.jsonl"):
            with wire.open(encoding="utf-8", errors="replace") as stream:
                for event in objects(stream):
                    if event.get("type") != "usage.record":
                        continue
                    if since is not None:
                        timestamp = event.get("time")
                        if type(timestamp) is not int:
                            return {}
                        if timestamp < since:
                            continue
                    usage = normalize(event.get("usage"))
                    if not usage:
                        return {}
                    for key in FIELDS:
                        totals[key] += usage[key]
                    found = True
        return dict(**totals, estimated=False) if found else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def recent_kimi_session(home, cwd, started):
    since = int(started.timestamp() * 1000)
    sessions = []
    for state_file in (home / "sessions").glob("*/*/state.json"):
        try:
            if state_file.stat().st_mtime_ns // 1_000_000 < since:
                continue
        except OSError:
            continue
        session = state_file.parent
        created = kimi_session_state(session, cwd).get("createdAt")
        if type(created) is int and created >= since:
            sessions.append(session)
            if len(sessions) > 1:
                return None
    return sessions[0] if sessions else None


def recent_kimi_usage(home, cwd, started):
    session = recent_kimi_session(home, cwd, started)
    return kimi_usage(session, cwd) if session else {}


def zcode_sessions(conn, cwd, start, end):
    return conn.execute(
        "SELECT id FROM session WHERE directory=? AND parent_id IS NULL AND time_created BETWEEN ? AND ?",
        (str(cwd), int(start.timestamp() * 1000), int(end.timestamp() * 1000)),
    ).fetchall()


def recent_zcode_session(cwd, start, end):
    database = Path.home() / ".zcode/cli/db/db.sqlite"
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                sessions = zcode_sessions(conn, cwd, start, end)
                if len(sessions) == 1:
                    return sessions[0][0]
        except sqlite3.Error:
            pass
    return None


def zcode_usage(cwd, start, end):
    database = Path.home() / ".zcode/cli/db/db.sqlite"
    if not database.is_file():
        return {}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            sessions = zcode_sessions(conn, cwd, start, end)
            if len(sessions) != 1:
                return {}
            rows = conn.execute(
                "SELECT input_tokens, output_tokens, cache_read_input_tokens, computed_total_tokens FROM model_usage "
                "WHERE session_id=? AND computed_total_tokens>0",
                sessions[0],
            ).fetchall()
    except sqlite3.Error:
        return {}
    totals = dict.fromkeys(FIELDS, 0)
    for incoming, outgoing, cache, total in rows:
        usage = normalize(dict(input_tokens=incoming, output_tokens=outgoing, cached_input_tokens=cache or 0))
        if not usage or sum(usage[key] for key in FIELDS) != total:
            return {}
        for key in FIELDS:
            totals[key] += usage[key]
    return dict(**totals, estimated=False) if rows else {}


def extract_usage(output, cwd=None, kimi_home=None, *, kimi_since=None):
    result = {}
    for event in objects(output.splitlines()):
        kind = event.get("type")
        if kind == "session.resume_hint" and cwd and kimi_home:
            session_id = event.get("session_id")
            if isinstance(session_id, str) and re.fullmatch(r"session_[a-fA-F0-9-]{36}", session_id):
                for session in (Path(kimi_home) / "sessions").glob(f"*/{session_id}"):
                    result = kimi_usage(session, cwd, kimi_since) or result
        if kind not in (None, "result", "turn.completed"):
            continue
        nested = event.get("result")
        usage = normalize(event.get("usage") or (nested.get("usage") if isinstance(nested, dict) else None) or event)
        if usage:
            if kind == "turn.completed" and result:
                for key in FIELDS:
                    usage[key] += result[key]
            result = usage
    return result
