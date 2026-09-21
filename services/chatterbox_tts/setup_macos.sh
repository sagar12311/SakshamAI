#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.cache/uv}"

uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
if [[ ! -x "$uv_bin" ]]; then
    echo "uv was not found at $uv_bin." >&2
    echo "Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
    "$uv_bin" venv --managed-python --python 3.11 --seed .venv
fi
"$uv_bin" pip install --python .venv/bin/python -r requirements-macos.txt

echo "Chatterbox dependencies are installed in services/chatterbox_tts/.venv"
