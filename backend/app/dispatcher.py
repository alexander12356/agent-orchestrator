import asyncio
import contextlib
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .config import settings
from .database import connect, decode_agent, decode_task, now, spent_tokens, transaction
from .events import bus
from .repo import RepoError, prepare_checkout, repository_url
from .runner import ProcessRunner, RunResult, signal_cli
from .schemas import IntegrationSettings
from .tracker import find_reply, integration_settings, publish_questions
from .worker_hub import hub


STAGE_FILES = {
    1: "01_tech_doc_v{attempt}.md",
    2: "02_doc_review_v{attempt}.json",
    3: "03_impl_report_v{attempt}.md",
    4: "04_code_review_v{attempt}.json",
}
VERDICT_CONTRACT = '{"status": "APPROVED"|"REJECTED", "summary": string, "comments": string[], "notes": string[]}'
VERSION_RE = re.compile(r"_v(\d+)\.")


class StageFailure(RuntimeError):
    """A server stage that did not produce a usable result.

    retryable marks failures that another run of the same stage, told what went wrong,
    can plausibly fix: a missing or malformed result file, a CLI that exited with an error.
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def task_dir(task_id: int) -> Path:
    path = settings.storage_dir / "tasks" / str(task_id)
    (path / "logs").mkdir(parents=True, exist_ok=True)
    return path


def numbered_versions(directory: Path, pattern: str) -> list[Path]:
    """Return revisions ordered numerically, so v10 follows v9."""
    revisions = [path for path in directory.glob(pattern) if VERSION_RE.search(path.name)]
    return sorted(revisions, key=lambda path: int(VERSION_RE.search(path.name)[1]))


def latest_version(directory: Path, pattern: str) -> Path | None:
    revisions = numbered_versions(directory, pattern)
    return revisions[-1] if revisions else None


def latest_document(task_id: int, directory: Path) -> Path | None:
    with connect() as conn:
        unfinished = {
            STAGE_FILES[1].format(attempt=row["attempt"])
            for row in conn.execute(
                "SELECT attempt FROM stage_runs WHERE task_id=? AND stage=1 AND status <> 'completed'",
                (task_id,),
            )
        }
    return next(
        (path for path in reversed(numbered_versions(directory, "01_tech_doc_v*.md")) if path.name not in unfinished),
        None,
    )


def document_review(task_id: int, doc: Path) -> dict[str, Any] | None:
    with connect() as conn:
        reviews = conn.execute(
            """SELECT review.attempt, review.reviewed_document, (
                   SELECT doc.attempt FROM stage_runs doc
                   WHERE doc.task_id=review.task_id AND doc.stage=1 AND doc.status='completed' AND doc.id < review.id
                   ORDER BY doc.id DESC LIMIT 1
               ) AS legacy_document
               FROM stage_runs review
               WHERE review.task_id=? AND review.stage=2 AND review.status='completed'
               ORDER BY review.id DESC""",
            (task_id,),
        ).fetchall()
    for review in reviews:
        target = review["reviewed_document"]
        if target is None and review["legacy_document"] is not None:
            target = STAGE_FILES[1].format(attempt=review["legacy_document"])
        if target == doc.name:
            try:
                return read_verdict(doc.parent / STAGE_FILES[2].format(attempt=review["attempt"]))
            except StageFailure:
                continue
    return None


def limits() -> dict[str, int]:
    """Stage limits from the settings; a missing or malformed value gives the default."""
    saved = integration_settings()
    defaults = IntegrationSettings()
    result = {}
    for key, top in (("max_auto_retries", 10), ("max_returns", 50), ("task_token_budget", 0)):
        default = getattr(defaults, key)
        try:
            value = int(saved[key]) if saved.get(key) not in (None, "") else default
        except (TypeError, ValueError):
            value = default
        result[key] = min(max(value, 0), top) if top else max(value, 0)
    return result


def token_budget(task: dict[str, Any], current_limits: dict[str, int]) -> int:
    """The token budget in force for a task: its own, else the shared one. 0 is no limit."""
    own = task.get("token_budget")
    return int(own) if own is not None else current_limits["task_token_budget"]


def describe_failure(result: RunResult, artifact_name: str) -> str:
    """Why a CLI run left no usable result, followed by the end of its output."""
    tail = result.output[-2000:].strip()
    if result.exit_code == 127:
        return tail or "CLI не найден"
    if result.timed_out:
        reason = "Таймаут выполнения CLI"
    elif result.rate_limited:
        reason = "Исчерпан лимит подписки (rate limit)"
    elif result.exit_code != 0:
        reason = f"CLI завершился с кодом {result.exit_code}"
    else:
        reason = f"Артефакт {artifact_name} не создан"
    return f"{reason}\n\n{tail}" if tail else reason


def read_verdict(path: Path) -> dict[str, Any]:
    """A review verdict that follows the contract; anything else is a retryable failure."""
    invalid = f"Вердикт {path.name} не соответствует контракту {VERDICT_CONTRACT}: "
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageFailure(f"Файл {path.name} не читается как JSON: {exc}", retryable=True) from exc
    if not isinstance(data, dict) or data.get("status") not in ("APPROVED", "REJECTED"):
        raise StageFailure(invalid + "status должен быть APPROVED или REJECTED", retryable=True)
    for key in ("comments", "notes"):
        items = data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise StageFailure(invalid + f"{key} должен быть списком строк", retryable=True)
    if data["status"] == "REJECTED" and not data.get("comments"):
        raise StageFailure(invalid + "при REJECTED нужны блокирующие замечания в comments", retryable=True)
    return data


def read_questions(path: Path) -> dict[str, Any]:
    """The spec questions of a tech doc stage; a missing file means there are none."""
    if not path.exists():
        return {"has_questions": False, "questions": []}
    invalid = f'Файл {path.name} не соответствует контракту {{"has_questions": boolean, "questions": string[]}}: '
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageFailure(f"Файл {path.name} не читается как JSON: {exc}", retryable=True) from exc
    questions = data.get("questions", []) if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("has_questions"), bool)
        or not isinstance(questions, list)
        or not all(isinstance(item, str) for item in questions)
        or (data["has_questions"] and not questions)
    ):
        raise StageFailure(invalid + "нужны has_questions и, когда он true, непустой список questions", retryable=True)
    return data


class Dispatcher:
    def __init__(self) -> None:
        self.active: dict[int, int] = {}
        self.pending: set[asyncio.Task[Any]] = set()
        self.runs: dict[int, asyncio.Task[Any]] = {}
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.stop_requested: set[int] = set()
        self.lock = asyncio.Lock()

    def recover(self) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE stage_runs SET status='error', error=?, finished_at=? WHERE status='running'",
                ("Сессия прервана: сервер был перезапущен", now()),
            )
            conn.execute("UPDATE tasks SET status='queued', updated_at=? WHERE status='running'", (now(),))

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def schedule(self) -> None:
        if not settings.auto_start:
            return
        async with self.lock:
            with connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status IN ('queued', 'waiting_worker') ORDER BY id"
                ).fetchall()
                tasks = [decode_task(row) for row in rows]
                agents = {row["id"]: decode_agent(row) for row in conn.execute("SELECT * FROM agents")}
            for task in tasks:
                stage = task["current_stage"]
                if task["id"] in self.active or task["id"] in self.stop_requested:
                    continue
                agent_id = task["stage_agents"].get(str(stage), task["stage_agents"].get(stage))
                agent = agents.get(agent_id)
                if not agent or sum(1 for active_agent in self.active.values() if active_agent == agent_id) >= agent["concurrency_limit"]:
                    continue
                if stage == 3 and not hub.online:
                    with connect() as conn:
                        conn.execute("UPDATE tasks SET status='waiting_worker', updated_at=? WHERE id=?", (now(), task["id"]))
                    continue
                if stage not in (1, 2, 3, 4):
                    continue
                self.active[task["id"]] = agent_id
                self.spawn(self.run_stage(task["id"], stage))

    async def start(self, task_id: int) -> None:
        with connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            task = decode_task(row) if row else None
            if not task or task["status"] not in ("queued", "waiting_worker"):
                return
        if settings.auto_start:
            await self.schedule()
        elif task_id not in self.active:
            self.active[task_id] = task["stage_agents"].get(str(task["current_stage"]), task["stage_agents"].get(task["current_stage"]))
            self.spawn(self.run_stage(task_id, task["current_stage"]))

    async def run_stage(self, task_id: int, stage: int) -> None:
        run_id: int | None = None
        self.runs[task_id] = asyncio.current_task()
        try:
            current_limits = limits()
            with transaction() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    return
                task = decode_task(row)
                budget = token_budget(task, current_limits)
                spent = spent_tokens(conn, task_id)
                if not budget or spent <= budget:
                    agent_id = task["stage_agents"].get(str(stage), task["stage_agents"].get(stage))
                    agent = decode_agent(conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())
                    attempt = conn.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 AS n FROM stage_runs WHERE task_id=? AND stage=?",
                        (task_id, stage),
                    ).fetchone()["n"]
                    started = now()
                    cursor = conn.execute(
                        "INSERT INTO stage_runs(task_id, stage, attempt, agent_id, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                        (task_id, stage, attempt, agent_id, started),
                    )
                    run_id = cursor.lastrowid
                    conn.execute("UPDATE tasks SET status='running', current_stage=?, updated_at=? WHERE id=?", (stage, started, task_id))
            if not run_id:
                await self._hold(task_id, stage, f"Бюджет токенов исчерпан: {spent} из {budget}. Увеличьте бюджет задачи или отключите его, затем нажмите «Продолжить».")
                return
            directory = task_dir(task_id)
            await bus.publish(task_id, stage, f"Этап {stage}, попытка {attempt}: запуск {agent['name']}\n")
            if task_id in self.stop_requested:
                return
            if stage == 3:
                await self._run_worker(task, agent, attempt, run_id)
            else:
                await self._run_server(task, agent, stage, attempt, run_id, directory)
        except asyncio.CancelledError:
            self._mark_stopped(run_id)
            raise
        except Exception as exc:
            if task_id in self.stop_requested:
                self._mark_stopped(run_id)
            elif run_id:
                await self._fail(task_id, stage, run_id, exc)
        finally:
            self.active.pop(task_id, None)
            self.processes.pop(task_id, None)
            self.runs.pop(task_id, None)
            if task_id not in self.stop_requested:
                await self.schedule()

    def _mark_stopped(self, run_id: int | None) -> None:
        if not run_id:
            return
        with connect() as conn:
            conn.execute(
                "UPDATE stage_runs SET status='error', error=?, finished_at=? WHERE id=? AND status='running'",
                ("Остановлено перезапуском", now(), run_id),
            )

    async def _hold(self, task_id: int, stage: int, reason: str) -> None:
        """Stop a task until a person decides; «Продолжить», restart and approve release it."""
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='needs_human', hold_reason=?, updated_at=? WHERE id=?", (reason, now(), task_id))
        await bus.publish(task_id, stage, f"Нужно решение человека: {reason}\n")

    async def _fail(self, task_id: int, stage: int, run_id: int, exc: Exception) -> None:
        message = str(exc)
        current_limits = limits()
        retried = 0
        with transaction() as conn:
            conn.execute(
                "UPDATE stage_runs SET status='error', error=?, finished_at=COALESCE(finished_at, ?) WHERE id=?",
                (message, now(), run_id),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            task = decode_task(row)
            # A failure of a run the user has already moved past (approve, restart, merge)
            # stays on its run row; it must not change the task or relaunch anything.
            if not (task["status"] == "running" and task["current_stage"] == stage):
                return
            budget = token_budget(task, current_limits)
            if (
                isinstance(exc, StageFailure)
                and exc.retryable
                and (not budget or spent_tokens(conn, task_id) <= budget)
                and task["auto_retries"] < current_limits["max_auto_retries"]
            ):
                retried = task["auto_retries"] + 1
                conn.execute("UPDATE tasks SET auto_retries=?, status='queued', updated_at=? WHERE id=?", (retried, now(), task_id))
            else:
                conn.execute("UPDATE tasks SET status='error', updated_at=? WHERE id=?", (now(), task_id))
        if retried:
            await bus.publish(task_id, stage, f"Этап {stage} не завершён: {message}\nАвтоповтор {retried} из {current_limits['max_auto_retries']}.\n")
        else:
            await bus.publish(task_id, stage, f"Ошибка: {message}\n")

    def _prompt(self, task: dict[str, Any], agent: dict[str, Any], stage: int, attempt: int, *,
                checkout: tuple[Path, str, str] | None = None, previous_error: str = "",
                doc: Path | None = None, review: dict[str, Any] | None = None, draft: Path | None = None) -> str:
        directory = task_dir(task["id"])
        artifact = directory / STAGE_FILES[stage].format(attempt=attempt)
        inputs = [f"ТЗ: {directory / '00_task.md'}"]
        chat = directory / "00_yougile_chat.md"
        if chat.is_file():
            inputs.append(f"Чат задачи: {chat}")
        if checkout:
            repository, ref, commit = checkout
            inputs.append(
                f"Локальный checkout репозитория: {repository}\n"
                f"Ветка {ref}, коммит {commit}. Используй этот каталог только для чтения. "
                + (
                    "Открывай исходники для проверки контракта, затронутого правкой."
                    if draft else
                    "Самостоятельно найди в нём исходники, тесты и данные для задачи. "
                    "Проверяй контракты по файлам выбранного коммита перед тем, как задавать вопросы о недостающих сведениях."
                )
            )
        inputs.append(
            "Используй указанные артефакты. Отметь недостающие сведения. "
            "Не открывай GitHub, другие каталоги сервера, чужие задачи и базу."
        )
        doc = doc or latest_document(task["id"], directory)
        questions_file = (draft.parent if draft else directory) / f"01_questions_v{attempt}.json"
        answers = numbered_versions(directory, "01_answers_v*.md")
        if answers:
            inputs.append("Согласованные ответы в порядке версий. Если поздний ответ уточняет тот же вопрос, используй уточнение.")
        for answer in answers:
            version = VERSION_RE.search(answer.name)[1]
            questions = read_questions(directory / f"01_questions_v{version}.json")["questions"]
            numbered = "\n".join(f"{i}. {question}" for i, question in enumerate(questions, start=1))
            inputs.append(
                f"Вопросы v{version}:\n{numbered or 'Исходные вопросы отсутствуют.'}\n"
                f"Ответ v{version}:\n{answer.read_text(encoding='utf-8').strip()}"
            )
        if stage == 1:
            if review and review["status"] == "REJECTED":
                inputs.append(
                    "Исправь блокеры ревью этой версии. Сохрани согласованное поведение.\n"
                    + "\n".join(f"- {comment}" for comment in review["comments"])
                )
            if doc and not draft and (review or answers):
                inputs.append(
                    f"Предыдущая версия: {doc}. Сохрани согласованное поведение. "
                    "Исправь затронутые места и примени правила детализации ко всему документу."
                )
            if draft:
                inputs.append(
                    f"Рабочая копия техдокумента: {draft}. Внеси точечные правки по новому ответу "
                    "или указанным блокерам. Сохрани согласованное поведение и текст, соответствующий правилам детализации. "
                    "Если изменилась связь или развилка, сверяй её по исходникам и обнови связанные места "
                    "в схеме и сценариях. Для правки используй редактирование отдельных фрагментов рабочей копии. "
                    "Запиши JSON с вопросами после завершения правок, даже если документ уже учитывает ответ."
                )
                output = "Сохрани три раздела в этом порядке:"
            else:
                output = f"Запиши полный техдокумент в {artifact}. Используй три раздела в таком порядке:"
            result = (
                f"{output}\n"
                "Правила детализации действуют во всех разделах. Из прежней версии переноси согласованные "
                "сущности, связи и наблюдаемое поведение. Поля, подписки, служебные вызовы, покадровые переходы "
                "и устройство тестов оставь этапу реализации. Если такие подробности уже есть, удали их "
                "из всего документа, включая разделы вне текущей правки. Согласованное требование из удаляемого "
                "фрагмента сохрани кратко как ответственность сущности, связь или ожидаемый результат сценария.\n"
                "1. «Сущности и связи». Перечисли новые и изменяемые сущности: компоненты, презентеры, "
                "сервисы, узлы и подграфы NodeCanvas и другие части проекта. Для каждой новой сущности "
                "укажи ответственность, для изменяемой — что меняется. Связанные узлы можешь описать "
                "как подграф. Для каждого нового узла достаточно имени и роли. Для каждой новой или изменяемой связи "
                "укажи направление и действие или передаваемые данные. "
                "Покажи распределение работы между компонентами и игровым графом. "
                "Ограничь описание сущности её ролью и связями. Исключи списки публичных методов, сигнатур, "
                "полей, внутренних событий и цепочки служебных вызовов. Если дизайн прямо требует "
                "конкретный метод или событие, укажи только его имя рядом со связью.\n"
                "2. «Алгоритм». Покажи в Mermaid маршрут от действия игрока или события игрового цикла "
                "через перечисленные сущности до результата. Отрази развилки, меняющие результат сценария, "
                "Укажи, какая сущность принимает действие, кому его передаёт и к какому результату это ведёт. "
                "Общий маршрут покажи один раз.\n"
                "3. «Сценарии». Для каждого обязательного сценария из дизайна, ТЗ и согласованных ответов "
                "укажи исходные условия, событие, ссылку на путь по схеме и ожидаемый результат. "
                "Варианты с общим путём опиши отдельными строками с нужными входами и результатами. "
                "Опирайся на исходники для существующих сущностей и связей. "
                "Открытое продуктовое решение отметь у затронутого сценария и запиши вопрос отдельно. "
                "Сохрани документ даже при открытых вопросах.\n"
                f"Запиши вопросы в {questions_file} как "
                '{"has_questions": boolean, "questions": string[]}. Без вопросов укажи false и [].'
            )
        else:
            if stage == 2:
                if not doc:
                    raise RuntimeError("Нет технического документа для ревью")
                inputs.append(f"Документ: {doc}")
                previous = latest_version(directory, "02_doc_review_v*.json")
                check = (
                    "Сверь алгоритм с дизайн-документом, ТЗ и согласованными ответами. "
                    "Для каждого обязательного сценария проследи путь от события через связи сущностей "
                    "и развилки Mermaid до результата. Проверь заданное требованиями распределение ответственности. "
                    "Если сценарий пропущен, путь обрывается, остаётся неоднозначным или допускает неверный исход, "
                    "назови сценарий, проблемное место, источник требования и ожидаемый результат. "
                    "Считай связь достаточной, если понятны участники, направление и смысл. "
                    "Отсутствие имён методов, их сигнатур или служебных шагов не является блокером. "
                    "Если утверждаешь, что связь мешает выполнить требование, назови конкретный сценарий и покажи, где обрывается его путь. "
                    "Проверь уровень детализации во всех разделах. Если документ предписывает внутренние "
                    "поля, подписки, служебные вызовы, покадровые переходы или устройство тестов, верни техдок "
                    "на доработку. Сгруппируй повторы одной проблемы в одно замечание. "
                    "На повторном ревью проверь актуальные прошлые блокеры и затронутые сценарии. "
                    "Если для вердикта не хватает обязательного входного материала, назови его и заверши этап с ошибкой."
                )
                comments = (
                    "Для блокера по сценарию укажи ожидаемое поведение. Для блокера по детализации "
                    "укажи фрагмент и что сохранить при сокращении. Выбор реализации оставь разработчику."
                )
            else:
                report = latest_version(directory, "03_impl_report_v*.md")
                if not report:
                    raise RuntimeError("Нет отчёта реализации для ревью")
                inputs.append(f"Отчёт реализации: {report}")
                if doc:
                    inputs.append(f"Утверждённый техдокумент: {doc}")
                previous = latest_version(directory, "04_code_review_v*.json")
                check = (
                    "Сверь реализацию в checkout задачи с ТЗ, утверждённым техдоком и применимыми правилами проекта. "
                    "Отчёт исполнителя используй как указатель на изменения и выполненные проверки.\n"
                    "1. Поведение. Для каждого обязательного сценария проследи путь от действия или события "
                    "через компоненты и граф NodeCanvas, если он участвует, до наблюдаемого результата. "
                    "Для UI проверь достижимость действия через рабочий интерфейс. "
                    "Для изменённых веток проверь достижимые отказы, повторные действия и прерывания.\n"
                    "2. Правила проекта. Прочитай AGENTS.md в checkout задачи и инструкции, применимые к изменённым файлам. "
                    "Сверь изменения с конкретными требованиями проекта. Например, при изменении префаба проверь "
                    "сохранность ссылок, при изменении общего метода — совместимость его вызывающего кода. "
                    "Для требований к процессу учитывай отчёт и доступные журналы проверок. "
                    "Если выполнение действия не подтверждено, укажи, что удалось установить. "
                    "Отсутствие записи о команде само по себе не доказывает нарушение.\n"
                    "3. Качество реализации. Проверь распределение ответственности, совместимость с существующими "
                    "потребителями и корректность состояния после операции. Для Unity учитывай жизненный цикл "
                    "и обработку null в затронутых путях. Привязывай замечания к конкретному месту и последствию. "
                    "Предложения по именованию, упрощению и рефакторингу помещай в notes.\n"
                    "Верни REJECTED при подтверждённом нарушении обязательного требования или достижимом дефекте. "
                    "На повторном ревью проверь исправление прежних актуальных блокеров и возможные регрессии "
                    "в затронутой области. Если для вердикта не хватает обязательного входного материала, "
                    "назови его и заверши этап с ошибкой."
                )
                comments = (
                    "В comments укажи место, требование или условие возникновения, фактический результат "
                    "и что должно быть исправлено. Для нарушения AGENTS.md назови файл и конкретное правило."
                )
            if previous:
                inputs.append(f"Предыдущий вердикт: {previous}")
            result = (
                f"{check}\nЗапиши вердикт в {artifact} по контракту {VERDICT_CONTRACT}. "
                f"Отклоняй только за подтверждённый блокер. {comments} "
                "При REJECTED comments не может быть пустым. При APPROVED оставь comments пустым. "
                "Неблокирующие замечания укажи в notes."
            )
        if previous_error:
            inputs.append(f"Предыдущая попытка этого этапа не завершена: {previous_error}\nУстрани причину и обязательно создай файл результата по контракту.")
        return (
            f"{agent['role_prompt']}\n\nЗадача #{task['id']}: {task['title']}\n"
            + "\n".join(inputs)
            + f"\n{result}\nЗапиши результат только в названные выходные файлы."
        )

    @staticmethod
    def _previous_error(task_id: int, stage: int, attempt: int) -> str:
        """What the attempt before an automatic retry got wrong."""
        if attempt <= 1:
            return ""
        with connect() as conn:
            row = conn.execute("SELECT status, error FROM stage_runs WHERE task_id=? AND stage=? AND attempt=?", (task_id, stage, attempt - 1)).fetchone()
        return (row["error"] or "")[:1500] if row and row["status"] == "error" else ""

    async def _run_server(self, task: dict[str, Any], agent: dict[str, Any], stage: int, attempt: int, run_id: int, directory: Path) -> None:
        log_path = directory / "logs" / f"stage_{stage}_attempt_{attempt}.log"

        async def log(text: str) -> None:
            with log_path.open("a", encoding="utf-8") as file:
                file.write(text)
            await bus.publish(task["id"], stage, text)

        url = repository_url(task, integration_settings().get("repository_url", ""))
        await log("Читаю удаленный репозиторий задачи…\n" if url else "Агент запущен\n")
        checkout = None
        env = {"ORCHESTRATOR_TASK_ID": str(task["id"]), "ORCHESTRATOR_STAGE": str(stage)}
        if url and task["id"] not in self.stop_requested:
            try:
                checkout = await asyncio.to_thread(
                    prepare_checkout, url, task["id"], task["branch_name"], task.get("external_issue_id") or ""
                )
            except RepoError as exc:
                await log(f"Репозиторий задачи недоступен: {exc}\n")
                raise
            repository, ref, commit = checkout
            env["ORCHESTRATOR_REPO"] = str(repository)
            await log(f"Локальный checkout ветки {ref}, коммит {commit}: {repository}\n")
        if task["id"] in self.stop_requested:
            return
        if url:
            await log("Агент запущен\n")
        previous_error = self._previous_error(task["id"], stage, attempt) if task["auto_retries"] else ""
        artifact_name = STAGE_FILES[stage].format(attempt=attempt)
        artifact = directory / artifact_name
        doc = latest_document(task["id"], directory) if stage in (1, 2) else None
        review = document_review(task["id"], doc) if stage == 1 and doc else None
        if stage == 2 and doc:
            with transaction() as conn:
                conn.execute("UPDATE stage_runs SET reviewed_document=? WHERE id=?", (doc.name, run_id))
        revise = False
        if stage == 1 and doc:
            version = VERSION_RE.search(doc.name)[1]
            has_feedback = (directory / f"01_answers_v{version}.md").is_file() or (
                review is not None and review["status"] == "REJECTED"
            )
            if has_feedback:
                headings = re.findall(r"^#{1,6} +(.+)$", doc.read_text(encoding="utf-8"), re.MULTILINE)
                sections = ("Сущности и связи", "Алгоритм", "Сценарии")
                revise = all(any(section in heading for heading in headings) for section in sections)

        result: RunResult | None = None
        success = False
        validation_error = None
        configs = [agent["primary_config"], *agent["fallback_configs"]]
        with contextlib.ExitStack() as stack:
            draft = None
            if revise:
                scratch = Path(stack.enter_context(TemporaryDirectory(prefix="tech-doc-", dir=directory / "logs")))
                draft = scratch / artifact_name
                draft_questions = scratch / f"01_questions_v{attempt}.json"
            prompt = self._prompt(
                task, agent, stage, attempt, checkout=checkout, previous_error=previous_error,
                doc=doc, review=review, draft=draft,
            )
            for index, config in enumerate(configs):
                if task["id"] in self.stop_requested:
                    return
                if config.get("environment") == "worker":
                    raise RuntimeError("Серверный этап не может выполняться на Local Worker")
                if draft:
                    shutil.copyfile(doc, draft)
                    draft_questions.unlink(missing_ok=True)
                if index:
                    await log(f"Fallback: {config['cli']} {config.get('model', '')}\n")
                runner = ProcessRunner(log_callback=log, on_process=lambda process, task_id=task["id"]: self.processes.__setitem__(task_id, process))
                result = await runner.run(
                    config, prompt, directory, env, agent["fallback_triggers"].get("timeout_minutes", 20),
                )
                if task["id"] in self.stop_requested:
                    self._mark_stopped(run_id)
                    return
                validation_error = None
                if draft and result.exit_code == 0:
                    try:
                        if not draft.is_file() or not draft.read_text(encoding="utf-8").strip() or not draft_questions.is_file():
                            raise StageFailure("Уточнение техдокумента не завершено: нужны рабочая копия и JSON с вопросами.")
                        read_questions(draft_questions)
                    except (StageFailure, OSError, UnicodeError) as exc:
                        validation_error = str(exc)
                    else:
                        draft_questions.replace(directory / draft_questions.name)
                        draft.replace(artifact)
                success = result.exit_code == 0 and not validation_error and artifact.exists()
                if success:
                    break
                triggers = agent["fallback_triggers"]
                retry = (
                    (result.rate_limited and triggers.get("rate_limit", True))
                    or (result.timed_out and triggers.get("timeout", True))
                    or triggers.get("exit_code", True)
                )
                if not retry or index == len(configs) - 1:
                    break
                if result.exit_code == 127:
                    await log(result.output)
                elif result.exit_code == 0:
                    await log((validation_error or f"CLI завершился без артефакта {artifact_name}.") + "\n")
        assert result is not None
        status = "completed" if success else "error"
        error = None if success else validation_error or describe_failure(result, artifact_name)
        with transaction() as conn:
            conn.execute(
                "UPDATE stage_runs SET cli_used=?, model_used=?, is_fallback=?, artifact_path=?, input_tokens=?, output_tokens=?, cache_tokens=?, tokens_estimated=?, status=?, finished_at=?, error=? WHERE id=?",
                (config["cli"], config.get("model", ""), int(configs.index(config) > 0), str(artifact), result.input_tokens, result.output_tokens, result.cache_tokens, int(result.estimated), status, now(), error, run_id),
            )
        if not success:
            raise StageFailure(error, retryable=not result.timed_out and not result.rate_limited and result.exit_code != 127)
        await self._process_artifact(task["id"], stage, attempt, from_run=True)

    async def _run_worker(self, task: dict[str, Any], agent: dict[str, Any], attempt: int, run_id: int) -> None:
        directory = task_dir(task["id"])
        docs = latest_document(task["id"], directory)
        reviews = latest_version(directory, "04_code_review_v*.json")
        sent = await hub.send({
            "type": "ASSIGN_TASK",
            "task_id": task["id"],
            "run_id": run_id,
            "branch_name": task["branch_name"],
            "task_title": task["title"],
            "task_description": task["description"],
            "external_issue_key": task.get("external_issue_key") or "",
            "external_issue_url": task.get("external_issue_url") or "",
            "tech_doc_content": docs.read_text(encoding="utf-8") if docs else "",
            "review_comments_content": reviews.read_text(encoding="utf-8") if reviews else "",
            "agent_config": {"primary": agent["primary_config"], "fallback": agent["fallback_configs"], "triggers": agent["fallback_triggers"], "role_prompt": agent["role_prompt"]},
        })
        if not sent:
            with connect() as conn:
                conn.execute("UPDATE tasks SET status='waiting_worker', updated_at=? WHERE id=?", (now(), task["id"]))
                conn.execute("UPDATE stage_runs SET status='queued', finished_at=? WHERE id=?", (now(), run_id))

    async def _process_artifact(self, task_id: int, stage: int, attempt: int, *, from_run: bool = False) -> None:
        directory = task_dir(task_id)
        current_limits = limits()
        with transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            task = decode_task(row)
            if from_run and (task["status"] != "running" or task["current_stage"] != stage):
                return
            doc_returns, code_returns = task["doc_returns"], task["code_returns"]
            hold, hold_stage, issue_id = "", 0, ""
            if stage == 1:
                questions = read_questions(directory / f"01_questions_v{attempt}.json")
                if questions["has_questions"]:
                    issue_id, next_stage, status = task["external_issue_id"], 1, "waiting_answer"
                else:
                    next_stage, status = 2, "queued"
            elif stage == 2:
                if read_verdict(directory / f"02_doc_review_v{attempt}.json")["status"] == "APPROVED":
                    next_stage, status, doc_returns = 3, "queued", 0
                else:
                    doc_returns += 1
                    next_stage, status = 1, "queued"
                    if doc_returns > current_limits["max_returns"]:
                        hold_stage = 2
                        hold = f"Ревью отклонило техническую документацию {doc_returns} раз подряд. Разберите замечания ревьюера, затем перезапустите этап 1 или утвердите этап 2 принудительно."
            elif stage == 4:
                if read_verdict(directory / f"04_code_review_v{attempt}.json")["status"] == "APPROVED":
                    next_stage, status, code_returns = 5, "waiting_pr", 0
                else:
                    code_returns += 1
                    next_stage, status = 3, "queued"
                    if code_returns > current_limits["max_returns"]:
                        hold_stage = 4
                        hold = f"Ревью отклонило реализацию {code_returns} раз подряд. Разберите замечания ревьюера, затем перезапустите этап 3 или утвердите этап 4 принудительно."
            else:
                next_stage, status = 4, "queued"
            if hold_stage:
                # The task stays on the review stage; only the return counter moves on.
                conn.execute("UPDATE tasks SET doc_returns=?, code_returns=?, updated_at=? WHERE id=?", (doc_returns, code_returns, now(), task_id))
            else:
                if status == "queued" and task["single_stage"]:
                    status = "stage_ready"
                conn.execute(
                    "UPDATE tasks SET current_stage=?, status=?, auto_retries=0, doc_returns=?, code_returns=?, hold_reason=NULL, updated_at=? WHERE id=?",
                    (next_stage, status, doc_returns, code_returns, now(), task_id),
                )
        if hold:
            await self._hold(task_id, hold_stage, hold)
        if issue_id:
            message_id = await publish_questions(issue_id, questions["questions"])
            with connect() as conn:
                conn.execute(
                    "UPDATE stage_runs SET yougile_question_message_id=? WHERE task_id=? AND stage=1 AND attempt=?",
                    (message_id, task_id, attempt),
                )

    async def worker_completed(self, message: dict[str, Any]) -> None:
        task_id = int(message["task_id"])
        run_id = int(message["run_id"])
        with connect() as conn:
            run = conn.execute("SELECT status FROM stage_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            return
        report = str(message.get("report", ""))
        with transaction() as conn:
            run = conn.execute("SELECT * FROM stage_runs WHERE id=?", (run_id,)).fetchone()
            attempt = run["attempt"]
            artifact = task_dir(task_id) / f"03_impl_report_v{attempt}.md"
            artifact.write_text(report, encoding="utf-8")
            conn.execute(
                "UPDATE stage_runs SET artifact_path=?, cli_used=?, model_used=?, is_fallback=?, input_tokens=?, output_tokens=?, cache_tokens=?, tokens_estimated=?, status='completed', finished_at=? WHERE id=?",
                (str(artifact), message.get("cli_used"), message.get("model_used", ""), int(message.get("is_fallback", False)), int(message.get("input_tokens", 0)), int(message.get("output_tokens", 0)), int(message.get("cache_tokens", 0)), int(message.get("tokens_estimated", message.get("estimated", False))), now(), run_id),
            )
            conn.execute(
                "UPDATE tasks SET pr_url=COALESCE(?, pr_url), worktree_path=?, current_stage=4, status=CASE WHEN single_stage THEN 'stage_ready' ELSE 'queued' END, auto_retries=0, hold_reason=NULL, updated_at=? WHERE id=?",
                (message.get("pr_url"), message.get("worktree_path"), now(), task_id),
            )
            self.active.pop(task_id, None)
        await self.schedule()

    async def worker_failed(self, message: dict[str, Any]) -> None:
        task_id, run_id = int(message["task_id"]), int(message["run_id"])
        with connect() as conn:
            run = conn.execute("SELECT status FROM stage_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] != "running":
                return
            error = str(message.get("error", "Worker failed"))
            conn.execute("UPDATE stage_runs SET status='error', error=?, finished_at=? WHERE id=?", (error, now(), run_id))
            conn.execute("UPDATE tasks SET status='error', updated_at=? WHERE id=?", (now(), task_id))
        self.active.pop(task_id, None)
        await bus.publish(task_id, 3, error)

    async def reconcile_yougile_answers(self, issue_id: str | None) -> list[int]:
        with connect() as conn:
            waiting = conn.execute(
                """SELECT tasks.id, tasks.external_issue_id, stage_runs.yougile_question_message_id
                   FROM tasks JOIN stage_runs ON stage_runs.id = (
                       SELECT id FROM stage_runs WHERE task_id=tasks.id AND stage=1 ORDER BY attempt DESC LIMIT 1
                   )
                   WHERE tasks.status='waiting_answer' AND tasks.external_issue_id IS NOT NULL
                     AND stage_runs.yougile_question_message_id IS NOT NULL
                     AND (? IS NULL OR tasks.external_issue_id=?)""",
                (issue_id, issue_id),
            ).fetchall()
        accepted = []
        for task in waiting:
            chat_id, question_id = task["external_issue_id"], task["yougile_question_message_id"]
            reply = await find_reply(chat_id, question_id)
            if reply and await self.answer(task["id"], reply, yougile_reply=(chat_id, question_id)):
                accepted.append(task["id"])
        return accepted

    async def answer(self, task_id: int, answer: str, *, yougile_reply: tuple[str, int] | None = None) -> bool:
        with transaction() as conn:
            row = conn.execute("SELECT status, external_issue_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] != "waiting_answer":
                if yougile_reply is not None:
                    return False
                raise ValueError("Задача не ожидает ответа")
            run = conn.execute(
                "SELECT attempt, yougile_question_message_id FROM stage_runs WHERE task_id=? AND stage=1 ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if yougile_reply is not None and (
                run is None or yougile_reply != (row["external_issue_id"], run["yougile_question_message_id"])
            ):
                return False
            attempt = run["attempt"] if run else 1
            (task_dir(task_id) / f"01_answers_v{attempt}.md").write_text(answer, encoding="utf-8")
            conn.execute("UPDATE tasks SET status='queued', updated_at=? WHERE id=?", (now(), task_id))
        await self.start(task_id)
        return True

    async def start_task(self, task_id: int, stage: int | None = None, pipeline: bool = False) -> None:
        with transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Задача не найдена")
            task = decode_task(row)
            allowed_statuses = ("open", "stage_ready", "needs_human", "error")
            if stage is not None:
                allowed_statuses += ("waiting_pr",)
            if (
                task["status"] not in allowed_statuses
                or task["merged_to_main"]
                or task_id in self.active
                or task_id in self.stop_requested
            ):
                raise ValueError("Задача сейчас недоступна для запуска")
            current_stage = 1 if task["status"] == "open" else task["current_stage"]
            if stage is not None and stage > current_stage:
                raise ValueError("Сначала завершите предыдущие этапы")
            next_stage = current_stage if stage is None else stage
            if not task["stage_agents"].get(next_stage):
                raise ValueError(f"Назначьте бота на этап {next_stage}")
            # A plain start on a human hold resumes the interrupted run mode.
            single_stage = stage is not None or (not pipeline and task["status"] == "needs_human" and task["single_stage"])
            conn.execute(
                "UPDATE tasks SET status='queued', current_stage=?, single_stage=?, auto_retries=CASE WHEN status='needs_human' AND current_stage=? THEN auto_retries ELSE 0 END, hold_reason=NULL, updated_at=? WHERE id=?",
                (next_stage, int(single_stage), next_stage, now(), task_id),
            )
        await self.start(task_id)

    async def restart(self, task_id: int, stage: int) -> None:
        with connect() as conn:
            task = conn.execute("SELECT status, current_stage FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["status"] in ("open", "blocked_main", "done") or stage > task["current_stage"]:
            raise ValueError("Сначала завершите предыдущие этапы")
        if task_id in self.stop_requested:
            raise ValueError("Перезапуск уже выполняется")
        self.stop_requested.add(task_id)
        await self._stop_run(task_id)
        if stage == 3 or task["current_stage"] == 3:
            await hub.send({"type": "CANCEL_TASK", "task_id": task_id})
        with connect() as conn:
            conn.execute(
                "UPDATE stage_runs SET status='error', error=?, finished_at=? WHERE task_id=? AND status='running'",
                ("Остановлено перезапуском", now(), task_id),
            )
            # A person restarting a stage gives it a fresh retry budget and return counter.
            conn.execute(
                "UPDATE tasks SET current_stage=?, status='queued', retry_count=retry_count+1, auto_retries=0, hold_reason=NULL, doc_returns=COALESCE(?, doc_returns), code_returns=COALESCE(?, code_returns), updated_at=? WHERE id=? AND merged_to_main=0",
                (stage, 0 if stage in (1, 2) else None, 0 if stage in (3, 4) else None, now(), task_id),
            )
        self.active.pop(task_id, None)
        self.stop_requested.discard(task_id)
        await bus.publish(task_id, stage, "Сессия остановлена. Запускается новая попытка.\n")
        await self.start(task_id)

    async def _stop_run(self, task_id: int) -> None:
        process = self.processes.get(task_id)
        if process:
            signal_cli(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                signal_cli(process, force=True)
        job = self.runs.get(task_id)
        if job and not job.done():
            try:
                await asyncio.wait_for(asyncio.shield(job), timeout=8)
            except TimeoutError:
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job
            except (asyncio.CancelledError, Exception):
                pass

    async def force_approve(self, task_id: int, stage: int) -> None:
        if stage not in (1, 2, 4):
            raise ValueError("Принудительно можно утвердить только этапы 1, 2 и 4")
        with connect() as conn:
            task = conn.execute("SELECT status, current_stage, single_stage FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["status"] in ("open", "blocked_main", "done") or stage != task["current_stage"]:
                raise ValueError("Утвердить можно только текущий этап задач в работе")
            next_stage, status = {1: (2, "queued"), 2: (3, "queued"), 4: (5, "waiting_pr")}[stage]
            if status == "queued" and task["single_stage"]:
                status = "stage_ready"
            conn.execute(
                "UPDATE tasks SET current_stage=?, status=?, auto_retries=0, hold_reason=NULL, doc_returns=COALESCE(?, doc_returns), code_returns=COALESCE(?, code_returns), updated_at=? WHERE id=?",
                (next_stage, status, 0 if stage in (1, 2) else None, 0 if stage == 4 else None, now(), task_id),
            )
        await self.start(task_id)

    async def merged(self, task_id: int, pr_url: str | None = None) -> None:
        with transaction() as conn:
            task = conn.execute("SELECT worktree_path, branch_name FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("Задача не найдена")
            conn.execute("UPDATE tasks SET current_stage=6, status='done', merged_to_main=1, pr_url=COALESCE(?, pr_url), updated_at=? WHERE id=?", (pr_url, now(), task_id))
            children = conn.execute("SELECT task_id FROM task_dependencies WHERE depends_on_task_id=?", (task_id,)).fetchall()
            for child in children:
                blocked = conn.execute(
                    "SELECT COUNT(*) AS n FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id WHERE d.task_id=? AND t.merged_to_main=0",
                    (child["task_id"],),
                ).fetchone()["n"]
                if not blocked:
                    conn.execute("UPDATE tasks SET current_stage=1, status='queued', updated_at=? WHERE id=? AND current_stage=0", (now(), child["task_id"]))
        await hub.send({"type": "CLEANUP_WORKTREE", "task_id": task_id, "branch_name": task["branch_name"], "worktree_path": task["worktree_path"]})
        await self.schedule()


dispatcher = Dispatcher()
