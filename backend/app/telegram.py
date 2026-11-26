import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.request import Request, urlopen

from .config import settings
from .database import connect


logger = logging.getLogger(__name__)

ACTIONS = {
    "waiting_answer": "Нужно ответить на вопросы по технической документации.",
    "needs_human": "Нужно решение человека.",
    "error": "Этап остановился с ошибкой. Откройте логи задачи и решите, как продолжить.",
    "stage_ready": "Одиночный этап завершён. Следующий этап можно запустить вручную.",
    "waiting_pr": "Проверьте pull request и выполните merge, если всё готово.",
}


def send_message(credentials_file: Path, message: str) -> None:
    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    token = credentials["botToken"]
    chat_id = str(credentials["chatId"])
    if not re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", token) or not re.fullmatch(r"-?[0-9]+", chat_id):
        raise ValueError("Invalid Telegram credentials")
    body = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "link_preview_options": {"is_disabled": True},
    }).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        if json.load(response)["ok"] is not True:
            raise ValueError("Telegram rejected the message")


async def notify_decision(task_id: int, status: str) -> None:
    credentials_file = settings.telegram_credentials_file
    if not credentials_file or status not in ACTIONS:
        return
    with connect() as conn:
        task = conn.execute(
            """SELECT title, current_stage, hold_reason, external_issue_key, external_issue_url, pr_url
               FROM tasks WHERE id=? AND status=?""",
            (task_id, status),
        ).fetchone()
    if not task:
        return

    label = f"{task['external_issue_key']} · " if task["external_issue_key"] else ""
    lines = [
        f"Agent Orchestrator · {label}задача #{task_id}, этап {task['current_stage']}",
        task["title"][:500],
        ACTIONS[status],
    ]
    if status == "needs_human" and task["hold_reason"]:
        lines.append(task["hold_reason"][:600])
    if status == "waiting_answer":
        if task["external_issue_url"]:
            lines.append(task["external_issue_url"][:500])
        lines.append("Откройте вопросы в инспекторе задачи. Если они опубликованы в YouGile, можно ответить там.")
    if status == "waiting_pr" and task["pr_url"]:
        lines.append(task["pr_url"][:500])

    try:
        await asyncio.to_thread(send_message, credentials_file, "\n".join(lines))
    except (OSError, ValueError, KeyError, TypeError):
        # Network exceptions may include the URL containing the bot token.
        logger.warning("Telegram notification failed for task #%s (%s)", task_id, status)
