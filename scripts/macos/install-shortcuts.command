#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_DIR="${HOME}/Desktop"

mkdir -p "$DESKTOP_DIR"
chmod +x \
    "$SCRIPT_DIR/start-saksham.command" \
    "$SCRIPT_DIR/stop-saksham.command" \
    "$SCRIPT_DIR/install-shortcuts.command"
ln -sfn "$SCRIPT_DIR/start-saksham.command" "$DESKTOP_DIR/Start Saksham.command"
ln -sfn "$SCRIPT_DIR/stop-saksham.command" "$DESKTOP_DIR/Stop Saksham.command"

printf 'Installed desktop shortcuts:\n'
printf '  %s\n' "$DESKTOP_DIR/Start Saksham.command"
printf '  %s\n' "$DESKTOP_DIR/Stop Saksham.command"
