import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import AsyncIterator, Awaitable, Callable


@asynccontextmanager
async def progress_prompt(
    prompt: str, cwd: Path, publish: Callable[[str], Awaitable[None]] | None,
) -> AsyncIterator[str]:
    if publish is None:
        yield prompt
        return

    with TemporaryDirectory(prefix=".orchestrator-progress-", dir=cwd) as directory:
        path = Path(directory) / "updates.txt"
        path.touch()
        stop = asyncio.Event()
        reader = asyncio.create_task(publish_updates(path, stop, publish))
        try:
            location = f"`{path.parent.name}/{path.name}`"
            yield (
                f"{prompt}\n\nДля Live Logs дописывай короткие публичные комментарии в "
                f"{location} относительно стартового каталога. "
                "Это служебный файл прогресса в дополнение к выходным файлам этапа. "
                "Перед первой проверкой запиши, что проверишь и зачем. В процессе сообщай о важных находках, "
                "принятых решениях и их причинах, а также о результатах проверок. "
                "Пиши по-русски, одно короткое сообщение на строку в UTF-8. Добавляй строки сразу по ходу работы. "
                "Оставляй только то, что полезно человеку, следящему за задачей."
            )
        finally:
            stop.set()
            await reader


async def publish_updates(
    path: Path, stop: asyncio.Event, publish: Callable[[str], Awaitable[None]],
) -> None:
    offset = 0
    limit = 64 * 1024
    while offset < limit:
        finished = stop.is_set()
        try:
            with path.open("rb") as file:
                file.seek(offset)
                data = file.read(limit - offset)
        except FileNotFoundError:
            data = b""
        # Keep an unfinished line for the next read, including split UTF-8 characters.
        end = len(data) if finished and offset + len(data) < limit else data.rfind(b"\n") + 1
        for raw in data[:end].split(b"\n"):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                await publish(f"{line[:1000]}\n")
        offset += end
        if finished or offset + len(data) - end >= limit:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except TimeoutError:
            pass
