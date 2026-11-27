import json


PROGRESS_PREFIX = "Прогресс:"
TOOL_PROGRESS_PROMPT = (
    "\n\nLive Logs автоматически показывают действия инструментов. "
    "Используй TodoList по необходимости. "
    f"О блокере или важном решении можешь сообщить короткой строкой «{PROGRESS_PREFIX} …» "
    "в обычном сообщении."
)

CURSOR_ACTIONS = {
    "readToolCall": "Читаю",
    "writeToolCall": "Записываю",
    "editToolCall": "Изменяю",
    "grepToolCall": "Ищу по содержимому",
    "globToolCall": "Ищу файлы",
    "lsToolCall": "Просматриваю каталог",
    "shellToolCall": "Выполняю команду",
}
KIMI_ACTIONS = {
    "Read": "Читаю",
    "Write": "Записываю",
    "Edit": "Изменяю",
    "Grep": "Ищу по содержимому",
    "Glob": "Ищу файлы",
    "Bash": "Выполняю команду",
}


def tool_message(action: str, args: object) -> str:
    path = args.get("path") if isinstance(args, dict) else None
    if isinstance(path, str) and (path := " ".join(path.splitlines()).strip()):
        return f"{action}: {path[:1000]}\n"
    return f"{action}\n"


def public_messages(content: object) -> list[str]:
    if not isinstance(content, str):
        return []
    messages = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(PROGRESS_PREFIX) and (body := line.removeprefix(PROGRESS_PREFIX).strip()):
            messages.append(f"{body[:1000]}\n")
    return messages


def live_log_messages(line: str, cli: str) -> list[str]:
    if not line.lstrip().startswith("{"):
        return []
    try:
        event = json.loads(line)
    except ValueError:
        return []
    if not isinstance(event, dict):
        return []
    if cli == "cursor":
        return cursor_messages(event)
    if cli == "kimi" and event.get("role") == "assistant":
        calls = event.get("tool_calls")
        messages = public_messages(event.get("content"))
        if isinstance(calls, list):
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                name, arguments = function.get("name"), function.get("arguments")
                if not isinstance(name, str) or name not in KIMI_ACTIONS or not isinstance(arguments, str):
                    continue
                try:
                    args = json.loads(arguments)
                except ValueError:
                    continue
                messages.append(tool_message(KIMI_ACTIONS[name], args))
        return messages
    return []


def cursor_messages(event: dict) -> list[str]:
    if event.get("type") == "tool_call" and event.get("subtype") == "started":
        call = event.get("tool_call")
        if isinstance(call, dict):
            for name, action in CURSOR_ACTIONS.items():
                if isinstance(details := call.get(name), dict):
                    return [tool_message(action, details.get("args"))]
    if event.get("type") != "assistant":
        return []
    # Use the full assistant message rather than its timestamped text deltas.
    if "timestamp_ms" in event and "model_call_id" not in event:
        return []
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        text for block in content if isinstance(block, dict) and block.get("type") == "text"
        for text in public_messages(block.get("text"))
    ]
