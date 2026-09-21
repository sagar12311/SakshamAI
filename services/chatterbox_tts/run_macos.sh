#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
    echo "Run ./setup_macos.sh before starting Chatterbox." >&2
    exit 1
fi
if [[ ! -f .env ]]; then
    echo "Create .env from .env.macos.example and set a generated TTS_API_KEY." >&2
    exit 1
fi

exec .venv/bin/python -m uvicorn app:app \
    --env-file .env \
    --host 127.0.0.1 \
    --port 8100 \
    --workers 1
