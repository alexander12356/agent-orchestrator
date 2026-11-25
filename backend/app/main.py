import asyncio
import hashlib
import hmac
import json
import mimetypes
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, settings
from .database import branch_name, connect, decode_agent, decode_task, dumps, init_db, now, spent_tokens, transaction
from .dispatcher import dispatcher, limits, task_dir
from .events import bus
from .runner import present_log_line
from .schemas import AgentCreate, AgentUpdate, IntegrationSettings, ManualMerge, TaskCreate, TaskUpdate, TrackerAnswer
from .tracker import BOT_PREFIX, YougileError, ensure_webhook, fetch_import_tasks, integration_settings, test_connection
from .worker_hub import hub


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    dispatcher.recover()
    yield
    for task in list(dispatcher.pending):
        task.cancel()


app = FastAPI(title="Agent Orchestrator", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_task(task_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Задача не найдена")
    return decode_task(row)


def task_view(task: dict[str, Any], conn: Any, default_budget: int = 0) -> dict[str, Any]:
    dependencies = conn.execute(
        "SELECT t.id, t.title, t.merged_to_main FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id WHERE d.task_id=? ORDER BY t.id",
        (task["id"],),
    ).fetchall()
    task["dependencies"] = [dict(row) for row in dependencies]
    agent_id = task["stage_agents"].get(task["current_stage"]) if task["status"] != "open" else None
    task["current_agent"] = decode_agent(conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()) if agent_id else None
    task["total_tokens"] = spent_tokens(conn, task["id"])
    # The budget the dispatcher enforces: the task's own, else the shared one.
    task["token_budget_effective"] = int(task["token_budget"]) if task.get("token_budget") is not None else default_budget
    return task


def validate_dependencies(conn: Any, task_id: int, dependencies: list[int]) -> None:
    if len(set(dependencies)) != len(dependencies):
        raise HTTPException(400, "Зависимости не должны повторяться")
    for dependency in dependencies:
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (dependency,)).fetchone():
            raise HTTPException(400, f"Задача #{dependency} не найдена")
        if dependency == task_id:
            raise HTTPException(400, "Задача не может зависеть от себя")
        seen: set[int] = set()
        stack = [dependency]
        while stack:
            current = stack.pop()
            if current == task_id:
                raise HTTPException(400, "Обнаружена циклическая зависимость")
            if current in seen:
                continue
            seen.add(current)
            stack.extend(row[0] for row in conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (current,)))


def validate_agent_assignments(conn: Any, assignments: dict[int, int]) -> None:
    for stage, agent_id in assignments.items():
        if stage not in (1, 2, 3, 4):
            raise HTTPException(400, "Назначения допустимы только для этапов 1–4")
        if not conn.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
            raise HTTPException(400, f"Бот #{agent_id} не найден")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "worker_online": hub.online}


@app.get("/api/agents")
def list_agents() -> list[dict[str, Any]]:
    with connect() as conn:
        return [decode_agent(row) for row in conn.execute("SELECT * FROM agents ORDER BY id")]


@app.post("/api/agents", status_code=201)
def create_agent(data: AgentCreate) -> dict[str, Any]:
    values = data.model_dump()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO agents(name, avatar_url, role_prompt, concurrency_limit, primary_config, fallback_configs, fallback_triggers, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (data.name, data.avatar_url, data.role_prompt, data.concurrency_limit, dumps(data.primary_config.model_dump()), dumps([item.model_dump() for item in data.fallback_configs]), dumps(data.fallback_triggers.model_dump()), now()),
        )
        row = conn.execute("SELECT * FROM agents WHERE id=?", (cursor.lastrowid,)).fetchone()
    return decode_agent(row)


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: int, data: AgentUpdate) -> dict[str, Any]:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
            raise HTTPException(404, "Бот не найден")
        conn.execute(
            "UPDATE agents SET name=?, avatar_url=?, role_prompt=?, concurrency_limit=?, primary_config=?, fallback_configs=?, fallback_triggers=? WHERE id=?",
            (data.name, data.avatar_url, data.role_prompt, data.concurrency_limit, dumps(data.primary_config.model_dump()), dumps([item.model_dump() for item in data.fallback_configs]), dumps(data.fallback_triggers.model_dump()), agent_id),
        )
        row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return decode_agent(row)


@app.delete("/api/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: int) -> None:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE stage_agents LIKE ?", (f'%"{agent_id}"%',)).fetchone():
            raise HTTPException(409, "Бот назначен на задачи")
        conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))


@app.post("/api/agents/{agent_id}/avatar")
async def upload_avatar(agent_id: int, file: UploadFile = File(...)) -> dict[str, str]:
    content_type = file.content_type or ""
    if content_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        raise HTTPException(400, "Поддерживаются PNG, JPEG, WebP и GIF")
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "Максимальный размер аватарки — 2 МБ")
    extension = mimetypes.guess_extension(content_type) or ".png"
    filename = f"{agent_id}-{uuid.uuid4().hex}{extension}"
    (settings.storage_dir / "avatars" / filename).write_bytes(content)
    url = f"/avatars/{filename}"
    with connect() as conn:
        conn.execute("UPDATE agents SET avatar_url=? WHERE id=?", (url, agent_id))
    return {"avatar_url": url}


@app.get("/api/tasks")
def list_tasks() -> list[dict[str, Any]]:
    default_budget = limits()["task_token_budget"]
    with connect() as conn:
        return [task_view(decode_task(row), conn, default_budget) for row in conn.execute("SELECT * FROM tasks ORDER BY id DESC")]


@app.post("/api/tasks", status_code=201)
async def create_task(data: TaskCreate) -> dict[str, Any]:
    default_budget = limits()["task_token_budget"]
    with transaction() as conn:
        validate_dependencies(conn, -1, data.dependencies)
        validate_agent_assignments(conn, data.stage_agents)
        assignments = {str(k): v for k, v in data.stage_agents.items()}
        blocked = bool(data.dependencies and conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({','.join('?' * len(data.dependencies))}) AND merged_to_main=0 LIMIT 1",
            data.dependencies,
        ).fetchone())
        timestamp = now()
        cursor = conn.execute(
            "INSERT INTO tasks(title, description, external_issue_id, milestone, sprint, repository_url, current_stage, status, branch_name, stage_agents, token_budget, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data.title, data.description, data.external_issue_id, data.milestone, data.sprint, data.repository_url.strip(), 0 if blocked else 1, "blocked_main" if blocked else "queued", "pending", dumps(assignments), data.token_budget, timestamp, timestamp),
        )
        task_id = cursor.lastrowid
        conn.execute("UPDATE tasks SET branch_name=? WHERE id=?", (branch_name(task_id), task_id))
        for dependency in data.dependencies:
            conn.execute("INSERT INTO task_dependencies VALUES (?, ?)", (task_id, dependency))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        task = task_view(decode_task(row), conn, default_budget)
    directory = task_dir(task_id)
    (directory / "00_task.md").write_text(f"# {data.title}\n\n{data.description}", encoding="utf-8")
    await dispatcher.start(task_id)
    return task


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int) -> dict[str, Any]:
    task = get_task(task_id)
    default_budget = limits()["task_token_budget"]
    with connect() as conn:
        result = task_view(task, conn, default_budget)
        result["runs"] = [dict(row) for row in conn.execute("SELECT * FROM stage_runs WHERE task_id=? ORDER BY id", (task_id,))]
    return result


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, data: TaskUpdate) -> dict[str, Any]:
    get_task(task_id)
    default_budget = limits()["task_token_budget"]
    with transaction() as conn:
        validate_dependencies(conn, task_id, data.dependencies)
        validate_agent_assignments(conn, data.stage_agents)
        assignments = {str(k): v for k, v in data.stage_agents.items()}
        conn.execute(
            "UPDATE tasks SET title=?, description=?, external_issue_id=?, milestone=?, sprint=?, repository_url=?, stage_agents=?, token_budget=?, updated_at=? WHERE id=?",
            (data.title, data.description, data.external_issue_id, data.milestone, data.sprint, data.repository_url.strip(), dumps(assignments), data.token_budget, now(), task_id),
        )
        conn.execute("DELETE FROM task_dependencies WHERE task_id=?", (task_id,))
        for dependency in data.dependencies:
            conn.execute("INSERT INTO task_dependencies VALUES (?, ?)", (task_id, dependency))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        result = task_view(decode_task(row), conn, default_budget)
    task_dir(task_id).joinpath("00_task.md").write_text(f"# {data.title}\n\n{data.description}", encoding="utf-8")
    return result


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: int) -> None:
    get_task(task_id)
    with connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    shutil.rmtree(settings.storage_dir / "tasks" / str(task_id), ignore_errors=True)


@app.get("/api/tasks/{task_id}/artifacts")
def artifacts(task_id: int) -> list[dict[str, Any]]:
    get_task(task_id)
    directory = task_dir(task_id)
    files = [p for p in directory.iterdir() if p.is_file() and p.name != "00_task.md"]
    return [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in sorted(files)]


@app.get("/api/tasks/{task_id}/artifacts/{name}")
def artifact(task_id: int, name: str) -> PlainTextResponse:
    get_task(task_id)
    if Path(name).name != name:
        raise HTTPException(400, "Некорректное имя файла")
    path = task_dir(task_id) / name
    if not path.exists():
        raise HTTPException(404, "Артефакт не найден")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.post("/api/tasks/{task_id}/answer", status_code=202)
async def answer(task_id: int, data: TrackerAnswer) -> dict[str, bool]:
    try:
        await dispatcher.answer(task_id, data.answer)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/start", status_code=202)
async def start_open(task_id: int) -> dict[str, bool]:
    get_task(task_id)
    try:
        await dispatcher.start_open(task_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/restart", status_code=202)
async def restart(task_id: int, stage: int) -> dict[str, bool]:
    get_task(task_id)
    if stage not in (1, 2, 3, 4):
        raise HTTPException(400, "Некорректный этап")
    await dispatcher.restart(task_id, stage)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/approve", status_code=202)
async def approve(task_id: int, stage: int) -> dict[str, bool]:
    get_task(task_id)
    try:
        await dispatcher.force_approve(task_id, stage)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/merge")
async def manual_merge(task_id: int, data: ManualMerge) -> dict[str, bool]:
    try:
        await dispatcher.merged(task_id, data.pr_url)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@app.get("/api/tasks/{task_id}/logs")
async def logs(task_id: int, request: Request) -> StreamingResponse:
    get_task(task_id)

    async def stream():
        with connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 100", (task_id,)).fetchall()
        for row in reversed(rows):
            yield f"data: {json.dumps(dict(row), ensure_ascii=False)}\n\n"
        async with bus.subscribe(task_id) as queue:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {event}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    defaults = IntegrationSettings().model_dump()
    saved = integration_settings()
    values = {key: saved.get(key, value) for key, value in defaults.items()}
    tracker_webhook_url = f"{settings.public_url.rstrip('/')}/api/webhooks/tracker"
    if values["tracker_webhook_secret"]:
        tracker_webhook_url += "?" + urlencode({"token": values["tracker_webhook_secret"]})
    return {
        **values,
        **limits(),
        "git_webhook_url": f"{settings.public_url}/api/webhooks/git",
        "tracker_webhook_url": tracker_webhook_url,
        "worker_websocket_url": settings.public_url.replace("http", "ws", 1) + "/ws/worker",
    }


@app.put("/api/settings")
def save_settings(data: IntegrationSettings) -> dict[str, bool]:
    with connect() as conn:
        for key, value in data.model_dump().items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return {"ok": True}


@app.post("/api/integrations/yougile/test")
async def test_yougile() -> dict[str, bool]:
    try:
        await test_connection()
    except YougileError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True}


@app.post("/api/integrations/yougile/import")
async def import_yougile_tasks() -> dict[str, int]:
    saved = integration_settings()
    project = saved.get("yougile_project", "").strip()
    board = saved.get("yougile_board", "").strip()
    if not project or not board:
        raise HTTPException(400, "Укажите название проекта и имя доски Yougile")
    try:
        items = await fetch_import_tasks(project, board, saved.get("yougile_sticker", "").strip())
    except YougileError as exc:
        raise HTTPException(502, str(exc))
    imported = updated = skipped = 0
    team_url = (saved.get("yougile_team_url") or IntegrationSettings().yougile_team_url).rstrip("/")
    repository = saved.get("repository_url", "").strip()
    for item in items:
        issue_url = f"{team_url}/#{item['key']}" if item.get("key") else ""
        with transaction() as conn:
            existing = conn.execute("SELECT id, status FROM tasks WHERE external_issue_id=?", (item["id"],)).fetchone()
            timestamp = now()
            if existing and existing["status"] != "open":
                skipped += 1
                continue
            if existing:
                task_id = existing["id"]
                conn.execute(
                    "UPDATE tasks SET title=?, description=?, external_issue_key=?, external_issue_url=?, repository_url=CASE WHEN repository_url IS NULL OR repository_url='' THEN ? ELSE repository_url END, updated_at=? WHERE id=?",
                    (item["title"], item["description"], item.get("key", ""), issue_url, repository, timestamp, task_id),
                )
                updated += 1
            else:
                cursor = conn.execute(
                    "INSERT INTO tasks(title, description, external_issue_id, external_issue_key, external_issue_url, repository_url, current_stage, status, branch_name, stage_agents, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', 'pending', ?, ?, ?)",
                    (item["title"], item["description"], item["id"], item.get("key", ""), issue_url, repository, dumps({}), timestamp, timestamp),
                )
                task_id = cursor.lastrowid
                conn.execute("UPDATE tasks SET branch_name=? WHERE id=?", (branch_name(task_id, item.get("key")), task_id))
                imported += 1
        directory = task_dir(task_id)
        (directory / "00_task.md").write_text(f"# {item['title']}\n\n{item['description']}", encoding="utf-8")
        (directory / "00_yougile_chat.md").write_text(item["chat"], encoding="utf-8")
    return {"imported": imported, "updated": updated, "skipped": skipped, "total": len(items)}


@app.post("/api/integrations/yougile/webhook")
async def register_yougile_webhook() -> dict[str, str]:
    saved = integration_settings()
    secret = saved.get("tracker_webhook_secret") or settings.tracker_webhook_secret
    if not secret:
        raise HTTPException(400, "Укажите Tracker webhook secret")
    if not settings.public_url.startswith("https://"):
        raise HTTPException(400, "Для Yougile webhook нужен публичный HTTPS ORCHESTRATOR_PUBLIC_URL")
    callback_url = f"{settings.public_url.rstrip('/')}/api/webhooks/tracker?" + urlencode({"token": secret})
    try:
        webhook_id = await ensure_webhook(callback_url)
    except YougileError as exc:
        raise HTTPException(502, str(exc))
    with connect() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES ('yougile_webhook_id', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (webhook_id,))
    return {"id": webhook_id}


def verify_signature(secret: str, body: bytes, signature: str | None) -> None:
    if not secret:
        return
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(401, "Отсутствует подпись webhook")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Некорректная подпись webhook")


@app.post("/api/webhooks/git")
async def git_webhook(request: Request, x_hub_signature_256: str | None = Header(None), x_github_event: str | None = Header(None)) -> dict[str, Any]:
    body = await request.body()
    saved = integration_settings()
    verify_signature(saved.get("git_webhook_secret") or settings.git_webhook_secret, body, x_hub_signature_256)
    payload = await request.json()
    pr = payload.get("pull_request", {})
    if x_github_event not in (None, "pull_request") or not pr.get("merged") or pr.get("base", {}).get("ref") != "main":
        return {"accepted": False}
    branch = pr.get("head", {}).get("ref", "")
    url = pr.get("html_url")
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE branch_name=? OR pr_url=?", (branch, url)).fetchone()
    if not row:
        return {"accepted": False}
    await dispatcher.merged(row["id"], url)
    return {"accepted": True, "task_id": row["id"]}


@app.post("/api/webhooks/tracker")
async def tracker_webhook(request: Request) -> dict[str, Any]:
    saved = integration_settings()
    secret = saved.get("tracker_webhook_secret") or settings.tracker_webhook_secret
    if secret and not secrets.compare_digest(request.query_params.get("token", ""), secret):
        raise HTTPException(401, "Некорректный token webhook")
    payload = await request.json()
    if payload.get("event") != "chat_message-created":
        return {"accepted": False}
    data = payload.get("data", {})
    issue = str(data.get("chatId") or data.get("taskId") or "")
    answer = data.get("text")
    if not issue or not answer:
        raise HTTPException(400, "В webhook Yougile нужны data.chatId и data.text")
    if str(answer).startswith(BOT_PREFIX):
        return {"accepted": False}
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE external_issue_id=? AND status='waiting_answer'", (issue,)).fetchone()
    if not row:
        return {"accepted": False}
    await dispatcher.answer(row["id"], str(answer))
    return {"accepted": True, "task_id": row["id"]}


@app.websocket("/ws/worker")
async def worker_socket(websocket: WebSocket, authorization: str | None = Header(None)) -> None:
    saved = integration_settings()
    token = saved.get("worker_token") or settings.worker_token
    if authorization != f"Bearer {token}":
        await websocket.close(code=1008, reason="Unauthorized")
        return
    await websocket.accept()
    await hub.connect(websocket)
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "LOG_CHUNK":
                visible = present_log_line(str(message.get("chunk", "")))
                if visible.text:
                    await bus.publish(int(message["task_id"]), 3, visible.text)
            elif kind == "TASK_COMPLETED":
                await dispatcher.worker_completed(message)
            elif kind == "TASK_FAILED":
                await dispatcher.worker_failed(message)
            elif kind == "HEARTBEAT":
                await websocket.send_json({"type": "HEARTBEAT", "time": now()})
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(websocket)


app.mount("/avatars", StaticFiles(directory=settings.storage_dir / "avatars"), name="avatars")
frontend_dist = ROOT / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        candidate = frontend_dist / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend_dist / "index.html")
