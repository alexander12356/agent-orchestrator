import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

from .config import settings
from .database import connect, decode_agent, decode_task, now, spent_tokens, transaction
from .events import bus
from .repo import RepoError, export_task_files, prepare_checkout, repository_url
from .runner import ProcessRunner, RunResult
from .schemas import IntegrationSettings
from .tracker import integration_settings, publish_questions
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


def read_artifact(task_id: int, name: str) -> str:
    path = task_dir(task_id) / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def latest_version(directory: Path, pattern: str) -> Path | None:
    """The newest revision of an artifact by the number in its name, so v10 follows v9."""
    revisions = [path for path in directory.glob(pattern) if VERSION_RE.search(path.name)]
    return max(revisions, key=lambda path: int(VERSION_RE.search(path.name)[1]), default=None)


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
            if not task or task["status"] in ("open", "done", "blocked_main", "waiting_answer", "waiting_pr", "running"):
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

    def _prompt(self, task: dict[str, Any], agent: dict[str, Any], stage: int, attempt: int, evidence: list[str] | None = None, missing: list[str] | None = None, repository_ref: str = "", previous_error: str = "") -> str:
        directory = task_dir(task["id"])
        artifact = directory / STAGE_FILES[stage].format(attempt=attempt)
        inputs = [f"ТЗ: {directory / '00_task.md'}"]
        if evidence is not None:
            lines = [f"Файлы репозитория, ветка {repository_ref}, лежат в {directory / 'repo'}:"]
            lines.extend(f"- {directory / 'repo' / name}" for name in evidence)
            if missing:
                lines.append("В снимке нет: " + ", ".join(missing))
            lines.append("Прочитай только ТЗ, чат задачи, перечисленные файлы и явно указанные замечания или ответы. Не открывай GitHub, другие каталоги сервера, чужие задачи и базу. Если файла нет, запиши это в результат и не выдумывай имена.")
            inputs.append("\n".join(lines))
        if stage == 1:
            review = latest_version(directory, "02_doc_review_v*.json")
            answers = latest_version(directory, "01_answers_v*.md")
            if review:
                inputs.append(f"Замечания ревью: {review} (comments — блокеры, исправить обязательно; notes — учесть, если не противоречат ТЗ)")
            if answers:
                inputs.append(f"Ответы на вопросы: {answers}")
            result = f"Создай Markdown-документ {artifact}. Отдельно запиши {directory / f'01_questions_v{attempt}.json'} по контракту {{\"has_questions\": boolean, \"questions\": string[]}}. Если вопросов нет, используй has_questions=false."
        elif stage == 2:
            doc = latest_version(directory, "01_tech_doc_v*.md")
            if not doc:
                raise RuntimeError("Нет технического документа для ревью")
            inputs.append(f"Документ: {doc}")
            result = (
                f"Проверь документ и запиши вердикт в {artifact} по контракту {VERDICT_CONTRACT}.\n"
                "Ты настроен на одобрение: REJECTED — только за блокер, который можешь подтвердить, назвав место в документе. "
                "Блокеры: документ не покрывает требование ТЗ; названные в нём файлы, классы или пути отсутствуют в снимке; пункты противоречат друг другу; "
                "пункт нереализуем без решения, которое должен принять автор; нет способа проверить результат. "
                "В comments перечисли только блокеры, каждый с местом и нужным исправлением. "
                "Стиль, формулировки, улучшения и вопросы, не мешающие реализации, — в notes: они не мешают APPROVED. "
                "При REJECTED comments не может быть пустым."
            )
        else:
            report = latest_version(directory, "03_impl_report_v*.md")
            if not report:
                raise RuntimeError("Нет отчёта реализации для ревью")
            inputs.append(f"Отчет реализации: {report}")
            result = (
                f"Проверь реализацию через доступные GitHub/CLI инструменты и запиши вердикт в {artifact} по контракту {VERDICT_CONTRACT}.\n"
                "Ты настроен на одобрение: REJECTED — только за блокер, который можешь подтвердить, назвав файл и место. "
                "Блокеры: изменение расходится с утверждённым документом или ТЗ; явный дефект, который приведёт к ошибке, потере данных или утечке; "
                "сломано существующее поведение без причины; в изменённом коде Unity нет нужных проверок на null и lifecycle. "
                "В comments перечисли только блокеры, каждый с местом и нужным исправлением. "
                "Стиль, именование, рефакторинг и возможные улучшения — в notes: они не мешают APPROVED. "
                "При REJECTED comments не может быть пустым."
            )
        if previous_error:
            inputs.append(f"Предыдущая попытка этого этапа не завершена: {previous_error}\nУстрани причину и обязательно создай файл результата по контракту.")
        return (
            f"{agent['role_prompt']}\n\nЗадача #{task['id']}: {task['title']}\n"
            + "\n".join(inputs)
            + f"\n{result}\nРаботай автономно. Не изменяй служебные JSON вне заданного формата."
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
        evidence: list[str] | None = None
        missing: list[str] = []
        repository_ref = ""
        if url and task["id"] not in self.stop_requested:
            try:
                repository, repository_ref = await asyncio.to_thread(prepare_checkout, url, task["id"], task["branch_name"], task.get("external_issue_id") or "")
                source = "\n".join(filter(None, [
                    task.get("title"),
                    task.get("description"),
                    task.get("external_issue_key"),
                    read_artifact(task["id"], "00_task.md"),
                    read_artifact(task["id"], "00_yougile_chat.md"),
                ]))
                evidence, missing = await asyncio.to_thread(export_task_files, repository, directory / "repo", source)
            except RepoError as exc:
                await log(f"Репозиторий задачи недоступен: {exc}\n")
                raise
            await log(f"Снимок ветки {repository_ref}. В выдачу: {', '.join(evidence) or 'ничего'}\n")
            if missing:
                await log(f"В снимке нет: {', '.join(missing)}\n")
        if task["id"] in self.stop_requested:
            return
        if url:
            await log("Агент запущен\n")
        previous_error = self._previous_error(task["id"], stage, attempt) if task["auto_retries"] else ""
        prompt = self._prompt(task, agent, stage, attempt, evidence, missing, repository_ref, previous_error)
        artifact_name = STAGE_FILES[stage].format(attempt=attempt)
        artifact = directory / artifact_name

        result: RunResult | None = None
        success = False
        configs = [agent["primary_config"], *agent["fallback_configs"]]
        for index, config in enumerate(configs):
            if task["id"] in self.stop_requested:
                return
            if config.get("environment") == "worker":
                raise RuntimeError("Серверный этап не может выполняться на Local Worker")
            if index:
                await log(f"Fallback: {config['cli']} {config.get('model', '')}\n")
            runner = ProcessRunner(log_callback=log, on_process=lambda process, task_id=task["id"]: self.processes.__setitem__(task_id, process))
            result = await runner.run(
                config,
                prompt,
                directory,
                {"ORCHESTRATOR_TASK_ID": str(task["id"]), "ORCHESTRATOR_STAGE": str(stage)},
                agent["fallback_triggers"].get("timeout_minutes", 20),
            )
            if task["id"] in self.stop_requested:
                self._mark_stopped(run_id)
                return
            # A usable result ends the chain whatever the output says about limits.
            success = result.exit_code == 0 and artifact.exists()
            if success:
                break
            triggers = agent["fallback_triggers"]
            retry = (
                (result.rate_limited and triggers.get("rate_limit", True))
                or (result.timed_out and triggers.get("timeout", True))
                or (result.exit_code != 0 and triggers.get("exit_code", True))
            )
            if not retry or index == len(configs) - 1:
                break
        assert result is not None
        status = "completed" if success else "error"
        error = None if success else describe_failure(result, artifact_name)
        with transaction() as conn:
            conn.execute(
                "UPDATE stage_runs SET cli_used=?, model_used=?, is_fallback=?, artifact_path=?, input_tokens=?, output_tokens=?, cache_tokens=?, tokens_estimated=?, status=?, finished_at=?, error=? WHERE id=?",
                (config["cli"], config.get("model", ""), int(configs.index(config) > 0), str(artifact), result.input_tokens, result.output_tokens, result.cache_tokens, int(result.estimated), status, now(), error, run_id),
            )
        if not success:
            raise StageFailure(error, retryable=not result.timed_out and not result.rate_limited and result.exit_code != 127)
        await self._process_artifact(task["id"], stage, attempt)

    async def _run_worker(self, task: dict[str, Any], agent: dict[str, Any], attempt: int, run_id: int) -> None:
        directory = task_dir(task["id"])
        docs = latest_version(directory, "01_tech_doc_v*.md")
        reviews = latest_version(directory, "04_code_review_v*.json")
        sent = await hub.send({
            "type": "ASSIGN_TASK",
            "task_id": task["id"],
            "run_id": run_id,
            "branch_name": task["branch_name"],
            "tech_doc_content": docs.read_text(encoding="utf-8") if docs else "",
            "review_comments_content": reviews.read_text(encoding="utf-8") if reviews else "",
            "agent_config": {"primary": agent["primary_config"], "fallback": agent["fallback_configs"], "triggers": agent["fallback_triggers"], "role_prompt": agent["role_prompt"]},
        })
        if not sent:
            with connect() as conn:
                conn.execute("UPDATE tasks SET status='waiting_worker', updated_at=? WHERE id=?", (now(), task["id"]))
                conn.execute("UPDATE stage_runs SET status='queued', finished_at=? WHERE id=?", (now(), run_id))

    async def _process_artifact(self, task_id: int, stage: int, attempt: int) -> None:
        directory = task_dir(task_id)
        current_limits = limits()
        with transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            task = decode_task(row)
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
                conn.execute(
                    "UPDATE tasks SET current_stage=?, status=?, auto_retries=0, doc_returns=?, code_returns=?, hold_reason=NULL, updated_at=? WHERE id=?",
                    (next_stage, status, doc_returns, code_returns, now(), task_id),
                )
        if hold:
            await self._hold(task_id, hold_stage, hold)
        if issue_id:
            await publish_questions(issue_id, questions["questions"])

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
            conn.execute("UPDATE tasks SET pr_url=COALESCE(?, pr_url), worktree_path=?, current_stage=4, status='queued', auto_retries=0, hold_reason=NULL, updated_at=? WHERE id=?", (message.get("pr_url"), message.get("worktree_path"), now(), task_id))
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

    async def answer(self, task_id: int, answer: str) -> None:
        with connect() as conn:
            row = conn.execute("SELECT current_stage, status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] != "waiting_answer":
                raise ValueError("Задача не ожидает ответа")
            attempt = conn.execute("SELECT COALESCE(MAX(attempt), 1) AS n FROM stage_runs WHERE task_id=? AND stage=1", (task_id,)).fetchone()["n"]
            (task_dir(task_id) / f"01_answers_v{attempt}.md").write_text(answer, encoding="utf-8")
            conn.execute("UPDATE tasks SET status='queued', updated_at=? WHERE id=?", (now(), task_id))
        await self.start(task_id)

    async def start_open(self, task_id: int) -> None:
        with connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status='queued', current_stage=CASE WHEN status='open' THEN 1 ELSE current_stage END, hold_reason=NULL, updated_at=? WHERE id=? AND status IN ('open', 'needs_human')",
                (now(), task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Задача не находится в статусе Open или «Нужен человек»")
        await self.start(task_id)

    async def restart(self, task_id: int, stage: int) -> None:
        self.stop_requested.add(task_id)
        await self._stop_run(task_id)
        if stage == 3:
            await hub.send({"type": "CANCEL_TASK", "task_id": task_id})
        with connect() as conn:
            task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["status"] == "open":
                self.stop_requested.discard(task_id)
                return
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
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
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
            if conn.execute("SELECT 1 FROM tasks WHERE id=? AND status='open'", (task_id,)).fetchone():
                return
            next_stage, status = {1: (2, "queued"), 2: (3, "queued"), 4: (5, "waiting_pr")}[stage]
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
