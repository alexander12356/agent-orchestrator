#!/usr/bin/env bash
# Установка DeepSeek Harness CLI (dsh) нативно в Ubuntu/WSL.
#
# Что делает:
#   1. ставит отдельный Node.js 24 в /usr/local/lib/nodejs/node24 (глобальный Node 22 не трогает);
#   2. ставит npm-пакет @deepseek-ai/dsh с этим Node;
#   3. создаёт /usr/local/bin/dsh — обёртку, которая подхватывает DEEPSEEK_API_KEY из ~/.dsh/.env;
#   4. создаёт /usr/local/bin/dsh-agent — обёртку для оркестратора, принимает --model provider/model.
#
# Запускать от пользователя, под которым работает оркестратор (обычно root):
#   bash scripts/install-dsh-cli.sh
# Переменные: DSH_PACKAGE (версия пакета), DSH_NODE_VERSION (точная версия Node 24),
#             DSH_FORCE=1 (переустановить пакет).
set -euo pipefail

NODE_MAJOR="${DSH_NODE_MAJOR:-24}"
NODE_MIN="${DSH_NODE_MIN:-24.9.0}"
PACKAGE="${DSH_PACKAGE:-@deepseek-ai/dsh@latest}"
NODE_ROOT="${DSH_NODE_ROOT:-/usr/local/lib/nodejs}"
NODE_LINK="$NODE_ROOT/node$NODE_MAJOR"
DSH_HOME_DIR="${DSH_HOME:-$HOME/.dsh}"
BIN_LINK="/usr/local/bin/dsh"
AGENT_LINK="/usr/local/bin/dsh-agent"

version_ge() {
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}

# --- Node.js -------------------------------------------------------------
if [ -x "$NODE_LINK/bin/node" ]; then
  echo "== Node.js $("$NODE_LINK/bin/node" -v) уже установлен ($NODE_LINK)"
else
  version="${DSH_NODE_VERSION:-}"
  if [ -z "$version" ]; then
    version=$(curl -fsSL --max-time 30 https://nodejs.org/dist/index.json \
      | grep -o "\"version\":\"v$NODE_MAJOR\.[0-9.]*\"" | head -1 | sed 's/.*"v/v/; s/"//')
  fi
  [ -n "$version" ] || { echo "Не удалось определить версию Node.js $NODE_MAJOR" >&2; exit 1; }
  arch=$(uname -m); case "$arch" in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; esac
  tarball="node-$version-linux-$arch.tar.xz"
  echo "== Устанавливаю Node.js $version в $NODE_ROOT"
  curl -fsSL "https://nodejs.org/dist/$version/$tarball" -o "/tmp/$tarball"
  mkdir -p "$NODE_ROOT"
  tar -xJf "/tmp/$tarball" -C "$NODE_ROOT"
  ln -sfn "$NODE_ROOT/node-$version-linux-$arch" "$NODE_LINK"
  rm -f "/tmp/$tarball"
fi

current=$("$NODE_LINK/bin/node" -v | sed 's/^v//')
if ! version_ge "$current" "$NODE_MIN"; then
  echo "Node.js $current ниже требуемого $NODE_MIN" >&2
  exit 1
fi

# --- npm-пакет -----------------------------------------------------------
# Запускаем npm самим Node 24: shebang `#!/usr/bin/env node` иначе подхватит Node 22.
NPM_CLI="$NODE_LINK/lib/node_modules/npm/bin/npm-cli.js"
ENTRY="$NODE_LINK/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"
if [ -f "$ENTRY" ] && [ "${DSH_FORCE:-0}" != "1" ]; then
  echo "== @deepseek-ai/dsh уже установлен"
else
  echo "== Устанавливаю $PACKAGE"
  "$NODE_LINK/bin/node" "$NPM_CLI" install -g --prefix "$NODE_LINK" --no-fund --no-audit \
    --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs \
    "$PACKAGE" >/dev/null
fi

# --- Обёртки -------------------------------------------------------------
cat >"$BIN_LINK" <<EOF
#!/bin/sh
# dsh запускается своим Node 24, чтобы не менять глобальный Node 22 (zcode/kimi).
# API key берётся из ~/.dsh/.env, если он там есть.
if [ -f "$DSH_HOME_DIR/.env" ]; then
  set -a
  . "$DSH_HOME_DIR/.env"
  set +a
fi
exec "$NODE_LINK/bin/node" "$ENTRY" "\$@"
EOF
chmod +x "$BIN_LINK"

cat >"$AGENT_LINK" <<'EOF'
#!/bin/sh
# dsh-agent — вход для оркестратора: dsh headless + выбор модели патч-оверлеем.
#   dsh-agent [--model provider/model] <prompt>
# Без --model используется модель профиля dsh по умолчанию.
model=""
if [ "$1" = "--model" ]; then
  model="${2:-}"
  shift 2 2>/dev/null || shift
fi
patch=""
case "$model" in
  */*) provider="${model%%/*}"; name="${model#*/}" ;;
  *) provider="deepseek-official"; name="$model" ;;
esac
if [ -n "$name" ] && ! printf '%s' "$name" | grep -q '[^A-Za-z0-9._-]'; then
  patch="$(mktemp /tmp/dsh-model-XXXXXX.yml)"
  printf -- '- id: agent-default-model\n  config:\n    provider: %s\n    model: %s\n' "$provider" "$name" >"$patch"
fi
if [ -n "$patch" ]; then
  dsh headless --patch "$patch" --approval-policy never "$@"
  status=$?
  rm -f "$patch"
  exit $status
fi
exec dsh headless --approval-policy never "$@"
EOF
chmod +x "$AGENT_LINK"
hash -r

mkdir -p "$DSH_HOME_DIR"
if [ ! -f "$DSH_HOME_DIR/.env" ]; then
  cat >"$DSH_HOME_DIR/.env" <<'EOF'
# Ключ DeepSeek API для dsh (https://platform.deepseek.com/api_keys).
DEEPSEEK_API_KEY=
EOF
  chmod 600 "$DSH_HOME_DIR/.env"
fi

echo "== dsh: $(command -v dsh) ($(dsh --version 2>&1 | head -1))"
cat <<TEXT

== Готово. Осталось вписать ключ:
     nano $DSH_HOME_DIR/.env      # DEEPSEEK_API_KEY=sk-...
   Проверка:
     dsh headless "Reply with OK."

== Для оркестратора (Custom CLI, среда Server):
     dsh-agent --model deepseek-v4-pro {prompt}
TEXT
