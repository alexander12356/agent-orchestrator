import asyncio
import io
import json

import pytest

from app.config import settings
from app.dispatcher import Dispatcher
from conftest import make_task


@pytest.mark.parametrize("accepted", [True, False])
def test_human_hold_notifies_once_even_if_delivery_fails(client, agent, tmp_path, monkeypatch, caplog, accepted):
    task = make_task(client, agent, title="Проверка документации")
    credentials = tmp_path / "telegram.json"
    credentials.write_text(json.dumps({"botToken": "1:test", "chatId": 123}), encoding="utf-8")
    monkeypatch.setattr(settings, "telegram_credentials_file", credentials)
    messages = []

    def telegram(request, timeout):
        payload = json.loads(request.data)
        assert payload["chat_id"] == "123"
        messages.append(payload["text"])
        return io.BytesIO(json.dumps({"ok": accepted}).encode("utf-8"))

    monkeypatch.setattr("app.telegram.urlopen", telegram)

    async def hold():
        dispatcher = Dispatcher()
        for _ in range(2):
            await dispatcher._hold(task["id"], 1, "Бюджет исчерпан.")
        await asyncio.gather(*dispatcher.pending)

    asyncio.run(hold())
    message, = messages
    assert f"задача #{task['id']}" in message
    assert "Проверка документации" in message
    assert "Бюджет исчерпан." in message
    assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "needs_human"
    assert ("Telegram notification failed" in caplog.text) == (not accepted)
