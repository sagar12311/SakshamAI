#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
injected_api_key="${MEETING_INTELLIGENCE_API_KEY:-}"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
if [[ -n "$injected_api_key" ]]; then
  export MEETING_INTELLIGENCE_API_KEY="$injected_api_key"
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "Meeting intelligence is not installed. Run ./setup_macos.sh first." >&2
  exit 1
fi

exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${MEETING_INTELLIGENCE_PORT:-8110}"
