import httpx

from .database import connect


def integration_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


async def publish_questions(issue_id: str, questions: list[str]) -> None:
    config = integration_settings()
    base = config.get("tracker_base_url", "")
    token = config.get("tracker_api_token", "")
    provider = config.get("tracker_provider", "github")
    body = "Вопросы по технической документации:\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        if provider == "github":
            if "/repos/" in base:
                url = f"{base}/issues/{issue_id}/comments"
            else:
                project = config.get("tracker_project", "")
                url = f"{base}/repos/{project}/issues/{issue_id}/comments"
            response = await client.post(url, headers=headers, json={"body": body})
        else:
            url = f"{base}/issues/{issue_id}/comments"
            response = await client.post(url, headers=headers, json={"body": body, "text": body, "comment": body})
        response.raise_for_status()
