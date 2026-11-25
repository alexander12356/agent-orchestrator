import html
from datetime import UTC, datetime
from typing import Any

import httpx

from .database import connect


BASE_URL = "https://yougile.com/api-v2"
BOT_PREFIX = "[Agent Orchestrator]"


class YougileError(RuntimeError):
    pass


def integration_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def headers(config: dict[str, str]) -> dict[str, str]:
    token = config.get("tracker_api_token", "")
    if not token:
        raise YougileError("Укажите API key Yougile в настройках")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if response.status_code in (401, 403):
            raise YougileError("Yougile отклонил API key или у него недостаточно прав") from exc
        raise YougileError(f"Yougile API вернул ошибку HTTP {response.status_code}") from exc


async def publish_questions(issue_id: str, questions: list[str]) -> int:
    config = integration_settings()
    body = BOT_PREFIX + "\nВопросы по технической документации:\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    body += '\n\nОтправьте все ответы одним сообщением через «Ответить на это сообщение».'
    text_html = f"<p>{html.escape(body).replace(chr(10), '<br>')}</p>"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{BASE_URL}/chats/{issue_id}/messages",
                headers=headers(config),
                json={"text": body, "textHtml": text_html},
            )
            await raise_for_status(response)
            message_id = response.json()["id"]
            if type(message_id) is not int or message_id <= 0:
                raise YougileError("Yougile вернул некорректный ID сообщения с вопросами")
            return message_id
    except (ValueError, KeyError, TypeError) as exc:
        raise YougileError("Yougile не вернул ID сообщения с вопросами") from exc
    except httpx.HTTPError as exc:
        raise YougileError("Не удалось подключиться к Yougile API") from exc


async def fetch_chat_message(issue_id: str, message_id: int) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{BASE_URL}/chats/{issue_id}/messages/{message_id}",
                headers=headers(integration_settings()),
            )
            if response.status_code == 404:
                return {}
            await raise_for_status(response)
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise YougileError("Не удалось прочитать сообщение Yougile") from exc


async def test_connection() -> None:
    config = integration_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{BASE_URL}/users", headers=headers(config), params={"limit": 1})
            await raise_for_status(response)
    except httpx.HTTPError as exc:
        raise YougileError("Не удалось подключиться к Yougile API") from exc


async def ensure_webhook(callback_url: str) -> str:
    config = integration_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{BASE_URL}/webhooks", headers=headers(config))
            await raise_for_status(response)
            payload = response.json()
            webhooks = payload.get("content", []) if isinstance(payload, dict) else payload
            for webhook in webhooks:
                if webhook.get("url") == callback_url:
                    return str(webhook["id"])
            response = await client.post(
                f"{BASE_URL}/webhooks",
                headers=headers(config),
                json={"url": callback_url, "event": "chat_message-created"},
            )
            await raise_for_status(response)
            return str(response.json()["id"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise YougileError("Не удалось зарегистрировать webhook Yougile") from exc


async def get_all(client: httpx.AsyncClient, path: str, config: dict[str, str], params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = await client.get(f"{BASE_URL}{path}", headers=headers(config), params={**(params or {}), "limit": 1000, "offset": offset})
        await raise_for_status(response)
        payload = response.json()
        content = payload.get("content", []) if isinstance(payload, dict) else payload
        items.extend(content)
        paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
        if not paging.get("next") or not content:
            return items
        offset += len(content)


async def find_reply(issue_id: str, question_id: int) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            messages = await get_all(client, f"/chats/{issue_id}/messages", integration_settings(), {"since": question_id})
    except (httpx.HTTPError, ValueError) as exc:
        raise YougileError("Не удалось прочитать ответы в YouGile") from exc
    for message in sorted(messages, key=lambda item: item["id"]):
        text = message.get("text")
        if (
            message["id"] > question_id
            and not message.get("deleted")
            and (message.get("properties") or {}).get("replyTo") == question_id
            and isinstance(text, str)
            and text.strip()
            and not text.startswith(BOT_PREFIX)
        ):
            return text
    return None


def named_item(items: list[dict[str, Any]], title: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item.get("title", "").casefold() == title.casefold():
            return item
    raise YougileError(f"{kind} «{title}» не найден в Yougile")


def format_chat(messages: list[dict[str, Any]], users: dict[str, str]) -> str:
    lines = ["# Чат Yougile"]
    for message in sorted(messages, key=lambda item: item.get("id", 0)):
        author_id = str(message.get("fromUserId", ""))
        author = users.get(author_id, author_id or "Неизвестный автор")
        timestamp = message.get("id")
        moment = datetime.fromtimestamp(timestamp / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC") if timestamp else ""
        lines.extend(["", f"## {author} · {moment}", "", str(message.get("text", ""))])
    return "\n".join(lines) + "\n"


async def fetch_import_tasks(project_name: str, board_name: str, sticker_filter: str = "") -> list[dict[str, str]]:
    config = integration_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            projects = await get_all(client, "/projects", config, {"title": project_name})
            project = named_item(projects, project_name, "Проект")
            boards = await get_all(client, "/boards", config, {"projectId": project["id"], "title": board_name})
            board = named_item(boards, board_name, "Доска")
            columns = await get_all(client, "/columns", config, {"boardId": board["id"]})
            selected = [column for column in columns if column.get("title", "").casefold() in ("open", "ready")]
            if not selected:
                raise YougileError("На доске нет колонок Open и Ready")
            sticker_id = state_id = None
            if sticker_filter.strip():
                parts = [part.strip() for part in sticker_filter.split(".", 1)]
                if len(parts) != 2 or not all(parts):
                    raise YougileError("Стикер указывается в формате Название.Состояние, например Development.Код")
                sticker_name, state_name = parts
                stickers = await get_all(client, "/string-stickers", config, {"boardId": board["id"], "name": sticker_name})
                sticker = next((item for item in stickers if item.get("name", "").casefold() == sticker_name.casefold()), None)
                if not sticker:
                    raise YougileError(f"Стикер «{sticker_name}» не найден на доске")
                state = next((item for item in sticker.get("states", []) if item.get("name", "").casefold() == state_name.casefold()), None)
                if not state:
                    raise YougileError(f"Состояние «{state_name}» не найдено у стикера «{sticker_name}»")
                sticker_id, state_id = sticker["id"], state["id"]
            users_list = await get_all(client, "/users", config)
            users = {
                str(user["id"]): str(user.get("realName") or user.get("name") or user.get("email") or user["id"])
                for user in users_list
            }
            result = []
            for column in selected:
                tasks = await get_all(client, "/tasks", config, {"columnId": column["id"]})
                for task in tasks:
                    if sticker_id:
                        value = (task.get("stickers") or {}).get(sticker_id)
                        states = value if isinstance(value, list) else [value]
                        if state_id not in states:
                            continue
                    if task.get("archived") or task.get("deleted"):
                        continue
                    messages = await get_all(client, f"/chats/{task['id']}/messages", config)
                    result.append({
                        "id": str(task["id"]),
                        "key": str(task.get("idTaskProject") or task.get("idTaskCommon") or ""),
                        "title": str(task["title"]),
                        "description": str(task.get("description") or ""),
                        "chat": format_chat(messages, users),
                    })
            return result
    except httpx.HTTPError as exc:
        raise YougileError("Не удалось импортировать задачи из Yougile") from exc
