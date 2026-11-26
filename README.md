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

Откройте `http://localhost:8000`. Сначала создайте ботов на вкладке «Настройки», затем задачу и назначьте ботов на нужные этапы. Если бот не указан, выполнение на этом этапе не начнётся.

Для разработки UI:

```bash
cd frontend
npm run dev
```

Vite доступен на `http://localhost:5173` и проксирует API на порт 8000.

## Управление запуском в WSL2 (Windows)

`scripts/orchestrator.bat` управляет сервером в копии репозитория `/root/projects/agent-orchestrator` внутри Ubuntu WSL2. Процесс стартует через `setsid nohup uvicorn` и переживает закрытие окна:

```bat
scripts\orchestrator.bat start     :: запуск на порту 8000
scripts\orchestrator.bat stop      :: остановка (SIGTERM, затем SIGKILL через 20 с)
scripts\orchestrator.bat restart   :: stop + start
scripts\orchestrator.bat status    :: процесс, health-check из WSL и из Windows
scripts\orchestrator.bat log       :: последние 40 строк uvicorn.log
```

Дистрибутив по умолчанию — `Ubuntu-24.04`, другой задаётся переменной `ORCHESTRATOR_WSL_DISTRO`. Файл должен оставаться с CRLF: `cmd.exe` не находит метки `goto` в батнике с LF. Открывайте `http://127.0.0.1:8000`: если в системе включён прокси без исключения для localhost, адрес `http://localhost:8000` может вернуть 502.

## Настройка CLI

В профиле бота выберите Claude Code, Codex, Kimi, ZCode, Gemini или Aider. Утилита должна быть установлена и авторизована в среде выполнения. Поле `command` позволяет переопределить команду и подключить любой Custom CLI.

ZCode CLI (Z.ai) ставится нативно в Ubuntu/WSL одним скриптом — он поднимает Node.js 22, глобальный пакет `zcode-app-cli` (терминальный клиент официального рантайма ZCode), хранилище учётных данных без keyring и обновляет каталог моделей до актуального релиза:

```bash
sudo bash scripts/install-zcode-cli.sh
zcode login --no-browser
zcode --model account:zai-individual-coding-plan/GLM-5.4 -p 'Reply with OK.'
```

`zcode login --no-browser` печатает ссылку авторизации Z.ai: откройте её один раз и подтвердите доступ. Полная ссылка на модель передаётся в поле «Модель» профиля бота (`provider/model`, например `account:zai-individual-coding-plan/GLM-5.4`); если поле пустое, используется модель, сохранённая в ZCode. Промпт всегда запускается с `--mode yolo`, чтобы агент мог писать файлы без подтверждений. После обновления `zcode-app-cli` повторите скрипт: он заново подтягивает каталог моделей.

DeepSeek Harness (`dsh`) ставится отдельным скриптом. Он кладёт Node.js 24 в `/usr/local/lib/nodejs/node24` (глобальный Node 22 не трогает), ставит `@deepseek-ai/dsh` и создаёт обёртки `/usr/local/bin/dsh` и `/usr/local/bin/dsh-agent`:

```bash
sudo bash scripts/install-dsh-cli.sh
echo 'DEEPSEEK_API_KEY=sk-...' >> ~/.dsh/.env   # ключ с platform.deepseek.com
dsh headless "Reply with OK."
```

Обёртка `dsh` читает `~/.dsh/.env` при запуске, поэтому ключ подхватывается и сервером оркестратора. Для профиля бота выберите Custom CLI, среду Server и команду `dsh-agent --model deepseek-v4-pro {prompt}` (или `deepseek-flash`): `dsh-agent` запускает `dsh headless --approval-policy never` (агент пишет файлы без подтверждений) и подставляет модель патч-оверлеем. Пустое поле «Модель» — модель профиля dsh по умолчанию (`deepseek-flash`).

Поддерживаются placeholder-ы `{prompt}`, `{model}`, `{artifact_dir}`, `{task_id}`, `{stage}`. Если `{prompt}` отсутствует, промпт добавляется последним аргументом. Пример:

```bash
python fake_agent.py --task {task_id} --stage {stage} --prompt {prompt}
```

CLI получает только пути к текущим артефактам. Он обязан создать файл по контракту из `SPEC.md`. Для этапов 2 и 4 JSON вердикт обязателен; для этапа 1 — Markdown и JSON вопросов. Ненулевой код, таймаут или rate limit включают следующий fallback, если соответствующий триггер активен.

Оркестратор суммирует подтверждённые токены всех попыток по трём категориям: вход без чтения из кэша, чтение из кэша и выход. При отсутствии статистики интерфейс помечает итог как неполный.

## Unity Worker

На ПК с Unity установите Git, GitHub CLI (`gh auth login`) и Python 3.11+:

```bash
pip install -r worker/requirements.txt
python worker/unity_worker.py \
  --server ws://VDS:8000/ws/worker \
  --token "$ORCHESTRATOR_WORKER_TOKEN" \
  --repo /path/to/UnityProject \
  --worktrees /path/to/worktrees
```

На Windows удобно задать параметры через переменные из `worker/.env.example` и запустить `python worker/unity_worker.py`. `--unity` / `UNITY_EDITOR` включает последовательный `Unity.exe -batchmode`; без пути к редактору Unity-сборка явно не выполняется. Воркер создает worktree от `origin/main`, использует отдельную локальную `Library` для каждой задачи, запускает CLI, коммитит изменения, делает push и создает Draft PR. Агент обязан оставить структурированное резюме `orchestrator-pr.json`: цель, сделанные изменения, способ реализации, поведение до/после и только реально выполненные проверки. Воркер превращает его в описание PR, добавляет результат Unity и diff-статистику; при повторной доработке обновляет заголовок и описание существующего PR. Ветка задачи называется `Agent-Orchestrator/<ключ Yougile>` (например, `Agent-Orchestrator/ZEL-17`), для задачи без ключа — `Agent-Orchestrator/task-{id}`. Повторный запуск продолжает существующий worktree и сохраняет его `Library`. Параметр `--library` / `UNITY_REFERENCE_LIBRARY` принимается для совместимости, но общая `Library` больше не связывается с worktree. После merge удаляет worktree и ветку.

**Важно:** настройте `git user.name`/`user.email`, права push и `gh`. Не используйте общий worktree для ручной разработки.

## Интеграции и Webhooks

Вкладка «Настройки» показывает готовые URL:

- `POST /api/webhooks/git` — GitHub `pull_request`; принимаются только `merged=true` и base `main`, подпись `X-Hub-Signature-256`.
- `POST /api/webhooks/tracker` — Yougile `chat_message-created`. Оркестратор проверяет `token` в query-параметре и принимает ответ через «Ответить на это сообщение» на текущий вопрос в чате задачи. Если в событии не хватает данных, оркестратор проверяет сообщения в ожидающих ответа чатах через REST API и сверяет `properties.replyTo`.
- `WS /ws/worker` — заголовок `Authorization: Bearer ...`, heartbeat каждые 10 секунд.

В списке задач и в инспекторе кнопка «Приостановить» останавливает активный этап или снимает задачу с очереди. Пауза сохраняется после перезапуска оркестратора. Кнопка «Продолжить» запускает текущий этап новой попыткой, сохраняя результаты завершённых этапов, режим запуска и изменения в рабочей копии реализации. Те же действия доступны через `POST /api/tasks/{id}/pause` и `POST /api/tasks/{id}/start`.

Yougile подключается через REST API v2. На вкладке «Настройки» введите API key, сохраните настройки и нажмите «Проверить подключение». Укажите название проекта и имя доски, при необходимости — стикер в формате `Название.Состояние`, например `Development.Код`. Нажмите «Импортировать задачи»: задачи из колонок `Open` и `Ready` с выбранным состоянием стикера будут импортированы с описанием и историей чата (`00_yougile_chat.md`) в локальный статус `open`. Если стикер не указан, импортируются все задачи из этих колонок. Боты не запускают их автоматически; после назначения ботов пайплайн запускается кнопкой «Запустить пайплайн» в строке задачи. В списке отображается проектный номер Yougile вида `ZEL-17` со ссылкой на задачу (`URL команды/#ZEL-17`); UUID остаётся во внутреннем `external_issue_id` для API и webhook. Описание с форматированием выводится в правой панели графа задачи. В редакторе можно указать майлстон и спринт, например `Прототип` и `Спринт 1`; они отображаются цепочкой и сохраняются при повторном импорте. Повторный импорт обновляет только задачи, которые всё ещё остаются в `open`, и не создаёт дубликаты. В поле внешней задачи оркестратора вручную укажите UUID задачи Yougile: вопросы бота публикуются в её чат. Для автоматических ответов задайте `ORCHESTRATOR_PUBLIC_URL` с публичным HTTPS-адресом сервера, укажите Tracker webhook secret и нажмите «Создать webhook». Yougile не может отправлять события на `localhost` из WSL; в этом случае используйте HTTPS reverse proxy/туннель или ручной ответ в инспекторе.

Переменные окружения задаются в `.env` (см. `.env.example`). Настройки, введенные в UI, имеют приоритет над webhook/worker-секретами окружения и сохраняются в SQLite; там же сохраняется Yougile API key. Используйте HTTPS/WSS и reverse proxy на публичном VDS.

### Telegram

Оркестратор отправляет уведомление, когда нужны ответы на вопросы, исчерпан лимит, этап завершился ошибкой без автоповтора, закончился одиночный этап или PR ждёт merge. Уведомления приходят при переходе в соответствующее состояние. Ответы и подтверждения выполняются в оркестраторе, YouGile или GitHub.

Оркестратор обращается к Telegram Bot API напрямую из WSL или с VPS. Создайте JSON-файл вне репозитория, доступный только пользователю сервиса (права `0600`). В поле `botToken` укажите токен бота, в поле `chatId` — ID чата. Получатель должен сначала открыть чат с ботом и нажать «Старт». Укажите путь к файлу в `.env` и перезапустите сервер.

```dotenv
ORCHESTRATOR_TELEGRAM_CREDENTIALS_FILE=/etc/agent-orchestrator/telegram.json
```

Сбой отправки записывается в журнал сервера без токена. Повторная отправка автоматически не выполняется.

## Лимиты и автоповторы

Вкладка «Настройки» задаёт общие лимиты: автоповторы этапа (по умолчанию 2), лимит возвратов ревью (по умолчанию 3) и бюджет токенов на задачу (0 — без лимита).

Если серверный этап не оставил результат по контракту (нет файла, битый JSON вердикта, CLI завершился с ошибкой), он запускается ещё раз с описанием ошибки предыдущей попытки. Таймаут, лимит подписки и отсутствие CLI не повторяются. Когда ревью отклоняет результат больше раз подряд, чем задано, или сумма токенов задачи превышает бюджет, задача получает статус «Нужен человек» и ждёт кнопки «Продолжить», перезапуска этапа или принудительного утверждения. Бюджет токенов отдельной задачи задаётся в её карточке: пустое поле — общий, 0 — без лимита.

## Основные API

- `GET/POST /api/agents`, `PUT/DELETE /api/agents/{id}`, `POST /api/agents/{id}/avatar`.
- `GET/POST /api/tasks`, `GET/PUT/DELETE /api/tasks/{id}`.
- `GET /api/tasks/{id}/artifacts[/{name}]`, `GET /api/tasks/{id}/logs` (SSE).
- `POST /api/tasks/{id}/start?pipeline=true` продолжает пайплайн с первого непройденного этапа. В графе задачи это кнопка «Запустить пайплайн».
- `POST /api/tasks/{id}/start?stage=N` запускает только выбранный этап (1–4), если предыдущие этапы пройдены. Кнопка «Запустить только этот этап» находится на вкладке «Этап».
- `POST /api/tasks/{id}/answer`, `/restart?stage=N`, `/approve?stage=N`, `/merge`.
- `GET/PUT /api/settings`, `POST /api/integrations/yougile/test`, `POST /api/integrations/yougile/import`, `POST /api/integrations/yougile/webhook`, `GET /api/health`.

## Тесты

```bash
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend pytest backend/tests
pytest worker/test_worker.py
cd frontend && npm test && npm run build
```
