#!/usr/bin/env bash
set -euo pipefail
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_project="saksham-smoke-$(date +%s)-$$"
compose=(docker compose -p "$smoke_project" -f "$release_root/deploy/docker-compose.smoke.yml")
cleanup() { "${compose[@]}" down --volumes --remove-orphans; }
trap cleanup EXIT
"${compose[@]}" up --build --wait --wait-timeout 120
"${compose[@]}" exec -T gateway python - < "$release_root/tools/smoke_public.py"
