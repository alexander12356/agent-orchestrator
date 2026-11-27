"""Bound the output of a repository search before it reaches the agent."""

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


MAX_LINES = 30
MAX_BYTES = 3000
ZCODE_SEARCH_ARGS = ("--disallowed-tools", "Glob,Grep")


def search_command() -> str:
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        python = sys.executable.replace("'", "''")
        script = script.replace("'", "''")
        return f"& '{python}' '{script}'"
    return shlex.join([sys.executable, script])


@contextmanager
def cursor_search_hook(cwd: Path) -> Iterator[None]:
    directory = cwd / ".cursor"
    path = directory / "hooks.json"
    original = path.read_bytes() if path.exists() else None
    config = json.loads(original) if original is not None else {"version": 1}
    hooks = config.setdefault("hooks", {}).setdefault("preToolUse", [])
    hook = {"matcher": "^(Glob|Grep)$", "command": f"{search_command()} --cursor-hook"}
    if hook in hooks:
        yield
        return
    hooks.insert(0, hook)
    directory.mkdir(exist_ok=True)
    configured = json.dumps(config, ensure_ascii=False).encode("utf-8")
    path.write_bytes(configured)
    try:
        yield
    finally:
        if path.exists() and path.read_bytes() == configured:
            if original is None:
                path.unlink()
                with suppress(OSError):
                    directory.rmdir()
            else:
                path.write_bytes(original)


def search_guidance() -> str:
    command = search_command()
    return (
        "Начни с ТЗ. При доработке прочитай затронутые разделы техдокумента и замечания ревью. "
        "Возьми из них имена классов, "
        "сцен, префабов, других сущностей и известные пути. Сначала ищи по имени или символу "
        "в профильном каталоге и ограничь поиск нужным типом файла. "
        "Если совпадений нет, постепенно расширяй область вплоть до всего Assets.\n"
        f"Для поиска файлов (Glob) и содержимого (Grep) запускай команду через `{command}`, "
        f"например `{command} rg --files -g '*Имя*.prefab' <каталог>` или "
        f"`{command} rg -n -g '*.cs' -F 'ИмяСимвола' <каталог>`. "
        "Подставляй фактические имена и пути, заключай пути с пробелами в кавычки. "
        f"Выдача ограничена {MAX_LINES} строками и {MAX_BYTES} байтами. "
        "Если есть дополнительные результаты, уточни имя, тип файла или каталог.\n"
        "Если rg недоступен, передай той же команде git -C <checkout> ls-files "
        "--cached --others --exclude-standard или git -C <checkout> grep --untracked -n -F 'ИмяСимвола' "
        "с ограничением путей после --. "
        "Для сериализованной ссылки прочитай GUID из .cs.meta и передай ограничителю "
        "git -C <checkout> grep -l -F '<GUID>' -- '*.prefab' '*.unity' '*.asset', "
        "сузив пути до профильного каталога. Открой нужный фрагмент и проверь поле ссылки."
    )


def main(command: list[str]) -> int:
    if command == ["--cursor-hook"]:
        if os.environ.get("ORCHESTRATOR_STAGE") == "1" and os.environ.get("ORCHESTRATOR_REPO"):
            print(json.dumps({
                "permission": "deny",
                "user_message": "На этапе 1 используй команду поиска с ограниченной выдачей из задания.",
                "agent_message": f"Для поиска используй {search_command()} с rg или git.",
            }))
        else:
            print('{"permission": "allow"}')
        return 0
    if not command or command[0] not in ("rg", "git"):
        print("Usage: search.py rg <args> | git <args>", file=sys.stderr)
        return 2
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as error:
        print(error, file=sys.stderr)
        return 2
    with process:
        output = process.stdout.read(MAX_BYTES + 1)
        lines = output[:MAX_BYTES].splitlines(keepends=True)
        truncated = len(output) > MAX_BYTES or len(lines) > MAX_LINES
        if truncated:
            process.kill()
        else:
            status = process.wait()
    sys.stdout.buffer.write(b"".join(lines[:MAX_LINES]).decode("utf-8", errors="ignore").encode("utf-8"))
    if truncated:
        sys.stdout.buffer.write("\nЕсть дополнительные результаты. Уточни запрос или каталог.\n".encode("utf-8"))
        return 0
    if status == 1 and not output:
        sys.stdout.buffer.write("Совпадений нет.\n".encode("utf-8"))
        return 0
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
