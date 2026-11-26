#!/usr/bin/env bash
set -euo pipefail

version=0.19.0
case "$(uname -m)" in
    x86_64)
        arch=x64
        digest=a7d047e2b61bba016b09cd9248f2090c3f90a304e720eb022c787505f5270d46
        ;;
    aarch64)
        arch=arm64
        digest=a668723f74a5c1f1e82c03829155beccef4b041c451189dc375a40f9ee27680e
        ;;
    *)
        echo "Unsupported Linux architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

if [[ "$EUID" != 0 ]]; then
    echo "Run as root (or with sudo)." >&2
    exit 1
fi

if ! command -v rg >/dev/null || ! command -v curl >/dev/null; then
    apt-get update
    apt-get install -y ripgrep curl
fi

target=/usr/local/bin/unity-cli
if ! printf '%s  %s\n' "$digest" "$target" | sha256sum --check --status 2>/dev/null; then
    download=$(mktemp)
    trap 'rm -f -- "$download"' EXIT
    curl --fail --silent --show-error --location --retry 3 \
        "https://github.com/akiojin/unity-cli/releases/download/v${version}/unity-cli-linux-${arch}" \
        --output "$download"
    printf '%s  %s\n' "$digest" "$download" | sha256sum --check --status
    chmod +x "$download"
    UNITY_CLI_NO_AUTO_UPDATE=1 "$download" --version
    install -m 0755 "$download" "$target"
fi

rg --version
UNITY_CLI_NO_AUTO_UPDATE=1 "$target" --version
