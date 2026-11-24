import html

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


async def publish_questions(issue_id: str, questions: list[str]) -> None:
    config = integration_settings()
    body = BOT_PREFIX + "\nВопросы по технической документации:\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    text_html = f"<p>{html.escape(body).replace(chr(10), '<br>')}</p>"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{BASE_URL}/chats/{issue_id}/messages",
                headers=headers(config),
                json={"text": body, "textHtml": text_html},
            )
            await raise_for_status(response)
    except httpx.HTTPError as exc:
        raise YougileError("Не удалось подключиться к Yougile API") from exc


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
