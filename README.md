# Agent Orchestrator

Система полного цикла разработки Unity-фич: документация → ревью → реализация на локальном Unity Worker → ревью кода → Draft PR → merge в `main`. FastAPI хранит SQLite БД, артефакты и логи; локальный воркер подключается к серверу только исходящим WebSocket.

## Структура

- `backend/app` — REST API, диспетчер, CLI runner, SSE, Webhooks и WebSocket.
- `frontend` — React/TypeScript/Tailwind/React Flow.
- `worker/unity_worker.py` — git worktree, CLI fallback, Unity batchmode, commit/push/Draft PR.
- `storage/tasks/{id}` — документы, JSON-вердикты, отчеты и логи попыток.

## Быстрый запуск

Требуются Python 3.11+ и Node.js 20+.

```bash
python -m venv .venv
# Linux/WSL: source .venv/bin/activate
# Windows Git Bash: source .venv/Scripts/activate
pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

Откройте `http://localhost:8000`. Сначала создайте ботов на вкладке «Настройки», затем задачу. Если боты существуют, они автоматически назначаются на этапы 1–4.

Для разработки UI:

```bash
cd frontend
npm run dev
```

Vite доступен на `http://localhost:5173` и проксирует API на порт 8000.

## Настройка CLI

В профиле бота выберите Claude Code, Codex, Gemini или Aider. Утилита должна быть установлена и авторизована в среде выполнения. Поле `command` позволяет переопределить команду и подключить любой Custom CLI.

Поддерживаются placeholder-ы `{prompt}`, `{model}`, `{artifact_dir}`, `{task_id}`, `{stage}`. Если `{prompt}` отсутствует, промпт добавляется последним аргументом. Пример:

```bash
python fake_agent.py --task {task_id} --stage {stage} --prompt {prompt}
```

CLI получает только пути к текущим артефактам. Он обязан создать файл по контракту из `SPEC.md`. Для этапов 2 и 4 JSON вердикт обязателен; для этапа 1 — Markdown и JSON вопросов. Ненулевой код, таймаут или rate limit включают следующий fallback, если соответствующий триггер активен.

Usage читается из JSON `usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`). При отсутствии статистики применяется `tiktoken`, а оценка помечается `~`.

## Unity Worker

На ПК с Unity установите Git, GitHub CLI (`gh auth login`) и Python 3.11+:

```bash
pip install -r worker/requirements.txt
python worker/unity_worker.py \
  --server ws://VDS:8000/ws/worker \
  --token "$ORCHESTRATOR_WORKER_TOKEN" \
  --repo /path/to/UnityProject \
  --worktrees /path/to/worktrees \
  --library /path/to/UnityProject/Library
```

На Windows удобно задать параметры через переменные из `worker/.env.example` и запустить `python worker/unity_worker.py`. `--unity` / `UNITY_EDITOR` включает последовательный `Unity.exe -batchmode`; без пути к редактору Unity-сборка явно не выполняется. Воркер создает worktree от `origin/main`, связывает эталонную `Library` symlink/junction, запускает CLI, коммитит изменения, делает push и создает Draft PR. После merge удаляет worktree и ветку.

**Важно:** настройте `git user.name`/`user.email`, права push и `gh`. Не используйте общий worktree для ручной разработки.

## Интеграции и Webhooks

Вкладка «Настройки» показывает готовые URL:

- `POST /api/webhooks/git` — GitHub `pull_request`; принимаются только `merged=true` и base `main`, подпись `X-Hub-Signature-256`.
- `POST /api/webhooks/tracker` — GitHub Issues (`issue.number`, `comment.body`) или универсальный JSON (`external_issue_id`, `answer`).
- `WS /ws/worker` — заголовок `Authorization: Bearer ...`, heartbeat каждые 10 секунд.

GitHub Issues поддерживается напрямую. Jira, Linear, YouTrack используют универсальный HTTP POST `BASE_URL/issues/ID/comments` с Bearer token; при другом API адаптируйте `backend/app/tracker.py`. Ручные ответы и подтверждение merge доступны в инспекторе.

Переменные окружения задаются в `.env` (см. `.env.example`). Настройки, введенные в UI, имеют приоритет над webhook/worker-секретами окружения и сохраняются в SQLite. Используйте HTTPS/WSS и reverse proxy на публичном VDS.

## Основные API

- `GET/POST /api/agents`, `PUT/DELETE /api/agents/{id}`, `POST /api/agents/{id}/avatar`.
- `GET/POST /api/tasks`, `GET/PUT/DELETE /api/tasks/{id}`.
- `GET /api/tasks/{id}/artifacts[/{name}]`, `GET /api/tasks/{id}/logs` (SSE).
- `POST /api/tasks/{id}/answer`, `/restart?stage=N`, `/approve?stage=N`, `/merge`.
- `GET/PUT /api/settings`, `GET /api/health`.

## Тесты

```bash
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend pytest backend/tests
pytest worker/test_worker.py
cd frontend && npm test && npm run build
```
