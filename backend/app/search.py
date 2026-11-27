"""Bound the output of a repository search before it reaches the agent."""

import os
import shlex
import subprocess
import sys
from pathlib import Path


MAX_LINES = 30
MAX_BYTES = 3000
ZCODE_SEARCH_ARGS = ("--disallowed-tools", "Glob,Grep")


def search_guidance() -> str:
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        python = sys.executable.replace("'", "''")
        script = script.replace("'", "''")
        command = f"& '{python}' '{script}'"
    else:
        command = shlex.join([sys.executable, script])
    return (
        "Начни с ТЗ и утверждённого техдокумента, если он есть. Возьми из них имена классов, "
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
