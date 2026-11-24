#!/usr/bin/env bash
# Установка ZCode CLI (Z.ai) нативно в Ubuntu/WSL.
#
# Что делает:
#   1. ставит Node.js 22 (если версия ниже 22.19);
#   2. ставит npm-пакет zcode-app-cli (терминальный клиент официального рантайма ZCode);
#   3. создаёт хранилище учётных данных без системного keyring;
#   4. обновляет каталог моделей до актуального релиза (появляется GLM-5.4);
#   5. если вход уже выполнен — переключает модель по умолчанию на GLM-5.4.
#
# Запускать от пользователя, под которым работает оркестратор (обычно root):
#   bash scripts/install-zcode-cli.sh
# Переменные: CATALOG_URL (URL каталога), ZCODE_DEFAULT_MODEL (модель по умолчанию).
set -euo pipefail

NODE_MIN="22.19.0"
PACKAGE="zcode-app-cli@latest"
DEFAULT_MODEL="${ZCODE_DEFAULT_MODEL:-GLM-5.4}"
DEFAULT_CATALOG_URL="https://cdn-zcode.z.ai/zcode/config/zcode-builtin-production-4135.json"
HOME_DIR="${HOME:?HOME не задан}"
ZCODE_DIR="$HOME_DIR/.zcode"

echo "== Пользователь: $(id -un) ($HOME_DIR)"

version_ge() {
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}

install_node() {
  local version arch tarball
  version=$(curl -fsSL --max-time 30 https://nodejs.org/dist/index.json \
    | grep -o '"version":"v22\.[0-9.]*"' | head -1 | sed 's/.*"v/v/; s/"//')
  [ -n "$version" ] || { echo "Не удалось определить версию Node.js 22" >&2; exit 1; }
  arch=$(uname -m); case "$arch" in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; esac
  tarball="node-$version-linux-$arch.tar.xz"
  echo "== Устанавливаю Node.js $version"
  curl -fsSL "https://nodejs.org/dist/$version/$tarball" -o "/tmp/$tarball"
  mkdir -p /usr/local/lib/nodejs
  rm -rf "/usr/local/lib/nodejs/node-$version-linux-$arch"
  tar -xJf "/tmp/$tarball" -C /usr/local/lib/nodejs
  ln -sfn "/usr/local/lib/nodejs/node-$version-linux-$arch" /usr/local/lib/nodejs/current
  for bin in node npm npx corepack; do
    ln -sfn "/usr/local/lib/nodejs/current/bin/$bin" "/usr/local/bin/$bin"
  done
  rm -f "/tmp/$tarball"
  hash -r
}

# --- Node.js -------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  install_node
else
  current=$(node -v | sed 's/v//')
  if ! version_ge "$current" "$NODE_MIN"; then
    install_node
  else
    echo "== Node.js $current уже установлен"
  fi
fi
echo "== node: $(node -v) ($(which node))"

# --- npm-пакет -----------------------------------------------------------
prefix=$(npm config get prefix 2>/dev/null || true)
if [ "$prefix" != "/usr/local" ]; then
  npm config set prefix /usr/local
fi
echo "== Устанавливаю $PACKAGE"
npm install -g "$PACKAGE" >/dev/null
hash -r
echo "== zcode: $(which zcode) ($(zcode --version 2>/dev/null | tail -1))"

# --- Хранилище учётных данных (headless) --------------------------------
mkdir -p "$ZCODE_DIR/v2"
if [ ! -f "$ZCODE_DIR/v2/credential-secret" ]; then
  head -c 32 /dev/urandom >"$ZCODE_DIR/v2/credential-secret"
  chmod 600 "$ZCODE_DIR/v2/credential-secret"
  echo "== Создан credential-secret"
fi

# --- Каталог моделей ----------------------------------------------------
VENDOR="$(npm root -g)/zcode-app-cli/vendor/provider/zcode-builtin.json"
refresh_catalog() {
  local url="${CATALOG_URL:-}" refresh_file
  if [ -z "$url" ]; then
    refresh_file=$(ls -1t "$ZCODE_DIR"/v2/runtime/provider/*/*/endpoint-*/zcode-builtin-refresh.json 2>/dev/null | head -1 || true)
    if [ -n "$refresh_file" ]; then
      url=$(grep -o '"releaseUrl": *"[^"]*"' "$refresh_file" | head -1 | sed 's/.*"\(https[^"]*\)"/\1/')
    fi
  fi
  url="${url:-$DEFAULT_CATALOG_URL}"
  echo "== Каталог моделей: $url"
  local tmp=/tmp/zcode-builtin-latest.json
  curl -fsSL --max-time 60 "$url" -o "$tmp"

  if [ -f "$VENDOR" ]; then
    [ -f "$VENDOR.orig" ] || cp "$VENDOR" "$VENDOR.orig"
    cp "$tmp" "$VENDOR"
  fi
  local cache
  for cache in "$ZCODE_DIR"/v2/runtime/provider/*/*/endpoint-*/zcode-builtin.json; do
    [ -f "$cache" ] || continue
    [ -f "$cache.orig" ] || cp "$cache" "$cache.orig"
    cp "$tmp" "$cache"
  done
  echo "== GLM-5.4 в каталоге: $(grep -c 'GLM-5.4' "$tmp" || true) записей"
}
refresh_catalog

# --- Модель по умолчанию ------------------------------------------------
PROVIDER_CONFIG="$ZCODE_DIR/v2/provider_config.json"
if [ -f "$PROVIDER_CONFIG" ]; then
  python3 - "$PROVIDER_CONFIG" "$DEFAULT_MODEL" <<'PY'
import json, sys
path, model = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
config = data.setdefault("config", {})
selection = config.get("defaultModelSelection")
if selection and selection.get("modelId") != model:
    selection["modelId"] = model
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    print(f"== Модель по умолчанию: {selection.get('providerId')}/{model}")
else:
    print("== Модель по умолчанию не менялась (вход ещё не выполнен)")
PY
else
  echo "== Вход не выполнен — модель по умолчанию будет выбрана при login"
fi

cat <<'TEXT'

== Готово. Осталось выполнить вход (один раз):
     zcode login --no-browser
   Откройте напечатанную ссылку и подтвердите доступ.

== Проверка:
     zcode --model account:zai-individual-coding-plan/GLM-5.4 -p 'Reply with OK.'
TEXT
